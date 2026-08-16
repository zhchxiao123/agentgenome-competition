"""LocalForge:对本地 bare 仓库的真实现,不是 mock。

它跑真实的 git 合并,只是没有网络。全仓后续所有涉及 PR 与合并的测试都建在它上面,
所以它的行为忠实度直接决定了那些测试的可信度——按生产代码要求写。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentgenome.space.forge import MergeConflict, PRNotFound, PRState
from agentgenome.space.local_forge import LocalForge
from tests.fixtures.mall import materialize_repo


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
    return materialize_repo("order-service", tmp_path / "upstream")


@pytest.fixture
def forge() -> LocalForge:
    return LocalForge()


def _push_branch(repo, branch: str, filename: str, content: str = "新内容\n") -> str:
    """在业务仓上开一个分支、改点东西、推到远端。"""
    _git(repo.worktree, "checkout", "-q", "-b", branch)
    (repo.worktree / filename).write_text(content)
    _git(repo.worktree, "add", "-A")
    _git(repo.worktree, "commit", "-m", f"feat: {filename}")
    _git(repo.worktree, "push", "-q", "origin", branch)
    rev = _git(repo.worktree, "rev-parse", "HEAD")
    _git(repo.worktree, "checkout", "-q", "main")
    return rev


# --- 开 PR ------------------------------------------------------------------


def test_open_pr_returns_a_reference(repo, forge: LocalForge) -> None:
    _push_branch(repo, "task/ag-1", "feature.txt")

    pr = forge.open_pr(repo.remote, head="task/ag-1", base="main", title="加个功能", body="正文")

    assert pr.number > 0
    assert pr.head == "task/ag-1"
    assert pr.base == "main"


def test_pr_status_reports_the_open_pr(repo, forge: LocalForge) -> None:
    _push_branch(repo, "task/ag-1", "feature.txt")
    pr = forge.open_pr(repo.remote, head="task/ag-1", base="main", title="t", body="b")

    status = forge.pr_status(pr)

    assert status.state is PRState.OPEN
    assert status.title == "t"


def test_pr_metadata_survives_a_new_forge_instance(repo, forge: LocalForge) -> None:
    """PR 元数据存在 bare 仓的引用里,不是进程内状态——崩溃恢复要能读回来。"""
    _push_branch(repo, "task/ag-1", "feature.txt")
    pr = forge.open_pr(repo.remote, head="task/ag-1", base="main", title="t", body="b")

    assert LocalForge().pr_status(pr).state is PRState.OPEN


def test_open_pr_on_the_same_branch_twice_returns_the_same_pr(repo, forge: LocalForge) -> None:
    """崩溃恢复会重放,重复开 PR 不能造出两个。"""
    _push_branch(repo, "task/ag-1", "feature.txt")

    first = forge.open_pr(repo.remote, head="task/ag-1", base="main", title="t", body="b")
    second = forge.open_pr(repo.remote, head="task/ag-1", base="main", title="t", body="b")

    assert second.number == first.number


def test_open_pr_rejects_an_unknown_head(repo, forge: LocalForge) -> None:
    with pytest.raises(PRNotFound):
        forge.open_pr(repo.remote, head="task/ghost", base="main", title="t", body="b")


# --- 合并 -------------------------------------------------------------------


def test_merge_pr_really_merges_into_the_base(repo, forge: LocalForge) -> None:
    _push_branch(repo, "task/ag-1", "feature.txt", "功能内容\n")
    pr = forge.open_pr(repo.remote, head="task/ag-1", base="main", title="t", body="b")

    rev = forge.merge_pr(pr)

    assert rev == _git(repo.remote, "rev-parse", "main")
    # 合并后基线上真的有那个文件——这是"真实现而非 mock"的验收点。
    assert "feature.txt" in _git(repo.remote, "ls-tree", "--name-only", "main")


def test_merged_pr_reports_its_state_and_revision(repo, forge: LocalForge) -> None:
    _push_branch(repo, "task/ag-1", "feature.txt")
    pr = forge.open_pr(repo.remote, head="task/ag-1", base="main", title="t", body="b")

    rev = forge.merge_pr(pr)
    status = forge.pr_status(pr)

    assert status.state is PRState.MERGED
    assert status.merged_rev == rev


def test_merge_pr_is_idempotent(repo, forge: LocalForge) -> None:
    """双层提交拓扑要能中断重放:已合并的 PR 再合一次必须返回同一结果而非报错。"""
    _push_branch(repo, "task/ag-1", "feature.txt")
    pr = forge.open_pr(repo.remote, head="task/ag-1", base="main", title="t", body="b")

    first = forge.merge_pr(pr)
    history_after_first = _git(repo.remote, "log", "--pretty=%H", "main")

    second = forge.merge_pr(pr)

    assert second == first
    # 第二次不能再造一个合并提交。
    assert _git(repo.remote, "log", "--pretty=%H", "main") == history_after_first


def test_merge_conflict_returns_the_conflicting_files(repo, forge: LocalForge) -> None:
    """冲突要给出结构化结果与冲突文件清单,而不是抛一个裸异常。"""
    _push_branch(repo, "task/a", "shared.txt", "来自 A\n")
    _push_branch(repo, "task/b", "shared.txt", "来自 B\n")
    pr_a = forge.open_pr(repo.remote, head="task/a", base="main", title="a", body="")
    pr_b = forge.open_pr(repo.remote, head="task/b", base="main", title="b", body="")
    forge.merge_pr(pr_a)

    with pytest.raises(MergeConflict) as excinfo:
        forge.merge_pr(pr_b)

    assert excinfo.value.files == ["shared.txt"]
    assert forge.pr_status(pr_b).state is PRState.OPEN


def test_a_conflicted_merge_leaves_the_base_untouched(repo, forge: LocalForge) -> None:
    _push_branch(repo, "task/a", "shared.txt", "来自 A\n")
    _push_branch(repo, "task/b", "shared.txt", "来自 B\n")
    pr_a = forge.open_pr(repo.remote, head="task/a", base="main", title="a", body="")
    pr_b = forge.open_pr(repo.remote, head="task/b", base="main", title="b", body="")
    forge.merge_pr(pr_a)
    before = _git(repo.remote, "rev-parse", "main")

    with pytest.raises(MergeConflict):
        forge.merge_pr(pr_b)

    assert _git(repo.remote, "rev-parse", "main") == before


def test_merging_an_unknown_pr_raises(repo, forge: LocalForge) -> None:
    _push_branch(repo, "task/ag-1", "feature.txt")
    pr = forge.open_pr(repo.remote, head="task/ag-1", base="main", title="t", body="b")
    ghost = pr.__class__(repo=pr.repo, number=999, head="x", base="main")

    with pytest.raises(PRNotFound):
        forge.merge_pr(ghost)


# --- 保护分支 ---------------------------------------------------------------


def test_branches_are_unprotected_by_default(repo, forge: LocalForge) -> None:
    assert forge.is_protected(repo.remote, "main") is False


def test_protecting_a_branch_is_reported(repo, forge: LocalForge) -> None:
    """主分支保护是平台强制,不是约定。员工的 git 身份无权直接 push。"""
    forge.protect(repo.remote, "main")

    assert forge.is_protected(repo.remote, "main") is True


def test_merging_a_pr_into_a_protected_branch_is_allowed(repo, forge: LocalForge) -> None:
    """分支保护挡的是直接 push;合并 PR 恰恰是变更合进受保护 main 的正当途径。"""
    _push_branch(repo, "task/ag-1", "feature.txt")
    forge.protect(repo.remote, "main")
    pr = forge.open_pr(repo.remote, head="task/ag-1", base="main", title="t", body="b")

    rev = forge.merge_pr(pr)

    assert rev == _git(repo.remote, "rev-parse", "main")


def test_protection_survives_a_new_forge_instance(repo, forge: LocalForge) -> None:
    forge.protect(repo.remote, "main")

    assert LocalForge().is_protected(repo.remote, "main") is True
