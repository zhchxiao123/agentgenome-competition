"""蒸馏是一条基因组任务,而不只是挂在源任务上的一段内联管道。

它此前没有身份:失败与耗时没有地方安放,进行中时人完全看不见。**给它一条记录**,同时在源
研发任务的时间线上留一条指针——「这个任务的经验变成了什么」仍然读得连贯。

指针不是内容,不违反「同一件事只由一个平面记内容」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.genome_task import (
    GenomeTaskKind,
    GenomeTaskState,
    GenomeTaskStore,
    Origin,
)
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskStore
from agentgenome.genome.evolution.pipeline import DistillResult
from agentgenome.genome.evolution.record import (
    DISTILL_TITLE,
    finish_distillation,
    open_distillation,
)


def _ok() -> DistillResult:
    return DistillResult(task_id="ag-1", ok=True)


def _failed(reason: str) -> DistillResult:
    return DistillResult(task_id="ag-1", ok=False, error=reason)


def _skipped(reason: str) -> DistillResult:
    return DistillResult(task_id="ag-1", ok=True, skipped=reason)


@pytest.fixture
def source(tmp_path: Path) -> str:
    store = TaskStore(tmp_path)
    task = store.create(title="下单要预占库存", requirement="x")
    return store.save(task.evolve(state=TaskState.COMPLETED)).id


def test_a_distillation_gets_its_own_record(tmp_path: Path, source: str) -> None:
    """它此前没有身份:失败与耗时没有地方安放。"""
    opened = open_distillation(tmp_path, source, budget_tokens=1000)

    stored = GenomeTaskStore(tmp_path).get(opened.id)
    assert stored.kind is GenomeTaskKind.DISTILL
    assert stored.origin is Origin.SYSTEM
    assert stored.source_task_id == source
    assert stored.budget_tokens == 1000
    assert DISTILL_TITLE in stored.title


def test_the_source_task_keeps_a_pointer(tmp_path: Path, source: str) -> None:
    """「这个任务的经验变成了什么」仍然读得连贯。"""
    opened = open_distillation(tmp_path, source)

    pointers = [
        event
        for event in EventLog(tmp_path).events(source)
        if event.payload.get("note") == "distillation"
    ]
    assert [event.payload["genome_task_id"] for event in pointers] == [opened.id]


def test_the_lifecycle_events_hang_on_the_genome_task(tmp_path: Path, source: str) -> None:
    opened = open_distillation(tmp_path, source)

    finish_distillation(tmp_path, opened.id, _ok())

    kinds = [event.kind for event in EventLog(tmp_path).events(opened.id)]
    assert LogKind.TRANSITION in kinds


def test_a_successful_distillation_ends_submitted(tmp_path: Path, source: str) -> None:
    opened = open_distillation(tmp_path, source)

    finish_distillation(tmp_path, opened.id, _ok())

    assert GenomeTaskStore(tmp_path).get(opened.id).state is GenomeTaskState.SUBMITTED


def test_a_failed_distillation_is_settled_and_bothers_nobody(tmp_path: Path, source: str) -> None:
    """一次蒸馏失败不该变成一条需要处理的告警。"""
    opened = open_distillation(tmp_path, source)

    finish_distillation(tmp_path, opened.id, _failed("素材收集失败"))

    store = GenomeTaskStore(tmp_path)
    failed = store.get(opened.id)
    assert failed.state is GenomeTaskState.FAILED
    assert failed.is_settled
    assert store.needs_attention() == ()
    assert store.awaiting_confirmation() == ()


def test_a_failed_distillation_is_still_findable(tmp_path: Path, source: str) -> None:
    """「这次蒸馏挂了」必须能被看到,只是不该变成待办。"""
    opened = open_distillation(tmp_path, source)

    finish_distillation(tmp_path, opened.id, _failed("素材收集失败"))

    assert GenomeTaskStore(tmp_path).get(opened.id).failure_reason == "素材收集失败"


def test_the_source_task_terminal_state_is_untouched(tmp_path: Path, source: str) -> None:
    """**管道失败绝不影响已经完成的任务。** 一个蒸馏 bug 会让所有任务看起来失败——
    那是这条管道能造成的最大伤害,比不蒸馏糟得多。"""
    opened = open_distillation(tmp_path, source)

    finish_distillation(tmp_path, opened.id, _failed("炸了"))

    assert TaskStore(tmp_path).get(source).state is TaskState.COMPLETED


def test_opening_a_distillation_never_raises(tmp_path: Path) -> None:
    """蒸馏的建档失败不该把一个已经跑完的任务拖下水——它连管道都还没进。"""
    assert open_distillation(tmp_path, "ag-does-not-exist") is not None


def test_a_skipped_distillation_is_not_reported_as_success(tmp_path: Path, source: str) -> None:
    """「这次没蒸馏」与「蒸馏出了三张卡」不能是同一个终态——否则看板上一条「已提交」的
    记录背后可能一张卡片都没有。"""
    opened = open_distillation(tmp_path, source)

    finish_distillation(tmp_path, opened.id, _skipped("预算不够,这次跳过"))

    record = GenomeTaskStore(tmp_path).get(opened.id)
    assert record.state is not GenomeTaskState.SUBMITTED
    assert record.is_settled, "跳过是正常结果,不该变成需要人看的异常"
    assert "预算" in (record.failure_reason or "")


def test_a_record_that_cannot_be_pushed_leaves_a_trail(tmp_path: Path, source: str) -> None:
    """崩在建档与收尾之间的话，记录会停在扫描中——不留痕的话那条僵尸记录没有任何线索。"""
    opened = open_distillation(tmp_path, source)
    store = GenomeTaskStore(tmp_path)
    store.save(opened.evolve(state=GenomeTaskState.SUBMITTED))

    finish_distillation(tmp_path, opened.id, _ok())

    notes = [
        event
        for event in EventLog(tmp_path).events(opened.id)
        if "推不动" in str(event.payload.get("note", ""))
    ]
    assert notes


def test_a_broken_store_does_not_break_the_finished_task(tmp_path: Path, source: str) -> None:
    """建档比管道更靠前。它要是能抛，「失败绝不影响已经完成的任务」从第一行起就不成立。"""
    (tmp_path / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tasks" / "agentgenome.db").write_text("不是一个数据库", encoding="utf-8")

    assert open_distillation(tmp_path, source) is None
