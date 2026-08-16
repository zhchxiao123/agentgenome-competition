"""规则层:Markdown 里的机器可读块、受保护路径、影响规则。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agentgenome.config import Config
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.rules import effective_max_fix_rounds, load_rules


def _write(tmp_path: Path, **files: str) -> Path:
    root = tmp_path / "ws"
    (root / "genome" / "rules").mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = {
            "architecture": root / "genome/rules/architecture.md",
            "protected": root / "genome/rules/protected.yaml",
            "impact": root / "genome/rules/impact.yaml",
        }[name]
        target.write_text(textwrap.dedent(content))
    return root


# --- Markdown 中的机器可读块 -------------------------------------------------


def test_reads_the_rules_block_and_ignores_the_prose(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        architecture="""\
        # 架构规则

        这段正文是给人看的,里面写 `forbidden_deps` 也不该被解析。

        ```rules
        forbidden_deps:
          - {from: "repos/inventory-service/**", to: "repos/order-service/**"}
        layering:
          - "api 层不得直接访问 db 层"
        max_fix_rounds: 3
        ```

        下接人类可读说明,里面也可以有别的代码块:

        ```yaml
        这段是示例代码,不是规则
        ```
        """,
    )

    rules = load_rules(root)

    assert len(rules.architecture.forbidden_deps) == 1
    assert rules.architecture.forbidden_deps[0].from_glob == "repos/inventory-service/**"
    assert rules.architecture.forbidden_deps[0].to_glob == "repos/order-service/**"
    assert rules.architecture.layering == ["api 层不得直接访问 db 层"]
    assert rules.architecture.max_fix_rounds == 3


def test_multiple_rules_blocks_are_merged_in_order(tmp_path: Path) -> None:
    """一个文档允许多个 rules 块,按出现顺序深度合并。"""
    root = _write(
        tmp_path,
        architecture="""\
        ```rules
        max_fix_rounds: 2
        forbidden_deps:
          - {from: "a/**", to: "b/**"}
        ```

        中间是说明文字。

        ```rules
        max_fix_rounds: 5
        layering: ["后写的补充进来"]
        ```
        """,
    )

    rules = load_rules(root)

    assert rules.architecture.max_fix_rounds == 5
    assert len(rules.architecture.forbidden_deps) == 1
    assert rules.architecture.layering == ["后写的补充进来"]


def test_missing_rule_files_yield_an_empty_ruleset(tmp_path: Path) -> None:
    """新 Workspace 还没积累规则,这是正常状态而非错误。"""
    root = tmp_path / "bare"
    root.mkdir()

    rules = load_rules(root)

    assert rules.architecture.forbidden_deps == []
    assert rules.protected.protected_paths == []
    assert rules.impact.rules == []
    assert rules.architecture.max_fix_rounds is None


def test_a_document_without_any_rules_block_is_fine(tmp_path: Path) -> None:
    root = _write(tmp_path, architecture="# 只有散文,没有规则块\n")

    assert load_rules(root).architecture.max_fix_rounds is None


def test_malformed_rules_block_is_a_readable_error(tmp_path: Path) -> None:
    root = _write(tmp_path, architecture="```rules\nmax_fix_rounds: [\n```\n")

    with pytest.raises(GenomeValidationError) as excinfo:
        load_rules(root)

    assert "architecture.md" in excinfo.value.render()


def test_unknown_field_in_rules_block_is_rejected(tmp_path: Path) -> None:
    root = _write(tmp_path, architecture="```rules\nmax_fix_round: 3\n```\n")

    with pytest.raises(GenomeValidationError) as excinfo:
        load_rules(root)

    assert "max_fix_round" in excinfo.value.render()


# --- 规则文件优先于根配置 ----------------------------------------------------


def test_rules_file_wins_over_root_config_for_max_fix_rounds(tmp_path: Path) -> None:
    """规则是项目资产,根配置是部署参数——冲突时项目资产赢。"""
    root = _write(tmp_path, architecture="```rules\nmax_fix_rounds: 7\n```\n")
    config = Config.model_validate({"limits": {"max_fix_rounds": 3}})

    assert effective_max_fix_rounds(load_rules(root), config) == 7


def test_root_config_applies_when_rules_are_silent(tmp_path: Path) -> None:
    root = _write(tmp_path, architecture="# 没有规则块\n")
    config = Config.model_validate({"limits": {"max_fix_rounds": 4}})

    assert effective_max_fix_rounds(load_rules(root), config) == 4


# --- 受保护路径与高风险模式 --------------------------------------------------


def test_loads_protected_paths_and_high_risk_patterns(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        protected="""\
        protected_paths:
          - genome/rules/**
          - .github/**
        high_risk:
          - id: migrations
            description: 迁移不可逆
            path_globs: ["**/migrations/**"]
          - id: mass-deletion
            description: 删除量过大
            deleted_lines_gt: 500
        """,
    )

    protected = load_rules(root).protected

    assert protected.all_paths == ["genome/rules/**", ".github/**"]
    assert [pattern.id for pattern in protected.high_risk] == ["migrations", "mass-deletion"]
    assert protected.high_risk[0].path_globs == ["**/migrations/**"]
    assert protected.high_risk[1].deleted_lines_gt == 500


def test_high_risk_pattern_must_carry_at_least_one_condition(tmp_path: Path) -> None:
    """一条什么都不匹配的规则要么是笔误,要么会静默放过高风险变更。"""
    root = _write(
        tmp_path,
        protected="high_risk:\n  - {id: empty, description: 什么条件都没写}\n",
    )

    with pytest.raises(GenomeValidationError) as excinfo:
        load_rules(root)

    assert "empty" in excinfo.value.render()


def test_duplicate_high_risk_ids_are_rejected(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        protected="""\
        high_risk:
          - {id: dup, description: 一, path_globs: ["a/**"]}
          - {id: dup, description: 二, path_globs: ["b/**"]}
        """,
    )

    with pytest.raises(GenomeValidationError) as excinfo:
        load_rules(root)

    assert "dup" in excinfo.value.render()


# --- 影响规则 ----------------------------------------------------------------


def test_loads_impact_rules_with_their_predicates(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        impact="""\
        rules:
          - id: interface-schema
            description: 触碰跨模块契约
            match: {touches_interface_schema: true}
            requires_itest: true
          - id: deploy
            description: 触碰部署文件
            match: {path_globs: ["deploy/**"]}
            requires_itest: true
          - id: cross-module
            description: 跨越两个以上模块
            match: {crosses_modules_gte: 2}
            requires_itest: true
        """,
    )

    impact = load_rules(root).impact

    assert [rule.id for rule in impact.rules] == ["interface-schema", "deploy", "cross-module"]
    assert impact.rules[0].match.touches_interface_schema is True
    assert impact.rules[1].match.path_globs == ["deploy/**"]
    assert impact.rules[2].match.crosses_modules_gte == 2


def test_impact_rule_with_an_unknown_predicate_is_rejected(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        impact="rules:\n  - {id: r, description: d, match: {touches_everything: true}}\n",
    )

    with pytest.raises(GenomeValidationError) as excinfo:
        load_rules(root)

    assert "touches_everything" in excinfo.value.render()


def test_impact_rule_must_carry_at_least_one_predicate(tmp_path: Path) -> None:
    root = _write(tmp_path, impact="rules:\n  - {id: r, description: d, match: {}}\n")

    with pytest.raises(GenomeValidationError) as excinfo:
        load_rules(root)

    assert "r" in excinfo.value.render()


def test_problems_across_rule_files_are_reported_together(tmp_path: Path) -> None:
    """一次报全,不是修一个文件跑一次。"""
    root = _write(
        tmp_path,
        protected="high_risk:\n  - {id: empty, description: 无条件}\n",
        impact="rules:\n  - {id: r, description: d, match: {}}\n",
    )

    with pytest.raises(GenomeValidationError) as excinfo:
        load_rules(root)

    files = {issue.file for issue in excinfo.value.issues}
    assert files == {"genome/rules/protected.yaml", "genome/rules/impact.yaml"}


def test_scaffolded_rules_are_valid(tmp_path: Path) -> None:
    """init 写出的规则模板必须自己就能通过校验。"""
    from agentgenome.genome import scaffold

    root = _write(
        tmp_path,
        architecture=scaffold.ARCHITECTURE_TEMPLATE,
        protected=scaffold.PROTECTED_TEMPLATE,
        impact=scaffold.IMPACT_TEMPLATE,
    )

    rules = load_rules(root)

    assert rules.architecture.max_fix_rounds is None
    assert "genome/rules/**" in rules.protected.all_paths
    assert {rule.id for rule in rules.impact.rules} == {
        "interface-schema",
        "migrations",
        "cross-module",
        "deploy-files",
    }


# --- 受保护路径的角色豁免 ----------------------------------------------------


def test_a_protected_path_forbids_everyone_by_default(tmp_path: Path) -> None:
    """默认值站在安全的那一侧:没写 writable_by 就是谁都不能碰。"""
    root = _write(tmp_path, protected="protected_paths:\n  - .github/**\n")

    protected = load_rules(root).protected

    assert protected.paths_for("arch-employee") == [".github/**"]
    assert protected.paths_for("dev-employee") == [".github/**"]


def test_a_protected_path_can_name_who_may_write_it(tmp_path: Path) -> None:
    """规则文件只有架构员工能动——这是**角色边界**,不是"谁都不能动"。

    豁免写在项目规则里而不是员工定义里:一个员工要是能在自己的文件里给自己开豁免,
    受保护路径就只是个建议了。
    """
    root = _write(
        tmp_path,
        protected="""\
        protected_paths:
          - path: genome/rules/**
            writable_by: [arch-employee]
          - .github/**
        """,
    )

    protected = load_rules(root).protected

    assert protected.paths_for("dev-employee") == ["genome/rules/**", ".github/**"]
    assert protected.paths_for("arch-employee") == [".github/**"]


def test_all_paths_lists_every_protected_glob_regardless_of_writers(tmp_path: Path) -> None:
    """展示与审计要看到全集,不是某个员工视角下的子集。"""
    root = _write(
        tmp_path,
        protected="protected_paths:\n  - path: genome/rules/**\n    writable_by: [arch-employee]\n",
    )

    assert load_rules(root).protected.all_paths == ["genome/rules/**"]


def test_an_unknown_field_on_a_protected_path_is_rejected(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        protected="protected_paths:\n  - path: genome/rules/**\n    writeable_by: [arch]\n",
    )

    with pytest.raises(GenomeValidationError) as excinfo:
        load_rules(root)

    assert "writeable_by" in excinfo.value.render()
