"""`agctl procedure validate`:写新 Procedure 时的反馈回路。"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agentgenome.cli import app
from tests.fixtures.procedures import write_agentic_procedure, write_procedure

runner = CliRunner()


def _validate(path: Path, *extra: str):
    return runner.invoke(app, ["procedure", "validate", str(path), *extra])


def test_a_valid_procedure_exits_zero(tmp_path: Path) -> None:
    fixture = write_procedure(tmp_path, "unit-gate")

    result = _validate(fixture.path)

    assert result.exit_code == 0, result.output
    assert "unit-gate@1.0.0" in result.output


def test_json_output_reports_the_essentials(tmp_path: Path) -> None:
    fixture = write_procedure(tmp_path, "unit-gate")

    payload = json.loads(_validate(fixture.path, "--json").output)

    assert payload == {"id": "unit-gate", "version": "1.0.0", "kind": "deterministic"}


def test_an_invalid_procedure_exits_nonzero_and_lists_the_problems(tmp_path: Path) -> None:
    """没有这条反馈回路的话,写错只能等到编排器启动时才知道。"""
    fixture = write_procedure(tmp_path, "unit-gate", version="nope")

    result = _validate(fixture.path)

    assert result.exit_code != 0
    assert "semver" in result.output
    assert "Traceback" not in result.output


def test_all_problems_are_listed_in_one_run(tmp_path: Path) -> None:
    """pydantic 层与语义层的问题要一起报出来,不是修完一批再跑一次才看到下一批。"""
    fixture = write_procedure(tmp_path, "unit-gate", version="nope")
    (fixture.path / "schemas" / "out.json").unlink()

    result = _validate(fixture.path)

    assert "semver" in result.output
    assert "schemas/out.json" in result.output


def test_a_deterministic_procedure_without_the_entry_point_is_rejected(tmp_path: Path) -> None:
    """校验放过它的话,派发时才发现找不到入口——那正是这条命令要提前拦住的。"""
    fixture = write_procedure(tmp_path, "unit-gate", script=None)
    (fixture.path / "scripts").mkdir(exist_ok=True)
    (fixture.path / "scripts" / "gate.sh").write_text("echo hi\n")

    result = _validate(fixture.path)

    assert result.exit_code != 0
    assert "run.py" in result.output


def test_an_agentic_procedure_without_a_prompt_is_rejected(tmp_path: Path) -> None:
    fixture = write_agentic_procedure(tmp_path, "code-develop")
    (fixture.path / "prompt.md").unlink()

    result = _validate(fixture.path)

    assert result.exit_code != 0
    assert "prompt.md" in result.output


def test_a_missing_directory_is_a_readable_error(tmp_path: Path) -> None:
    result = _validate(tmp_path / "does-not-exist")

    assert result.exit_code != 0
    assert "procedure.yaml" in result.output
    assert "Traceback" not in result.output
