"""门禁配置:三级来源与推导降级。

这一层是纯的——不跑子进程、不碰 worktree。三级叠加之后"这个模块到底跑哪几关"人是
算不清的,所以测试逐级钉死。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agentgenome.gates.config import (
    DEFAULT_SECRETS_CMD,
    GateSource,
    effective_gates,
    overall_passed,
)
from agentgenome.genome.errors import GenomeValidationError
from tests.fixtures.mini_map import write_workspace

REPO_GATES = """\
gates:
  - id: unit
    cmd: pytest -q --tb=short
  - id: lint
    cmd: ruff check .
    required: false
"""

GENOME_GATES = """\
gates:
  - id: unit
    cmd: 基因组里的命令
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return write_workspace(tmp_path / "ws")


def _repo_gates(workspace: Path, module_path: str, body: str = REPO_GATES) -> None:
    (workspace / module_path / "gates.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


def _genome_gates(workspace: Path, module_id: str, body: str = GENOME_GATES) -> None:
    directory = workspace / "genome" / "gates"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{module_id}.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


# --- 三级优先级 -------------------------------------------------------------


def test_the_repo_file_wins(workspace: Path) -> None:
    _repo_gates(workspace, "repos/order-service")
    _genome_gates(workspace, "order-service")

    effective = effective_gates(workspace, "order-service")

    assert effective.source is GateSource.REPO
    assert [gate.id for gate in effective.gates] == ["unit", "lint"]


def test_the_genome_file_wins_over_derivation(workspace: Path) -> None:
    """不想碰业务仓时用它。"""
    _genome_gates(workspace, "order-service")

    effective = effective_gates(workspace, "order-service")

    assert effective.source is GateSource.GENOME
    assert effective.gates[0].cmd == "基因组里的命令"


def test_a_higher_source_replaces_the_lower_one_entirely(workspace: Path) -> None:
    """不是逐关合并——半份来自这儿半份来自那儿,没人能推理出最终会跑什么。"""
    _repo_gates(workspace, "repos/order-service")

    effective = effective_gates(workspace, "order-service")

    assert [gate.id for gate in effective.gates] == ["unit", "lint"]
    assert "secrets" not in [gate.id for gate in effective.gates]


# --- 推导降级 ---------------------------------------------------------------


def test_a_module_with_no_config_still_gets_gates(workspace: Path) -> None:
    """这条降级路径是硬要求:它兑现"业务仓零改造"的承诺。"""
    effective = effective_gates(workspace, "order-service")

    assert effective.source is GateSource.DERIVED
    assert [gate.id for gate in effective.gates] == ["unit", "build", "secrets"]


def test_the_derived_unit_gate_uses_the_project_map_command(workspace: Path) -> None:
    unit = effective_gates(workspace, "order-service").gate("unit")

    assert unit is not None
    assert unit.cmd == "pytest -q"


def test_a_module_without_a_test_command_gets_no_unit_gate(workspace: Path) -> None:
    """没声明就是没声明。编一条命令出来跑,失败时人会以为是代码的问题。"""
    effective = effective_gates(workspace, "inventory-service")

    assert effective.gate("unit") is None
    assert "inventory-service 没有声明 test_cmd" in " ".join(effective.notes)


def test_the_derived_set_always_carries_a_required_secrets_gate(workspace: Path) -> None:
    """默认值站在安全的那一侧。工具没装的话任务会因环境类失败升级人工,那是刻意的。"""
    secrets = effective_gates(workspace, "inventory-service").gate("secrets")

    assert secrets is not None
    assert secrets.required is True
    assert secrets.cmd == DEFAULT_SECRETS_CMD


def test_the_junit_path_comes_along_from_the_project_map(workspace: Path) -> None:
    """不往用户写的 test_cmd 上拼 `--junitxml`——那太脆。位置由地图声明。"""
    assert effective_gates(workspace, "order-service").junit_xml_path == "reports/junit.xml"


# --- 校验 -------------------------------------------------------------------


def test_required_defaults_to_true(workspace: Path) -> None:
    """写了一半的配置不该是"都不阻塞"。"""
    _repo_gates(workspace, "repos/order-service", "gates:\n  - id: unit\n    cmd: pytest\n")

    assert effective_gates(workspace, "order-service").gate("unit").required is True


def test_an_unknown_field_is_rejected(workspace: Path) -> None:
    _repo_gates(
        workspace, "repos/order-service", "gates:\n  - id: unit\n    cmd: pytest\n    surprise: 1\n"
    )

    with pytest.raises(GenomeValidationError) as excinfo:
        effective_gates(workspace, "order-service")

    assert "surprise" in excinfo.value.render()


def test_all_problems_are_reported_at_once(workspace: Path) -> None:
    _repo_gates(workspace, "repos/order-service", "gates:\n  - cmd: pytest\n  - id: lint\n")

    with pytest.raises(GenomeValidationError) as excinfo:
        effective_gates(workspace, "order-service")

    assert len(excinfo.value.issues) >= 2


def test_duplicate_gate_ids_are_rejected(workspace: Path) -> None:
    """同 id 两关时"哪一关挂了"这个问题没有答案。"""
    _repo_gates(
        workspace,
        "repos/order-service",
        "gates:\n  - id: unit\n    cmd: a\n  - id: unit\n    cmd: b\n",
    )

    with pytest.raises(GenomeValidationError):
        effective_gates(workspace, "order-service")


def test_an_unknown_module_lists_what_the_map_has(workspace: Path) -> None:
    with pytest.raises(LookupError) as excinfo:
        effective_gates(workspace, "ghost")

    assert "order-service" in str(excinfo.value)


# --- 判定 -------------------------------------------------------------------


def test_all_required_gates_passing_means_the_whole_thing_passed(workspace: Path) -> None:
    gates = effective_gates(workspace, "order-service").gates

    assert overall_passed(gates, {"unit": True, "build": True, "secrets": True}) is True


def test_an_optional_gate_failing_does_not_block(workspace: Path) -> None:
    """覆盖率这类指标先观察不阻塞走的就是它。"""
    _repo_gates(workspace, "repos/order-service")
    gates = effective_gates(workspace, "order-service").gates

    assert overall_passed(gates, {"unit": True, "lint": False}) is True


def test_a_required_gate_failing_blocks(workspace: Path) -> None:
    _repo_gates(workspace, "repos/order-service")
    gates = effective_gates(workspace, "order-service").gates

    assert overall_passed(gates, {"unit": False, "lint": True}) is False


def test_a_required_gate_with_no_result_is_not_treated_as_passed(workspace: Path) -> None:
    """没跑与跑过了必须分得开。缺结果当成通过的话,漏跑一关会静默放行。"""
    gates = effective_gates(workspace, "order-service").gates

    assert overall_passed(gates, {"unit": True}) is False


# --- 这一层是纯的 -----------------------------------------------------------


def test_reading_the_config_runs_nothing(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """配置层跑子进程的话,`agctl gate show` 就会有副作用。"""
    import subprocess

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("配置层不该跑子进程")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)

    effective_gates(workspace, "order-service")


def test_an_unknown_parser_is_rejected_at_validation_time(workspace: Path) -> None:
    """跑到一半才发现解析器不存在的话,这一关的失败会被当成解析器的问题。"""
    _repo_gates(
        workspace,
        "repos/order-service",
        "gates:\n  - id: unit\n    cmd: pytest\n    parser: nope\n",
    )

    with pytest.raises(GenomeValidationError) as excinfo:
        effective_gates(workspace, "order-service")

    assert "nope" in excinfo.value.render()


def test_a_known_parser_passes_validation(workspace: Path) -> None:
    _repo_gates(
        workspace,
        "repos/order-service",
        "gates:\n  - id: unit\n    cmd: pytest\n    parser: junit\n",
    )

    assert effective_gates(workspace, "order-service").gate("unit").parser == "junit"
