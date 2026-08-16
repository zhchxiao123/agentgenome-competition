"""人工介入处置：结束待办，不篡改机器执行的终态。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.genome_task import GenomeTaskKind, GenomeTaskState, GenomeTaskStore, Origin
from agentgenome.core.intervention import InterventionError, resolve_dev, resolve_genome
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskStore


def test_resolving_a_dev_escalation_preserves_state_and_writes_an_event(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    task = store.create(title="新增接口", requirement="新增库存接口")
    store.save(task.evolve(state=TaskState.ESCALATED, escalate_reason="需求解析失败"))
    stamp = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    resolved = resolve_dev(tmp_path, task.id, actor="alice", note="已改需求", now=stamp)

    assert resolved.state is TaskState.ESCALATED
    assert resolved.intervention_resolved_at == stamp
    event = EventLog(tmp_path).events(task.id)[-1]
    assert event.kind is LogKind.INTERVENTION_RESOLVED
    assert event.actor == "alice"
    assert event.payload["note"] == "已改需求"


def test_only_an_escalated_dev_task_can_be_resolved(tmp_path: Path) -> None:
    task = TaskStore(tmp_path).create(title="新增接口", requirement="新增库存接口")

    with pytest.raises(InterventionError, match="不需要人工介入"):
        resolve_dev(tmp_path, task.id, actor="alice", note="误操作")


def test_resolving_a_human_genome_failure_preserves_failed_state(tmp_path: Path) -> None:
    store = GenomeTaskStore(tmp_path)
    task = store.create(title="知识初始化", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    store.save(task.evolve(state=GenomeTaskState.FAILED, failure_reason="超时"))

    resolved = resolve_genome(tmp_path, task.id, actor="alice", note="暂不重试")

    assert resolved.state is GenomeTaskState.FAILED
    assert resolved.is_settled
    assert EventLog(tmp_path).events(task.id)[-1].actor == "alice"
    assert EventLog(tmp_path).events(task.id)[-1].payload["note"] == "暂不重试"


def test_system_genome_failures_cannot_be_manually_resolved(tmp_path: Path) -> None:
    store = GenomeTaskStore(tmp_path)
    task = store.create(title="蒸馏", kind=GenomeTaskKind.DISTILL, origin=Origin.SYSTEM)
    store.save(task.evolve(state=GenomeTaskState.FAILED))

    with pytest.raises(InterventionError, match="不需要人工介入"):
        resolve_genome(tmp_path, task.id, actor="alice", note="误操作")
