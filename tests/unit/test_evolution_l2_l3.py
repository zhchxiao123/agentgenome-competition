"""L2 规则提案、L3 工序进化与效果度量。

这一层最该钉死的两条:**规则永远不自动合并**,**没有回归证据的工序改动不许合并**。
两条都是"人必须在环里"的具体化。
"""

from __future__ import annotations

import pytest

from agentgenome.genome.evolution.cards import Applicability, Evidence, LessonCard, Level
from agentgenome.genome.evolution.proposals import (
    Backtest,
    ProcedureProposal,
    RegressionMissing,
    RegressionResult,
    RuleProposal,
    backtest,
    failure_pattern,
    propose_rules,
)
from agentgenome.genome.evolution.trends import MIN_SAMPLES, Direction, Metric, weekly

HISTORY = {
    "ag-1": ["repos/order-service/migrations/001.sql", "repos/order-service/src/a.py"],
    "ag-2": ["repos/order-service/src/b.py"],
    "ag-3": ["repos/order-service/migrations/002.sql"],
}


def _card(index: int, globs: list[str], level: Level = Level.L2) -> LessonCard:
    return LessonCard(
        id=f"L-{index:04d}",
        title=f"迁移脚本的坑 {index}",
        applies_to=Applicability(path_globs=globs),
        conclusion="迁移脚本要带回滚语句。",
        evidence=[Evidence(task_id="ag-1", path="artifacts/x.json")],
        level=level,
    )


# --- 回溯分析 ---------------------------------------------------------------


def test_backtest_really_runs_over_history() -> None:
    """**真的跑一遍,不是估算。**

    一个估出来的「预计拦截 5 个」没有审批价值——审批人无法判断那 5 个里有几个是误伤。
    """
    result = backtest(["repos/order-service/migrations/**"], HISTORY)

    assert result.would_have_flagged == ("ag-1", "ag-3")
    assert result.total_tasks == 3


def test_backtest_with_no_history_says_it_has_no_basis() -> None:
    text = backtest(["repos/order-service/**"], {}).render()

    assert "缺少判断依据" in text


def test_a_rule_that_hits_nothing_is_still_reported() -> None:
    """命中零个也是结论——它说明这条规则可能没必要加。"""
    result = backtest(["deploy/**"], HISTORY)

    assert result.would_have_flagged == ()
    assert "一个都没有" in result.render()


# --- 规则提案 ---------------------------------------------------------------


def test_repeated_lessons_become_a_rule_proposal() -> None:
    cards = [_card(index, ["repos/order-service/migrations/**"]) for index in range(1, 4)]

    proposals = propose_rules(cards, HISTORY)

    assert len(proposals) == 1
    assert len(proposals[0].supported_by) == 3


def test_too_few_lessons_do_not_become_a_rule() -> None:
    """一两次踩坑是偶然。把偶然提炼成规则,规则层很快就没人信了。"""
    assert propose_rules([_card(1, ["repos/order-service/migrations/**"])], HISTORY) == []


def test_only_l2_cards_are_considered() -> None:
    """L1 已经自动入库了,再提炼成规则等于把同一件事说两遍。"""
    cards = [
        _card(index, ["repos/order-service/migrations/**"], level=Level.L1) for index in range(1, 5)
    ]

    assert propose_rules(cards, HISTORY) == []


def test_a_proposal_carries_all_three_materials() -> None:
    """支撑卡片、回溯分析、预期效果——少一样审批人就得自己去翻。"""
    proposals = propose_rules(
        [_card(i, ["repos/order-service/migrations/**"]) for i in range(1, 4)], HISTORY
    )

    text = proposals[0].render()
    assert "支撑它的经验卡片" in text
    assert "回溯分析" in text
    assert "预期效果" in text


def test_a_proposal_says_it_will_not_auto_merge() -> None:
    """**规则层是唯一能大范围改变行为的杠杆。** 这句话要出现在 PR 里,不只在代码注释里。"""
    proposal = RuleProposal(
        rule_id="x",
        description="",
        path_globs=("a/**",),
        supported_by=("L-0001",),
        backtest=Backtest(),
    )

    assert "不会自动合并" in proposal.render()


# --- 工序进化 ---------------------------------------------------------------


def test_a_procedure_change_without_regression_evidence_cannot_merge() -> None:
    """**这是 L3 与 L2 的关键区别。**

    规则的影响可以靠人读懂,提示词的影响读不出来,只能测出来。
    """
    proposal = ProcedureProposal(
        procedure_id="code-develop", reason="老是忘了跑测试", diff="+ 记得跑测试"
    )

    with pytest.raises(RegressionMissing) as error:
        proposal.ensure_mergeable()

    assert "只能测出来" in str(error.value)


def test_zero_samples_counts_as_no_evidence() -> None:
    proposal = ProcedureProposal("s", "r", "d", regression=RegressionResult(samples=0))

    with pytest.raises(RegressionMissing):
        proposal.ensure_mergeable()


def test_a_regression_that_got_worse_is_refused() -> None:
    proposal = ProcedureProposal(
        "s", "r", "d", regression=RegressionResult(samples=10, before_pass=8, after_pass=5)
    )

    with pytest.raises(RegressionMissing) as error:
        proposal.ensure_mergeable()

    assert "变差" in str(error.value)


def test_an_improved_procedure_can_merge() -> None:
    proposal = ProcedureProposal(
        "s", "r", "d", regression=RegressionResult(samples=10, before_pass=5, after_pass=8)
    )

    proposal.ensure_mergeable()


def test_the_proposal_says_the_regression_needs_a_real_agent() -> None:
    proposal = ProcedureProposal("s", "r", "d", regression=RegressionResult(samples=10))

    assert "不进常规 CI" in proposal.render()


# --- 失败模式识别 -----------------------------------------------------------


class _Event:
    def __init__(self, payload: dict) -> None:
        self.payload = payload


def test_a_repeated_failure_is_a_pattern() -> None:
    """偶发失败没有进化价值。反复以完全相同的方式失败,才说明是能力本身的问题。"""
    events = [
        _Event({"ok": False, "procedure_ref": "code-develop@1.0.0", "failure_detail": "忘了写测试"})
        for _ in range(3)
    ]

    assert failure_pattern(events, "code-develop") == ("忘了写测试",)


def test_a_one_off_failure_is_not_a_pattern() -> None:
    events = [
        _Event(
            {"ok": False, "procedure_ref": "code-develop@1.0.0", "failure_detail": f"第 {i} 种错"}
        )
        for i in range(3)
    ]

    assert failure_pattern(events, "code-develop") == ()


def test_successful_jobs_are_not_patterns() -> None:
    events = [_Event({"ok": True, "procedure_ref": "code-develop@1.0.0"}) for _ in range(5)]

    assert failure_pattern(events, "code-develop") == ()


# --- 效果度量 ---------------------------------------------------------------


def test_not_enough_samples_gives_no_trend() -> None:
    """**三个任务画出来的「下降趋势」是噪声。**

    一旦它被贴进汇报,后面所有的判断都建立在它上面。
    """
    metric = Metric("平均修复轮次", Direction.DOWN, 2.0, 1.0, samples=3)

    assert metric.improving is None
    assert "数据不足" in metric.render()


def test_no_data_is_not_the_same_as_getting_worse() -> None:
    """返回 False 的话,「没数据」会被读成「在变差」——那会触发本不该有的行动。"""
    assert Metric("x", Direction.DOWN, 2.0, 3.0, samples=2).improving is None


def test_a_metric_knows_which_way_is_better() -> None:
    """方向写死在定义里。一个不知道自己该往哪走的指标,看多久都得不出结论。"""
    down = Metric("修复轮次", Direction.DOWN, 2.0, 1.4, samples=MIN_SAMPLES)
    up = Metric("一次通过率", Direction.UP, 0.6, 0.76, samples=MIN_SAMPLES)

    assert down.improving is True
    assert up.improving is True


def test_a_metric_moving_the_wrong_way_is_flagged() -> None:
    metric = Metric("升级人工率", Direction.DOWN, 0.02, 0.09, samples=MIN_SAMPLES)

    assert metric.improving is False
    assert "在变差" in metric.render()


def test_a_report_with_no_data_refuses_to_conclude() -> None:
    """一个诚实说「还看不出来」的报告是有用的,一个画着好看曲线的假报告是有害的。"""
    report = weekly("2026-W32", [2.0] * 5, [1.5] * 5, [2] * 5)

    text = report.render()
    assert not report.has_enough
    assert "不给任何趋势结论" in text
    assert "→" not in text


def test_a_report_with_data_names_what_got_worse_first() -> None:
    """趋势反了通常意味着某类任务在系统性地卡住——那是最该先看的。"""
    report = weekly("2026-W32", [2.0, 0.6, 30, 0.02, 0.5], [1.4, 0.76, 24, 0.09, 0.6], [20] * 5)

    text = report.render()
    assert "升级人工率" in text.split("值得先看")[0]
    assert "在变差" in text
