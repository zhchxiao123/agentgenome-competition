"""自动门禁从 Workspace 控制面读取已确认规格。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agentgenome.gates.procedure_entry import _control_root


def test_a_task_worktree_resolves_the_workspace_control_checkout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    worktree = tmp_path / "task-worktree"
    workspace.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=workspace, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workspace,
        check=True,
    )
    (workspace / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=workspace, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "task/example", str(worktree)],
        cwd=workspace,
        check=True,
        capture_output=True,
    )

    assert _control_root(worktree) == workspace.resolve()
