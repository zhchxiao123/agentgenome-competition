"""按角色定制的手艺集:开发员工不该看到架构员工的手艺清单。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome.agents import capabilities
from agentgenome.employees import load_employees
from agentgenome.genome import craft
from agentgenome.genome.errors import GenomeValidationError


def _write_craft(root: Path, name: str, body: str = "# 手艺\n") -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / craft.CRAFT_MANIFEST).write_text(body, encoding="utf-8")


class TestCollect:
    def test_the_employee_only_gets_the_common_crafts_it_declared(self, tmp_path: Path) -> None:
        common = tmp_path / "_common" / "craft"
        _write_craft(common, "genome-navigation")
        _write_craft(common, "map-authoring")

        collected = craft.collect(None, common, ["genome-navigation"])

        assert sorted(collected) == ["genome-navigation"]

    def test_declaring_nothing_means_nothing_not_everything(self, tmp_path: Path) -> None:
        """空表示不带,不是带全部。理由与 procedures 白名单一致:能力要显式授予。"""
        common = tmp_path / "_common" / "craft"
        _write_craft(common, "genome-navigation")

        assert craft.collect(None, common, ()) == {}

    def test_the_procedure_craft_comes_along_regardless(self, tmp_path: Path) -> None:
        common = tmp_path / "_common" / "craft"
        _write_craft(common, "genome-navigation")
        own = tmp_path / "proc" / "craft"
        _write_craft(own, "failure-diagnosis")

        collected = craft.collect(own, common, ["genome-navigation"])

        assert sorted(collected) == ["failure-diagnosis", "genome-navigation"]

    def test_the_procedure_wins_when_both_carry_the_same_name(self, tmp_path: Path) -> None:
        """工序对这次作业的了解比员工级声明多,所以同名时它说了算。"""
        common = tmp_path / "_common" / "craft"
        _write_craft(common, "shared", "通用版\n")
        own = tmp_path / "proc" / "craft"
        _write_craft(own, "shared", "工序版\n")

        collected = craft.collect(own, common, ["shared"])

        body = (collected["shared"] / craft.CRAFT_MANIFEST).read_text(encoding="utf-8")
        assert body == "工序版\n"

    def test_two_employees_do_not_see_each_others_crafts(self, tmp_path: Path) -> None:
        common = tmp_path / "_common" / "craft"
        _write_craft(common, "codebase-survey")
        _write_craft(common, "failure-diagnosis")

        arch = craft.collect(None, common, ["codebase-survey"])
        dev = craft.collect(None, common, ["failure-diagnosis"])

        assert "failure-diagnosis" not in arch
        assert "codebase-survey" not in dev


class TestMissingCommon:
    def test_reports_what_was_declared_but_does_not_exist(self, tmp_path: Path) -> None:
        common = tmp_path / "_common" / "craft"
        _write_craft(common, "exists")

        assert craft.missing_common(common, ["exists", "typo-here"]) == ["typo-here"]

    def test_declaring_nothing_is_never_missing(self, tmp_path: Path) -> None:
        assert craft.missing_common(None, ()) == []


class TestEmployeeValidation:
    def _workspace(self, tmp_path: Path, crafts_line: str) -> Path:
        root = tmp_path / "ws"
        (root / "employees" / "prompts").mkdir(parents=True)
        (root / "employees" / "prompts" / "dev.md").write_text("你是开发。\n", encoding="utf-8")
        (root / "employees" / "dev.yaml").write_text(
            "id: dev\nruntime: claude-code\nprompt: prompts/dev.md\n"
            "procedures: [code-develop]\n" + crafts_line,
            encoding="utf-8",
        )
        return root / "employees"

    def test_an_undeclared_craft_is_caught_at_load_time(self, tmp_path: Path) -> None:
        """配置写错该在启动时暴露,不是任务跑一半才发现员工少带了一份方法论。"""
        root = self._workspace(tmp_path, "crafts: [does-not-exist]\n")

        with pytest.raises(GenomeValidationError) as caught:
            load_employees(root)

        assert "does-not-exist" in caught.value.render()

    def test_a_declared_craft_that_exists_loads_fine(self, tmp_path: Path) -> None:
        root = self._workspace(tmp_path, "crafts: [genome-navigation]\n")
        _write_craft(
            root.parent / "genome" / "procedures" / "_common" / "craft", "genome-navigation"
        )

        registry = load_employees(root)

        assert registry.get("dev").crafts == ["genome-navigation"]

    def test_declaring_no_crafts_needs_no_common_library(self, tmp_path: Path) -> None:
        """手艺是增强不是前置依赖——没有通用库的 Workspace 照样能加载员工。"""
        root = self._workspace(tmp_path, "")

        assert load_employees(root).get("dev").crafts == []

    def test_crafts_do_not_widen_the_write_scope(self, tmp_path: Path) -> None:
        """`crafts` 不是权限字段。当权限管的话它会进越权检查,而越权失败要回滚工作区
        ——对"少带了一份方法论"这件事是完全不成比例的后果。
        """
        root = self._workspace(tmp_path, "crafts: [genome-navigation]\n")
        _write_craft(
            root.parent / "genome" / "procedures" / "_common" / "craft", "genome-navigation"
        )

        scope = load_employees(root).get("dev").scope("t-1")

        assert scope.write_paths == ()


class TestRuntimeDowngrade:
    def test_claude_code_mounts_natively(self) -> None:
        assert capabilities.CLAUDE_CODE.craft_mounting is True

    def test_a_runtime_without_the_mechanism_is_marked_so(self) -> None:
        """降级是内联摘要,不是不给——手艺内容只写一份、运行时无关。"""
        assert capabilities.QWEN_CODE.craft_mounting is False

    def test_replay_matches_the_runtime_it_replays(self) -> None:
        assert capabilities.REPLAY.craft_mounting == capabilities.CLAUDE_CODE.craft_mounting

    def test_the_flag_is_part_of_the_published_matrix(self) -> None:
        assert "craft_mounting" in capabilities.CLAUDE_CODE.as_dict()
