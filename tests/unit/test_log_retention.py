"""原始日志什么时候可以删。

没有保留期不等于永久保留,只等于**磁盘满的那天由运维随手删掉**——那是最不可控的清理策略。

判定是纯函数:收任务集与截止时刻,给出该清理谁。测试因此不需要注入时钟,也不需要等三十天。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentgenome.core.states import TaskState
from agentgenome.core.task import Task
from agentgenome.security.retention import SkipReason, plan_prune

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=30)


def _task(task_id: str, state: TaskState, *, age_days: int) -> Task:
    stamp = NOW - timedelta(days=age_days)
    return Task(
        id=task_id,
        title=task_id,
        requirement="x",
        state=state,
        created_at=stamp,
        updated_at=stamp,
    )


def test_an_old_terminal_task_with_a_bundle_is_pruned() -> None:
    task = _task("ag-1", TaskState.COMPLETED, age_days=40)

    plan = plan_prune([task], workspace_root=Path(), cutoff=CUTOFF, sealed={"ag-1"})

    assert [item.task_id for item in plan.prune] == ["ag-1"]
    assert not plan.skipped


def test_a_task_inside_the_retention_window_is_kept() -> None:
    task = _task("ag-1", TaskState.COMPLETED, age_days=3)

    plan = plan_prune([task], workspace_root=Path(), cutoff=CUTOFF, sealed={"ag-1"})

    assert not plan.prune
    assert plan.skipped[0].reason is SkipReason.WITHIN_RETENTION


def test_a_running_task_is_never_pruned() -> None:
    """哪怕远超保留期。它还在跑,日志正在被写。"""
    task = _task("ag-1", TaskState.DEVELOPING, age_days=400)

    plan = plan_prune([task], workspace_root=Path(), cutoff=CUTOFF, sealed={"ag-1"})

    assert not plan.prune
    assert plan.skipped[0].reason is SkipReason.NOT_TERMINAL


def test_without_a_bundle_nothing_is_pruned_however_old() -> None:
    """宁可占磁盘,不可丢证据。

    反过来的设计(超期就删、不管有没有包)会在归档盘满这种故障下**静默地把证据全删光**,
    而且删得越干净越像一切正常。
    """
    task = _task("ag-1", TaskState.ESCALATED, age_days=400)

    plan = plan_prune([task], workspace_root=Path(), cutoff=CUTOFF, sealed=set())

    assert not plan.prune
    assert plan.skipped[0].reason is SkipReason.NO_BUNDLE


def test_escalated_tasks_are_prunable_once_sealed() -> None:
    """已升级人工是终态。它不是已了结,但那管的是"列表里还要不要出现",不是日志留多久。"""
    task = _task("ag-1", TaskState.ESCALATED, age_days=40)

    plan = plan_prune([task], workspace_root=Path(), cutoff=CUTOFF, sealed={"ag-1"})

    assert [item.task_id for item in plan.prune] == ["ag-1"]


def test_every_task_lands_in_exactly_one_bucket() -> None:
    tasks = [
        _task("ag-old", TaskState.COMPLETED, age_days=40),
        _task("ag-fresh", TaskState.COMPLETED, age_days=1),
        _task("ag-running", TaskState.DEVELOPING, age_days=99),
        _task("ag-unsealed", TaskState.CANCELLED, age_days=99),
    ]

    plan = plan_prune(
        tasks, workspace_root=Path(), cutoff=CUTOFF, sealed={"ag-old", "ag-fresh", "ag-running"}
    )

    assert len(plan.prune) + len(plan.skipped) == len(tasks)
    assert {item.task_id for item in plan.skipped} == {"ag-fresh", "ag-running", "ag-unsealed"}
