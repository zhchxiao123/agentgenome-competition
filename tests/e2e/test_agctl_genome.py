"""`agctl genome` 子命令的端到端验收。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from tests.fixtures.mall import materialize_mall

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        app,
        [
            "init", "--local-only",
            str(root),
            "--name",
            "example-mall",
            "--repo",
            mall["order-service"].remote_url,
            "--repo",
            mall["inventory-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    return root


def test_show_rules_prints_the_effective_ruleset(workspace: Path) -> None:
    result = runner.invoke(app, ["genome", "show", "rules", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "genome/rules/**" in result.output
    assert "migrations" in result.output
    assert "max_fix_rounds" in result.output


def test_show_rules_supports_structured_output(workspace: Path) -> None:
    result = runner.invoke(
        app, ["genome", "show", "rules", "--workspace", str(workspace), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["architecture"]["max_fix_rounds"] is None
    protected = payload["protected"]["protected_paths"]
    assert {"path": "genome/rules/**", "writable_by": ["arch-employee"]} in protected
    assert {rule["id"] for rule in payload["impact"]["rules"]} == {
        "interface-schema",
        "migrations",
        "cross-module",
        "deploy-files",
    }


def test_show_rules_reflects_the_rules_file_overriding_root_config(workspace: Path) -> None:
    """打印的是"当前生效"的值,所以覆盖语义必须体现在输出里。"""
    (workspace / "genome/rules/architecture.md").write_text("```rules\nmax_fix_rounds: 9\n```\n")

    result = runner.invoke(
        app, ["genome", "show", "rules", "--workspace", str(workspace), "--json"]
    )

    payload = json.loads(result.output)
    assert payload["effective"]["max_fix_rounds"] == 9


def test_validate_also_checks_the_rule_files(workspace: Path) -> None:
    (workspace / "genome/rules/protected.yaml").write_text(
        "high_risk:\n  - {id: empty, description: 什么条件都没写}\n"
    )

    result = runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "empty" in result.output
    assert "protected.yaml" in result.output


def test_validate_passes_on_a_freshly_initialised_workspace(workspace: Path) -> None:
    result = runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
