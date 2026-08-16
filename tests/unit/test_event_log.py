"""EventLog:审计与回放的最小事实集。

"这个任务为什么修了四轮?第三轮是哪个员工用哪一版能力干的?"——如果这些信息只存在于
滚动的终端输出里,出了事什么都查不到。所以这一层的测试盯的是**两份记录对不对得上**。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.task import Task, TaskStore
from agentgenome.core.transitions import Decision, Effect

ACTOR = "orchestrator"


@pytest.fixture
def log(tmp_path: Path) -> EventLog:
    TaskStore(tmp_path)  # 建库
    return EventLog(tmp_path)


def _task(**kwargs: object) -> Task:
    return Task(id="ag-1", title="t", requirement="r", **kwargs)  # type: ignore[arg-type]


def _append(log: EventLog, kind: LogKind = LogKind.NOTE, **payload: object) -> None:
    log.append("ag-1", actor=ACTOR, kind=kind, payload=payload)


# --- 双写 -------------------------------------------------------------------


def test_an_event_lands_in_both_the_database_and_the_jsonl(log: EventLog) -> None:
    """两个消费者需求不同:库用来查询,JSONL 给前端 tail。"""
    _append(log, note="开工")

    from_db = log.events("ag-1")
    from_file = [json.loads(line) for line in log.jsonl_path("ag-1").read_text().splitlines()]

    assert len(from_db) == 1
    assert len(from_file) == 1


def test_the_two_records_agree_field_by_field(log: EventLog) -> None:
    """写一份再导出另一份的话,导出那一步迟早会漏或会滞后。"""
    _append(log, LogKind.JOB_FINISHED, tokens=42, procedure_ref="code-develop@1.0.0")
    _append(log, LogKind.TRANSITION, to="DEVELOPING")

    from_db = [event.as_dict() for event in log.events("ag-1")]
    from_file = [json.loads(line) for line in log.jsonl_path("ag-1").read_text().splitlines()]

    assert from_db == from_file


def test_events_keep_their_order_even_within_the_same_instant(log: EventLog) -> None:
    """按时间戳排序的话,同一毫秒写入的几条会乱序,而事件流乱序等于历史不可回放。"""
    stamp = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    for index in range(20):
        log.append("ag-1", actor=ACTOR, kind=LogKind.NOTE, payload={"n": index}, now=stamp)

    assert [event.payload["n"] for event in log.events("ag-1")] == list(range(20))


def test_the_jsonl_is_flushed_line_by_line(log: EventLog) -> None:
    """任务跑到一半我要能 tail 它,不是等结束才落盘。"""
    _append(log, note="第一条")

    assert log.jsonl_path("ag-1").read_text().strip()


def test_events_of_other_tasks_are_not_mixed_in(log: EventLog) -> None:
    _append(log, note="给 ag-1 的")
    log.append("ag-2", actor=ACTOR, kind=LogKind.NOTE, payload={})

    assert len(log.events("ag-1")) == 1
    assert len(log.events("ag-2")) == 1


def test_a_task_without_events_reads_back_empty(log: EventLog) -> None:
    assert log.events("ag-never") == ()


# --- 状态迁移事件 -----------------------------------------------------------


def test_a_transition_records_enough_to_replay_it(log: EventLog) -> None:
    """ "这个任务为什么走到这一步"要能只看事件流回答出来。"""
    task = _task(state=TaskState.UNIT_TESTING, fix_rounds=1)
    decision = Decision(
        True, to=TaskState.DEVELOPING, effects=(Effect.DISPATCH_DEV,), fix_rounds_delta=1
    )

    log.transition(task, TaskEvent.GATE_FAIL, decision)

    payload = log.events("ag-1")[0].payload
    assert payload["from"] == "UNIT_TESTING"
    assert payload["to"] == "DEVELOPING"
    assert payload["event"] == "gate_fail"
    assert payload["effects"] == ["dispatch_dev"]
    assert payload["fix_rounds_delta"] == 1


def test_a_refused_transition_is_recorded_too(log: EventLog) -> None:
    """非法迁移必须留痕——不记的话"它怎么没动"这个问题查无可查。"""
    task = _task(state=TaskState.CREATED)

    log.transition(task, TaskEvent.MERGED, Decision(False, reason="表里没有这条边"))

    event = log.events("ag-1")[0]
    assert event.kind is LogKind.TRANSITION_REFUSED
    assert "表里没有这条边" in event.payload["reason"]


# --- Job 事件 ---------------------------------------------------------------


def test_a_finished_job_records_cost_and_responsibility(log: EventLog) -> None:
    log.job_finished(
        "ag-1",
        employee_id="dev-employee",
        procedure_ref="code-develop@1.0.0",
        ok=True,
        tokens_used=1234,
        tokens_available=True,
        duration_s=12.5,
    )

    event = log.events("ag-1")[0]
    assert event.actor == "dev-employee"
    assert event.payload["procedure_ref"] == "code-develop@1.0.0"
    assert event.payload["tokens_used"] == 1234
    assert event.payload["duration_s"] == 12.5


def test_unavailable_token_usage_is_marked_not_zeroed(log: EventLog) -> None:
    """填 0 会让成本看板悄悄少算,比缺数据更糟(沿用 PRD 02 的约定)。"""
    log.job_finished(
        "ag-1",
        employee_id="dev-employee",
        procedure_ref="x@1.0.0",
        ok=True,
        tokens_used=0,
        tokens_available=False,
        duration_s=1.0,
    )

    payload = log.events("ag-1")[0].payload
    assert payload["tokens_used"] is None
    assert payload["tokens_available"] is False


def test_total_tokens_can_be_summed_from_the_log(log: EventLog) -> None:
    """ "这个任务总共烧了多少"必须能从事件流算出来,而不是只信任务表上的那个数。"""
    for tokens in (100, 200, 300):
        log.job_finished(
            "ag-1",
            employee_id="dev",
            procedure_ref="x@1.0.0",
            ok=True,
            tokens_used=tokens,
            tokens_available=True,
            duration_s=1.0,
        )

    assert log.tokens_used("ag-1") == 600


def test_unavailable_usage_does_not_count_as_zero_in_the_total(log: EventLog) -> None:
    log.job_finished(
        "ag-1",
        employee_id="dev",
        procedure_ref="x@1.0.0",
        ok=True,
        tokens_used=0,
        tokens_available=False,
        duration_s=1.0,
    )

    assert log.tokens_used("ag-1") == 0
