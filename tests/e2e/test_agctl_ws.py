"""`agctl ws` 子命令:让隔离工作区这一层能独立驱动与演示。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from tests.fixtures.mall import materialize_mall

runner = CliRunner()
TASK = "ag-20260901-001"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        app,
        [
            "init", "--local-only",
            str(root),
            "--repo",
            mall["order-service"].remote_url,
            "--repo",
            mall["inventory-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    return root


def _ws(workspace: Path, *args: str):
    return runner.invoke(app, ["ws", *args, "--workspace", str(workspace)])


def test_checkout_prints_the_worktree_path(workspace: Path) -> None:
    result = _ws(workspace, "checkout", TASK)

    assert result.exit_code == 0, result.output
    assert Path(result.output.strip().splitlines()[-1]).is_dir()


def test_diff_lists_changes_with_their_kind(workspace: Path) -> None:
    _ws(workspace, "checkout", TASK)
    worktree = Path(_ws(workspace, "checkout", TASK).output.strip().splitlines()[-1])
    (worktree / "scripts" / "new.sh").write_text("echo hi\n")

    result = _ws(workspace, "diff", TASK, "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    kinds = {entry["path"]: entry["kind"] for entry in payload["entries"]}
    assert kinds["scripts/new.sh"] == "untracked"


def test_commit_then_diff_shows_the_change_as_added(workspace: Path) -> None:
    worktree = Path(_ws(workspace, "checkout", TASK).output.strip().splitlines()[-1])
    (worktree / "scripts" / "new.sh").write_text("echo hi\n")

    commit = _ws(workspace, "commit", TASK, "-m", "feat: 加个脚本")
    assert commit.exit_code == 0, commit.output

    payload = json.loads(_ws(workspace, "diff", TASK, "--json").output)
    kinds = {entry["path"]: entry["kind"] for entry in payload["entries"]}
    assert kinds["scripts/new.sh"] == "added"


def test_pointers_lists_submodule_revisions(workspace: Path) -> None:
    result = _ws(workspace, "pointers", "--json")

    assert result.exit_code == 0, result.output
    assert set(json.loads(result.output)) == {"repos/order-service", "repos/inventory-service"}


def test_cleanup_removes_the_worktree(workspace: Path) -> None:
    worktree = Path(_ws(workspace, "checkout", TASK).output.strip().splitlines()[-1])

    result = _ws(workspace, "cleanup", TASK)

    assert result.exit_code == 0, result.output
    assert not worktree.exists()
