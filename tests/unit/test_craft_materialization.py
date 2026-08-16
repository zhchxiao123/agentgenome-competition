"""手艺物化。

**用真实文件系统测,不 mock。** 物化就是复制文件——纯文件系统操作、无网络、确定性、
毫秒级,和 `INDEX.md` 里"明确不开缝"清单上的那些是同一类东西。给它套一层假文件系统,
测的就是那层假的了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome.genome import craft


def _write_craft(root: Path, name: str, body: str = "# 手艺\n\n照着做。\n") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / craft.CRAFT_MANIFEST).write_text(body, encoding="utf-8")
    return directory


class TestDiscover:
    def test_a_procedure_without_any_craft_is_normal_not_broken(self, tmp_path: Path) -> None:
        """冷启动时工序只靠 prompt.md 也能跑——手艺是增强不是前置依赖。"""
        assert craft.discover(tmp_path / "craft") == {}

    def test_finds_every_directory_carrying_a_manifest(self, tmp_path: Path) -> None:
        _write_craft(tmp_path, "failure-diagnosis")
        _write_craft(tmp_path, "output-discipline")

        assert sorted(craft.discover(tmp_path)) == ["failure-diagnosis", "output-discipline"]

    def test_a_directory_without_a_manifest_is_not_a_craft(self, tmp_path: Path) -> None:
        (tmp_path / "just-notes").mkdir()
        (tmp_path / "just-notes" / "README.md").write_text("x")

        assert craft.discover(tmp_path) == {}


class TestValidate:
    def test_a_craft_missing_its_manifest_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "broken").mkdir()

        errors, warnings = craft.validate(tmp_path)

        assert any("broken" in message and craft.CRAFT_MANIFEST in message for message in errors)
        assert warnings == []

    def test_an_oversized_craft_only_warns(self, tmp_path: Path) -> None:
        """行数预算是内容质量问题,不是配置错误。

        拒绝加载会让一份稍长的手艺把整个工序打挂——完全不成比例的后果。
        """
        _write_craft(tmp_path, "too-long", "行\n" * (craft.LINE_BUDGET + 5))

        errors, warnings = craft.validate(tmp_path)

        assert errors == []
        assert any("too-long" in message for message in warnings)

    def test_a_craft_within_budget_says_nothing(self, tmp_path: Path) -> None:
        _write_craft(tmp_path, "fine", "行\n" * 10)

        assert craft.validate(tmp_path) == ([], [])


class TestMaterialize:
    def test_the_craft_lands_where_the_agent_natively_looks(self, tmp_path: Path) -> None:
        source = tmp_path / "procedure" / "craft"
        _write_craft(source, "failure-diagnosis")
        workdir = tmp_path / "work"
        workdir.mkdir()

        report = craft.materialize(workdir, craft.collect(source, None, ()))

        mounted = workdir / craft.MOUNT_SUBPATH / "failure-diagnosis" / craft.CRAFT_MANIFEST
        assert mounted.is_file()
        assert report.crafts == ["failure-diagnosis"]

    def test_it_copies_rather_than_links(self, tmp_path: Path) -> None:
        """软链会让员工顺着链接改到 genome 里去,而"篡改不持久化"就没了。"""
        source = tmp_path / "procedure" / "craft"
        _write_craft(source, "one")
        workdir = tmp_path / "work"
        workdir.mkdir()

        craft.materialize(workdir, craft.collect(source, None, ()))

        mounted = workdir / craft.MOUNT_SUBPATH / "one"
        assert not mounted.is_symlink()
        assert not (mounted / craft.CRAFT_MANIFEST).is_symlink()

    def test_the_previous_job_leaves_nothing_behind(self, tmp_path: Path) -> None:
        """先清空再复制。

        残留会让"这个员工只看得到自己的手艺"在第二个 Job 之后就不成立——而那正是角色
        定制要保证的事。
        """
        first = tmp_path / "a" / "craft"
        _write_craft(first, "arch-only")
        second = tmp_path / "b" / "craft"
        _write_craft(second, "dev-only")
        workdir = tmp_path / "work"
        workdir.mkdir()

        craft.materialize(workdir, craft.collect(first, None, ()))
        craft.materialize(workdir, craft.collect(second, None, ()))

        mounted = workdir / craft.MOUNT_SUBPATH
        assert sorted(p.name for p in mounted.iterdir()) == ["dev-only"]

    def test_a_job_with_no_craft_still_clears_the_leftovers(self, tmp_path: Path) -> None:
        source = tmp_path / "a" / "craft"
        _write_craft(source, "stale")
        workdir = tmp_path / "work"
        workdir.mkdir()
        craft.materialize(workdir, craft.collect(source, None, ()))

        report = craft.materialize(workdir, {})

        assert report.crafts == []
        assert not (workdir / craft.MOUNT_SUBPATH).exists()

    def test_tampering_with_the_mounted_copy_does_not_survive(self, tmp_path: Path) -> None:
        """员工改了挂载副本也不会留下——它不入库,而且下一个 Job 会重新物化覆盖掉。"""
        source = tmp_path / "procedure" / "craft"
        _write_craft(source, "one", "原文\n")
        workdir = tmp_path / "work"
        workdir.mkdir()
        craft.materialize(workdir, craft.collect(source, None, ()))

        mounted = workdir / craft.MOUNT_SUBPATH / "one" / craft.CRAFT_MANIFEST
        mounted.write_text("员工偷偷改的\n", encoding="utf-8")

        craft.materialize(workdir, craft.collect(source, None, ()))

        assert mounted.read_text(encoding="utf-8") == "原文\n"
        assert (source / "one" / craft.CRAFT_MANIFEST).read_text(encoding="utf-8") == "原文\n"

    def test_a_mount_failure_is_reported_not_swallowed(self, tmp_path: Path) -> None:
        """静默跳过的话"手艺没挂上"会表现为"最近它好像变笨了"——最难归因的一类。"""
        source = tmp_path / "procedure" / "craft"
        _write_craft(source, "one")
        workdir = tmp_path / "work"
        workdir.mkdir()
        # 目标路径上放一个文件,复制就没法在它下面建目录。
        (workdir / ".claude").write_text("我不是目录", encoding="utf-8")

        with pytest.raises(craft.CraftMountError):
            craft.materialize(workdir, craft.collect(source, None, ()))


class TestCollect:
    def test_a_procedure_craft_is_taken_as_is(self, tmp_path: Path) -> None:
        source = tmp_path / "craft"
        _write_craft(source, "one")

        assert sorted(craft.collect(source, None, ())) == ["one"]

    def test_nothing_is_collected_when_there_is_nothing(self, tmp_path: Path) -> None:
        assert craft.collect(None, None, ()) == {}
