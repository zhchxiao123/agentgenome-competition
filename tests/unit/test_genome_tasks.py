"""基因组任务:与研发任务并列的第二类任务。

系统做的事分两类,但只有一类有身份。研发任务有 id、状态、事件流、看板卡片;而知识初始化、
经验蒸馏、按模块重建什么都没有——成功时留一个结果,失败时什么都不留,进行中时人完全看不见。

这一份只管**结构**:一条能被创建、查询、写事件的记录。状态集、闸门、并发都在后面。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentgenome.core.events import ORCHESTRATOR, EventLog, LogKind
from agentgenome.core.genome_task import (
    GenomeTaskKind,
    GenomeTaskState,
    GenomeTaskStore,
    Origin,
)
from agentgenome.core.task import TaskNotFound, TaskStore


@pytest.fixture
def store(tmp_path: Path) -> GenomeTaskStore:
    return GenomeTaskStore(tmp_path)


def test_a_genome_task_can_be_created_and_read_back(store: GenomeTaskStore) -> None:
    created = store.create(
        title="知识初始化",
        kind=GenomeTaskKind.INIT,
        origin=Origin.HUMAN,
    )

    assert store.get(created.id).kind is GenomeTaskKind.INIT
    assert store.get(created.id).origin is Origin.HUMAN


def test_it_records_which_module_it_acts_on(store: GenomeTaskStore) -> None:
    created = store.create(
        title="重建 order-service",
        kind=GenomeTaskKind.REINIT,
        origin=Origin.HUMAN,
        subject="order-service",
    )

    assert store.get(created.id).subject == "order-service"


def test_it_records_which_dev_task_triggered_it(store: GenomeTaskStore) -> None:
    """「这条认知是从哪次经验来的」要能被回答。"""
    created = store.create(
        title="蒸馏",
        kind=GenomeTaskKind.DISTILL,
        origin=Origin.SYSTEM,
        source_task_id="ag-20260901-001",
    )

    assert store.get(created.id).source_task_id == "ag-20260901-001"


def test_it_does_not_carry_dev_task_fields(store: GenomeTaskStore) -> None:
    """一个永远为空的字段,读的人无从判断是「还没填」还是「不适用」。"""
    created = store.create(title="x", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)

    for absent in ("branch", "fix_rounds", "plan_retries", "needs_itest", "risk_level"):
        assert not hasattr(created, absent), f"基因组任务不该有 {absent}"


def test_an_unknown_genome_task_says_so(store: GenomeTaskStore) -> None:
    with pytest.raises(TaskNotFound):
        store.get("gn-nope")


# --- id 空间 -----------------------------------------------------------------


def test_the_id_prefix_tells_the_two_kinds_apart(store: GenomeTaskStore, tmp_path: Path) -> None:
    genome = store.create(title="x", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    dev = TaskStore(tmp_path).create(title="y", requirement="z")

    assert genome.id.startswith("gn-")
    assert dev.id.startswith("ag-")


def test_the_two_kinds_never_collide(store: GenomeTaskStore, tmp_path: Path) -> None:
    """事件表以任务 id 为键,两类必须能进同一条流——撞了的话一条流会串成两个任务的历史。"""
    dev_store = TaskStore(tmp_path)
    genome_ids = {
        store.create(title=f"g{i}", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN).id
        for i in range(5)
    }
    dev_ids = {dev_store.create(title=f"d{i}", requirement="x").id for i in range(5)}

    assert len(genome_ids) == 5
    assert not genome_ids & dev_ids


def test_a_genome_task_id_does_not_resolve_in_the_dev_task_store(
    store: GenomeTaskStore, tmp_path: Path
) -> None:
    """两类分表存:一类的 schema 变更不波及另一类。"""
    created = store.create(title="x", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)

    with pytest.raises(TaskNotFound):
        TaskStore(tmp_path).get(created.id)


# --- 事件 --------------------------------------------------------------------


def test_both_kinds_write_into_the_same_event_stream(
    store: GenomeTaskStore, tmp_path: Path
) -> None:
    """一次检索覆盖系统的全部行为。"""
    genome = store.create(title="x", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    dev = TaskStore(tmp_path).create(title="y", requirement="z")
    log = EventLog(tmp_path)
    log.append(genome.id, actor=ORCHESTRATOR, kind=LogKind.NOTE, payload={"note": "g"})
    log.append(dev.id, actor=ORCHESTRATOR, kind=LogKind.NOTE, payload={"note": "d"})

    assert [event.payload["note"] for event in log.events(genome.id)] == ["g"]
    assert [event.payload["note"] for event in log.events(dev.id)] == ["d"]


# --- 状态与恢复 ---------------------------------------------------------------


def test_a_new_genome_task_starts_in_the_first_state(store: GenomeTaskStore) -> None:
    created = store.create(title="x", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)

    assert created.state is GenomeTaskState.SCANNING


def test_saving_replaces_the_record_without_mutating_the_old_value(
    store: GenomeTaskStore,
) -> None:
    """就地改字段的话，"这个对象现在是库里的样子还是改了一半的样子"没人说得清。"""
    created = store.create(title="x", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)

    store.save(created.evolve(state=GenomeTaskState.DEEP_READ))

    assert created.state is GenomeTaskState.SCANNING
    assert store.get(created.id).state is GenomeTaskState.DEEP_READ


def test_open_genome_tasks_survive_a_restart(store: GenomeTaskStore, tmp_path: Path) -> None:
    """状态即事实:崩溃恢复要能扫到非终态的基因组任务。"""
    running = store.create(title="x", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    done = store.create(title="y", kind=GenomeTaskKind.DISTILL, origin=Origin.SYSTEM)
    store.save(done.evolve(state=GenomeTaskState.SUBMITTED))

    reopened = GenomeTaskStore(tmp_path)

    assert [item.id for item in reopened.open_tasks()] == [running.id]


def test_they_can_be_filtered_by_kind_and_subject(store: GenomeTaskStore) -> None:
    store.create(title="a", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    store.create(
        title="b", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="order-service"
    )

    assert [item.title for item in store.all_tasks(kind=GenomeTaskKind.REINIT)] == ["b"]
    assert [item.title for item in store.all_tasks(subject="order-service")] == ["b"]


# --- 「已了结」的判定收敛在一处 ------------------------------------------------


def test_a_human_started_failure_still_needs_attention(store: GenomeTaskStore) -> None:
    """有人敲了命令正在等结果。它是终态，但不是已了结。"""
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)

    failed = store.save(
        created.evolve(state=GenomeTaskState.FAILED, failure_reason="第三个模块深读超时")
    )

    assert failed.is_terminal
    assert not failed.is_settled
    assert [item.id for item in store.unsettled_tasks()] == [failed.id]


def test_a_resolved_human_failure_leaves_the_attention_queue(
    store: GenomeTaskStore,
) -> None:
    """人工确认处置完毕后保留 FAILED 事实，只结束这张人工待办。"""
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    resolved = store.save(
        created.evolve(
            state=GenomeTaskState.FAILED,
            failure_reason="深读超时",
            intervention_resolved_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        )
    )

    assert resolved.is_settled
    assert store.unsettled_tasks() == ()
    assert store.needs_attention() == ()


def test_a_system_started_failure_is_settled(store: GenomeTaskStore) -> None:
    """一次蒸馏失败不该变成一条需要处理的告警。"""
    created = store.create(title="蒸馏", kind=GenomeTaskKind.DISTILL, origin=Origin.SYSTEM)

    failed = store.save(created.evolve(state=GenomeTaskState.FAILED))

    assert failed.is_terminal
    assert failed.is_settled
    assert store.unsettled_tasks() == ()


def test_awaiting_confirmation_is_not_terminal(store: GenomeTaskStore) -> None:
    """它等的是人，不是终结。人一答复机器自己往下走。"""
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)

    waiting = store.save(created.evolve(state=GenomeTaskState.AWAITING_CONFIRMATION))

    assert not waiting.is_terminal
    assert not waiting.is_settled
    assert [item.id for item in store.open_tasks()] == [waiting.id]


def test_a_completed_genome_task_is_settled(store: GenomeTaskStore) -> None:
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)

    done = store.save(created.evolve(state=GenomeTaskState.SUBMITTED))

    assert done.is_settled
    assert store.unsettled_tasks() == ()


def test_the_snapshot_is_written_for_human_eyes(store: GenomeTaskStore, tmp_path: Path) -> None:
    """数据库损坏时至少还能打开任务目录看看这个任务是谁、卡在哪。"""
    import json

    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)

    snapshot = json.loads((tmp_path / "tasks" / created.id / "task.json").read_text())

    assert snapshot["kind"] == "init"
    assert snapshot["origin"] == "human"


# --- 待办队列与异常队列是两个 ------------------------------------------------


def test_waiting_tasks_go_to_the_todo_queue_not_the_exception_queue(
    store: GenomeTaskStore,
) -> None:
    """**这是这一片最容易实现错的地方。** 合成一个「未了结」的话，一批健康的、只是在等人
    点一下的任务会混进异常列表，把真正出事的那个淹没。"""
    waiting = store.save(
        store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN).evolve(
            state=GenomeTaskState.AWAITING_CONFIRMATION
        )
    )
    broken = store.save(
        store.create(title="reinit", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN).evolve(
            state=GenomeTaskState.FAILED, failure_reason="深读超时"
        )
    )

    assert [item.id for item in store.awaiting_confirmation()] == [waiting.id]
    assert [item.id for item in store.needs_attention()] == [broken.id]


def test_a_system_failure_is_in_neither_queue(store: GenomeTaskStore) -> None:
    store.save(
        store.create(title="蒸馏", kind=GenomeTaskKind.DISTILL, origin=Origin.SYSTEM).evolve(
            state=GenomeTaskState.FAILED
        )
    )

    assert store.awaiting_confirmation() == ()
    assert store.needs_attention() == ()


# --- 超时提醒 ----------------------------------------------------------------


def test_a_gate_that_has_waited_too_long_is_reported(store: GenomeTaskStore) -> None:
    from datetime import UTC, datetime, timedelta

    from agentgenome.core.genome_task import overdue_confirmations

    now = datetime(2026, 8, 9, 12, tzinfo=UTC)
    stale = store.save(
        store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN).evolve(
            state=GenomeTaskState.AWAITING_CONFIRMATION
        ),
        now=now - timedelta(days=3),
    )
    fresh = store.save(
        store.create(title="init2", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN).evolve(
            state=GenomeTaskState.AWAITING_CONFIRMATION
        ),
        now=now,
    )

    overdue = overdue_confirmations([stale, fresh], cutoff=now - timedelta(days=1))

    assert [item.id for item in overdue] == [stale.id]


def test_only_waiting_tasks_can_be_overdue(store: GenomeTaskStore) -> None:
    """跑着的任务不是「等太久了」，它是「还在跑」。"""
    from datetime import UTC, datetime, timedelta

    from agentgenome.core.genome_task import overdue_confirmations

    now = datetime(2026, 8, 9, 12, tzinfo=UTC)
    running = store.save(
        store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN).evolve(
            state=GenomeTaskState.DEEP_READ
        ),
        now=now - timedelta(days=30),
    )

    assert overdue_confirmations([running], cutoff=now - timedelta(days=1)) == ()


# --- 推进器：拒绝也要留痕 -----------------------------------------------------


def test_a_refused_transition_is_recorded(store: GenomeTaskStore, tmp_path: Path) -> None:
    """**不记的话「它怎么没动」查无可查。** 基因组任务最容易卡住的地方恰恰是闸门：一份
    不合契约的答复会让任务原地不动，人看到的是「我明明回答了，它却没往下走」。"""
    from agentgenome.core.genome_driver import GenomeDriver
    from agentgenome.core.genome_transitions import GenomeEvent, GenomeFacts

    driver = GenomeDriver(store, EventLog(tmp_path))
    waiting = store.save(
        store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN).evolve(
            state=GenomeTaskState.AWAITING_CONFIRMATION
        )
    )

    applied = driver.deliver(waiting.id, GenomeEvent.CONFIRMED, GenomeFacts(answer_valid=False))

    assert not applied.moved
    assert store.get(waiting.id).state is GenomeTaskState.AWAITING_CONFIRMATION
    refusals = [
        event
        for event in EventLog(tmp_path).events(waiting.id)
        if event.kind is LogKind.TRANSITION_REFUSED
    ]
    assert refusals and "答复" in refusals[0].payload["reason"]


def test_an_allowed_transition_lands_before_it_is_logged(
    store: GenomeTaskStore, tmp_path: Path
) -> None:
    from agentgenome.core.genome_driver import GenomeDriver
    from agentgenome.core.genome_transitions import GenomeEvent

    driver = GenomeDriver(store, EventLog(tmp_path))
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)

    applied = driver.deliver(created.id, GenomeEvent.DRAFT_READY)

    assert applied.moved
    assert store.get(created.id).state is GenomeTaskState.AWAITING_CONFIRMATION
    kinds = [event.kind for event in EventLog(tmp_path).events(created.id)]
    assert LogKind.TRANSITION in kinds


def test_a_failure_reason_lands_on_the_task(store: GenomeTaskStore, tmp_path: Path) -> None:
    """「失败」两个字指不出任何动作。这条记录存在的全部理由，是告诉接手的人从哪儿开始。"""
    from agentgenome.core.genome_driver import GenomeDriver
    from agentgenome.core.genome_transitions import GenomeEvent, GenomeFacts

    driver = GenomeDriver(store, EventLog(tmp_path))
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)

    driver.deliver(created.id, GenomeEvent.FAILED, GenomeFacts(stop_reason="归档盘写不进去"))

    assert store.get(created.id).failure_reason == "归档盘写不进去"
