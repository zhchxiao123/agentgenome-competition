"""REST 控制面。

用 FastAPI 的测试客户端发真实请求,底下跑真实的库与文件系统——不 mock 服务层,这样测的是
端到端的真实路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from agentgenome.core.states import TaskState
from agentgenome.core.store import task_dir
from agentgenome.core.task import TaskStore
from agentgenome.jobs.artifacts import ArtifactBus
from agentgenome.server.app import API_VERSION, create_app
from tests.fixtures.git import commit_all
from tests.fixtures.mall import materialize_mall

runner = CliRunner()
APPROVER = "alice"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        cli_app,
        [
            "init",
            "--local-only",
            str(root),
            "--name",
            "mall",
            "--repo",
            mall["order-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    config = root / "agentgenome.yaml"
    config.write_text(
        config.read_text(encoding="utf-8") + f"\napproval:\n  approvers: [{APPROVER}]\n",
        encoding="utf-8",
    )
    commit_all(root, "chore: 审批人")
    return root


@pytest.fixture
def client(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace))


def _submit(client: TestClient, requirement: str = "加一个字段") -> dict:
    response = client.post("/tasks", json={"requirement": requirement})
    assert response.status_code == 201, response.text
    return response.json()


# --- 任务 -------------------------------------------------------------------


def test_submitting_returns_the_task(client: TestClient) -> None:
    payload = _submit(client)

    assert payload["id"].startswith("ag-")
    assert payload["state"] == "CREATED"
    assert payload["requirement"] == "加一个字段"


def test_an_empty_requirement_is_refused(client: TestClient) -> None:
    """需求是任务的全部起点。空需求进来的话,后面每一步都在处理一个空字符串。"""
    assert client.post("/tasks", json={"requirement": ""}).status_code == 422


def test_an_unknown_task_is_a_404_not_a_500(client: TestClient) -> None:
    assert client.get("/tasks/ag-nope").status_code == 404


def test_the_list_hides_tasks_nobody_needs_to_look_at(client: TestClient) -> None:
    first = _submit(client)
    client.post(f"/tasks/{first['id']}/cancel")
    second = _submit(client, "另一个")

    ids = [item["id"] for item in client.get("/tasks").json()]

    assert second["id"] in ids
    assert first["id"] not in ids


def test_settled_true_brings_the_hidden_tasks_back(client: TestClient, workspace: Path) -> None:
    """`?settled=true` 是任务中心「显示已完结任务」开关的后端一侧。

    默认列表把已完成/已取消的任务藏起来是故意的
    (见 `test_the_list_hides_tasks_nobody_needs_to_look_at`),
    但藏起来不该等于"再也看不到"——没有这个开关,已完成的任务就从界面上彻底消失了。
    """
    first = _submit(client)
    client.post(f"/tasks/{first['id']}/cancel")
    second = _submit(client, "另一个")

    default_ids = [item["id"] for item in client.get("/tasks").json()]
    settled_ids = [item["id"] for item in client.get("/tasks?settled=true").json()]

    assert first["id"] not in default_ids
    assert first["id"] in settled_ids
    assert second["id"] in settled_ids


def test_the_list_still_shows_escalated_tasks(client: TestClient, workspace: Path) -> None:
    """回归:列表接口曾经复用调度队列的 `open_tasks`,把 `ESCALATED` 一起滤掉了。

    症状特别难认——任务提交成功、`GET /tasks/{id}` 也查得到,但看板上那一列永远是空的,
    跟"这个 Workspace 还没有任务"长得一模一样。而看板恰恰给 `ESCALATED` 留了一整列。
    """
    task = _submit(client)
    store = TaskStore(workspace)
    store.save(store.get(task["id"]).evolve(state=TaskState.ESCALATED, escalate_reason="环境缺件"))

    listed = client.get("/tasks").json()

    assert task["id"] in [item["id"] for item in listed]
    assert [item["state"] for item in listed] == ["ESCALATED"]


def test_an_old_plan_failure_is_no_longer_presented_as_code_fix_rounds(
    client: TestClient, workspace: Path
) -> None:
    task = _submit(client)
    store = TaskStore(workspace)
    store.save(
        store.get(task["id"]).evolve(
            state=TaskState.ESCALATED,
            plan_retries=1,
            escalate_reason="在 CREATED 态修复轮次已达上限 3",
        )
    )

    payload = client.get(f"/tasks/{task['id']}").json()
    requirement = client.get(f"/requirements/{task['requirement_id']}").json()

    assert "需求解析重试次数已达上限 1" in payload["escalate_reason"]
    assert "修复轮次" not in payload["escalate_reason"]
    assert requirement["chain"][0]["escalate_reason"] == payload["escalate_reason"]


def test_an_escalation_can_be_resolved_without_rewriting_its_state(
    client: TestClient, workspace: Path
) -> None:
    task = _submit(client)
    store = TaskStore(workspace)
    store.save(store.get(task["id"]).evolve(state=TaskState.ESCALATED, escalate_reason="需求不清"))

    response = client.post(f"/tasks/{task['id']}/intervention/resolve", json={"note": "已修改需求"})

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "ESCALATED"
    assert store.get(task["id"]).intervention_resolved_at is not None
    assert task["id"] not in [item["id"] for item in client.get("/tasks").json()]


def test_retrying_an_escalation_atomically_creates_a_successor_and_resolves_the_todo(
    client: TestClient, workspace: Path
) -> None:
    task = _submit(client, "需求还不够清楚")
    store = TaskStore(workspace)
    store.save(
        store.get(task["id"]).evolve(
            state=TaskState.ESCALATED,
            escalate_reason="需要人工修改或澄清需求",
        )
    )

    first = client.post(
        f"/tasks/{task['id']}/intervention/retry",
        json={"requirement": "补充验收条件后的需求"},
    )
    replay = client.post(
        f"/tasks/{task['id']}/intervention/retry",
        json={"requirement": "补充验收条件后的需求"},
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert first.json()["requirement_id"] == task["requirement_id"]
    old = client.get(f"/tasks/{task['id']}").json()
    assert old["state"] == "ESCALATED"
    assert old["intervention_resolved_at"] is not None
    assert old["intervention_successor_task_id"] == first.json()["id"]
    listed = {item["id"] for item in client.get("/tasks").json()}
    assert task["id"] not in listed
    assert first.json()["id"] in listed
    chain = client.get(f"/requirements/{task['requirement_id']}").json()["chain"]
    assert [attempt["id"] for attempt in chain] == [task["id"], first.json()["id"]]


def test_a_healthy_task_has_no_intervention_to_resolve(client: TestClient) -> None:
    task = _submit(client)

    response = client.post(f"/tasks/{task['id']}/intervention/resolve", json={})

    assert response.status_code == 409


def test_cancelling_twice_is_idempotent(client: TestClient) -> None:
    """崩溃恢复会重放这个动作。第二次报错的话,恢复本身就成了新的失败源。"""
    task = _submit(client)

    client.post(f"/tasks/{task['id']}/cancel")
    again = client.post(f"/tasks/{task['id']}/cancel")

    assert again.status_code == 200
    assert again.json()["state"] == "CANCELLED"


# --- 推进 -------------------------------------------------------------------
#
# 真的把一个任务推过一步(需要装配出能跑的运行时)在 test_task_run_wiring.py 里,
# 走的是那份不塞替身的装配路径。这里只测挡在派发之前的校验——它们不需要任何
# 真的能跑的运行时,塞进这个文件的通用夹具里最省事。


def test_a_freshly_created_task_can_be_run(client: TestClient) -> None:
    """`can_run` 是服务端算好的,前端据此决定要不要显示"启动"按钮。"""
    task = _submit(client)
    assert task["can_run"] is True


def test_running_an_unknown_task_is_a_404(client: TestClient) -> None:
    assert client.post("/tasks/ag-nope/run").status_code == 404


def test_running_a_terminal_task_is_refused(client: TestClient) -> None:
    """已取消的任务没有下一步——`can_advance` 说不能推,接口不该悄悄放行。"""
    task = _submit(client)
    client.post(f"/tasks/{task['id']}/cancel")

    response = client.post(f"/tasks/{task['id']}/run")

    assert response.status_code == 409
    assert "终态" in response.json()["detail"]


def test_a_cancelled_task_cannot_run(client: TestClient) -> None:
    task = _submit(client)
    client.post(f"/tasks/{task['id']}/cancel")

    assert client.get(f"/tasks/{task['id']}").json()["can_run"] is False


# --- 执行轨迹 -----------------------------------------------------------------
#
# `jobs.trace.read_trace` 自己的行为(空 stage、多次尝试拼接、坏行不炸)在
# `tests/unit/test_job_trace.py` 里已经测过了。这里只测接口这一层的映射对不对。


def test_trace_is_empty_before_any_job_has_run(client: TestClient) -> None:
    task = _submit(client)

    response = client.get(f"/tasks/{task['id']}/trace")

    assert response.status_code == 200
    assert response.json() == {"task_id": task["id"], "stages": []}


def test_trace_surfaces_blocks_from_a_real_job_log(client: TestClient, workspace: Path) -> None:
    from agentgenome.agents.artifacts import log_filename
    from agentgenome.agents.events import EventKind, NormalizedEvent
    from agentgenome.core.store import task_dir
    from agentgenome.jobs.artifacts import ArtifactBus

    task = _submit(client)
    bus = ArtifactBus(task_dir(workspace, task["id"]))
    slot = bus.allocate("plan")
    event = NormalizedEvent(kind=EventKind.TEXT, text="我来先看看当前项目的结构。")
    (slot.path / log_filename(1)).write_text(event.to_json() + "\n", encoding="utf-8")

    body = client.get(f"/tasks/{task['id']}/trace").json()

    assert body["stages"] == [
        {
            "stage": "plan",
            "number": 1,
            "blocks": [
                {"seq": 1, "kind": "text", "text": "我来先看看当前项目的结构。", "detail": {}}
            ],
        }
    ]


def test_trace_of_an_unknown_task_is_a_404(client: TestClient) -> None:
    assert client.get("/tasks/ag-nope/trace").status_code == 404


# --- 审批 -------------------------------------------------------------------


def test_someone_not_on_the_list_gets_a_403(client: TestClient) -> None:
    """身份校验在服务端。不校验的话这道关卡就只是个仪式。"""
    task = _submit(client)

    response = client.post(
        f"/tasks/{task['id']}/approval", json={"actor": "mallory", "approved": True}
    )

    assert response.status_code == 403


def test_approving_a_task_that_is_not_waiting_is_a_409(client: TestClient) -> None:
    task = _submit(client)

    response = client.post(
        f"/tasks/{task['id']}/approval", json={"actor": APPROVER, "approved": True}
    )

    assert response.status_code == 409


# --- 事件、日志、产物 -------------------------------------------------------


def test_events_are_paged(client: TestClient) -> None:
    task = _submit(client)

    page = client.get(f"/tasks/{task['id']}/events", params={"limit": 1}).json()

    assert page["limit"] == 1
    assert len(page["items"]) <= 1
    assert page["total"] >= 1


def test_the_log_cursor_is_continuous(client: TestClient, workspace: Path) -> None:
    """第二页的第一行紧接第一页的最后一行,不重不漏。"""
    task = _submit(client)
    path = workspace / "tasks" / task["id"] / "logs" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"line-{index}" for index in range(10)) + "\n", encoding="utf-8")

    first = client.get(f"/tasks/{task['id']}/logs", params={"limit": 4}).json()
    second = client.get(
        f"/tasks/{task['id']}/logs", params={"limit": 4, "cursor": first["next_cursor"]}
    ).json()

    assert [item["line"] for item in first["items"]] == [1, 2, 3, 4]
    assert [item["line"] for item in second["items"]] == [5, 6, 7, 8]
    assert second["items"][0]["text"] == "line-4"


def test_the_cursor_ends_at_none(client: TestClient, workspace: Path) -> None:
    task = _submit(client)
    path = workspace / "tasks" / task["id"] / "logs" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("only\n", encoding="utf-8")

    page = client.get(f"/tasks/{task['id']}/logs").json()

    assert page["next_cursor"] is None


def test_appending_while_paging_does_not_repeat_lines(client: TestClient, workspace: Path) -> None:
    """日志在追加。游标是行号而不是字节偏移,所以已经看过的内容不会再出现一次。"""
    task = _submit(client)
    path = workspace / "tasks" / task["id"] / "logs" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a\nb\n", encoding="utf-8")
    first = client.get(f"/tasks/{task['id']}/logs", params={"limit": 2}).json()

    with path.open("a", encoding="utf-8") as handle:
        handle.write("c\n")
    second = client.get(f"/tasks/{task['id']}/logs", params={"cursor": 2}).json()

    assert [item["text"] for item in first["items"]] == ["a", "b"]
    assert [item["text"] for item in second["items"]] == ["c"]


def test_an_artifact_path_cannot_escape_the_task_directory(client: TestClient) -> None:
    """`{path}` 来自 URL,是最典型的不可信输入。"""
    task = _submit(client)

    response = client.get(f"/tasks/{task['id']}/artifacts/../../../etc/passwd")

    assert response.status_code == 404


def test_artifacts_can_be_listed_and_read(client: TestClient) -> None:
    task = _submit(client)

    listing = client.get(f"/tasks/{task['id']}/artifacts").json()

    assert any(item["path"] == "task.json" for item in listing["items"])
    body = client.get(f"/tasks/{task['id']}/artifacts/task.json")
    assert body.status_code == 200
    assert json.loads(body.text)["id"] == task["id"]


# --- 报告 -------------------------------------------------------------------


def test_a_report_can_be_read_before_the_task_finishes(client: TestClient) -> None:
    """需求方问"现在到哪了"的时候,任务通常正好还没走完。"""
    task = _submit(client, "把预占接口改成幂等")

    body = client.get(f"/tasks/{task['id']}/report").json()

    assert "把预占接口改成幂等" in body["markdown"]
    assert task["id"] in body["markdown"]


# --- 运维 -------------------------------------------------------------------


def test_health_does_not_need_anything_but_the_process(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_the_version_endpoint_carries_the_api_contract_version(client: TestClient) -> None:
    payload = client.get("/api/version").json()

    assert payload["api"] == API_VERSION
    assert payload["version"]


def test_metrics_are_prometheus_text(client: TestClient) -> None:
    _submit(client)

    body = client.get("/metrics").text

    assert "# HELP agentgenome_tasks_by_state" in body
    assert "# TYPE agentgenome_tasks_by_state gauge" in body
    assert 'agentgenome_tasks_by_state{state="CREATED"} 1' in body


def test_metrics_work_with_no_tasks_at_all(client: TestClient) -> None:
    """一个任务都没有时也要返回合法输出。空响应会让抓取端报解析错误。"""
    body = client.get("/metrics").text

    assert "# HELP" in body
    assert "agentgenome_approval_queue_depth 0" in body


def test_the_openapi_schema_has_concrete_types_not_objects(client: TestClient) -> None:
    """`dict` 兜底会让类型退化成 `object`,生成的客户端就此失去全部价值。"""
    schema = client.get("/openapi.json").json()

    task = schema["components"]["schemas"]["TaskSummary"]["properties"]
    assert task["state"]["type"] == "string"
    assert task["fix_rounds"]["type"] == "integer"


def test_the_committed_openapi_spec_matches_the_app() -> None:
    """契约变更要出现在评审的 diff 里,而不是等前端某天编译报错才发现。

    这条红了就跑 `agctl openapi --out docs/openapi.json` 并把它一起提交。
    """
    import json as json_module
    from pathlib import Path as PathType

    from agentgenome.server.app import create_app as build

    committed = PathType("docs/openapi.json")
    assert committed.is_file(), "仓库里没有已提交的 OpenAPI 规范"
    current = json_module.loads(
        json_module.dumps(build(PathType(".")).openapi(), ensure_ascii=False, sort_keys=True)
    )
    assert current == json_module.loads(committed.read_text(encoding="utf-8"))


def test_the_task_detail_carries_the_scope_grants(client, workspace: Path) -> None:
    """审批人面对的是一份可能横跨两个域的 diff。

    不告诉他"原本只授权了订单域、中途申请加了库存域、理由是 X",他就得自己从 diff 里把
    这件事重新推一遍——而那恰恰是最容易漏看的部分。
    """
    from agentgenome.core.scope_grants import ScopeGrant, append_grants

    task_id = client.post("/tasks", json={"requirement": "下单要预占库存"}).json()["id"]
    append_grants(
        workspace, task_id, [ScopeGrant(module="inventory-service", reason="预占要扣减", round_=1)]
    )

    payload = client.get(f"/tasks/{task_id}").json()

    assert payload["scope_grants"] == [
        {"module": "inventory-service", "reason": "预占要扣减", "round": 1}
    ]


def test_a_task_that_never_widened_has_no_grant_noise(client) -> None:
    """没扩过就是空列表,不是"扩权 0 次"这种要人自己过滤的噪声。"""
    task_id = client.post("/tasks", json={"requirement": "加一行日志"}).json()["id"]

    assert client.get(f"/tasks/{task_id}").json()["scope_grants"] == []


# --- 待办:派给人的那些 Job --------------------------------------------------


def _todo(workspace: Path, assignee: str = "alice") -> str:
    """直接落一张待办。**接口这一层验的是接口**,投递那一条在 human 运行时的测试里。"""
    from agentgenome.todo.store import Todo, TodoStore

    task = TaskStore(workspace).create(title="下单预占", requirement="下单时预占库存")
    slot = ArtifactBus(task_dir(workspace, task.id)).allocate("develop")
    (slot.path / "context.md").write_text("这活是什么", encoding="utf-8")
    TodoStore(workspace).deliver(
        Todo(
            id="todo-api-1",
            task_id=task.id,
            stage="develop",
            node="",
            attempt=1,
            assignee=assignee,
            employee_id="dev-employee",
            procedure_id="code-develop",
            output_dir=str(slot.path.relative_to(workspace)),
            context_file=str((slot.path / "context.md").relative_to(workspace)),
        )
    )
    return "todo-api-1"


def test_the_todo_list_only_shows_what_is_still_waiting(
    client: TestClient, workspace: Path
) -> None:
    _todo(workspace)

    body = client.get("/todos").json()

    assert [item["id"] for item in body["items"]] == ["todo-api-1"]
    assert body["items"][0]["assignee"] == "alice"


def test_the_todo_detail_says_what_to_hand_in(client: TestClient, workspace: Path) -> None:
    """人不知道产物契约的话,会交一份看起来对的东西然后被打回。"""
    todo_id = _todo(workspace)

    body = client.get(f"/todos/{todo_id}").json()

    assert body["context_file"].endswith("context.md")
    assert "changed_files" in body["schema"]["required"]


def test_a_bad_artifact_comes_back_with_the_same_error_a_robot_gets(
    client: TestClient, workspace: Path
) -> None:
    """没有"跳过校验"这条路。"""
    todo_id = _todo(workspace)

    body = client.post(f"/todos/{todo_id}/submit", json={"result": {"passed": True}}).json()

    assert body["ok"] is False
    assert "result.json" in body["detail"] or "schema" in body["detail"]
    assert client.get("/todos").json()["items"], "打回之后待办还在"


def test_a_missing_todo_is_a_404(client: TestClient, workspace: Path) -> None:
    assert client.get("/todos/nope").status_code == 404


# --- 知识健康:可疑账与深化队列的只读透出(PRD 41) ----------------------------


def _seed_knowledge(workspace: Path) -> None:
    from tests.fixtures.tree import patch_module_map

    patch_module_map(
        workspace,
        "order-service",
        features=[
            {
                "id": "reserve-flow",
                "summary": "下单预占",
                "scope": ["repos/order-service/**"],
                "card": "features/reserve-flow.md",
            }
        ],
    )
    card = workspace / "genome/knowledge/modules/order-service/features/reserve-flow.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("---\nid: reserve-flow\n---\n\n细节。\n", encoding="utf-8")


def test_knowledge_status_reports_suspects_and_the_deepen_queue(
    workspace: Path, client: TestClient
) -> None:
    from agentgenome.genome.suspects import Suspect, SuspectKind, record_suspects

    _seed_knowledge(workspace)
    record_suspects(
        workspace,
        (
            Suspect(
                kind=SuspectKind.STALE,
                task_id="ag-1",
                card="order-service/reserve-flow",
                changed=("repos/order-service/src/x.py",),
            ),
            Suspect(kind=SuspectKind.EVAPORATED, task_id="ag-2", round=1),
        ),
    )

    response = client.get("/genome/knowledge")

    assert response.status_code == 200, response.text
    payload = response.json()
    kinds = {item["kind"] for item in payload["suspects"]}
    assert kinds == {"stale_card", "evaporated_lesson"}
    assert [item["card"] for item in payload["deepen_queue"]] == ["order-service/reserve-flow"]


def test_knowledge_status_is_read_only_byte_for_byte(workspace: Path, client: TestClient) -> None:
    """全链路无写操作:status 调用前后,账本与知识树逐字节不变。"""
    from agentgenome.genome.suspects import Suspect, SuspectKind, record_suspects

    _seed_knowledge(workspace)
    record_suspects(
        workspace,
        (
            Suspect(
                kind=SuspectKind.STALE,
                task_id="ag-1",
                card="order-service/reserve-flow",
                changed=("repos/order-service/src/x.py",),
            ),
        ),
    )

    def snapshot() -> dict[str, bytes]:
        return {
            str(path): path.read_bytes()
            for base in (workspace / "genome", workspace / "tasks")
            if base.is_dir()
            for path in sorted(base.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    assert client.get("/genome/knowledge").status_code == 200
    assert snapshot() == before


# --- 执行策略(任务级) -------------------------------------------------------


def test_a_task_remembers_the_strategy_it_was_submitted_with(client: TestClient) -> None:
    payload = client.post("/tasks", json={"requirement": "重构下单", "topology": "critique-loop"})

    assert payload.status_code == 201, payload.text
    assert payload.json()["topology"] == "critique-loop"


def test_not_choosing_a_strategy_leaves_it_empty_not_single(client: TestClient) -> None:
    """ "没表态"与"明确选了 single"是两件事——展开成具体模板名会把它们并成一条记录。"""
    assert _submit(client)["topology"] == ""


def test_an_unknown_strategy_is_refused_at_submit(client: TestClient) -> None:
    """拼错的名字不校验的话会一路活到派发那一刻,而那时人早已离开提交页。"""
    response = client.post("/tasks", json={"requirement": "重构", "topology": "critique-lop"})

    assert response.status_code == 422
    assert "critique-loop" in response.json()["detail"]


def test_a_template_with_an_executor_but_no_buildable_graph_is_refused(
    client: TestClient,
) -> None:
    """校验用的是"能派发"那份名单,不是执行器注册表——后者宽,前者才是能跑的。"""
    response = client.post("/tasks", json={"requirement": "重构", "topology": "test-first"})

    assert response.status_code == 422


def test_the_catalog_lists_what_can_actually_be_dispatched(client: TestClient) -> None:
    payload = client.get("/topologies").json()

    assert payload["default"] == "single"
    ids = [item["id"] for item in payload["options"]]
    assert set(ids) == {"single", "critique-loop", "assisted", "dag", "best-of-n"}
    assert all(item["summary"] for item in payload["options"])
    assert all(item["steps"] for item in payload["options"])


def test_every_strategy_that_can_run_is_offered(client: TestClient) -> None:
    options = {item["id"]: item for item in client.get("/topologies").json()["options"]}

    assert all(item["available"] for item in options.values())


def test_the_cli_and_the_api_refuse_a_bad_strategy_with_the_same_words(
    client: TestClient, workspace: Path
) -> None:
    """两条入口背后是同一个判断。各写一份的话,第一次改文案就会分叉。"""
    detail = client.post("/tasks", json={"requirement": "重构", "topology": "critique-lop"}).json()[
        "detail"
    ]
    result = runner.invoke(
        cli_app,
        [
            "task",
            "submit",
            "--requirement",
            "重构",
            "--topology",
            "critique-lop",
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code != 0
    assert detail in result.output.replace("\n", "")


def test_best_of_n_says_how_many_times_it_costs(client: TestClient) -> None:
    """N 倍成本必须是一个被看见的决定(PRD 39)——倍数是这句话的最低实现。"""
    options = {item["id"]: item for item in client.get("/topologies").json()["options"]}

    assert options["best-of-n"]["available"] is True
    assert options["best-of-n"]["cost_multiplier"] == 3
    assert options["best-of-n"]["experimental"] is True
    assert options["single"]["cost_multiplier"] == 1


def test_without_history_there_is_no_absolute_cost_number(client: TestClient) -> None:
    """编不出来的数字就不显示:一个假的绝对值比不显示更糟,因为人会拿它做决定。"""
    options = {item["id"]: item for item in client.get("/topologies").json()["options"]}

    assert options["best-of-n"]["cost_estimate_tokens"] is None


def test_the_estimate_is_the_multiple_of_what_single_path_tasks_actually_cost(
    client: TestClient, workspace: Path
) -> None:
    store = TaskStore(workspace)
    for spend in (1000, 2000, 3000):
        task = store.create(title="旧任务", requirement="x")
        store.save(task.evolve(state=TaskState.COMPLETED, tokens_used=spend))

    options = {item["id"]: item for item in client.get("/topologies").json()["options"]}

    assert options["best-of-n"]["cost_estimate_tokens"] == 6000
    assert options["single"]["cost_estimate_tokens"] == 2000


def test_a_task_that_never_spent_anything_does_not_drag_the_estimate_down(
    client: TestClient, workspace: Path
) -> None:
    """取消在第一步的任务花了 0——把它算进中位数,估算会一路趋近于 0 而没人发现。"""
    store = TaskStore(workspace)
    for spend in (0, 0, 4000):
        task = store.create(title="旧任务", requirement="x")
        store.save(task.evolve(state=TaskState.COMPLETED, tokens_used=spend))

    options = {item["id"]: item for item in client.get("/topologies").json()["options"]}

    assert options["single"]["cost_estimate_tokens"] == 4000


def test_best_of_n_cannot_be_chosen_when_it_has_nothing_to_compare(
    client: TestClient, workspace: Path
) -> None:
    """一路的"多路择优"不是择优,是单路加一次裁决的钱。"""
    config = workspace / "agentgenome.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\ntopology:\n  best_of_n:\n    attempts: [{key: minimal}]\n",
        encoding="utf-8",
    )

    options = {item["id"]: item for item in client.get("/topologies").json()["options"]}

    assert options["best-of-n"]["available"] is False
    assert "变体" in options["best-of-n"]["unavailable_reason"]


# --- 信任爬坡:员工的执行档位 -----------------------------------------------


def test_the_roster_says_which_rung_each_employee_is_on(client: TestClient) -> None:
    """auto / assisted / manual 是同一条曲线上的三个点,不是三个无关的开关。"""
    members = {item["id"]: item for item in client.get("/insights/roster").json()["employees"]}

    assert members["dev-employee"]["execution"] == "auto"


def test_putting_an_employee_on_the_assisted_rung_shows_up_on_the_roster(
    client: TestClient, workspace: Path
) -> None:
    config = workspace / "agentgenome.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\ntopology:\n  assisted:\n    employees: [dev-employee]\n    confirmer: alice\n",
        encoding="utf-8",
    )

    members = {item["id"]: item for item in client.get("/insights/roster").json()["employees"]}

    assert members["dev-employee"]["execution"] == "assisted"
    # 谁来确认要看得见:没有主人的确认不会被任何人收到。
    assert members["dev-employee"]["confirmer"] == "alice"
    assert members["arch-employee"]["execution"] == "auto"


def _rung(client: TestClient, employee: str, execution: str, **body: object):
    return client.put(
        f"/employees/{employee}/execution",
        json={"execution": execution, **body},
    )


def test_moving_an_employee_to_assisted_lands_in_the_confirmation_list(
    client: TestClient, workspace: Path
) -> None:
    """三档住在两个存储上,而人只做了一个动作——分派由服务端做。"""
    response = _rung(client, "dev-employee", "assisted", assignee="alice")

    assert response.status_code == 200, response.text
    from agentgenome.config import load_config

    assert load_config(workspace).topology.assisted.employees == ["dev-employee"]


def test_moving_an_employee_to_manual_rewrites_its_definition(
    client: TestClient, workspace: Path
) -> None:
    response = _rung(client, "dev-employee", "manual", assignee="alice")

    assert response.status_code == 200, response.text
    from agentgenome.employees import load_employees, workspace_employees_root

    employee = load_employees(workspace_employees_root(workspace)).get("dev-employee")
    assert employee.runtime == "human"
    assert employee.assignee == "alice"


def test_manual_without_an_assignee_is_refused(client: TestClient, workspace: Path) -> None:
    """没有主人的待办不会被任何人看到,只会在三窗口超时之后升级——一次静默的死循环。"""
    before = (workspace / "employees" / "dev-employee.yaml").read_bytes()

    response = _rung(client, "dev-employee", "manual")

    assert response.status_code == 422
    assert "指派人" in response.json()["detail"]
    assert (workspace / "employees" / "dev-employee.yaml").read_bytes() == before


def test_coming_back_down_from_manual_restores_a_machine_runtime(
    client: TestClient, workspace: Path
) -> None:
    from agentgenome.employees import load_employees, workspace_employees_root

    assert _rung(client, "dev-employee", "manual", assignee="alice").status_code == 200
    assert _rung(client, "dev-employee", "auto").status_code == 200

    employee = load_employees(workspace_employees_root(workspace)).get("dev-employee")
    assert employee.runtime != "human"


def test_only_the_runtime_and_the_assignee_can_be_written(
    client: TestClient, workspace: Path
) -> None:
    """工序白名单、权限、写集是安全边界。从界面上改它们是另一份 PRD,而且大概率不该有。"""
    from agentgenome.employees import load_employees, workspace_employees_root

    root = workspace_employees_root(workspace)
    before = load_employees(root).get("dev-employee")

    assert _rung(client, "dev-employee", "manual", assignee="alice").status_code == 200

    after = load_employees(root).get("dev-employee")
    assert after.procedures == before.procedures
    assert after.permissions == before.permissions
    assert after.crafts == before.crafts


def test_an_unknown_employee_is_a_404(client: TestClient) -> None:
    assert _rung(client, "nobody-employee", "manual", assignee="alice").status_code == 404


def test_an_unknown_rung_is_refused(client: TestClient) -> None:
    assert _rung(client, "dev-employee", "sometimes").status_code == 422


def test_changing_a_rung_is_audited_like_any_other_config_change(
    client: TestClient, workspace: Path
) -> None:
    """ "谁把这个角色放开成全自动的"与"谁改的并发数"是同一类问题,进同一条审计。"""
    assert _rung(client, "dev-employee", "manual", assignee="alice").status_code == 200

    from agentgenome.server.settings import history

    sections = [change.section for change in history(workspace)]
    assert any("dev-employee" in section for section in sections)


def test_someone_without_permission_cannot_move_a_rung(workspace: Path) -> None:
    from agentgenome.server.rbac import Principal, Role

    app = create_app(
        workspace, principals={"reader": Principal("reader", frozenset({Role.REQUESTER}))}
    )
    response = TestClient(app).put(
        "/employees/dev-employee/execution",
        json={"execution": "manual", "assignee": "alice"},
        headers={"x-actor": "reader"},
    )

    assert response.status_code == 403


def test_a_request_that_tries_to_write_another_field_is_refused(client: TestClient) -> None:
    """只放开运行时与指派人。权限、写集、工序白名单是安全边界——**默默忽略等于默默拒绝**,
    而调用方会以为自己改成了。"""
    response = client.put(
        "/employees/dev-employee/execution",
        json={"execution": "manual", "assignee": "alice", "procedures": ["anything"]},
    )

    assert response.status_code == 422


def test_naming_the_confirmer_on_the_assisted_rung_sticks(
    client: TestClient, workspace: Path
) -> None:
    """确认人填了就得落下去。**落不下去比不给填更糟**:人以为自己指了个主人。"""
    assert _rung(client, "dev-employee", "assisted", assignee="alice").status_code == 200

    members = {item["id"]: item for item in client.get("/insights/roster").json()["employees"]}
    assert members["dev-employee"]["confirmer"] == "alice"


def test_a_project_wide_confirmer_does_not_hide_whose_field_is_being_edited(
    client: TestClient, workspace: Path
) -> None:
    """项目配了统一确认人时,那一行显示的是全局值、改的却是员工自己的字段。

    两者合成一个字段的话,保存成功而显示不变——一次没有任何报错的空操作。
    """
    config = workspace / "agentgenome.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\ntopology:\n  assisted:\n    employees: [dev-employee]\n    confirmer: 全局审核组\n",
        encoding="utf-8",
    )

    assert _rung(client, "dev-employee", "assisted", assignee="alice").status_code == 200

    members = {item["id"]: item for item in client.get("/insights/roster").json()["employees"]}
    assert members["dev-employee"]["assignee"] == "alice"
    assert members["dev-employee"]["confirmer"] == "全局审核组"
