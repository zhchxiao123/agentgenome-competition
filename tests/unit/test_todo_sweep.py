"""到期扫描:提醒 → 改派 → 升级人工。

**人的待办超时是常态,不是事故。** 直接升级的话,"等另一个人接管"这句话接不回来——
已升级人工是终态,那个任务只能被新建一个来替代,而它已经花掉的钱都跟着废掉。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentgenome.config import Config, HumanConfig
from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskStore
from agentgenome.todo.store import PENDING, Todo, TodoStore
from agentgenome.todo.sweep import REASSIGNED, REMINDED, sweep

START = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    store = TaskStore(tmp_path)
    task = store.create(title="下单预占", requirement="下单时预占库存")
    TodoStore(tmp_path).deliver(
        Todo(
            id="todo-1",
            task_id=task.id,
            stage="develop",
            node="",
            attempt=1,
            assignee="alice",
            employee_id="dev-employee",
            procedure_id="code-develop",
            output_dir=f"tasks/{task.id}/artifacts/01-develop",
            context_file=f"tasks/{task.id}/artifacts/01-develop/context.md",
        ),
        now=START,
    )
    store.save(store.get(task.id).evolve(pending_todo_id="todo-1"))
    return tmp_path


def settings(**overrides: object) -> Config:
    return Config(human=HumanConfig(backups=["bob"], **overrides))  # type: ignore[arg-type]


def actions(root: Path) -> list[str]:
    task_id = TaskStore(root).open_tasks()[0].id if TaskStore(root).open_tasks() else ""
    events = EventLog(root).events(task_id) if task_id else ()
    return [item.payload.get("action", "") for item in events if item.kind is LogKind.TODO]


def only(root: Path) -> Todo:
    return TodoStore(root).get("todo-1")


def test_nothing_happens_before_the_first_window(workspace: Path) -> None:
    report = sweep(workspace, settings(), now=START + timedelta(hours=2))

    assert report.as_dict() == {
        "reminded": [],
        "reassigned": [],
        "escalated": [],
        "would_remind": [],
        "would_escalate": [],
    }
    assert only(workspace).state == PENDING


def test_the_first_window_reminds_without_moving_anything(workspace: Path) -> None:
    """提醒不改待办也不改任务:它只是拍拍肩膀。"""
    report = sweep(workspace, settings(), now=START + timedelta(days=1, hours=1))

    assert report.reminded == ["todo-1"]
    todo = only(workspace)
    assert todo.state == PENDING and todo.assignee == "alice" and todo.reminded


def test_sweeping_three_times_in_a_row_reminds_once(workspace: Path) -> None:
    """幂等靠待办上的标记,不靠"上次扫描是什么时候"这种会随部署丢失的记忆。"""
    when = START + timedelta(days=1, hours=1)

    for _ in range(3):
        sweep(workspace, settings(), now=when)

    assert [item for item in actions(workspace) if item == REMINDED] == [REMINDED]


def test_the_second_window_hands_it_to_someone_else(workspace: Path) -> None:
    """改派仍然是**待确认**:机器停手等人,任务健康。"""
    report = sweep(workspace, settings(), now=START + timedelta(days=3, hours=1))

    assert report.reassigned == ["todo-1"]
    todo = only(workspace)
    assert todo.assignee == "bob"
    assert todo.state == PENDING
    assert todo.reassignments == 1
    assert todo.history == ("alice", "bob")
    assert TaskStore(workspace).open_tasks()[0].state is TaskState.CREATED


def test_the_clock_restarts_after_a_reassignment(workspace: Path) -> None:
    """换了人就是新的一段等待。不重置的话,新接手的人一秒钟都没有就被判超时。"""
    moved = START + timedelta(days=3, hours=1)
    sweep(workspace, settings(), now=moved)

    sweep(workspace, settings(), now=moved + timedelta(hours=1))

    assert only(workspace).state == PENDING


# 第三段(升级人工)走的是编排器既有的那条落地路径,它要一个真实的 Workspace(配置、
# 规则、封存)。所以它在 `tests/e2e/test_human_runtime.py` 里验——**在这里用替身糊过去的话,
# 验到的是替身,而这一段最要紧的恰恰是"它与别的升级走同一条路"。**


def test_a_dry_run_has_no_side_effects_at_all(workspace: Path) -> None:
    """"我先看看有哪些要升级"不该改变任何东西。

    尤其**不该顺手把提醒标记烧掉**:掉进提醒分支的话,一次只看的动作会让真正那一遍扫描
    从此再也不提醒——一个只读操作改掉了将来的行为,这是 dry-run 最坏的失败形态。
    """
    report = sweep(workspace, settings(), now=START + timedelta(days=9), escalate=False)

    assert report.escalated == [] and report.reminded == []
    assert report.would_escalate == ["todo-1"]
    assert only(workspace).reminded is False
    assert TaskStore(workspace).open_tasks()[0].state is TaskState.CREATED


def test_the_first_two_stages_are_in_the_event_plane(workspace: Path) -> None:
    """"这张待办现在卡在谁那儿、卡了多久"要答得出来。"""
    sweep(workspace, settings(), now=START + timedelta(days=1, hours=1))
    sweep(workspace, settings(), now=START + timedelta(days=3, hours=1))

    assert actions(workspace) == [REMINDED, REASSIGNED]


def test_the_windows_must_be_ordered() -> None:
    """提醒窗口不小于改派窗口的话,提醒永远来不及发。"""
    with pytest.raises(ValueError):
        HumanConfig(reminder_after_days=3, reassign_after_days=3)
