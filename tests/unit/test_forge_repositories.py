from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome.jobs.orchestrator import (
    WorkspaceRemoteMissing,
    submodule_forge_repositories,
    workspace_forge_repository,
)
from agentgenome.space.gitcmd import published_branch_exists
from tests.fixtures.git import commit_all, git


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / ".gitmodules").write_text(
        """[submodule \"services/api\"]
\tpath = services/api
\turl = https://github.com/acme/api.git
""",
        encoding="utf-8",
    )
    module = root / "services" / "api"
    module.mkdir(parents=True)
    git(module, "init", "-b", "main")
    git(module, "remote", "add", "origin", "https://github.com/acme/api.git")
    return root


def test_hosted_forge_uses_local_checkouts_instead_of_turning_urls_into_paths(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    git(root, "remote", "add", "origin", "https://github.com/acme/workspace.git")

    assert submodule_forge_repositories(root, "github") == {
        "services/api": root / "services" / "api"
    }
    assert workspace_forge_repository(root, "github") == root


def test_local_forge_keeps_using_filesystem_remotes(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    module_remote = tmp_path / "api.git"
    workspace_remote = tmp_path / "workspace.git"
    git(tmp_path, "init", "--bare", str(module_remote))
    git(tmp_path, "init", "--bare", str(workspace_remote))
    git(root, "config", "-f", ".gitmodules", "submodule.services/api.url", str(module_remote))
    git(root, "remote", "add", "origin", str(workspace_remote))

    assert submodule_forge_repositories(root, "local") == {
        "services/api": module_remote
    }
    assert workspace_forge_repository(root, "local") == workspace_remote


def test_hosted_forge_reports_a_missing_workspace_origin_before_opening_prs(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)

    with pytest.raises(WorkspaceRemoteMissing, match="顶层 Workspace 缺少 origin"):
        workspace_forge_repository(root, "github")


def test_published_branch_is_found_on_origin_when_checkout_has_no_local_branch(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    module = root / "services" / "api"
    remote = tmp_path / "api.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(module, "remote", "set-url", "origin", str(remote))
    (module / "README.md").write_text("main\n", encoding="utf-8")
    commit_all(module, "initial")
    git(module, "push", "-u", "origin", "main")
    git(module, "checkout", "-b", "task/ag-0001")
    (module / "README.md").write_text("task\n", encoding="utf-8")
    commit_all(module, "task")
    git(module, "push", "origin", "task/ag-0001")
    git(module, "checkout", "main")
    git(module, "branch", "-D", "task/ag-0001")

    assert published_branch_exists(module, "task/ag-0001") is True
