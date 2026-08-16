"""这次改动到底要不要跑集成测试。

## 三级,顺序执行,先命中先赢

1. **人工覆盖**——提交时 `--itest always|never`,或运行中强制标记。人最大。
2. **机器规则**——对本次 diff 求值基因组里的影响规则。
3. **AI 兜底**——规则一条都没命中时,架构员工做一次廉价判断。

**规则优先不是因为规则更聪明,是因为规则更稳定。** 一个每次都给同样答案的粗糙规则,比一个
平均更准但会抖动的判断更适合当质量门禁的开关——团队要能预测系统行为才会信任它。AI 只在规则
明确表示"我不知道"的地方补位。

## 「一条都没命中」不等于「判定为否」

这两件事在这一层被严格分开:`rule_decision` 对空命中返回 `None`,意思是"规则说不知道",
调用方据此往下走兜底。合成一个布尔的话,灰色地带会被静默判成"不用跑",而那正是这套机制要
覆盖的地方。

反过来,命中了但命中的规则都写着 `requires_itest: false`,那是规则给出的**明确答案**,
不该再问 AI。

## 一条规则里的多个谓词是「与」

取并集的话,`{path_globs: [...], crosses_modules_gte: 5}` 这种"只在大范围改动时才管的
路径规则"根本没法表达——写下去会退化成那条 glob 单独生效。`path_globs` 列表内部仍是「或」。

## 兜底失败站在「跑」这一侧

判成不跑的话,这次改动就此完全没有跨模块验证,而且没有任何人会知道。一次静默的漏测比一次
多余的集成测试贵得多。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agentgenome.core.scope import compile_glob, is_under, normalize_path
from agentgenome.core.task import ItestNeed, ItestOverride
from agentgenome.gates.runner import modules_touched
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.rules import ImpactMatch, ImpactRules


class DecisionSource(StrEnum):
    """这个判定是谁给的。事后复核时第一个要看的就是它。"""

    MANUAL = "manual"
    RULE = "rule"
    AGENT = "agent"
    #: 兜底调用失败或产物不可用,按安全侧降级。
    AGENT_UNAVAILABLE = "agent_unavailable"


@dataclass(frozen=True)
class RuleHit:
    """一条命中的规则,以及它是靠哪几个谓词命中的。"""

    rule_id: str
    predicates: tuple[str, ...]
    requires_itest: bool


@dataclass(frozen=True)
class RuleVerdict:
    """规则这一级的结论。"""

    hits: tuple[RuleHit, ...] = ()

    @property
    def needs_itest(self) -> bool:
        return any(hit.requires_itest for hit in self.hits)

    @property
    def matched(self) -> tuple[str, ...]:
        return tuple(hit.rule_id for hit in self.hits)


@dataclass(frozen=True)
class ItestDecision:
    """判定结论。理由与来源一起走——只记结论的话,事后没法复核它判得对不对。"""

    need: ItestNeed
    source: DecisionSource
    reason: str
    matched_rules: tuple[str, ...] = ()
    confidence: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "needs_itest": self.need.value,
            "source": self.source.value,
            "reason": self.reason,
            "matched_rules": list(self.matched_rules),
            "confidence": self.confidence,
        }


def evaluate_rules(
    changed: Sequence[str], project_map: ProjectMap, impact: ImpactRules
) -> RuleVerdict:
    """对本次 diff 求值全部影响规则。**纯函数。**

    返回全部命中项而不是第一条:排查"为什么这次跑了集成测试"时,只看到第一条会让人以为
    其余规则没生效。
    """
    paths = [normalize_path(path) for path in changed]
    touched = modules_touched(paths, project_map.module_paths())
    hits = []
    for rule in impact.rules:
        fired = _predicates_fired(rule.match, paths, project_map, touched)
        if fired is not None:
            hits.append(RuleHit(rule.id, fired, rule.requires_itest))
    return RuleVerdict(tuple(hits))


def rule_decision(verdict: RuleVerdict) -> ItestDecision | None:
    """把规则结论变成判定。一条都没命中时返回 `None`——那是"规则说不知道"。"""
    if not verdict.hits:
        return None
    if verdict.needs_itest:
        required = [hit for hit in verdict.hits if hit.requires_itest]
        detail = ", ".join(f"{hit.rule_id}({'+'.join(hit.predicates)})" for hit in required)
        return ItestDecision(
            need=ItestNeed.YES,
            source=DecisionSource.RULE,
            reason=f"命中影响规则: {detail}",
            matched_rules=verdict.matched,
        )
    return ItestDecision(
        need=ItestNeed.NO,
        source=DecisionSource.RULE,
        reason=f"命中的规则都声明不需要集成测试: {', '.join(verdict.matched)}",
        matched_rules=verdict.matched,
    )


def manual_decision(override: ItestOverride) -> ItestDecision | None:
    """人工覆盖。`auto` 不是覆盖,返回 `None` 让下面两级去算。"""
    if override is ItestOverride.ALWAYS:
        return ItestDecision(ItestNeed.YES, DecisionSource.MANUAL, "人工要求必须跑集成测试")
    if override is ItestOverride.NEVER:
        return ItestDecision(ItestNeed.NO, DecisionSource.MANUAL, "人工要求跳过集成测试")
    return None


def agent_decision(payload: dict[str, Any] | None) -> ItestDecision:
    """把兜底 Job 的产物变成判定。**永远返回一个判定,不会返回 None。**

    产物缺失、自评失败、字段类型不对——一律降级为"跑"。判定悬空的话任务会卡在
    `needs_itest=undecided`,而两条守卫都不认这个值,任务原地打转直到升级人工。
    """
    verdict = payload.get("needs_itest") if payload else None
    if not payload or not payload.get("passed") or not isinstance(verdict, bool):
        return ItestDecision(
            need=ItestNeed.YES,
            source=DecisionSource.AGENT_UNAVAILABLE,
            reason="兜底判定不可用,按安全侧处理:跑集成测试",
        )
    confidence = payload.get("confidence")
    return ItestDecision(
        need=ItestNeed.YES if verdict else ItestNeed.NO,
        source=DecisionSource.AGENT,
        reason=str(payload.get("reason") or "(兜底判定未给出理由)"),
        confidence=float(confidence) if isinstance(confidence, int | float) else None,
    )


def _predicates_fired(
    match: ImpactMatch, paths: Sequence[str], project_map: ProjectMap, touched: Sequence[str]
) -> tuple[str, ...] | None:
    """这条规则的匹配块成不成立。成立时给出是哪几个谓词撑起来的。

    一个谓词都没写的规则不可能到这里——`ImpactMatch` 在加载时就拒了空匹配块。
    """
    fired = []
    if match.path_globs:
        if not _matches_any_glob(paths, match.path_globs):
            return None
        fired.append("path_globs")
    if match.touches_interface_schema:
        declared = [face.schema_path for face in project_map.interfaces if face.schema_path]
        if not _touches_any(paths, declared):
            return None
        fired.append("touches_interface_schema")
    if match.touches_migrations:
        declared = [store.migrations for store in project_map.datastores if store.migrations]
        if not _touches_any(paths, declared):
            return None
        fired.append("touches_migrations")
    if match.crosses_modules_gte is not None:
        if len(touched) < match.crosses_modules_gte:
            return None
        fired.append("crosses_modules_gte")
    return tuple(fired)


def _matches_any_glob(paths: Sequence[str], globs: Sequence[str]) -> bool:
    """glob 走越权检查那套翻译,不用 `fnmatch`。

    `fnmatch` 不区分 `*` 与 `**`,于是 `itest/*` 会匹配 `itest/a/b.yaml`。在这里过度匹配的
    方向是"多跑一次集成测试",不像权限检查那么致命,但两套 glob 语义并存迟早让人写错。
    """
    return any(compile_glob(glob).match(path) for glob in globs for path in paths)


def _touches_any(paths: Sequence[str], declared: Sequence[str]) -> bool:
    """改动路径有没有落在这几条声明路径上。

    按目录边界比,不按字符串前缀:声明 `repos/order-service/migrations` 时,
    `repos/order-service/migrations_old/x.sql`
    不算碰了它。前缀匹配在这里的方向是误判为"要跑",看起来无害,但它让规则失去可预测性
    ——而可预测正是规则优先于 AI 的全部理由。
    """
    return any(is_under(path, item) for item in declared for path in paths)


__all__ = [
    "DecisionSource",
    "ItestDecision",
    "RuleHit",
    "RuleVerdict",
    "agent_decision",
    "evaluate_rules",
    "manual_decision",
    "rule_decision",
]
