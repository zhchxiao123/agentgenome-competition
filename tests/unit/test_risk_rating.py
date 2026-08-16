"""风险评级。

这一层决定"哪些改动要人来看一眼"。判错的两个方向都有代价:漏判是不该自动合的东西合了,
误判是人被打扰太多次、开始不看内容直接点同意——后者会让整道关卡悄悄失效。
"""

from __future__ import annotations

import pytest

from agentgenome.genome.models import ProjectMap
from agentgenome.genome.rules import ProtectedRules
from agentgenome.security.risk import (
    DEFAULT_DELETED_LINES_GT,
    DiffStat,
    RiskLevel,
    assess,
    effective_patterns,
)

MAP = ProjectMap.model_validate(
    {
        "version": 1,
        "project": {"name": "mall"},
        "modules": [{"id": "order-service", "path": "repos/order-service/"}],
        "interfaces": [
            {
                "id": "order-to-inventory",
                "kind": "http",
                "provider": "order-service",
                "consumers": [],
                "schema": "repos/inventory-service/api/reserve.yaml",
            }
        ],
        "datastores": [
            {
                "id": "orders",
                "kind": "postgres",
                "owner": "order-service",
                "migrations": "repos/order-service/migrations",
            }
        ],
    }
)

NONE = ProtectedRules()


def _assess(paths: list[str], added: int = 0, deleted: int = 0, rules: ProtectedRules = NONE):
    return assess(paths, DiffStat(added=added, deleted=deleted), rules, MAP)


# --- 五种内置模式 -----------------------------------------------------------


@pytest.mark.parametrize(
    "path,rule",
    [
        ("repos/order-service/migrations/0002_add_column.sql", "migrations"),
        ("repos/order-service/src/auth/token.py", "auth"),
        ("repos/order-service/src/security/policy.py", "auth"),
        (".github/workflows/ci.yml", "deploy"),
        ("deploy/k8s/order.yaml", "deploy"),
        ("repos/order-service/Dockerfile", "deploy"),
        ("repos/inventory-service/api/reserve.yaml", "interface-schema"),
    ],
)
def test_builtin_path_patterns_are_high_risk(path: str, rule: str) -> None:
    verdict = _assess([path])

    assert verdict.level is RiskLevel.HIGH
    assert rule in verdict.matched


def test_a_plain_code_change_is_low_risk() -> None:
    """低风险自动合并是效率的全部来源。把普通改动评成高的话,人很快就不看了。"""
    assert _assess(["repos/order-service/src/order/reserve.py"]).level is RiskLevel.LOW


def test_mass_deletion_is_high_risk() -> None:
    assert _assess(["repos/order-service/src/a.py"], deleted=DEFAULT_DELETED_LINES_GT + 1).is_high


def test_deletion_is_counted_net_of_additions() -> None:
    """一次大重构删几千行也加回几千行,那不是"顺手清理"。

    按总删除算的话,每一次大重命名都会被评成高风险,然后人开始不看。
    """
    churn = _assess(["repos/order-service/src/a.py"], added=5000, deleted=5000)

    assert churn.level is RiskLevel.LOW


def test_all_matching_rules_are_recorded() -> None:
    """ "这次为什么要我审"必须可回答,而且答案要全。"""
    verdict = _assess([".github/workflows/ci.yml", "repos/order-service/migrations/1.sql"])

    assert set(verdict.matched) == {"deploy", "migrations"}


def test_nothing_matched_says_so() -> None:
    verdict = _assess(["README.md"])

    assert verdict.matched == ()
    assert "没有命中" in verdict.reason


# --- 项目自定义 -------------------------------------------------------------


def test_a_project_can_add_its_own_red_line() -> None:
    rules = ProtectedRules.model_validate(
        {"high_risk": [{"id": "pricing", "path_globs": ["repos/order-service/src/pricing/**"]}]}
    )

    assert _assess(["repos/order-service/src/pricing/rules.py"], rules=rules).is_high


def test_a_project_rule_replaces_the_builtin_with_the_same_id() -> None:
    """整条覆盖,不是逐字段合并——半份来自内置半份来自项目的规则没人能推理。"""
    rules = ProtectedRules.model_validate(
        {"high_risk": [{"id": "mass-deletion", "deleted_lines_gt": 10}]}
    )

    assert _assess(["repos/order-service/src/a.py"], deleted=11, rules=rules).is_high
    ids = [pattern.id for pattern in effective_patterns(rules, MAP)]
    assert ids.count("mass-deletion") == 1, "内置那条没被替换掉,两条同名规则并存"


def test_a_project_can_relax_a_builtin_threshold() -> None:
    rules = ProtectedRules.model_validate(
        {"high_risk": [{"id": "mass-deletion", "deleted_lines_gt": 100000}]}
    )

    assert not _assess(["repos/order-service/src/a.py"], deleted=1000, rules=rules).is_high


# --- 从项目地图推导 ---------------------------------------------------------


def test_a_missing_project_map_still_yields_the_builtins() -> None:
    """地图读不出来时不该整个评级失效——那个方向是把高风险改动放过去。"""
    verdict = assess([".github/workflows/ci.yml"], DiffStat(), NONE, None)

    assert verdict.is_high


def test_migration_and_contract_paths_come_from_the_map_not_from_a_second_list() -> None:
    """位置已经在地图里声明过了,再让人抄一遍的话两份迟早对不上——而对不上的那次就是漏判。"""
    ids = [pattern.id for pattern in effective_patterns(NONE, MAP)]

    assert "migrations" in ids
    assert "interface-schema" in ids
    assert [pattern.id for pattern in effective_patterns(NONE, None)] == [
        "auth",
        "deploy",
        "mass-deletion",
    ]


# --- 扩权必经人工 -------------------------------------------------------------


def test_a_task_that_widened_its_scope_is_high_risk() -> None:
    """扩权自动放行,但**必经人工审批**——两者合起来才是那个设计。

    只有自动放行的话,员工可以自助扩权而没有任何人过目;只有人工闸门的话,每个合法的跨模块
    任务都要停摆几小时等人。真正要守的性质是"没有一次跨计划的改动能在无人过目的情况下合入",
    而它在评审环节守得住——何况「这个任务该不该碰库存域」这个问题,人**拿着 diff** 回答
    比在飞行途中凭一句理由回答准得多。
    """
    verdict = assess(
        ["repos/inventory-service/src/stock.py"],
        DiffStat(added=3, deleted=0),
        ProtectedRules(),
        widened=["inventory-service"],
    )

    assert verdict.is_high
    assert "scope-widened" in verdict.matched
    # "这次为什么要我审"必须可回答。
    assert "inventory-service" in verdict.reason


def test_a_task_that_never_widened_is_rated_exactly_as_before() -> None:
    """零影响回归:没扩过权的任务,评级结果与这条改动之前逐字一致。"""
    changed = ["repos/order-service/src/app.py"]
    stat = DiffStat(added=3, deleted=0)

    assert assess(changed, stat, ProtectedRules(), widened=[]) == assess(
        changed, stat, ProtectedRules()
    )


def test_widening_stacks_with_the_other_patterns() -> None:
    """命中的规则 id 全部列出——人要知道这次是几件事凑到一起。"""
    verdict = assess(
        [".github/workflows/ci.yml"],
        DiffStat(added=1, deleted=0),
        ProtectedRules(),
        widened=["inventory-service"],
    )

    assert set(verdict.matched) >= {"deploy", "scope-widened"}
