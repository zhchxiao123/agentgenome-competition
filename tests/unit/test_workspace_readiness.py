"""业务仓挂载是否真的能供任务 worktree 使用。"""

from pathlib import Path

from agentgenome.genome.scaffold import RepoSpec, pending_mounts, write_mount_plan
from agentgenome.space.gitcmd import ORCHESTRATOR_IDENTITY, git


def test_an_empty_git_repository_is_still_a_pending_mount(tmp_path: Path) -> None:
    """只留下 `.git` 不算挂好：没有 HEAD 的仓无法出现在任务 worktree 里。"""
    repo = RepoSpec(
        url="https://example.test/sql-db.git",
        module_id="sql-db",
        path="repos/sql-db/",
    )
    write_mount_plan(tmp_path, [repo])
    mount = tmp_path / repo.mount_point
    mount.mkdir(parents=True)
    git(mount, "init", "--initial-branch=main")

    assert pending_mounts(tmp_path) == (repo,)


def test_a_nested_repo_not_committed_as_a_gitlink_is_still_pending(tmp_path: Path) -> None:
    """业务仓有提交也不够；父仓 HEAD 看不见它时，任务 worktree 同样拿不到代码。"""
    git(tmp_path, "init", "--initial-branch=main")
    (tmp_path / "README.md").write_text("workspace\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, *ORCHESTRATOR_IDENTITY, "commit", "-m", "init workspace")
    repo = RepoSpec(
        url="https://example.test/sql-db.git",
        module_id="sql-db",
        path="repos/sql-db/",
    )
    write_mount_plan(tmp_path, [repo])
    mount = tmp_path / repo.mount_point
    mount.mkdir(parents=True)
    git(mount, "init", "--initial-branch=main")
    (mount / "README.md").write_text("sql-db\n", encoding="utf-8")
    git(mount, "add", "README.md")
    git(mount, *ORCHESTRATOR_IDENTITY, "commit", "-m", "init sql-db")

    assert pending_mounts(tmp_path) == (repo,)


def test_a_gitlink_without_submodule_metadata_is_still_pending(tmp_path: Path) -> None:
    """裸 gitlink 没有 URL，`git submodule update --init` 无法在任务 worktree 还原它。"""
    git(tmp_path, "init", "--initial-branch=main")
    repo = RepoSpec(
        url="https://example.test/sql-db.git",
        module_id="sql-db",
        path="repos/sql-db/",
    )
    write_mount_plan(tmp_path, [repo])
    mount = tmp_path / repo.mount_point
    mount.mkdir(parents=True)
    git(mount, "init", "--initial-branch=main")
    (mount / "README.md").write_text("sql-db\n", encoding="utf-8")
    git(mount, "add", "README.md")
    git(mount, *ORCHESTRATOR_IDENTITY, "commit", "-m", "init sql-db")
    git(tmp_path, "add", repo.mount_point)
    git(tmp_path, *ORCHESTRATOR_IDENTITY, "commit", "-m", "record bare gitlink")

    assert pending_mounts(tmp_path) == (repo,)
