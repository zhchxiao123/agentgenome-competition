"""`agctl forge` 子命令:让 PR 与合并能被一条命令独立驱动和演示。

只覆盖 `Forge` 协议的四个原语,不引入任务级编排语义——那属于 PRD 08 的 agctl task。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.space.local_forge import LocalForge
from tests.fixtures.mall import materialize_repo

runner = CliRunner()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path):
    """一个带 bare 远端、且已经推了一个任务分支的业务仓。"""
    materialized = materialize_repo("order-service", tmp_path / "upstream")
    _git(materialized.worktree, "checkout", "-q", "-b", "task/ag-1")
    (materialized.worktree / "feature.txt").write_text("功能内容\n")
    _git(materialized.worktree, "add", "-A")
    _git(materialized.worktree, "commit", "-m", "feat: 加个功能")
    _git(materialized.worktree, "push", "-q", "origin", "task/ag-1")
    _git(materialized.worktree, "checkout", "-q", "main")
    return materialized


def _forge(repo, *args: str):
    """本地演示走 --git-host local,与真实平台是同一条命令。"""
    return runner.invoke(app, ["forge", *args, "--repo", str(repo.remote), "--git-host", "local"])


def _create(repo, head: str = "task/ag-1") -> int:
    result = _forge(
        repo,
        "pr",
        "create",
        "--head",
        head,
        "--base",
        "main",
        "--title",
        "t",
        "--body",
        "b",
        "--json",
    )
    assert result.exit_code == 0, result.output
    return int(json.loads(result.output)["number"])


def test_pr_create_reports_the_number(repo) -> None:
    result = _forge(
        repo,
        "pr",
        "create",
        "--head",
        "task/ag-1",
        "--base",
        "main",
        "--title",
        "加个功能",
        "--body",
        "正文",
    )

    assert result.exit_code == 0, result.output
    assert "#1" in result.output


def test_pr_create_is_idempotent(repo) -> None:
    """协议要求 open_pr 幂等,命令面不能把这条性质吃掉。"""
    first = _create(repo)
    second = _create(repo)

    assert second == first


def test_pr_status_reports_state_and_title(repo) -> None:
    number = _create(repo)

    result = _forge(repo, "pr", "status", str(number), "--json")

    payload = json.loads(result.output)
    assert payload["state"] == "open"
    assert payload["title"] == "t"
    assert payload["merged_rev"] is None


def test_pr_merge_really_merges_and_reports_the_revision(repo) -> None:
    number = _create(repo)

    result = _forge(repo, "pr", "merge", str(number), "--json")

    assert result.exit_code == 0, result.output
    rev = json.loads(result.output)["merged_rev"]
    assert rev == _git(repo.remote, "rev-parse", "main")
    assert "feature.txt" in _git(repo.remote, "ls-tree", "--name-only", "main")


def test_status_after_merge_reports_merged(repo) -> None:
    number = _create(repo)
    _forge(repo, "pr", "merge", str(number))

    payload = json.loads(_forge(repo, "pr", "status", str(number), "--json").output)

    assert payload["state"] == "merged"
    assert payload["merged_rev"]


def test_merging_twice_is_idempotent(repo) -> None:
    number = _create(repo)
    first = json.loads(_forge(repo, "pr", "merge", str(number), "--json").output)["merged_rev"]
    history = _git(repo.remote, "log", "--pretty=%H", "main")

    second = _forge(repo, "pr", "merge", str(number), "--json")

    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["merged_rev"] == first
    assert _git(repo.remote, "log", "--pretty=%H", "main") == history


def test_protected_reports_branch_protection(repo) -> None:
    before = _forge(repo, "protected", "main", "--json")
    assert json.loads(before.output)["protected"] is False

    LocalForge().protect(repo.remote, "main")

    after = _forge(repo, "protected", "main", "--json")
    assert json.loads(after.output)["protected"] is True


# --- 失败路径:退出码与可读错误,不打堆栈 -------------------------------------


def test_merge_conflict_lists_the_files_and_exits_nonzero(repo) -> None:
    _git(repo.worktree, "checkout", "-q", "-b", "task/other")
    (repo.worktree / "feature.txt").write_text("冲突内容\n")
    _git(repo.worktree, "add", "-A")
    _git(repo.worktree, "commit", "-m", "feat: 冲突")
    _git(repo.worktree, "push", "-q", "origin", "task/other")
    first = _create(repo)
    second = _create(repo, head="task/other")
    _forge(repo, "pr", "merge", str(first))

    result = _forge(repo, "pr", "merge", str(second))

    assert result.exit_code != 0
    assert "feature.txt" in result.output
    assert "Traceback" not in result.output


def test_unknown_pr_gives_a_readable_error(repo) -> None:
    result = _forge(repo, "pr", "status", "999")

    assert result.exit_code != 0
    assert "999" in result.output
    assert "Traceback" not in result.output


def test_unknown_branch_gives_a_readable_error(repo) -> None:
    result = _forge(
        repo,
        "pr",
        "create",
        "--head",
        "task/ghost",
        "--base",
        "main",
        "--title",
        "t",
        "--body",
        "b",
    )

    assert result.exit_code != 0
    assert "task/ghost" in result.output
    assert "Traceback" not in result.output


def test_git_host_comes_from_the_workspace_config_by_default(tmp_path: Path, repo) -> None:
    """不给 --git-host 时按根配置的 platform.git_host 选实现,本地演示与真实平台同一条命令。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "agentgenome.yaml").write_text("platform: {git_host: local}\n")

    result = runner.invoke(
        app,
        [
            "forge",
            "pr",
            "create",
            "--head",
            "task/ag-1",
            "--base",
            "main",
            "--title",
            "t",
            "--body",
            "b",
            "--repo",
            str(repo.remote),
            "--workspace",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["number"] == 1
