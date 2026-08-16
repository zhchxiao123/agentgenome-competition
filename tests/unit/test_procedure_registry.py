"""Procedure 注册表:两级来源、覆盖、可用性与冻结。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentgenome.genome.procedures import ProcedureSource, load_registry
from tests.fixtures.procedures import write_broken_procedure, write_procedure


def _write_procedure(root: Path, procedure_id: str, *, broken: bool = False, **kwargs: Any) -> Path:
    if broken:
        return write_broken_procedure(root, procedure_id).path
    return write_procedure(root, procedure_id, **kwargs).path


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """项目级与全局级两个来源。"""
    project = tmp_path / "ws" / "genome" / "procedures"
    global_ = tmp_path / "home" / "procedures"
    project.mkdir(parents=True)
    global_.mkdir(parents=True)
    return project, global_


# --- 基本加载 ---------------------------------------------------------------


def test_loads_procedures_from_the_project_source(roots: tuple[Path, Path]) -> None:
    project, global_ = roots
    _write_procedure(project, "unit-gate")

    registry = load_registry(project, global_)

    assert [spec.id for spec in registry.all()] == ["unit-gate"]
    assert registry.get("unit-gate").source is ProcedureSource.PROJECT


def test_loads_procedures_from_the_global_source(roots: tuple[Path, Path]) -> None:
    project, global_ = roots
    _write_procedure(global_, "contract-test")

    registry = load_registry(project, global_)

    assert registry.get("contract-test").source is ProcedureSource.GLOBAL


def test_missing_source_directories_are_not_an_error(tmp_path: Path) -> None:
    """新 Workspace 还没有 Procedure,这是正常状态而非错误。"""
    registry = load_registry(tmp_path / "nope", tmp_path / "also-nope")

    assert registry.all() == ()


def test_a_non_directory_entry_is_ignored(roots: tuple[Path, Path]) -> None:
    project, global_ = roots
    (project / "README.md").write_text("这不是一个 Procedure\n")
    _write_procedure(project, "unit-gate")

    assert [spec.id for spec in load_registry(project, global_).all()] == ["unit-gate"]


# --- 项目级覆盖全局级 -------------------------------------------------------


def test_project_procedure_overrides_the_global_one(roots: tuple[Path, Path]) -> None:
    """项目可以定制能力而不必 fork 全局的。"""
    project, global_ = roots
    _write_procedure(global_, "unit-gate", version="1.0.0", summary="全局版")
    _write_procedure(project, "unit-gate", version="2.0.0", summary="项目定制版")

    spec = load_registry(project, global_).get("unit-gate")

    # 生效的是项目级那份的**内容**,不只是 id 对上了。
    assert spec.version == "2.0.0"
    assert spec.summary == "项目定制版"
    assert spec.source is ProcedureSource.PROJECT


def test_an_override_is_recorded(roots: tuple[Path, Path]) -> None:
    """ "我明明改了全局的怎么没生效"不该变成一次漫长的排查。"""
    project, global_ = roots
    _write_procedure(global_, "unit-gate")
    _write_procedure(project, "unit-gate")

    registry = load_registry(project, global_)

    assert "unit-gate" in registry.overrides


def test_no_override_is_recorded_when_ids_do_not_collide(roots: tuple[Path, Path]) -> None:
    project, global_ = roots
    _write_procedure(global_, "contract-test")
    _write_procedure(project, "unit-gate")

    assert load_registry(project, global_).overrides == {}


# --- 单个坏 Procedure 不影响整体 ------------------------------------------------


def test_a_broken_procedure_is_skipped_and_the_rest_still_load(roots: tuple[Path, Path]) -> None:
    """一个手滑写坏的实验性 Procedure 不该让编排器罢工。"""
    project, global_ = roots
    _write_procedure(project, "broken-one", broken=True)
    _write_procedure(project, "good-one")

    registry = load_registry(project, global_)

    assert [spec.id for spec in registry.all()] == ["good-one"]


def test_a_broken_procedure_is_reported_with_its_reason(roots: tuple[Path, Path]) -> None:
    project, global_ = roots
    _write_procedure(project, "broken-one", broken=True)

    registry = load_registry(project, global_)

    assert "broken-one" in registry.rejected
    assert "semver" in registry.rejected["broken-one"]


# --- 缺依赖:注册但不可用 ---------------------------------------------------


def test_a_procedure_missing_its_commands_stays_registered_but_unavailable(
    roots: tuple[Path, Path],
) -> None:
    """直接不出现在列表里的话,`list` 会显示"没有这个能力",而真相是"环境缺依赖"。

    后者能直接行动,前者只会让人去翻代码找它为什么消失了。
    """
    project, global_ = roots
    _write_procedure(project, "secrets-scan", required_cmds=["definitely-not-installed"])

    spec = load_registry(project, global_).get("secrets-scan")

    assert spec.available is False
    assert "definitely-not-installed" in (spec.unavailable_reason or "")


def test_a_procedure_with_available_commands_is_available(roots: tuple[Path, Path]) -> None:
    project, global_ = roots
    _write_procedure(project, "python-gate", required_cmds=["python3"])

    assert load_registry(project, global_).get("python-gate").available is True


def test_unavailable_procedures_are_listed_separately(roots: tuple[Path, Path]) -> None:
    project, global_ = roots
    _write_procedure(project, "ok-one")
    _write_procedure(project, "missing-dep", required_cmds=["definitely-not-installed"])

    registry = load_registry(project, global_)

    assert [spec.id for spec in registry.available()] == ["ok-one"]
    assert len(registry.all()) == 2


# --- 冻结 -------------------------------------------------------------------


def test_the_registry_exposes_immutable_views(roots: tuple[Path, Path]) -> None:
    """任务跑到一半能力定义发生变化,是审计噩梦——事件流里记的版本会对不上实际跑的。"""
    project, global_ = roots
    _write_procedure(project, "unit-gate")
    registry = load_registry(project, global_)

    assert isinstance(registry.all(), tuple)
    with pytest.raises(TypeError):
        registry.overrides["unit-gate"] = ProcedureSource.GLOBAL  # type: ignore[index]
    with pytest.raises(TypeError):
        registry.rejected["unit-gate"] = "乱写"  # type: ignore[index]


def test_writing_a_new_procedure_to_disk_does_not_change_a_loaded_registry(
    roots: tuple[Path, Path],
) -> None:
    project, global_ = roots
    _write_procedure(project, "unit-gate")
    registry = load_registry(project, global_)

    _write_procedure(project, "added-later")

    assert [spec.id for spec in registry.all()] == ["unit-gate"]


# --- 查询 -------------------------------------------------------------------


def test_getting_an_unknown_procedure_lists_what_is_registered(roots: tuple[Path, Path]) -> None:
    project, global_ = roots
    _write_procedure(project, "unit-gate")

    with pytest.raises(KeyError) as excinfo:
        load_registry(project, global_).get("ghost")

    assert "unit-gate" in str(excinfo.value)


def test_procedures_are_listed_in_a_stable_order(roots: tuple[Path, Path]) -> None:
    """顺序不稳定的话,`list` 的输出没法用来 diff。"""
    project, global_ = roots
    for procedure_id in ("zebra", "alpha", "middle"):
        _write_procedure(project, procedure_id)

    assert [spec.id for spec in load_registry(project, global_).all()] == [
        "alpha",
        "middle",
        "zebra",
    ]
