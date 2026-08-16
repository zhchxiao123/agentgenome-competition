"""`agctl procedure` 子命令。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from tests.fixtures.procedures import write_broken_procedure, write_procedure

runner = CliRunner()


def _procedure(
    root: Path, procedure_id: str, *, version: str = "1.0.0", broken: bool = False
) -> None:
    if broken:
        write_broken_procedure(root, procedure_id)
    else:
        write_procedure(root, procedure_id, version=version, schema_ref=None)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    root = tmp_path / "ws"
    (root / "genome" / "procedures").mkdir(parents=True)
    return root


def _list(workspace: Path, *extra: str):
    return runner.invoke(app, ["procedure", "list", "--workspace", str(workspace), *extra])


def test_list_reports_registered_procedures(workspace: Path) -> None:
    _procedure(workspace / "genome" / "procedures", "unit-gate")

    result = _list(workspace, "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["id"] for row in payload["procedures"]] == ["unit-gate"]
    assert payload["procedures"][0]["source"] == "project"


def test_list_shows_the_override_relationship(workspace: Path, tmp_path: Path) -> None:
    """ "我明明改了全局的怎么没生效"要能一眼看出来。"""
    _procedure(tmp_path / "global", "unit-gate", version="1.0.0")
    _procedure(workspace / "genome" / "procedures", "unit-gate", version="2.0.0")

    payload = json.loads(_list(workspace, "--json").output)

    assert payload["procedures"][0]["version"] == "2.0.0"
    assert payload["procedures"][0]["overrides"] == "global"


def test_list_shows_unavailable_procedures_rather_than_hiding_them(workspace: Path) -> None:
    directory = workspace / "genome" / "procedures" / "secrets-scan"
    directory.mkdir(parents=True)
    (directory / "procedure.yaml").write_text(
        "id: secrets-scan\nversion: 1.0.0\nkind: deterministic\n"
        "tools:\n  required_cmds: [definitely-not-installed]\n"
    )
    (directory / "scripts").mkdir()
    (directory / "scripts" / "run.py").write_text("x")

    payload = json.loads(_list(workspace, "--json").output)

    assert payload["procedures"][0]["available"] is False
    assert "definitely-not-installed" in payload["procedures"][0]["unavailable_reason"]


def test_list_reports_rejected_procedures_without_failing(workspace: Path) -> None:
    """一个手滑写坏的 Procedure 不该让命令罢工,但也不该被静默吞掉。"""
    _procedure(workspace / "genome" / "procedures", "broken", broken=True)
    _procedure(workspace / "genome" / "procedures", "fine")

    result = _list(workspace, "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["id"] for row in payload["procedures"]] == ["fine"]
    assert "broken" in payload["rejected"]


def test_list_on_an_empty_workspace_is_not_an_error(workspace: Path) -> None:
    result = _list(workspace)

    assert result.exit_code == 0, result.output
    assert "没有已注册" in result.output
