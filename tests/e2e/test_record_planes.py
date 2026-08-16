"""三平面的分工与事件面的检索维度。

**这一组盯的是两种相反的错误。** 一件事哪里都没记(配置变更曾经就是),或者一件事在两个
平面上各记一份、然后慢慢对不上——后者更难查:两份记录打架时,没有任何办法判断哪份是对的。
所以既断言"记了",也断言**"没有在第二个地方也记一份"**。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentgenome import paths
from agentgenome.core.events import (
    ALERT_ACTOR,
    GATE_ACTOR,
    IM_ACTOR,
    ORCHESTRATOR,
    SYSTEM_SUBJECT,
    ActorKind,
    EventLog,
    LogKind,
    infer_actor_kind,
)
from agentgenome.core.genome_task import (
    GenomeTaskKind,
    GenomeTaskState,
    GenomeTaskStore,
    Origin,
)
from agentgenome.genome.deep_read import DeepReadResult, ModuleOutcome, write_progress
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.tree import write_tree
from agentgenome.server.app import create_app
from agentgenome.server.rbac import Principal, Role
from agentgenome.server.tenancy import WorkspaceRegistry

ROOT = Principal("root", frozenset({Role.ADMIN}))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / paths.TASKS).mkdir(parents=True)
    return root


def _client(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace, principals={"root": ROOT}))


def _events(workspace: Path, **query: object) -> list[dict]:
    response = _client(workspace).get("/audit/events", params=query, headers={"x-actor": "root"})
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


# --- 行为主体的类别 ---------------------------------------------------------


def test_a_person_an_employee_and_the_machines_are_told_apart(workspace: Path) -> None:
    """`actor` 是自由字符串,按它筛只能一个一个精确匹配。

    而审计员问的是**一整类**:"人干的有哪些"。没有类别这一层的话,这个问题要靠猜名字的
    形状来答——而猜错了不会有任何症状。
    """
    log = EventLog(workspace)
    log.append("ag-0001", actor="alice", kind=LogKind.APPROVAL, payload={})
    log.append(
        "ag-0001", actor="dev-01", kind=LogKind.NOTE, payload={}, actor_kind=ActorKind.EMPLOYEE
    )
    log.append("ag-0001", actor=ORCHESTRATOR, kind=LogKind.NOTE, payload={})
    log.append("ag-0001", actor=GATE_ACTOR, kind=LogKind.GATE_RESULT, payload={})

    kinds = {event.actor: event.actor_kind for event in log.events("ag-0001")}

    assert kinds == {
        "alice": ActorKind.HUMAN,
        "dev-01": ActorKind.EMPLOYEE,
        ORCHESTRATOR: ActorKind.ORCHESTRATOR,
        GATE_ACTOR: ActorKind.GATE,
    }


def test_an_unknown_actor_is_assumed_to_be_a_person(workspace: Path) -> None:
    """猜不出来时往"人"上靠。

    **方向是刻意选的**:把一个人的操作记成机器,追责时那条线索直接断掉;反过来把机器记成
    人,只是让人多看一眼。
    """
    assert infer_actor_kind("someone-new") is ActorKind.HUMAN
    assert infer_actor_kind(ORCHESTRATOR) is ActorKind.ORCHESTRATOR


def test_events_can_be_filtered_by_actor_kind(workspace: Path) -> None:
    """ "人干的有哪些"是审计的第一个问题,而 actor 是自由字符串,按它筛只能一个个试。"""
    log = EventLog(workspace)
    log.append("ag-0001", actor="alice", kind=LogKind.APPROVAL, payload={})
    log.append("ag-0001", actor=ORCHESTRATOR, kind=LogKind.NOTE, payload={})

    found = _events(workspace, actor_kind=ActorKind.HUMAN.value)

    assert [item["actor"] for item in found] == ["alice"]


def test_an_old_database_gets_the_column_and_a_backfill(workspace: Path) -> None:
    """老库里的行要按 actor 与事件类型反推补上,**不能用列默认值**。

    给它一个 `default 'human'` 的话,编排器、门禁与升级之前的全部员工作业会一次性变成
    "人干的"——而按类别筛选恰恰是这一列存在的理由,于是这个维度从上线第一天起就是错的。

    这里**真的把列删掉**再重开,而不是把值置空:`create table if not exists` 对已存在的表
    什么都不做,所以只置空的话,那条 `alter table` 删掉测试照样绿,而真实老库上的症状是
    每一次读都报"没有这一列"。
    """
    log = EventLog(workspace)
    log.append("ag-0001", actor=ORCHESTRATOR, kind=LogKind.NOTE, payload={})
    log.append("ag-0001", actor="dev-01", kind=LogKind.JOB_FINISHED, payload={})
    log.append("ag-0001", actor="alice", kind=LogKind.APPROVAL, payload={})
    with log._connect() as connection:  # noqa: SLF001 - 造一个还没有这一列的老库
        connection.execute("alter table events drop column actor_kind")

    reopened = EventLog(workspace)

    assert {event.actor: event.actor_kind for event in reopened.events("ag-0001")} == {
        ORCHESTRATOR: ActorKind.ORCHESTRATOR,
        # Job 事件只可能是员工写的——那是唯一能从事件本身反推出员工身份的线索。
        "dev-01": ActorKind.EMPLOYEE,
        "alice": ActorKind.HUMAN,
    }
    assert [item["actor"] for item in _events(workspace, actor_kind="employee")] == ["dev-01"]


def test_an_integration_entrance_is_neither_a_person_nor_an_employee(workspace: Path) -> None:
    """告警回调与群机器人建的任务,记成编排器的话"哪些是从集成入口进来的"就查不出来。

    类别取值也**不叫 `system`**:那与系统主体 id 撞了,而两者是完全不同的东西。
    """
    assert infer_actor_kind(ALERT_ACTOR) is ActorKind.INTEGRATION
    assert infer_actor_kind(IM_ACTOR) is ActorKind.INTEGRATION
    assert ActorKind.INTEGRATION.value != SYSTEM_SUBJECT


def test_a_malformed_filter_value_is_the_callers_mistake(workspace: Path) -> None:
    """拼错的筛选条件是 422 不是 500。

    500 说的是"服务端出问题了",于是排查会从服务端日志开始——而那里什么都没有。
    """
    response = _client(workspace).get(
        "/audit/events", params={"actor_kind": "bogus"}, headers={"x-actor": "root"}
    )

    assert response.status_code == 422
    assert "actor_kind" in response.json()["detail"]


# --- 检索维度 ---------------------------------------------------------------


def test_events_can_be_filtered_by_workspace(tmp_path: Path) -> None:
    """按工作空间过滤。

    **没说清楚就拒绝,不落到默认工作区**——那是跨租户泄漏最常见的来源,而且没有任何症状,
    直到有人发现自己的记录出现在别人的审计里。
    """
    registry = WorkspaceRegistry()
    for name in ("alpha", "beta"):
        root = tmp_path / name
        (root / paths.TASKS).mkdir(parents=True)
        registry.register(name, root)
        EventLog(root).append(f"ag-{name}", actor="alice", kind=LogKind.NOTE, payload={})
    client = TestClient(create_app(workspaces=registry, principals={"root": ROOT}))

    found = client.get(
        "/audit/events", params={"workspace": "beta"}, headers={"x-actor": "root"}
    ).json()["items"]

    assert [item["task_id"] for item in found] == ["ag-beta"]
    assert client.get("/audit/events", headers={"x-actor": "root"}).status_code == 400


def test_events_can_be_filtered_by_kind_and_time_range(workspace: Path) -> None:
    """时间**范围**要两头都作数。

    只测 `since` 的话,`until` 那一头写反了也没人知道——而一个只有下界的"范围"筛出来的
    结果里,永远混着比你想看的时间更晚的东西。
    """
    log = EventLog(workspace)
    now = datetime.now(UTC)
    log.append("ag-0001", actor="alice", kind=LogKind.NOTE, payload={}, now=now - timedelta(days=2))
    log.append(
        "ag-0001", actor="alice", kind=LogKind.APPROVAL, payload={}, now=now - timedelta(hours=2)
    )
    log.append("ag-0001", actor="alice", kind=LogKind.ESCALATED, payload={}, now=now)

    by_kind = _events(workspace, kind=LogKind.APPROVAL.value)
    window = _events(
        workspace,
        since=(now - timedelta(hours=6)).isoformat(),
        until=(now - timedelta(hours=1)).isoformat(),
    )

    assert [item["kind"] for item in by_kind] == [LogKind.APPROVAL.value]
    # 更早的那条与更晚的那条都要被排除掉。
    assert [item["kind"] for item in window] == [LogKind.APPROVAL.value]


def test_a_system_level_event_can_be_written_and_read_back(workspace: Path) -> None:
    """不属于任何任务的事件也要查得回来,否则它等于没记。"""
    EventLog(workspace).append(
        SYSTEM_SUBJECT, actor="alice", kind=LogKind.CONFIG_CHANGED, payload={"section": "limits"}
    )

    found = _events(workspace, task_id=SYSTEM_SUBJECT)

    assert [item["kind"] for item in found] == [LogKind.CONFIG_CHANGED.value]


def test_system_level_events_do_not_pollute_a_task_timeline(workspace: Path) -> None:
    """**按任务 id 查询时系统级事件不出现。**

    用空 task_id 的话,每一处按任务查询都要各自记得排除它;漏掉的那一处会让一个任务的历史
    里凭空出现别人改配置的记录——而看的人没有任何理由怀疑它不属于这个任务。
    """
    log = EventLog(workspace)
    log.append("ag-0001", actor="alice", kind=LogKind.NOTE, payload={})
    log.append(SYSTEM_SUBJECT, actor="bob", kind=LogKind.CONFIG_CHANGED, payload={})

    assert [item["actor"] for item in _events(workspace, task_id="ag-0001")] == ["alice"]
    assert [event.actor for event in log.events("ag-0001")] == ["alice"]


# --- 知识/规则变更只记指针 ---------------------------------------------------


def test_the_planes_division_is_written_down_where_it_gets_asked(workspace: Path) -> None:
    """ "该记在哪个平面"是每次加一类记录都会被问到的问题。

    没有一处权威说法的话,每次都会重新讨论一遍、每次答案不同——而这个模块的文档就是那一处。
    这条守着的是"文档慢慢被删干净"这类改动:它没有任何运行时症状。
    """
    from agentgenome.core import events

    doc = events.__doc__ or ""

    assert "同一件事只由一个平面记内容" in doc
    for plane in ("事件面", "日志面", "版本面"):
        assert plane in doc


# --- 基因组任务的接口 -------------------------------------------------------


def test_genome_tasks_are_listable_with_an_explicit_model(workspace: Path) -> None:
    """列表要有显式模型。

    返回裸 `dict` 的话,OpenAPI 里的类型退化成 `object`,生成的 TS 客户端就此失去价值——
    而那是选 FastAPI 的唯一理由。
    """
    store = GenomeTaskStore(workspace)
    store.create(title="全量初始化", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    store.create(
        title="重建 order",
        kind=GenomeTaskKind.REINIT,
        origin=Origin.HUMAN,
        subject="order",
        source_task_id="ag-0001",
    )

    body = _client(workspace).get("/genome/tasks", headers={"x-actor": "root"}).json()

    assert {item["kind"] for item in body["items"]} == {"init", "reinit"}
    reinit = next(item for item in body["items"] if item["kind"] == "reinit")
    assert reinit["subject"] == "order"
    assert reinit["source_task_id"] == "ag-0001"


def test_the_genome_task_list_can_be_filtered(workspace: Path) -> None:
    store = GenomeTaskStore(workspace)
    store.create(title="全量初始化", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    store.create(title="蒸馏", kind=GenomeTaskKind.DISTILL, origin=Origin.SYSTEM)

    body = (
        _client(workspace)
        .get("/genome/tasks", params={"kind": "distill"}, headers={"x-actor": "root"})
        .json()
    )

    assert [item["kind"] for item in body["items"]] == ["distill"]


def test_a_system_distillation_failure_does_not_stay_in_the_list(workspace: Path) -> None:
    """系统自发的蒸馏失败算已了结。

    全量列出的话,这类记录会随时间累积成一大堆没人需要处理的东西,把真正在跑的那几个淹没
    ——而"人还用不用管它"这条判定只住在 `GenomeTask.is_settled` 一处。
    """
    store = GenomeTaskStore(workspace)
    failed = store.create(title="蒸馏", kind=GenomeTaskKind.DISTILL, origin=Origin.SYSTEM)
    store.save(failed.evolve(state=GenomeTaskState.FAILED))
    client = _client(workspace)

    default = client.get("/genome/tasks", headers={"x-actor": "root"}).json()
    everything = client.get(
        "/genome/tasks", params={"settled": "true"}, headers={"x-actor": "root"}
    ).json()

    assert default["items"] == []
    assert [item["id"] for item in everything["items"]] == [failed.id]


def test_a_long_unanswered_gate_is_marked_overdue(workspace: Path) -> None:
    """等太久的闸门要被标出来,否则它会被无声地遗忘。**标记不判死**——健康任务不该因为
    人休假而失败。"""
    store = GenomeTaskStore(workspace)
    task = store.create(title="初始化", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    store.save(
        task.evolve(state=GenomeTaskState.AWAITING_CONFIRMATION),
        now=datetime.now(UTC) - timedelta(days=30),
    )
    (workspace / paths.ROOT_CONFIG).write_text("{}\n", encoding="utf-8")

    body = _client(workspace).get("/genome/tasks", headers={"x-actor": "root"}).json()

    assert [item["overdue"] for item in body["items"]] == [True]


def test_an_unknown_genome_task_kind_is_the_callers_mistake(workspace: Path) -> None:
    response = _client(workspace).get(
        "/genome/tasks", params={"kind": "bogus"}, headers={"x-actor": "root"}
    )

    assert response.status_code == 422


def test_the_progress_of_a_run_that_has_not_started_is_not_an_empty_run(workspace: Path) -> None:
    """ "还没开始"与"零个模块"不是一回事,界面上要说不同的话。"""
    task = GenomeTaskStore(workspace).create(
        title="初始化", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN
    )

    body = (
        _client(workspace)
        .get(f"/genome/tasks/{task.id}/progress", headers={"x-actor": "root"})
        .json()
    )

    assert body["started"] is False
    assert body["modules"] == []


def test_a_failed_module_carries_the_reason(workspace: Path) -> None:
    """只说"三个失败了"的话,人的下一步是去翻日志——而这个页面存在的理由就是让他不必翻。"""
    task = GenomeTaskStore(workspace).create(
        title="初始化", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN
    )
    write_progress(
        workspace,
        task.id,
        DeepReadResult(
            planned=["order", "pay", "ship"],
            done=["order"],
            failed=[ModuleOutcome("pay", ok=False, detail="Job 超时")],
        ),
    )

    body = (
        _client(workspace)
        .get(f"/genome/tasks/{task.id}/progress", headers={"x-actor": "root"})
        .json()
    )

    by_id = {item["module_id"]: item for item in body["modules"]}
    assert by_id["order"]["status"] == "done"
    assert by_id["pay"]["status"] == "failed"
    assert by_id["pay"]["detail"] == "Job 超时"
    # 三态而不是布尔:"还没读"与"读失败了"是完全不同的两件事。
    assert by_id["ship"]["status"] == "pending"


def test_a_genome_task_can_be_cancelled_over_rest(workspace: Path) -> None:
    """停在待确认的任务不是终态。没有这条路的话,一个没人回答的闸门会把那个模块永久堵住。"""
    store = GenomeTaskStore(workspace)
    task = store.create(
        title="重建", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="order"
    )

    body = (
        _client(workspace)
        .post(f"/genome/tasks/{task.id}/cancel", headers={"x-actor": "root"})
        .json()
    )

    assert body["state"] == GenomeTaskState.CANCELLED.value
    # 堵住的模块要真的放开了。
    assert store.open_for_subject("order") == ()


def _with_project_map(workspace: Path) -> None:
    """一份最小的知识树。**不这么做的话,「模块不存在」那条分支根本走不到**——
    项目地图读不出来会先一步抛,而测试只看状态码时两者长得一模一样。"""
    write_tree(
        workspace,
        ProjectMap.model_validate(
            {
                "version": 1,
                "project": {"name": "demo"},
                "modules": [{"id": "order", "path": "order/"}],
            }
        ),
    )


def test_rebuilding_an_unknown_module_is_refused(workspace: Path) -> None:
    """凭空发明模块会让下游的影响判定失去依据。"""
    _with_project_map(workspace)

    response = _client(workspace).post(
        "/genome/tasks/reinit", json={"modules": ["nope"]}, headers={"x-actor": "root"}
    )

    assert response.status_code == 422
    assert "nope" in response.json()["detail"]


def test_rebuilding_a_known_module_creates_a_human_reinit_task(workspace: Path) -> None:
    """从详情页发起的重建要落成一条真任务,而不是一次什么都没发生的成功响应。"""
    _with_project_map(workspace)

    body = (
        _client(workspace)
        .post("/genome/tasks/reinit", json={"modules": ["order"]}, headers={"x-actor": "root"})
        .json()
    )

    (created,) = body["items"]
    assert created["kind"] == GenomeTaskKind.REINIT.value
    assert created["origin"] == Origin.HUMAN.value
    assert created["subject"] == "order"


def test_a_cancel_is_recorded_against_the_person_who_did_it(workspace: Path) -> None:
    """取消是一次人为介入。

    记成编排器的话,「这个人干过什么」与「人对系统的每次介入都可见」对它就答不上来——
    而它恰恰是人能对基因组任务做的两件事之一。
    """
    task = GenomeTaskStore(workspace).create(
        title="重建", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="order"
    )

    _client(workspace).post(f"/genome/tasks/{task.id}/cancel", headers={"x-actor": "root"})

    (event,) = EventLog(workspace).all_events(task_id=task.id, kind=LogKind.TRANSITION)
    assert event.actor == "root"
    assert event.actor_kind is ActorKind.HUMAN


def test_the_workspaces_a_server_serves_are_listable(tmp_path: Path) -> None:
    """界面要知道能切到哪几个。**不回传路径**——那是部署细节,不该挂在一个只需认证就能读
    的接口上。"""
    registry = WorkspaceRegistry()
    for name in ("alpha", "beta"):
        root = tmp_path / name
        (root / paths.TASKS).mkdir(parents=True)
        registry.register(name, root)
    client = TestClient(create_app(workspaces=registry, principals={"root": ROOT}))

    body = client.get("/workspaces").json()

    assert sorted(body["items"]) == ["alpha", "beta"]
    assert str(tmp_path) not in json.dumps(body)


def test_the_progress_carries_how_long_each_module_took(workspace: Path) -> None:
    """「哪个模块特别慢」是下一次调预算与并发时唯一有用的那条线索。"""
    task = GenomeTaskStore(workspace).create(
        title="初始化", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN
    )
    write_progress(
        workspace,
        task.id,
        DeepReadResult(planned=["order"], done=["order"], timing={"order": 12.5}),
    )

    body = (
        _client(workspace)
        .get(f"/genome/tasks/{task.id}/progress", headers={"x-actor": "root"})
        .json()
    )

    assert body["modules"][0]["duration_s"] == 12.5


def test_the_knowledge_pull_requests_a_task_produced_are_listed(workspace: Path) -> None:
    """产出的知识 PR 要能从详情页直接点过去评审。

    **是指针不是内容**——改成了什么去那个 PR 里看,事件面本来就不存内容。
    """
    task = GenomeTaskStore(workspace).create(
        title="初始化", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN
    )
    EventLog(workspace).append(
        task.id,
        actor="arch-lead",
        kind=LogKind.GENOME_PR,
        payload={"asset": "rules", "pr": {"repo": "ws", "number": 42}},
    )

    body = (
        _client(workspace)
        .get(f"/genome/tasks/{task.id}/progress", headers={"x-actor": "root"})
        .json()
    )

    assert body["pull_requests"] == ["42"]


def test_each_layer_of_the_genome_has_its_own_history(workspace: Path) -> None:
    """三层都要能回溯。

    只给知识那一层的话,「规则是什么时候被谁改成这样的」在界面上无解——而规则层恰恰是唯一
    能大范围改变系统行为的杠杆。
    """
    from agentgenome.genome.history import paths_of

    # 三层看的是三组不同的路径。指到同一组的话,"规则改过没有"会跟着知识一起动。
    assert paths_of("rules") != paths_of("knowledge") != paths_of("procedures")
    response = _client(workspace).get(
        "/genome/project-map/versions", params={"asset": "bogus"}, headers={"x-actor": "root"}
    )

    # 认不出来的那一层是 422,不是默默退回知识——后者会给出一份答非所问、看起来却正常的历史。
    assert response.status_code == 422
