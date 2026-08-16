"""needs_itest 判定的纯逻辑。

这一层是本 PRD 最该被密集测试的地方:它决定了「跨模块的改动会不会被验」,而判错的
两个方向代价都很高——漏判是线上事故,误判是每次改文案都等五分钟。
"""

from __future__ import annotations

import pytest

from agentgenome.core.task import ItestNeed, ItestOverride
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.rules import ImpactRules
from agentgenome.itest.decide import (
    DecisionSource,
    agent_decision,
    evaluate_rules,
    manual_decision,
    rule_decision,
)

MAP = ProjectMap.model_validate(
    {
        "version": 1,
        "project": {"name": "mall"},
        "modules": [
            {"id": "order-service", "path": "repos/order-service/"},
            {"id": "inventory-service", "path": "repos/inventory-service/"},
            {"id": "gateway", "path": "repos/order-service0/"},
        ],
        "interfaces": [
            {
                "id": "order-to-inventory",
                "kind": "http",
                "provider": "inventory-service",
                "consumers": ["order-service"],
                "schema": "contracts/inventory.openapi.yaml",
            }
        ],
        "datastores": [
            {
                "id": "orders-db",
                "kind": "postgres",
                "owner": "order-service",
                "migrations": "repos/order-service/migrations",
            }
        ],
    }
)


def rules(*entries: dict) -> ImpactRules:
    return ImpactRules.model_validate({"rules": list(entries)})


INTERFACE_RULE = {"id": "interface-schema", "match": {"touches_interface_schema": True}}
MIGRATION_RULE = {"id": "migrations", "match": {"touches_migrations": True}}
CROSS_RULE = {"id": "cross-module", "match": {"crosses_modules_gte": 2}}
DEPLOY_RULE = {"id": "deploy", "match": {"path_globs": ["itest/**", "deploy/**"]}}


# --- 谓词 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "changed,expected",
    [
        (["contracts/inventory.openapi.yaml"], True),
        # 契约声明的是一个目录时,目录下的任何文件都算碰了它。
        (["contracts/inventory.openapi.yaml/v2.yaml"], True),
        (["contracts/other.yaml"], False),
        # 前缀像但不是同一条路径——按目录边界比,不按字符串前缀。
        (["contracts/inventory.openapi.yaml.bak"], False),
        ([], False),
    ],
)
def test_touches_interface_schema(changed: list[str], expected: bool) -> None:
    verdict = evaluate_rules(changed, MAP, rules(INTERFACE_RULE))
    assert verdict.needs_itest is expected


@pytest.mark.parametrize(
    "changed,expected",
    [
        (["repos/order-service/migrations/003_add_column.sql"], True),
        (["repos/order-service/migrations"], True),
        (["repos/order-service/migrations_old/003.sql"], False),
        (["repos/order-service/src/main.py"], False),
    ],
)
def test_touches_migrations(changed: list[str], expected: bool) -> None:
    verdict = evaluate_rules(changed, MAP, rules(MIGRATION_RULE))
    assert verdict.needs_itest is expected


@pytest.mark.parametrize(
    "changed,expected",
    [
        (["itest/compose.yaml"], True),
        (["deploy/k8s/order.yaml"], True),
        (["repos/order-service/src/main.py"], False),
        # `*` 不跨目录:`itest/**` 覆盖 `itest` 自身与其下一切,但 `itestx/` 不算。
        (["itestx/compose.yaml"], False),
    ],
)
def test_path_globs(changed: list[str], expected: bool) -> None:
    verdict = evaluate_rules(changed, MAP, rules(DEPLOY_RULE))
    assert verdict.needs_itest is expected


@pytest.mark.parametrize(
    "changed,expected",
    [
        (["repos/order-service/a.py", "repos/inventory-service/b.py"], True),
        (["repos/order-service/a.py", "repos/order-service/b.py"], False),
        # `repos/order-service` 与 `repos/order-service0` 是两个仓,不能靠字符串前缀数成一个。
        (["repos/order-service/a.py", "repos/order-service0/b.py"], True),
        (["README.md"], False),
    ],
)
def test_crosses_modules_gte(changed: list[str], expected: bool) -> None:
    verdict = evaluate_rules(changed, MAP, rules(CROSS_RULE))
    assert verdict.needs_itest is expected


def test_crosses_modules_threshold_is_respected() -> None:
    """阈值是规则写的,不是硬编码的 2。"""
    three = rules({"id": "three", "match": {"crosses_modules_gte": 3}})
    assert not evaluate_rules(
        ["repos/order-service/a", "repos/inventory-service/b"], MAP, three
    ).needs_itest
    assert evaluate_rules(
        ["repos/order-service/a", "repos/inventory-service/b", "repos/order-service0/c"], MAP, three
    ).needs_itest


# --- 规则的组合 -------------------------------------------------------------


def test_predicates_within_one_rule_are_conjunctive() -> None:
    """一条规则里写了两个谓词,是「都成立才算命中」。

    取并集的话,`{path_globs: [...], crosses_modules_gte: 5}` 这种「只在大范围改动时
    才管的路径规则」根本没法表达——写下去会退化成那条 glob 单独生效。
    """
    both = rules(
        {
            "id": "both",
            "match": {"path_globs": ["repos/order-service/**"], "crosses_modules_gte": 2},
        },
    )
    assert not evaluate_rules(["repos/order-service/a.py"], MAP, both).needs_itest
    assert evaluate_rules(
        ["repos/order-service/a.py", "repos/inventory-service/b.py"], MAP, both
    ).needs_itest


def test_all_matching_rules_are_recorded() -> None:
    """命中列表要全,不是命中一条就收工。

    排查「为什么这次跑了集成测试」时,只看到第一条会让人以为其余规则没生效。
    """
    verdict = evaluate_rules(
        [
            "contracts/inventory.openapi.yaml",
            "repos/order-service/a.py",
            "repos/inventory-service/b.py",
        ],
        MAP,
        rules(INTERFACE_RULE, CROSS_RULE, MIGRATION_RULE),
    )
    assert verdict.matched == ("interface-schema", "cross-module")


def test_rule_that_does_not_require_itest_does_not_flip_the_verdict() -> None:
    verdict = evaluate_rules(
        ["repos/order-service/docs/readme.md"],
        MAP,
        rules(
            {
                "id": "docs",
                "match": {"path_globs": ["repos/order-service/docs/**"]},
                "requires_itest": False,
            }
        ),
    )
    assert verdict.matched == ("docs",)
    assert verdict.needs_itest is False


def test_no_rules_at_all_means_no_hits() -> None:
    verdict = evaluate_rules(["repos/order-service/a.py"], MAP, ImpactRules())
    assert verdict.matched == ()
    assert rule_decision(verdict) is None


def test_rule_decision_is_none_when_nothing_matched() -> None:
    """一条都没命中不等于判定为否——它是「规则说不知道」,该往下走 AI 兜底。"""
    verdict = evaluate_rules(["repos/order-service/a.py"], MAP, rules(INTERFACE_RULE, CROSS_RULE))
    assert verdict.matched == ()
    assert rule_decision(verdict) is None


def test_rule_decision_carries_the_matched_ids() -> None:
    verdict = evaluate_rules(["contracts/inventory.openapi.yaml"], MAP, rules(INTERFACE_RULE))
    decision = rule_decision(verdict)
    assert decision is not None
    assert decision.need is ItestNeed.YES
    assert decision.source is DecisionSource.RULE
    assert decision.matched_rules == ("interface-schema",)
    assert "interface-schema" in decision.reason


def test_matched_but_not_required_yields_a_no_decision() -> None:
    """命中了、但命中的规则都说不用跑——这是规则给出的明确答案,不该再问 AI。"""
    verdict = evaluate_rules(
        ["repos/order-service/docs/readme.md"],
        MAP,
        rules(
            {
                "id": "docs",
                "match": {"path_globs": ["repos/order-service/docs/**"]},
                "requires_itest": False,
            }
        ),
    )
    decision = rule_decision(verdict)
    assert decision is not None
    assert decision.need is ItestNeed.NO
    assert decision.matched_rules == ("docs",)


# --- 人工覆盖 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "override,need",
    [(ItestOverride.ALWAYS, ItestNeed.YES), (ItestOverride.NEVER, ItestNeed.NO)],
)
def test_manual_override_decides_outright(override: ItestOverride, need: ItestNeed) -> None:
    decision = manual_decision(override)
    assert decision is not None
    assert decision.need is need
    assert decision.source is DecisionSource.MANUAL


def test_auto_is_not_an_override() -> None:
    assert manual_decision(ItestOverride.AUTO) is None


# --- AI 兜底 ----------------------------------------------------------------


def test_agent_decision_reads_the_payload() -> None:
    decision = agent_decision(
        {"passed": True, "needs_itest": False, "reason": "只改了日志文案", "confidence": 0.9}
    )
    assert decision.need is ItestNeed.NO
    assert decision.source is DecisionSource.AGENT
    assert decision.confidence == 0.9
    assert decision.reason == "只改了日志文案"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"passed": False, "needs_itest": False},
        {"passed": True},
        {"passed": True, "needs_itest": "maybe"},
    ],
    ids=["missing", "empty", "self-reported-failure", "no-verdict", "wrong-type"],
)
def test_agent_failure_degrades_to_running_the_tests(payload: dict | None) -> None:
    """兜底失败时站在安全的那一侧:跑。

    判成不跑的话,这次改动就此完全没有跨模块验证,而且没有任何人会知道——一次静默的
    漏测比一次多余的集成测试贵得多。
    """
    decision = agent_decision(payload)
    assert decision.need is ItestNeed.YES
    assert decision.source is DecisionSource.AGENT_UNAVAILABLE


def test_decision_serialises_for_the_event_stream() -> None:
    verdict = evaluate_rules(["contracts/inventory.openapi.yaml"], MAP, rules(INTERFACE_RULE))
    decision = rule_decision(verdict)
    assert decision is not None
    assert decision.as_dict() == {
        "needs_itest": "yes",
        "source": "rule",
        "reason": decision.reason,
        "matched_rules": ["interface-schema"],
        "confidence": None,
    }
