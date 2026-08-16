"""L2 规则提案与 L3 工序进化。

## 规则层永远握在人手里

**规则是这个系统里唯一能大范围改变行为的杠杆。** 一条错的规则影响之后每一个任务,而且它的
错法是"系统性地做错同一件事"——比一次任务失败贵得多。所以这里没有任何自动合并的路径。

## 回溯分析是提案的硬要求

没有它,审批人只能凭直觉判断一条规则值不值得加,而直觉在这件事上很不可靠。回溯要**真的跑一遍
历史任务的改动集**,不是估算:"这条规则如果早存在,会拦下哪几个任务"是个可以精确回答的问题。

## L3 与 L2 的关键区别

**规则的影响可以靠人读懂,提示词的影响读不出来,只能测出来。** 一句看起来更清楚的提示词,
完全可能让某类任务的成功率掉一半,而没有任何人能靠读它发现这一点。所以工序提案除了人工审批
还必须过回归验证——**没有回归证据的工序改动不允许合并**。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from agentgenome.core.scope import compile_glob, normalize_path
from agentgenome.genome.craft import CRAFT_DIR
from agentgenome.genome.evolution.cards import LessonCard, Level

#: 多少张同类卡片才值得提炼成一条规则。
DEFAULT_THRESHOLD = 3


@dataclass(frozen=True)
class Backtest:
    """这条规则如果早存在,会影响哪些历史任务。

    **真的跑一遍,不是估算。** 一个估出来的"预计拦截 5 个"没有任何审批价值——审批人无法
    判断那 5 个里有几个是误伤。
    """

    would_have_flagged: tuple[str, ...] = ()
    total_tasks: int = 0

    @property
    def hit_ratio(self) -> float:
        return len(self.would_have_flagged) / self.total_tasks if self.total_tasks else 0.0

    def render(self) -> str:
        if not self.total_tasks:
            return "回溯分析:没有历史任务可比对——**这条提案缺少判断依据**。"
        names = ", ".join(self.would_have_flagged) or "(一个都没有)"
        return (
            f"回溯分析:在 {self.total_tasks} 个历史任务里,这条规则会拦下 "
            f"{len(self.would_have_flagged)} 个({self.hit_ratio:.0%}):{names}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "would_have_flagged": list(self.would_have_flagged),
            "total_tasks": self.total_tasks,
            "hit_ratio": round(self.hit_ratio, 4),
        }


@dataclass(frozen=True)
class RuleProposal:
    """一条规则变更提案。**永远走人工审批。**"""

    rule_id: str
    description: str
    path_globs: tuple[str, ...]
    #: 支撑它的卡片。**没有支撑卡片的提案不该存在**——那是凭空造规则。
    supported_by: tuple[str, ...]
    backtest: Backtest
    expected_effect: str = ""

    def render(self) -> str:
        """PR 描述。三样材料一次给全,审批人不必再去翻。"""
        lines = [
            f"## 规则提案:`{self.rule_id}`",
            "",
            self.description or "(没有描述)",
            "",
            "### 支撑它的经验卡片",
            "",
            *[f"- {item}" for item in self.supported_by],
            "",
            "### 回溯分析",
            "",
            self.backtest.render(),
            "",
            "### 预期效果",
            "",
            self.expected_effect or "(没有给出)",
            "",
            "---",
            "**这条提案不会自动合并。** 规则层是唯一能大范围改变行为的杠杆,它必须握在人手里。",
        ]
        return "\n".join(lines) + "\n"

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "description": self.description,
            "path_globs": list(self.path_globs),
            "supported_by": list(self.supported_by),
            "backtest": self.backtest.as_dict(),
            "expected_effect": self.expected_effect,
        }


def backtest(path_globs: Sequence[str], history: Mapping[str, Sequence[str]]) -> Backtest:
    """把这条规则套在历史任务的改动集上跑一遍。

    `history` 是 `任务 id → 那个任务改过的路径`。
    """
    flagged = []
    for task_id, paths in sorted(history.items()):
        normalized = [normalize_path(item) for item in paths]
        if any(compile_glob(glob).match(path) for glob in path_globs for path in normalized):
            flagged.append(task_id)
    return Backtest(would_have_flagged=tuple(flagged), total_tasks=len(history))


def propose_rules(
    cards: Sequence[LessonCard],
    history: Mapping[str, Sequence[str]],
    threshold: int = DEFAULT_THRESHOLD,
) -> list[RuleProposal]:
    """从重复出现的知识模式里提炼规则候选。

    **只看 L2 卡片。** L1 已经自动入库了,把它们再提炼成规则等于把同一件事说两遍;而 L3/L4
    去向完全不同。

    按 `path_globs` 聚类:同一批路径被反复踩,那就是一条规则该管的地方。
    """
    buckets: dict[tuple[str, ...], list[LessonCard]] = {}
    for card in cards:
        if card.level is not Level.L2 or not card.applies_to.path_globs:
            continue
        key = tuple(sorted(card.applies_to.path_globs))
        buckets.setdefault(key, []).append(card)

    found = []
    for globs, group in sorted(buckets.items()):
        if len(group) < threshold:
            continue
        found.append(
            RuleProposal(
                rule_id=_rule_id(globs),
                description=f"这些路径上反复出现同类问题({len(group)} 张卡片)",
                path_globs=globs,
                supported_by=tuple(f"{card.id} {card.title}" for card in group),
                backtest=backtest(globs, history),
                expected_effect="命中这些路径的改动将被强制转人工审批。",
            )
        )
    return found


@dataclass(frozen=True)
class RegressionResult:
    """一次回归验证的结果。"""

    samples: int = 0
    before_pass: int = 0
    after_pass: int = 0

    @property
    def improved(self) -> bool:
        return self.after_pass > self.before_pass

    @property
    def regressed(self) -> bool:
        return self.after_pass < self.before_pass

    def render(self) -> str:
        if not self.samples:
            return "回归验证:**没有样本**。"
        return (
            f"回归验证:{self.samples} 个历史任务重跑,"
            f"改前通过 {self.before_pass}、改后通过 {self.after_pass}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "before_pass": self.before_pass,
            "after_pass": self.after_pass,
            "improved": self.improved,
            "regressed": self.regressed,
        }


class RegressionMissing(RuntimeError):
    """没有回归证据。**工序改动不允许在这种情况下合并。**"""


@dataclass(frozen=True)
class ProcedureProposal:
    """一条工序进化提案。"""

    procedure_id: str
    reason: str
    diff: str
    regression: RegressionResult | None = None
    failure_pattern: tuple[str, ...] = field(default_factory=tuple)
    #: 本次改动碰到的路径。分级看它,不看提案自述。
    changed_paths: tuple[str, ...] = field(default_factory=tuple)

    @property
    def level(self) -> Level:
        """L3a(工序,人工审批)还是 L3b(手艺,回归即可合)。"""
        return classify_l3(self.changed_paths)

    @property
    def needs_human(self) -> bool:
        return needs_human_approval(self.level)

    def ensure_mergeable(self) -> None:
        """能不能合。**没有回归证据一律拒绝。**

        这是 L3 与 L2 的关键区别:规则的影响可以靠人读懂,提示词的影响读不出来,只能测出来。
        一句看起来更清楚的提示词完全可能让某类任务的成功率掉一半,而没有任何人能靠读它发现。

        L3a 与 L3b 在这一条上**没有区别**——快通道换掉的是"要不要人看",不是"要不要验证"。
        """
        if self.regression is None or self.regression.samples == 0:
            raise RegressionMissing(
                f"{self.procedure_id} 的改动没有回归证据。提示词的影响读不出来,只能测出来——"
                "用历史任务的录制素材重跑改前改后再来。"
            )
        if self.regression.regressed:
            raise RegressionMissing(
                f"{self.procedure_id} 的回归验证显示**变差了**"
                f"({self.regression.before_pass} → {self.regression.after_pass})。"
            )

    def render(self) -> str:
        lines = [
            f"## 工序进化提案:`{self.procedure_id}`",
            "",
            self.reason or "(没有说明)",
            "",
            "### 识别到的固定失败模式",
            "",
            *(self.failure_pattern or ("(没有识别到固定模式)",)),
            "",
            "### 改动",
            "",
            "```diff",
            self.diff.strip() or "(空)",
            "```",
            "",
            "### 回归验证",
            "",
            self.regression.render() if self.regression else "**没有回归证据 —— 不允许合并。**",
            "",
            "### 通道",
            "",
            (
                "**L3a 工序(接口级)· 慢通道** —— 改动碰到了 craft/ 之外的东西,"
                "影响编排与审计,必须人工审批。"
                if self.needs_human
                else "**L3b 手艺(内容级)· 快通道** —— 改动只在 craft/ 内,回归验证通过即可合并。"
            ),
            "",
            "---",
            "**这套验证必须跑真实 Agent,所以不进常规 CI。**",
            "手工触发,与 PRD 16 的一致性套件同一条路。",
        ]
        return "\n".join(lines) + "\n"


def failure_pattern(
    events: Sequence[Any], procedure_id: str, threshold: int = 3
) -> tuple[str, ...]:
    """某个 Procedure 反复以同一种方式失败。

    只看**同一个** `failure_detail` 的重复:偶发失败没有进化价值,而反复以完全相同的方式失败
    正好说明是能力本身的问题。
    """
    counts: dict[str, int] = {}
    for event in events:
        payload = getattr(event, "payload", {}) or {}
        if payload.get("ok") or procedure_id not in str(payload.get("procedure_ref", "")):
            continue
        detail = str(payload.get("failure_detail") or "")[:200]
        if detail:
            counts[detail] = counts.get(detail, 0) + 1
    return tuple(sorted(item for item, count in counts.items() if count >= threshold))


def _rule_id(globs: tuple[str, ...]) -> str:
    stem = globs[0].strip("*/").replace("/", "-").replace("*", "").strip("-") or "path"
    return f"distilled-{stem}"[:48]


__all__ = [
    "DEFAULT_THRESHOLD",
    "Backtest",
    "RegressionMissing",
    "RegressionResult",
    "RuleProposal",
    "ProcedureProposal",
    "backtest",
    "failure_pattern",
    "propose_rules",
]


# --- L3 分级 -------------------------------------------------------------------

#: 判为 L3a(工序,接口级)的文件名。
_PROCEDURE_FILES = ("procedure.yaml", "prompt.md")


def classify_l3(paths: Sequence[str]) -> Level:
    """一组改动路径属于 L3a 还是 L3b。

    ## 判据是路径,不是提案自述

    让蒸馏器自己声明级别等于把闸门交给被审查方——而它恰好有动机把自己的改动说成低风险的
    那一档。路径是客观的:改了 `procedure.yaml` 就是接口变更,没有别的解释。

    ## 混合提案按更严的那档处理

    一个提案同时含两类路径时判 L3a,走人工审批。反过来的话,只要在一堆手艺改动里夹一行
    `procedure.yaml` 的改动,整个提案就能走快通道溜过去——而那正是这道闸要防的事。
    """
    saw_craft = False
    for raw in paths:
        parts = PurePosixPath(str(raw).replace("\\", "/")).parts
        if any(part == CRAFT_DIR for part in parts):
            saw_craft = True
            continue
        # 不在 craft/ 下的任何改动都按接口级处理:包含 procedure.yaml、prompt.md、
        # scripts/、schemas/ —— 它们都会改变编排器看到的契约或确定性行为。
        return Level.L3A
    return Level.L3B if saw_craft else Level.L3A


def needs_human_approval(level: Level) -> bool:
    """这一级要不要人工审批。

    L3b 走快通道**不等于没有门**——它只是把门从"人读"换成了"回归测出来"
    (见 `ProcedureProposal.ensure_mergeable`)。
    """
    return level is not Level.L3B
