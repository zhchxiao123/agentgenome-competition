"""需求实体:仓储与状态推导。

状态没有列——它是从尝试链算出来的(PRD 43 D2)。这里测的就是那四行推导规则的全谱,
用真实的 `Task` 值,不 mock。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agentgenome.core.requirement import (
    Requirement,
    RequirementState,
    RequirementStore,
    derive_state,
)
from agentgenome.core.states import TaskState
from agentgenome.core.task import Task


def _requirement(parked: str = "") -> Requirement:
    return Requirement(
        id="req-20260813-001",
        title="退款",
        text="支持部分退款",
        priority=5,
        parked=parked,
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        updated_at=datetime(2026, 8, 13, tzinfo=UTC),
    )


def _attempt(state: TaskState) -> Task:
    return Task(id="ag-20260813-001", title="退款", requirement="支持部分退款", state=state)


class TestDeriveState:
    def test_no_attempts_is_queued(self) -> None:
        assert derive_state(_requirement(), ()) is RequirementState.QUEUED

    def test_open_attempt_is_in_progress(self) -> None:
        found = derive_state(_requirement(), (_attempt(TaskState.DEVELOPING),))
        assert found is RequirementState.IN_PROGRESS

    def test_escalated_attempt_is_queued_not_in_progress(self) -> None:
        """升级人工是终态:那次尝试停了,需求回到排队,等下一次尝试。"""
        found = derive_state(_requirement(), (_attempt(TaskState.ESCALATED),))
        assert found is RequirementState.QUEUED

    def test_completed_attempt_is_delivered(self) -> None:
        found = derive_state(_requirement(), (_attempt(TaskState.COMPLETED),))
        assert found is RequirementState.DELIVERED

    def test_delivered_wins_over_new_open_attempt(self) -> None:
        """交付之后又发起跟进尝试,列表上仍以已交付计(PRD 43 D2 的优先序)。"""
        attempts = (_attempt(TaskState.COMPLETED), _attempt(TaskState.DEVELOPING))
        assert derive_state(_requirement(), attempts) is RequirementState.DELIVERED

    def test_parked_wins_over_everything(self) -> None:
        attempts = (_attempt(TaskState.COMPLETED), _attempt(TaskState.DEVELOPING))
        assert derive_state(_requirement(parked="不做了"), attempts) is RequirementState.PARKED


class TestRequirementStore:
    def test_create_assigns_prefixed_sequential_ids(self, tmp_path: Path) -> None:
        store = RequirementStore(tmp_path)
        now = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
        first = store.create(title="退款", text="支持部分退款", now=now)
        second = store.create(title="对账", text="对账单导出", now=now)
        assert first.id == "req-20260813-001"
        assert second.id == "req-20260813-002"

    def test_roundtrip(self, tmp_path: Path) -> None:
        store = RequirementStore(tmp_path)
        created = store.create(title="退款", text="支持部分退款", priority=7)
        found = store.get(created.id)
        assert found == created
        assert found.parked == ""

    def test_all_newest_first(self, tmp_path: Path) -> None:
        store = RequirementStore(tmp_path)
        first = store.create(title="a", text="a", now=datetime(2026, 8, 13, 9, 0, tzinfo=UTC))
        second = store.create(title="b", text="b", now=datetime(2026, 8, 13, 10, 0, tzinfo=UTC))
        assert [item.id for item in store.all()] == [second.id, first.id]

    def test_shares_database_with_tasks(self, tmp_path: Path) -> None:
        """需求与任务同库:`requirement_id` 挂链后一次连接能看全两张表。"""
        from agentgenome.core.task import TaskStore

        requirement = RequirementStore(tmp_path).create(title="退款", text="支持部分退款")
        task = TaskStore(tmp_path).create(
            title="退款", requirement="支持部分退款", requirement_id=requirement.id
        )
        found = TaskStore(tmp_path).get(task.id)
        assert found.requirement_id == requirement.id
        assert TaskStore(tmp_path).attempts_of(requirement.id) == (found,)

    def test_legacy_task_has_no_requirement(self, tmp_path: Path) -> None:
        """存量任务不回填:`requirement_id` 是 None,不是空串——"没有"与"查不到"分得开。"""
        from agentgenome.core.task import TaskStore

        task = TaskStore(tmp_path).create(title="旧任务", requirement="旧需求")
        assert TaskStore(tmp_path).get(task.id).requirement_id is None
