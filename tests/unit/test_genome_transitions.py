"""基因组任务的迁移表。

**不与研发任务共用枚举。** 它没有开发、没有单元测试、没有风险评级、没有分支——共用一套的话,
读状态机的人会看到一堆对某一类永远不成立的迁移,而「迁移即文档」这条原则当场失效。

这份测试走全路径:每一条边至少一次,每一条守卫的两侧各一次。
"""

from __future__ import annotations

import pytest

from agentgenome.core.genome_task import (
    GenomeTask,
    GenomeTaskKind,
    GenomeTaskState,
    Origin,
)
from agentgenome.core.genome_transitions import (
    GenomeEffect,
    GenomeEvent,
    GenomeFacts,
    decide,
)


def _task(
    state: GenomeTaskState = GenomeTaskState.SCANNING,
    kind: GenomeTaskKind = GenomeTaskKind.INIT,
    origin: Origin = Origin.HUMAN,
    **extra: object,
) -> GenomeTask:
    return GenomeTask(id="gn-1", title="t", kind=kind, origin=origin, state=state, **extra)


# --- 初始化的主路径 -----------------------------------------------------------


def test_scanning_produces_a_draft_and_waits_for_a_human() -> None:
    decision = decide(_task(), GenomeEvent.DRAFT_READY, GenomeFacts())

    assert decision.allowed
    assert decision.to is GenomeTaskState.AWAITING_CONFIRMATION
    assert GenomeEffect.NOTIFY_CONFIRMER in decision.effects


def test_a_confirmed_draft_moves_on_to_the_deep_read() -> None:
    decision = decide(
        _task(GenomeTaskState.AWAITING_CONFIRMATION), GenomeEvent.CONFIRMED, GenomeFacts()
    )

    assert decision.to is GenomeTaskState.DEEP_READ


def test_a_confirmed_verification_candidate_finishes_without_a_deep_read() -> None:
    decision = decide(
        _task(
            GenomeTaskState.AWAITING_CONFIRMATION,
            kind=GenomeTaskKind.VERIFICATION,
        ),
        GenomeEvent.VERIFICATION_CONFIRMED,
        GenomeFacts(),
    )

    assert decision.allowed
    assert decision.to is GenomeTaskState.SUBMITTED


def test_the_deep_read_hands_over_to_the_summary() -> None:
    decision = decide(_task(GenomeTaskState.DEEP_READ), GenomeEvent.READ_DONE, GenomeFacts())

    assert decision.to is GenomeTaskState.SUMMARISING


def test_the_summary_submits_a_knowledge_change() -> None:
    decision = decide(_task(GenomeTaskState.SUMMARISING), GenomeEvent.SUBMITTED, GenomeFacts())

    assert decision.to is GenomeTaskState.SUBMITTED


def test_a_distillation_skips_the_gate_entirely() -> None:
    """蒸馏没有什么要问人的。给它一个闸门等于让一条系统自发的管道等人。"""
    decision = decide(
        _task(kind=GenomeTaskKind.DISTILL, origin=Origin.SYSTEM),
        GenomeEvent.READ_DONE,
        GenomeFacts(),
    )

    assert decision.allowed
    assert decision.to is GenomeTaskState.SUMMARISING


# --- 待确认:计划内的闸门 -----------------------------------------------------


def test_waiting_for_a_human_is_not_a_failure() -> None:
    """任务是健康的。人做的是确认或调整,不是诊断。

    所以它带的是"提醒该回答的人",不是"通知发起人出事了",而且没有失败原因。
    """
    decision = decide(_task(), GenomeEvent.DRAFT_READY, GenomeFacts())

    assert decision.effects == (GenomeEffect.NOTIFY_CONFIRMER,)
    assert not decision.reason


def test_a_reminder_does_not_move_the_task() -> None:
    """一个等人的健康任务不该因为人休假而失败。"""
    waiting = _task(GenomeTaskState.AWAITING_CONFIRMATION)

    decision = decide(waiting, GenomeEvent.REMINDED, GenomeFacts())

    assert decision.allowed
    assert decision.to is GenomeTaskState.AWAITING_CONFIRMATION
    assert GenomeEffect.NOTIFY_CONFIRMER in decision.effects


def test_an_answer_that_does_not_parse_leaves_the_task_waiting() -> None:
    decision = decide(
        _task(GenomeTaskState.AWAITING_CONFIRMATION),
        GenomeEvent.CONFIRMED,
        GenomeFacts(answer_valid=False),
    )

    assert not decision.allowed
    assert "答复" in decision.reason


# --- 失败与取消 ---------------------------------------------------------------


def test_any_stage_can_fail() -> None:
    for state in (
        GenomeTaskState.SCANNING,
        GenomeTaskState.AWAITING_CONFIRMATION,
        GenomeTaskState.DEEP_READ,
        GenomeTaskState.SUMMARISING,
    ):
        decision = decide(_task(state), GenomeEvent.FAILED, GenomeFacts())
        assert decision.to is GenomeTaskState.FAILED, state


def test_a_human_started_failure_notifies() -> None:
    """有人在等结果。"""
    decision = decide(_task(origin=Origin.HUMAN), GenomeEvent.FAILED, GenomeFacts())

    assert GenomeEffect.NOTIFY_OWNER in decision.effects


def test_a_system_started_failure_stays_quiet() -> None:
    """一次蒸馏失败不该变成一条需要处理的告警。"""
    decision = decide(
        _task(kind=GenomeTaskKind.DISTILL, origin=Origin.SYSTEM),
        GenomeEvent.FAILED,
        GenomeFacts(),
    )

    assert GenomeEffect.NOTIFY_OWNER not in decision.effects


def test_any_stage_can_be_cancelled() -> None:
    for state in (GenomeTaskState.SCANNING, GenomeTaskState.AWAITING_CONFIRMATION):
        decision = decide(_task(state), GenomeEvent.CANCEL, GenomeFacts())
        assert decision.to is GenomeTaskState.CANCELLED, state


def test_running_out_of_budget_fails_the_task_with_a_reason() -> None:
    over = _task(budget_tokens=100, tokens_used=100)

    decision = decide(over, GenomeEvent.READ_DONE, GenomeFacts())

    assert decision.to is GenomeTaskState.FAILED
    assert "预算" in decision.reason


def test_disabled_budget_gate_keeps_counting_without_failing_the_task() -> None:
    over = _task(budget_tokens=100, tokens_used=100)

    decision = decide(over, GenomeEvent.READ_DONE, GenomeFacts(enforce_budget=False))

    assert decision.to is GenomeTaskState.SUMMARISING


def test_cancelling_beats_an_exhausted_budget() -> None:
    """预算用光了人更想取消它，而不是被告知「已经失败所以取消不了」。"""
    over = _task(budget_tokens=100, tokens_used=100)

    assert decide(over, GenomeEvent.CANCEL, GenomeFacts()).to is GenomeTaskState.CANCELLED


# --- 表的边界 ----------------------------------------------------------------


def test_a_terminal_task_refuses_everything() -> None:
    decision = decide(_task(GenomeTaskState.SUBMITTED), GenomeEvent.CANCEL, GenomeFacts())

    assert not decision.allowed
    assert "终态" in decision.reason


def test_an_edge_that_is_not_in_the_table_is_refused_by_name() -> None:
    """被拒时要说清楚是哪条边不存在——「非法迁移」四个字帮不上任何忙。"""
    decision = decide(_task(GenomeTaskState.SCANNING), GenomeEvent.CONFIRMED, GenomeFacts())

    assert not decision.allowed
    assert "SCANNING" in decision.reason
    assert "confirmed" in decision.reason


@pytest.mark.parametrize("state", list(GenomeTaskState))
def test_every_state_is_reachable_or_terminal(state: GenomeTaskState) -> None:
    """孤岛状态是最难发现的一类 bug:它在枚举里、在文档里,但任务永远到不了。"""
    from agentgenome.core.genome_transitions import TRANSITIONS

    reachable = {row.to for row in TRANSITIONS}
    assert state is GenomeTaskState.SCANNING or state in reachable


# --- 评审抓到的:预算不该压过"原地不动"与"停下来" ------------------------------


def test_a_reminder_does_not_kill_an_over_budget_waiting_task() -> None:
    """**这是这一片要防的事情本身。** 一个在等人的任务没有在烧 token;让一条提醒把它判死,
    等于"等人的任务因为人休假而失败"。"""
    over = _task(GenomeTaskState.AWAITING_CONFIRMATION, budget_tokens=100, tokens_used=100)

    decision = decide(over, GenomeEvent.REMINDED, GenomeFacts())

    assert decision.to is GenomeTaskState.AWAITING_CONFIRMATION
    assert not decision.reason


def test_an_over_budget_task_that_fails_keeps_its_real_reason() -> None:
    """再判一次预算只会把真正的失败原因换成「预算耗尽」。"""
    over = _task(budget_tokens=100, tokens_used=100)

    decision = decide(over, GenomeEvent.FAILED, GenomeFacts(stop_reason="归档盘写不进去"))

    assert decision.to is GenomeTaskState.FAILED
    assert decision.reason == "归档盘写不进去"


def test_a_failure_carries_the_reason_it_was_given() -> None:
    """「失败」两个字不是原因——它指不出下一步该从哪查。"""
    decision = decide(_task(), GenomeEvent.FAILED, GenomeFacts(stop_reason="第三个模块深读超时"))

    assert decision.reason == "第三个模块深读超时"


def test_the_cancel_row_is_the_one_that_actually_fires() -> None:
    """表是文档。取消要是被 `decide` 里的一条特判提前接走，那行就成了死代码——
    给它加守卫或副作用会被静默忽略。"""
    from agentgenome.core.genome_transitions import ANY_STATE, TRANSITIONS

    row = next(
        row
        for row in TRANSITIONS
        if row.event is GenomeEvent.CANCEL and row.from_state is ANY_STATE
    )
    assert row.to is GenomeTaskState.CANCELLED
    assert decide(_task(), GenomeEvent.CANCEL, GenomeFacts()).effects == row.effects
