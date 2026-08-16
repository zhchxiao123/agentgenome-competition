"""人工介入的处置入口。

机器状态与人的待办是两个维度：处置一次升级不应该把 ``ESCALATED`` 或 ``FAILED``
伪装成成功。这里保留机器事实，只给待办盖章并写审计事件。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.genome_task import GenomeTask, GenomeTaskState, GenomeTaskStore, Origin
from agentgenome.core.states import TaskState
from agentgenome.core.task import Task, TaskStore


class InterventionError(ValueError):
    """目标任务当前没有一张可处置的人工介入待办。"""


def resolve_dev(
    workspace_root: Path,
    task_id: str,
    *,
    actor: str,
    note: str = "",
    now: datetime | None = None,
) -> Task:
    """处置研发任务升级，保留 ``ESCALATED`` 状态。"""
    store = TaskStore(workspace_root)
    task = store.get(task_id)
    if task.state is not TaskState.ESCALATED:
        raise InterventionError(f"{task.id} 当前不需要人工介入")
    if task.intervention_resolved_at is not None:
        return task
    return _save_and_log_dev(store, workspace_root, task, actor, note, now)


def resolve_genome(
    workspace_root: Path,
    task_id: str,
    *,
    actor: str,
    note: str = "",
    now: datetime | None = None,
) -> GenomeTask:
    """处置人发起的基因组失败，保留 ``FAILED`` 状态。"""
    store = GenomeTaskStore(workspace_root)
    task = store.get(task_id)
    if task.state is not GenomeTaskState.FAILED or task.origin is not Origin.HUMAN:
        raise InterventionError(f"{task.id} 当前不需要人工介入")
    if task.intervention_resolved_at is not None:
        return task
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    resolved = store.save(task.evolve(intervention_resolved_at=stamp), now=stamp)
    _log(workspace_root, task.id, actor, note, stamp)
    return resolved


def _save_and_log_dev(
    store: TaskStore,
    workspace_root: Path,
    task: Task,
    actor: str,
    note: str,
    now: datetime | None,
) -> Task:
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    resolved = store.save(task.evolve(intervention_resolved_at=stamp), now=stamp)
    _log(workspace_root, task.id, actor, note, stamp)
    return resolved


def _log(workspace_root: Path, task_id: str, actor: str, note: str, now: datetime) -> None:
    EventLog(workspace_root).append(
        task_id,
        actor=actor,
        kind=LogKind.INTERVENTION_RESOLVED,
        payload={"note": note.strip()},
        now=now,
    )


__all__ = ["InterventionError", "resolve_dev", "resolve_genome"]
