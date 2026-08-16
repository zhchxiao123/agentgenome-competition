"""human 运行时端到端:把一份活派给人,人交回来,流水线照常往下走。

**挂起不是失败,而"继续"就是崩溃恢复那条路。** 这组测试盯的就是这两句话:任务在等人的时候
状态不动、不占并发、重启不重复投递;人交完活之后,产物已经在槽里,照常推一步就前进了。
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner, Result

from agentgenome.agents.pool import AgentPool
from agentgenome.agents.recording import RecordingLibrary
from agentgenome.agents.replay import ReplayRuntime
from agentgenome.cli import app
from agentgenome.config import Config, HumanConfig
from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskStore
from agentgenome.jobs.handlers import can_advance
from agentgenome.jobs.orchestrator import Orchestrator
from agentgenome.jobs.reports import read_failure_reports
from agentgenome.server.metrics import collect, render
from agentgenome.space.git_ws import GitWorkspace
from agentgenome.todo.store import ESCALATED, PENDING, Todo, TodoStore
from agentgenome.todo.sweep import sweep
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    PASSING_TEST,
    _arm,
    _submit,
    library,
    workspace,
)

runner = CliRunner()

__all__ = ["library", "workspace"]

DEV_RESULT_BY_HAND = {
    "task_id": "",
    "producer": "alice",
    "created_at": "2026-09-01T11:00:00Z",
    "passed": True,
    "changed_files": ["repos/order-service/src/order/reserve.py"],
    "self_test": {"command": "pytest -q", "exit_code": 0, "passed": True},
    "impact": {"modules": ["order-service"], "rationale": "人自己判断只动了订单域"},
    "questions": [],
}


def hand_dev_to_a_human(workspace: Path, assignee: str = "alice") -> None:
    """把开发员工改成"由人执行"。

    员工定义是资产:改一个 yaml 就换了执行者,这正是"人也是一种运行时"该有的样子——
    不改代码、不改流程、不改状态机。
    """
    config = workspace / "agentgenome.yaml"
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    payload.setdefault("runtime", {})["human"] = {}
    config.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    definition = workspace / "employees" / "dev-employee.yaml"
    employee = yaml.safe_load(definition.read_text(encoding="utf-8"))
    employee["runtime"] = "human"
    employee["assignee"] = assignee
    definition.write_text(yaml.safe_dump(employee, allow_unicode=True), encoding="utf-8")


def orchestrator_of(workspace: Path, library: Path, global_jobs: int = 3) -> Orchestrator:
    """真实装配:硅基那几步走回放,开发那一步走 human。

    **不给编排器锁定运行时**——每个员工自己声明跑在哪儿,而"这个员工是人"正是本 PRD 要
    表达的东西。锁死一个名字的话,员工定义里那行 `runtime: human` 根本不会被读到。
    回放顶替的是 `claude-code` 那把缝(默认花名册里的员工都声明它)。
    """
    from agentgenome.agents.human import HumanRuntime

    pool = AgentPool(
        {
            "claude-code": ReplayRuntime(RecordingLibrary(library)),
            "human": HumanRuntime(workspace),
        },
        global_jobs=global_jobs,
    )
    return Orchestrator(workspace, pool=pool)


def walk_sync(workspace: Path, library: Path) -> tuple[Orchestrator, str]:
    """同步版本的"走到待办为止"。

    **交活那几条测试必须是同步的**:命令行的提交动作自己开事件循环(它是一条真实的用户
    路径),而在异步测试里那会撞上"已经有一个循环在跑"。测异步就绕不开这条,所以这里
    索性把驱动放在测试外面。
    """
    return asyncio.run(walk_to_the_todo(workspace, library))


async def walk_to_the_todo(workspace: Path, library: Path) -> tuple[Orchestrator, str]:
    hand_dev_to_a_human(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = orchestrator_of(workspace, library)
    await orchestrator.advance(task_id)  # 需求解析(回放)
    await orchestrator.advance(task_id)  # 开发 → 投待办
    return orchestrator, task_id


def only_todo(workspace: Path) -> Todo:
    todos = TodoStore(workspace).open_todos()
    assert len(todos) == 1, f"待办数不对: {[item.id for item in todos]}"
    return todos[0]


# --- 投递 -------------------------------------------------------------------


async def test_a_human_job_lands_as_a_todo_and_the_state_does_not_move(
    workspace: Path, library: Path
) -> None:
    """挂起是"待确认":机器停手,任务健康,人一答复就往下走。"""
    _, task_id = await walk_to_the_todo(workspace, library)

    todo = only_todo(workspace)

    assert todo.assignee == "alice"
    assert todo.state == PENDING
    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.DEVELOPING
    assert task.pending_todo_id == todo.id


async def test_a_task_waiting_on_a_person_cannot_be_pushed(
    workspace: Path, library: Path
) -> None:
    """推它只会再投一张一模一样的待办,而人看到两张都"合法"。"""
    _, task_id = await walk_to_the_todo(workspace, library)

    assert can_advance(TaskStore(workspace).get(task_id)) is False


async def test_recovery_does_not_deliver_a_second_todo(
    workspace: Path, library: Path
) -> None:
    """崩溃恢复会把非终态任务各推一步,而那一步会重新走到派发。"""
    orchestrator, _ = await walk_to_the_todo(workspace, library)

    for _ in range(3):
        await orchestrator.recover()

    assert len(TodoStore(workspace).open_todos()) == 1


async def test_a_suspended_job_does_not_hold_a_concurrency_slot(
    workspace: Path, library: Path
) -> None:
    """泳道的理由是"子进程既吃机器也吃 API 配额"。人两样都不吃。

    并发上限设成 1,投出待办之后硅基 Job 仍然派得出去——挂着的那个如果占着名额,
    下面这一步会永远等下去。
    """
    hand_dev_to_a_human(workspace)
    first = _submit(workspace)
    _arm(library, first, PASSING_TEST)
    orchestrator = orchestrator_of(workspace, library, global_jobs=1)
    await orchestrator.advance(first)
    await orchestrator.advance(first)  # 投出待办并挂起

    second = _submit(workspace)
    _arm(library, second, PASSING_TEST)
    await orchestrator.advance(second)  # 硅基的需求解析:占不到名额就会卡死在这里

    assert TaskStore(workspace).get(second).state is TaskState.DEVELOPING


async def test_the_delivery_lands_in_the_event_plane(workspace: Path, library: Path) -> None:
    """三平面对 human Job 没有例外。"""
    _, task_id = await walk_to_the_todo(workspace, library)

    actions = [
        event.payload.get("action")
        for event in EventLog(workspace).events(task_id)
        if event.kind is LogKind.TODO
    ]

    assert actions == ["delivered"]


# --- 交活 -------------------------------------------------------------------


def submit_todo(
    workspace: Path, todo_id: str, payload: dict[str, Any], actor: str = "alice"
) -> Result:
    result = workspace / "handed-in.json"
    result.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return runner.invoke(
        app,
        [
            "todo",
            "submit",
            todo_id,
            "--result",
            str(result),
            "--as",
            actor,
            "--workspace",
            str(workspace),
            "--json",
        ],
    )


def test_handing_in_a_valid_artifact_moves_the_task_on(
    workspace: Path, library: Path
) -> None:
    """交完 = 产物已存在 = 崩溃恢复那条重放路。"""
    _, task_id = walk_sync(workspace, library)
    todo = only_todo(workspace)

    result = submit_todo(workspace, todo.id, DEV_RESULT_BY_HAND | {"task_id": task_id})

    assert result.exit_code == 0, result.output
    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.UNIT_TESTING
    assert task.pending_todo_id == ""


def test_an_invalid_artifact_is_refused_with_the_same_error_as_a_robot(
    workspace: Path, library: Path
) -> None:
    """没有"跳过校验"按钮。有那个按钮的话,"人也是一种运行时"就只剩一半。"""
    _, task_id = walk_sync(workspace, library)
    todo = only_todo(workspace)

    result = submit_todo(workspace, todo.id, {"task_id": task_id, "passed": True})

    assert result.exit_code != 0
    assert "不合契约" in result.output
    assert "Traceback" not in result.output
    # 待办还在,产物留在原地供人对照修改——与硅基员工的契约重试拿到的是同一份现场。
    assert TodoStore(workspace).get(todo.id).state == PENDING


def test_someone_else_may_not_hand_it_in(workspace: Path, library: Path) -> None:
    """一个能构造请求的人不受前端约束,而这条通路的产物会直接进流水线。"""
    _, task_id = walk_sync(workspace, library)
    todo = only_todo(workspace)

    result = submit_todo(
        workspace, todo.id, DEV_RESULT_BY_HAND | {"task_id": task_id}, actor="mallory"
    )

    assert result.exit_code != 0
    assert "指派人" in result.output


def test_the_todo_can_be_read_before_doing_the_work(
    workspace: Path, library: Path
) -> None:
    """人不知道产物契约的话,会交一份看起来对的东西然后被打回。"""
    _, _ = walk_sync(workspace, library)
    todo = only_todo(workspace)

    shown = runner.invoke(
        app, ["todo", "show", todo.id, "--workspace", str(workspace), "--json"]
    )

    payload = json.loads(shown.output)
    assert payload["procedure_id"] == "code-develop"
    assert "changed_files" in payload["schema"]["required"]
    assert payload["context_file"].endswith(".md")


async def test_a_tight_budget_does_not_block_handing_work_to_a_person(
    workspace: Path, library: Path
) -> None:
    """人的 Job 不预扣预算。

    沿用缺省额度的话,一个预算吃紧的任务会在待办投出去之前就被判"装不下下一个 Job"并升级
    人工——而它本来正要交给人做。
    """
    hand_dev_to_a_human(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    store = TaskStore(workspace)
    orchestrator = orchestrator_of(workspace, library)
    await orchestrator.advance(task_id)
    store.save(store.get(task_id).evolve(budget_tokens=10, tokens_used=9))

    await orchestrator.advance(task_id)

    assert len(TodoStore(workspace).open_todos()) == 1
    assert store.get(task_id).state is TaskState.DEVELOPING


@pytest.mark.parametrize("configured", [False])
async def test_a_workspace_without_human_behaves_exactly_as_before(
    workspace: Path, library: Path, configured: bool
) -> None:
    """不配 human 的部署行为与今天逐字节相同。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = orchestrator_of(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    assert TodoStore(workspace).open_todos() == ()
    assert TaskStore(workspace).get(task_id).state is TaskState.READY_TO_COMMIT


# --- 工作树类待办:人写的代码也照查越权 --------------------------------------


def write_in_the_worktree(workspace: Path, task_id: str, relative: str, body: str) -> None:
    """人在任务工作树里改了一个文件。"""
    target = GitWorkspace(workspace).worktree_path(task_id) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def test_a_coding_todo_points_at_the_worktree_not_a_form(
    workspace: Path, library: Path
) -> None:
    """能写代码的活就是"去工作树里干"的活——判据取自这次派发的授权范围。"""
    _, task_id = walk_sync(workspace, library)

    todo = only_todo(workspace)

    assert todo.kind == "worktree"
    assert todo.workdir  # 人要知道去哪儿改代码
    shown = runner.invoke(app, ["todo", "show", todo.id, "--workspace", str(workspace)])
    assert "去这里改代码" in shown.output


def test_the_receipt_records_what_git_saw_not_what_the_person_claimed(
    workspace: Path, library: Path
) -> None:
    """**验证产物,不验证自述。** 自评会漏、会瞒,而 git 不会。"""
    _, task_id = walk_sync(workspace, library)
    todo = only_todo(workspace)
    write_in_the_worktree(
        workspace, task_id, "repos/order-service/src/order/by_hand.py", "# 人写的\n"
    )

    result = submit_todo(
        workspace,
        todo.id,
        DEV_RESULT_BY_HAND | {"task_id": task_id, "changed_files": ["我随便写的.py"]},
    )

    assert result.exit_code == 0, result.output
    landed = json.loads(
        (workspace / todo.output_dir / "result.json").read_text(encoding="utf-8")
    )
    # 业务仓是子模块,所以顶层工作树看到的是**子模块指针**动了——与结对那条路看到的
    # 是同一份东西(它们共用同一个读法)。关键在于:这不是人自报的那一行。
    assert landed["changed_files"] == ["repos/order-service"]


def test_a_person_writing_outside_the_authorised_paths_is_refused(
    workspace: Path, library: Path
) -> None:
    """人在场不放宽边界。工具集这一层对人根本不存在,最小权限只剩这一道检查。"""
    _, task_id = walk_sync(workspace, library)
    todo = only_todo(workspace)
    write_in_the_worktree(workspace, task_id, "genome/rules/architecture.md", "# 一切皆可\n")

    result = submit_todo(workspace, todo.id, DEV_RESULT_BY_HAND | {"task_id": task_id})

    assert result.exit_code != 0
    assert "超出" in result.output
    assert TodoStore(workspace).get(todo.id).state == PENDING
    # **不回滚人的劳动**:拒收这次提交已经拦住了"越权改动进合并",而删掉他刚写的东西
    # 只会让人绕开这套系统干活。
    assert (
        GitWorkspace(workspace).worktree_path(task_id) / "genome/rules/architecture.md"
    ).exists()


def test_the_scope_check_is_the_same_one_the_pair_session_uses() -> None:
    """两条各写一遍的话,"人改的不用查"这个口子会以另一种形式再开一次。"""
    import inspect

    from agentgenome.jobs.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator._enforce_pair_scope)

    assert "enforce_human_scope" in source


# --- 超时三段的第三段:升级人工 ----------------------------------------------


def test_a_todo_nobody_picks_up_escalates_through_the_normal_path(
    workspace: Path, library: Path
) -> None:
    """三段走完才进终态,而且走的是**编排器既有的那条升级路径**。

    自己写一条状态迁移的话,"任务怎么进的终态"就有了第二个答案——而那条新写的路多半会漏掉
    封存与蒸馏触发,漏掉的那一次恰好是最需要证据的那一次。
    """
    _, task_id = walk_sync(workspace, library)
    todo = only_todo(workspace)
    late = todo.created_at + timedelta(days=30)

    report = sweep(workspace, Config(human=HumanConfig()), now=late)

    assert report.escalated == [todo.id]
    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.ESCALATED
    assert "找一个能接手的人" in (task.escalate_reason or "")
    assert task.pending_todo_id == ""
    assert TodoStore(workspace).get(todo.id).state == ESCALATED


def test_the_person_it_moved_away_from_may_no_longer_hand_it_in(
    workspace: Path, library: Path
) -> None:
    """改派之后原指派人再交活 → 拒绝。他已经不是这张待办的人了。"""
    _, task_id = walk_sync(workspace, library)
    todo = only_todo(workspace)
    late = todo.created_at + timedelta(days=30)
    sweep(workspace, Config(human=HumanConfig(backups=["bob"])), now=late)

    result = submit_todo(
        workspace, todo.id, DEV_RESULT_BY_HAND | {"task_id": task_id}, actor="alice"
    )

    assert result.exit_code != 0
    assert "指派人" in result.output


# --- assisted:机器干、人确认 ------------------------------------------------


def enable_assisted(workspace: Path, confirmer: str = "alice") -> None:
    config = workspace / "agentgenome.yaml"
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    payload.setdefault("runtime", {})["human"] = {}
    payload["topology"] = {"assisted": {"employees": ["dev-employee"], "confirmer": confirmer}}
    config.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def walk_to_the_confirmation(workspace: Path, library: Path) -> tuple[Orchestrator, str]:
    enable_assisted(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    # assisted 是**两个节点**的图,于是开发那一步的回放键带上了节点名与轮次
    # (`main.1`)——不带的话,同一轮里的两次派发会撞在一个键上。
    from tests.e2e.test_critique_loop import record_node  # noqa: PLC2701
    from tests.e2e.test_orchestrator import DEV_RESULT  # noqa: PLC2701

    record_node(
        library,
        "dev-employee",
        "code-develop",
        "main.1",
        DEV_RESULT | {"task_id": task_id},
        {"repos/order-service/tests/test_reserve.py": PASSING_TEST},
    )
    orchestrator = orchestrator_of(workspace, library)

    async def go() -> None:
        await orchestrator.advance(task_id)  # 需求解析
        await orchestrator.advance(task_id)  # 开发(机器)→ 确认待办

    asyncio.run(go())
    return orchestrator, task_id


CONFIRMED = {
    "task_id": "",
    "producer": "alice",
    "created_at": "2026-09-01T12:00:00Z",
    "passed": True,
    "approved": True,
    "changed_files": [],
    "self_test": {"command": "pytest -q", "exit_code": 0, "passed": True},
    "impact": {"modules": ["order-service"], "rationale": "确认通过"},
    "questions": [],
}
REJECTED = {**CONFIRMED, "approved": False, "note": "错误信息里带了客户手机号"}


def test_an_assisted_employee_gets_a_confirmation_todo(
    workspace: Path, library: Path
) -> None:
    """机器干完之后出现一张确认待办,而任务留在开发态等人。"""
    _, task_id = walk_to_the_confirmation(workspace, library)

    todo = only_todo(workspace)

    assert todo.assignee == "alice"
    assert todo.node == "confirm"
    assert TaskStore(workspace).get(task_id).state is TaskState.DEVELOPING


def test_confirming_lets_the_task_move_on(workspace: Path, library: Path) -> None:
    _, task_id = walk_to_the_confirmation(workspace, library)
    todo = only_todo(workspace)

    result = submit_todo(workspace, todo.id, CONFIRMED | {"task_id": task_id})

    assert result.exit_code == 0, result.output
    assert TaskStore(workspace).get(task_id).state is TaskState.UNIT_TESTING


def test_rejecting_sends_the_note_into_the_next_round(
    workspace: Path, library: Path
) -> None:
    """**意见进上下文,不是只记一条事件。**

    只记事件的话,下一轮的员工看不到它,于是原样再做一遍——而人会以为自己已经说过了。
    """
    _, task_id = walk_to_the_confirmation(workspace, library)
    todo = only_todo(workspace)

    result = submit_todo(workspace, todo.id, REJECTED | {"task_id": task_id})

    assert result.exit_code == 0, result.output
    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.DEVELOPING
    assert task.fix_rounds == 1
    reports = read_failure_reports(workspace / "tasks" / task_id)
    assert any("客户手机号" in item.body for item in reports)


def test_a_confirmation_todo_uses_the_same_todo_machinery(
    workspace: Path, library: Path
) -> None:
    """确认不是新原语:它自动获得待办、超时三段、改派、RBAC 全套设施。"""
    _, task_id = walk_to_the_confirmation(workspace, library)
    todo = only_todo(workspace)

    late = todo.created_at + timedelta(days=30)
    report = sweep(workspace, Config(human=HumanConfig(backups=["bob"])), now=late)

    assert report.reassigned == [todo.id]
    assert TodoStore(workspace).get(todo.id).assignee == "bob"


def test_without_assisted_nothing_changes(workspace: Path, library: Path) -> None:
    """不配 assisted 时行为与今天逐字节相同。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = orchestrator_of(workspace, library)

    async def go() -> None:
        for _ in range(3):
            await orchestrator.advance(task_id)

    asyncio.run(go())

    assert TodoStore(workspace).open_todos() == ()
    assert TaskStore(workspace).get(task_id).state is TaskState.READY_TO_COMMIT


# --- 指标:自动化率必须与质量指标成对 ----------------------------------------


def test_the_three_modes_are_counted_separately(workspace: Path, library: Path) -> None:
    """auto / assisted / manual 各自计数,能按工作区查。

    **它必须与门禁一次通过率一起看**:关掉确认节点就能刷高自动化率,单独上报它等于奖励一个
    把信任爬坡走成信任跳崖的动作——所以同一份快照里两个数都在。
    """
    _, task_id = walk_to_the_confirmation(workspace, library)

    snapshot = collect(workspace)

    assert snapshot.execution_modes["auto"] >= 1  # 需求解析那一步是纯自动
    assert snapshot.execution_modes["assisted"] == 1  # 开发那一步是机器干、人确认
    assert snapshot.execution_modes["manual"] == 0
    body = render(snapshot)
    assert "agentgenome_execution_mode_total" in body
    assert "agentgenome_gate_first_pass_ratio" in body


def test_a_fully_manual_step_counts_as_manual(workspace: Path, library: Path) -> None:
    """整步交给人时,它不是 assisted——那是两个不同的信任爬坡位置。"""
    _, task_id = walk_sync(workspace, library)

    snapshot = collect(workspace)

    assert snapshot.execution_modes["manual"] == 1
    assert snapshot.execution_modes["assisted"] == 0


def test_a_refused_submission_leaves_a_trace(workspace: Path, library: Path) -> None:
    """"谁试过替别人交活"正是审计要问的问题。

    一次被拒的提交在事件面上不存在的话,那个问题只能靠"没人报告过"来回答。
    """
    _, task_id = walk_sync(workspace, library)
    todo = only_todo(workspace)

    submit_todo(workspace, todo.id, DEV_RESULT_BY_HAND | {"task_id": task_id}, actor="mallory")

    actions = [
        event.payload.get("action")
        for event in EventLog(workspace).events(task_id)
        if event.kind is LogKind.TODO
    ]
    assert "refused" in actions


def test_a_todo_assigned_to_a_role_may_be_handed_in_by_a_member() -> None:
    """指派人可以是一个角色。只认名字相等的话,派给"审批组"的待办永远没人交得掉。"""
    from agentgenome.todo.service import _may_submit
    from agentgenome.todo.store import Todo

    todo = Todo(
        id="t",
        task_id="ag-1",
        stage="develop",
        node="",
        attempt=1,
        assignee="approver",
        employee_id="dev-employee",
        procedure_id="code-develop",
        output_dir="x",
        context_file="y",
    )

    assert _may_submit(todo, "alice", frozenset({"approver"}))
    assert not _may_submit(todo, "alice", frozenset({"developer"}))


def test_moving_an_employee_up_the_rung_from_the_console_really_stops_the_work(
    workspace: Path, library: Path
) -> None:
    """**配置落盘不等于生效。**

    这一条走人真会走的那条路:从 REST 面把开发员工从 auto 拧到 assisted,然后跑一个任务,
    断言它真的停下来等确认——而不是只断言配置文件里多了一行。
    """
    from fastapi.testclient import TestClient

    from agentgenome.server.app import create_app
    from agentgenome.server.rbac import Principal, Role

    config = workspace / "agentgenome.yaml"
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    payload.setdefault("runtime", {})["human"] = {}
    config.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    client = TestClient(
        create_app(workspace, principals={"root": Principal("root", frozenset({Role.ADMIN}))})
    )
    section = client.get("/settings", headers={"x-actor": "root"}).json()["topology"]
    section["assisted"] = {"employees": ["dev-employee"], "confirmer": "alice"}
    written = client.put(
        "/settings",
        json={"section": "topology", "value": section},
        headers={"x-actor": "root"},
    )
    assert written.status_code == 200, written.text

    # 拧完之后花名册上那一行就是新档位——人下一眼看的就是这里。
    members = {
        item["id"]: item for item in client.get("/insights/roster").json()["employees"]
    }
    assert members["dev-employee"]["execution"] == "assisted"
    assert members["dev-employee"]["confirmer"] == "alice"

    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    from tests.e2e.test_critique_loop import record_node  # noqa: PLC2701
    from tests.e2e.test_orchestrator import DEV_RESULT  # noqa: PLC2701

    record_node(
        library,
        "dev-employee",
        "code-develop",
        "main.1",
        DEV_RESULT | {"task_id": task_id},
        {"repos/order-service/tests/test_reserve.py": PASSING_TEST},
    )
    orchestrator = orchestrator_of(workspace, library)

    async def go() -> None:
        await orchestrator.advance(task_id)
        await orchestrator.advance(task_id)

    asyncio.run(go())

    todo = only_todo(workspace)
    assert todo.assignee == "alice"
    assert TaskStore(workspace).get(task_id).state is TaskState.DEVELOPING


def test_moving_an_employee_to_manual_from_the_console_lands_work_in_that_person_inbox(
    workspace: Path, library: Path
) -> None:
    """**这一片的价值不在于文件写成功**,而在于这条链路从一次配置动作通到某个人的收件箱。"""
    from fastapi.testclient import TestClient

    from agentgenome.server.app import create_app
    from agentgenome.server.rbac import Principal, Role

    config = workspace / "agentgenome.yaml"
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    payload.setdefault("runtime", {})["human"] = {}
    config.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    client = TestClient(
        create_app(workspace, principals={"root": Principal("root", frozenset({Role.ADMIN}))})
    )
    response = client.put(
        "/employees/dev-employee/execution",
        json={"execution": "manual", "assignee": "alice"},
        headers={"x-actor": "root"},
    )
    assert response.status_code == 200, response.text

    members = {item["id"]: item for item in client.get("/insights/roster").json()["employees"]}
    assert members["dev-employee"]["execution"] == "manual"

    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = orchestrator_of(workspace, library)

    async def go() -> None:
        await orchestrator.advance(task_id)
        await orchestrator.advance(task_id)

    asyncio.run(go())

    todo = only_todo(workspace)
    assert todo.assignee == "alice"
    assert client.get("/todos?assignee=alice").json()["items"][0]["id"] == todo.id
