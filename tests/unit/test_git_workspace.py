"""隔离工作区:在真实 git 仓上跑真实 git 命令,不 mock。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.space.git_ws import GitWorkspace
from agentgenome.space.gitcmd import git_out
from agentgenome.space.workspace import ChangeKind
from tests.fixtures.mall import materialize_mall

runner = CliRunner()

TASK = "ag-20260901-001"


@pytest.fixture
def workspace(tmp_path: Path) -> GitWorkspace:
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
    return GitWorkspace(root, worktrees_home=tmp_path / "worktrees")


def two_git_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for root in (first_root, second_root):
        root.mkdir()
        (root / "README.md").write_text(f"{root.name}\n", encoding="utf-8")
        git_out(root, "init", "--initial-branch=main")
        git_out(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "README.md")
        git_out(
            root,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-m",
            "initial",
        )
    return first_root, second_root, tmp_path / "shared-worktrees"


# --- 隔离工作区 --------------------------------------------------------------


def test_checkout_isolated_creates_a_worktree_on_a_task_branch(workspace: GitWorkspace) -> None:
    path = workspace.checkout_isolated(TASK)

    assert path.is_dir()
    assert path != workspace.root
    assert git_out(path, "rev-parse", "--abbrev-ref", "HEAD") == f"task/{TASK}"


def test_isolated_worktree_lives_outside_the_main_checkout(workspace: GitWorkspace) -> None:
    """物理隔离:员工在自己的目录里干活,碰不到主 checkout。"""
    path = workspace.checkout_isolated(TASK)

    assert workspace.root not in path.parents


def test_checkout_isolated_is_idempotent(workspace: GitWorkspace) -> None:
    """崩溃恢复会重放处理器,重复建工作区必须是安全的。"""
    first = workspace.checkout_isolated(TASK)
    (first / "note.txt").write_text("已经干了一半的活")

    second = workspace.checkout_isolated(TASK)

    assert second == first
    assert (second / "note.txt").read_text() == "已经干了一半的活"


def test_same_task_id_in_two_workspaces_gets_two_isolated_worktrees(tmp_path: Path) -> None:
    first_root, second_root, shared = two_git_roots(tmp_path)
    first = GitWorkspace(first_root, worktrees_home=shared)
    second = GitWorkspace(second_root, worktrees_home=shared)

    first_tree = first.checkout_isolated(TASK)
    second_tree = second.checkout_isolated(TASK)

    assert first_tree != second_tree
    assert (first_tree / "README.md").read_text(encoding="utf-8") == "first\n"
    assert (second_tree / "README.md").read_text(encoding="utf-8") == "second\n"


def test_same_session_id_in_two_workspaces_gets_two_isolated_worktrees(tmp_path: Path) -> None:
    first_root, second_root, shared = two_git_roots(tmp_path)
    first = GitWorkspace(first_root, worktrees_home=shared)
    second = GitWorkspace(second_root, worktrees_home=shared)

    first_tree = first.checkout_session("sess-20260901-001")
    second_tree = second.checkout_session("sess-20260901-001")

    assert first_tree != second_tree
    assert (first_tree / "README.md").read_text(encoding="utf-8") == "first\n"
    assert (second_tree / "README.md").read_text(encoding="utf-8") == "second\n"


def test_cleanup_never_removes_same_task_from_another_workspace(tmp_path: Path) -> None:
    first_root, second_root, shared = two_git_roots(tmp_path)
    first = GitWorkspace(first_root, worktrees_home=shared)
    second = GitWorkspace(second_root, worktrees_home=shared)
    first_tree = first.checkout_isolated(TASK)
    second_tree = second.checkout_isolated(TASK)

    second.cleanup(TASK)

    assert first_tree.is_dir()
    assert not second_tree.exists()


def test_legacy_flat_worktree_is_reused_only_by_its_owner(tmp_path: Path) -> None:
    first_root, second_root, shared = two_git_roots(tmp_path)
    shared.mkdir()
    legacy = shared / TASK
    git_out(first_root, "worktree", "add", "-b", f"task/{TASK}", str(legacy), "HEAD")
    first = GitWorkspace(first_root, worktrees_home=shared)
    second = GitWorkspace(second_root, worktrees_home=shared)

    assert first.worktree_path(TASK) == legacy
    assert second.worktree_path(TASK) != legacy

    second.cleanup(TASK)

    assert legacy.is_dir()


def test_branch_name_carries_an_optional_slug(workspace: GitWorkspace) -> None:
    path = workspace.checkout_isolated(TASK, slug="add-coupon")

    assert git_out(path, "rev-parse", "--abbrev-ref", "HEAD") == f"task/{TASK}/add-coupon"


def test_cleanup_removes_the_worktree_and_leaves_main_checkout_intact(
    workspace: GitWorkspace,
) -> None:
    path = workspace.checkout_isolated(TASK)
    before = git_out(workspace.root, "rev-parse", "HEAD")

    workspace.cleanup(TASK)

    assert not path.exists()
    assert TASK not in git_out(workspace.root, "worktree", "list")
    assert git_out(workspace.root, "rev-parse", "HEAD") == before
    assert git_out(workspace.root, "status", "--porcelain") == ""


def test_cleanup_can_drop_the_branch_too(workspace: GitWorkspace) -> None:
    workspace.checkout_isolated(TASK)

    workspace.cleanup(TASK, delete_branch=True)

    assert f"task/{TASK}" not in git_out(workspace.root, "branch", "--list")


def test_cleanup_of_an_unknown_task_is_a_no_op(workspace: GitWorkspace) -> None:
    workspace.cleanup("ag-never-existed")


# --- ChangeSet 的六类改动形态 ------------------------------------------------


def test_diff_reports_added_files(workspace: GitWorkspace) -> None:
    path = workspace.checkout_isolated(TASK)
    (path / "scripts" / "new.sh").write_text("echo hi\n")
    workspace.commit(TASK, "feat: 加个脚本")

    changes = workspace.diff(TASK)

    assert changes.paths() == {"scripts/new.sh"}
    assert changes.entry("scripts/new.sh").kind is ChangeKind.ADDED


def test_diff_reports_modified_files(workspace: GitWorkspace) -> None:
    path = workspace.checkout_isolated(TASK)
    (path / "agentgenome.yaml").write_text("concurrency: {global_jobs: 9}\n")
    workspace.commit(TASK, "chore: 调并发")

    changes = workspace.diff(TASK)

    assert changes.entry("agentgenome.yaml").kind is ChangeKind.MODIFIED


def test_diff_reports_deleted_files(workspace: GitWorkspace) -> None:
    path = workspace.checkout_isolated(TASK)
    (path / "genome" / "rules" / "coding.md").unlink()
    workspace.commit(TASK, "chore: 删掉编码规范")

    changes = workspace.diff(TASK)

    assert changes.entry("genome/rules/coding.md").kind is ChangeKind.DELETED


def test_diff_reports_renames_as_renames_not_add_plus_delete(workspace: GitWorkspace) -> None:
    """重命名是最容易被漏掉的一类,漏了会让越权检查误判。"""
    path = workspace.checkout_isolated(TASK)
    (path / "genome" / "rules" / "coding.md").rename(path / "genome" / "rules" / "style.md")
    workspace.commit(TASK, "chore: 改名")

    changes = workspace.diff(TASK)

    entry = changes.entry("genome/rules/style.md")
    assert entry.kind is ChangeKind.RENAMED
    assert entry.old_path == "genome/rules/coding.md"
    # 两端路径都要出现在受影响集合里——越权检查两边都得看。
    assert "genome/rules/coding.md" in changes.touched_paths()
    assert "genome/rules/style.md" in changes.touched_paths()


def test_diff_reports_untracked_files(workspace: GitWorkspace) -> None:
    """员工新写的文件还没 add,但它已经改变了工作区——越权检查必须看得见。"""
    path = workspace.checkout_isolated(TASK)
    (path / "scripts" / "sneaky.sh").write_text("echo 我还没被 add\n")

    changes = workspace.diff(TASK)

    assert changes.entry("scripts/sneaky.sh").kind is ChangeKind.UNTRACKED


def test_diff_reports_submodule_pointer_moves(workspace: GitWorkspace) -> None:
    """子模块指针变化在 porcelain 里长得跟普通修改一样,必须单独识别出来。"""
    path = workspace.checkout_isolated(TASK)
    submodule = path / "repos/order-service"
    (submodule / "README.md").write_text("业务仓里的新提交\n")
    git_out(submodule, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git_out(submodule, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "feat: x")

    changes = workspace.diff(TASK)

    entry = changes.entry("repos/order-service")
    assert entry.kind is ChangeKind.SUBMODULE
    assert changes.submodule_paths() == {"repos/order-service"}


def test_diff_counts_deleted_lines_against_the_baseline(workspace: GitWorkspace) -> None:
    """删除量是高风险评级的内置判据之一,算的是相对基线的净删除。"""
    path = workspace.checkout_isolated(TASK)
    target = path / "genome" / "rules" / "coding.md"
    baseline_lines = len(target.read_text().splitlines())
    assert baseline_lines > 0
    target.unlink()
    workspace.commit(TASK, "chore: 删掉编码规范")

    changes = workspace.diff(TASK)

    assert changes.deleted_lines == baseline_lines


def test_diff_is_empty_on_a_fresh_worktree(workspace: GitWorkspace) -> None:
    workspace.checkout_isolated(TASK)

    assert workspace.diff(TASK).paths() == set()


# --- 提交 --------------------------------------------------------------------


def test_commit_records_a_new_revision(workspace: GitWorkspace) -> None:
    path = workspace.checkout_isolated(TASK)
    before = git_out(path, "rev-parse", "HEAD")
    (path / "scripts" / "a.sh").write_text("a\n")

    rev = workspace.commit(TASK, "feat: a")

    assert rev != before
    assert rev == git_out(path, "rev-parse", "HEAD")


def test_commit_can_be_scoped_to_specific_paths(workspace: GitWorkspace) -> None:
    path = workspace.checkout_isolated(TASK)
    (path / "scripts" / "wanted.sh").write_text("yes\n")
    (path / "scripts" / "unwanted.sh").write_text("no\n")

    workspace.commit(TASK, "feat: 只提交一个", paths=["scripts/wanted.sh"])

    # 两者都还在变更集里(diff 是相对基线的),区别在于一个已提交、一个仍未跟踪。
    changes = workspace.diff(TASK)
    assert changes.entry("scripts/wanted.sh").kind is ChangeKind.ADDED
    assert changes.entry("scripts/unwanted.sh").kind is ChangeKind.UNTRACKED


def test_commit_with_nothing_staged_returns_the_current_revision(
    workspace: GitWorkspace,
) -> None:
    """幂等前提:处理器重放时没有新改动,不该炸也不该造空提交。"""
    path = workspace.checkout_isolated(TASK)
    before = git_out(path, "rev-parse", "HEAD")

    assert workspace.commit(TASK, "chore: 无事发生") == before


# --- 子模块指针 --------------------------------------------------------------


def test_submodule_pointers_lists_每个模块指向的版本(workspace: GitWorkspace) -> None:
    pointers = workspace.submodule_pointers()

    assert set(pointers) == {"repos/order-service", "repos/inventory-service"}
    assert all(len(rev) == 40 for rev in pointers.values())


def test_advance_submodule_moves_the_gitlink(workspace: GitWorkspace) -> None:
    path = workspace.checkout_isolated(TASK)
    submodule = path / "repos/order-service"
    (submodule / "README.md").write_text("新提交\n")
    git_out(submodule, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git_out(submodule, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "feat: x")
    target = git_out(submodule, "rev-parse", "HEAD")
    before = workspace.submodule_pointers(TASK)["repos/order-service"]

    workspace.advance_submodule(TASK, "repos/order-service", target)

    after = workspace.submodule_pointers(TASK)["repos/order-service"]
    assert after != before
    assert after == target


def test_protected_paths_come_from_the_rules_layer(workspace: GitWorkspace) -> None:
    assert "genome/rules/**" in workspace.protected_paths()


# --- 槽:(task_id, slot) 这把键 -----------------------------------------------
#
# 一个任务可以同时有多个工作树(并行节点各一个、多方案尝试各一个)。键先立后用:
# 等到并行执行器落地时再改这一层,等于把"重构空间层"和"第一次真并行"叠在同一次合入里。


def test_the_default_slot_is_the_task_worktree(workspace: GitWorkspace) -> None:
    """缺省即今天的行为。既有调用方一个字不改。"""
    assert workspace.worktree_path(TASK) == workspace.worktree_path(TASK, None)
    assert workspace.worktree_path(TASK).name == TASK


def test_a_slot_worktree_is_a_sibling_not_a_child(workspace: GitWorkspace) -> None:
    """建在任务工作树里面的话,它的 `git status` 会把整棵槽看成未跟踪文件。

    于是越权检查判它碰了不该碰的东西,而那是系统自己建的目录——并行落地前最难归因的一脚。
    """
    task_tree = workspace.worktree_path(TASK)
    slot_tree = workspace.worktree_path(TASK, "node-1")

    assert slot_tree.parent == task_tree.parent
    assert task_tree not in slot_tree.parents


def test_a_slot_name_with_odd_characters_is_refused(workspace: GitWorkspace) -> None:
    """槽名进路径也进分支名。放任 `../` 进去等于让工作树建到隔离根之外。"""
    for bad in ("../escape", "Node 1", ""):
        with pytest.raises(ValueError):
            workspace.worktree_path(TASK, bad)


def test_a_slot_gets_its_own_branch(workspace: GitWorkspace) -> None:
    """共用一条分支的话,两个并行的槽会互相看见对方写到一半的提交。

    分隔符是点不是斜杠:git 的 ref 是文件系统上的路径,`task/<id>` 存在时
    `task/<id>/node-1` 建不出来——而任务分支恰恰总是存在。
    """
    workspace.checkout_isolated(TASK)
    path = workspace.checkout_isolated(TASK, slot="node-1")

    assert path.is_dir()
    assert git_out(path, "rev-parse", "--abbrev-ref", "HEAD") == f"task/{TASK}.node-1"


def test_two_slots_do_not_see_each_others_work(workspace: GitWorkspace) -> None:
    left = workspace.checkout_isolated(TASK, slot="node-1")
    workspace.checkout_isolated(TASK, slot="node-2")
    (left / "note.md").write_text("左边写的", encoding="utf-8")

    assert workspace.diff(TASK, slot="node-1").paths() == {"note.md"}
    assert workspace.diff(TASK, slot="node-2").is_empty()


def test_resetting_a_slot_starts_the_retry_from_the_current_task_branch(
    workspace: GitWorkspace,
) -> None:
    """上一轮节点分支不能把失败尝试带进下一轮。"""
    ignore = workspace.root / ".gitignore"
    ignore.write_text(ignore.read_text(encoding="utf-8") + "\n.retry-cache/\n", encoding="utf-8")
    git_out(workspace.root, "add", ".gitignore")
    git_out(
        workspace.root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "test: ignore retry cache",
    )
    task_tree = workspace.checkout_isolated(TASK)
    slot_tree = workspace.checkout_isolated(TASK, slot="node-1")
    (slot_tree / "stale.md").write_text("上一轮\n", encoding="utf-8")
    (slot_tree / ".retry-cache").mkdir()
    (slot_tree / ".retry-cache/generated.bin").write_text("旧缓存", encoding="utf-8")
    workspace.commit(TASK, "feat: stale attempt", slot="node-1")
    (task_tree / "accepted.md").write_text("任务基线\n", encoding="utf-8")
    workspace.commit(TASK, "feat: accepted task work")

    workspace.reset_slot(TASK, "node-1")

    assert not (slot_tree / "stale.md").exists()
    assert not (slot_tree / ".retry-cache").exists()
    assert (slot_tree / "accepted.md").read_text(encoding="utf-8") == "任务基线\n"
    assert git_out(slot_tree, "rev-parse", "--abbrev-ref", "HEAD") == f"task/{TASK}.node-1"
    assert not git_out(slot_tree, "status", "--porcelain")


def test_the_task_worktree_does_not_see_slot_worktrees(workspace: GitWorkspace) -> None:
    workspace.checkout_isolated(TASK)
    workspace.checkout_isolated(TASK, slot="node-1")

    assert workspace.diff(TASK).is_empty()


def test_a_slot_can_commit_on_its_own_branch(workspace: GitWorkspace) -> None:
    path = workspace.checkout_isolated(TASK, slot="node-1")
    (path / "note.md").write_text("槽里提交的", encoding="utf-8")

    rev = workspace.commit(TASK, "feat: 槽里的一次提交", slot="node-1")

    assert rev
    assert workspace.diff(TASK, slot="node-1").paths() == {"note.md"}


def test_cleanup_takes_the_slots_with_it(workspace: GitWorkspace) -> None:
    """漏掉槽会留下孤儿工作树,而下一次同名分支创建会以一条难懂的 git 报错失败。"""
    workspace.checkout_isolated(TASK)
    slot_tree = workspace.checkout_isolated(TASK, slot="node-1")

    workspace.cleanup(TASK)

    assert not slot_tree.exists()
    assert "node-1" not in git_out(workspace.root, "worktree", "list")


# --- 节点改动合回任务分支 -----------------------------------------------------


def test_a_slots_work_squashes_into_the_task_branch(workspace: GitWorkspace) -> None:
    """顶层仍然一个任务分支、一个 PR:拆成 N 个 PR 的话,审批人要自己把碎片拼回去。"""
    workspace.checkout_isolated(TASK)
    node = workspace.checkout_isolated(TASK, slot="node-1")
    (node / "feature.md").write_text("节点写的\n", encoding="utf-8")
    workspace.commit(TASK, "feat: 节点内部的第一次提交", slot="node-1")
    (node / "feature.md").write_text("节点改了两次\n", encoding="utf-8")
    workspace.commit(TASK, "feat: 节点内部的第二次提交", slot="node-1")

    conflict = workspace.squash_into_task(TASK, "node-1", "feat(node-1): 节点产出")

    assert conflict == ""
    task_tree = workspace.worktree_path(TASK)
    assert (task_tree / "feature.md").read_text(encoding="utf-8") == "节点改了两次\n"
    # squash:节点内部的两次试错不进任务分支的历史。
    log = git_out(task_tree, "log", "--oneline", "-1")
    assert "node-1" in log


def test_two_nodes_touching_the_same_line_report_a_conflict(workspace: GitWorkspace) -> None:
    """写集不相交由校验器事先保证,所以撞车说明声明与实际不符——**不自动解决**。"""
    workspace.checkout_isolated(TASK)
    for slot, body in (("node-1", "左边写的\n"), ("node-2", "右边写的\n")):
        tree = workspace.checkout_isolated(TASK, slot=slot)
        (tree / "same.md").write_text(body, encoding="utf-8")
        workspace.commit(TASK, f"feat: {slot}", slot=slot)

    assert workspace.squash_into_task(TASK, "node-1", "feat: 左") == ""
    conflict = workspace.squash_into_task(TASK, "node-2", "feat: 右")

    assert "same.md" in conflict
    # 冲突之后任务分支要留在一个干净的状态:半合并的工作树比冲突本身更难排查。
    assert not git_out(workspace.worktree_path(TASK), "status", "--porcelain")


def test_merging_slots_is_atomic_when_a_later_node_conflicts(workspace: GitWorkspace) -> None:
    """整张图失败时，先合成功的节点也不能残留在任务分支。"""
    (workspace.root / "same.md").write_text("基线\n", encoding="utf-8")
    git_out(workspace.root, "add", "same.md")
    git_out(
        workspace.root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "test: add merge base",
    )
    task_tree = workspace.checkout_isolated(TASK)
    before = git_out(task_tree, "rev-parse", "HEAD")
    for slot, filename, body in (
        ("node-1", "left.md", "第一节点\n"),
        ("node-2", "same.md", "第二节点冲突\n"),
    ):
        tree = workspace.checkout_isolated(TASK, slot=slot)
        (tree / filename).write_text(body, encoding="utf-8")

    # 任务分支先制造与 node-2 的真实冲突；它不属于任何节点产出。
    (task_tree / "same.md").write_text("任务侧修改\n", encoding="utf-8")
    workspace.commit(TASK, "fix: task-side change")
    before = git_out(task_tree, "rev-parse", "HEAD")

    conflict = workspace.merge_slots_into_task(TASK, ["node-1", "node-2"])

    assert "node-2" in conflict
    assert git_out(task_tree, "rev-parse", "HEAD") == before
    assert not (task_tree / "left.md").exists()
    assert (task_tree / "same.md").read_text(encoding="utf-8") == "任务侧修改\n"
    assert not git_out(task_tree, "status", "--porcelain")


def test_merging_slots_rolls_back_when_a_later_git_operation_raises(
    workspace: GitWorkspace,
) -> None:
    """Git 命令抛异常与返回冲突字符串共享同一个事务边界。"""
    task_tree = workspace.checkout_isolated(TASK)
    first = workspace.checkout_isolated(TASK, slot="node-1")
    workspace.checkout_isolated(TASK, slot="node-2")
    (first / "left.md").write_text("第一节点\n", encoding="utf-8")
    before = git_out(task_tree, "rev-parse", "HEAD")
    workspace.cleanup_slot(TASK, "node-2")

    conflict = workspace.merge_slots_into_task(TASK, ["node-1", "node-2"])

    assert "node-2" in conflict
    assert git_out(task_tree, "rev-parse", "HEAD") == before
    assert not (task_tree / "left.md").exists()
    assert not git_out(task_tree, "status", "--porcelain")


def test_disjoint_changes_inside_one_submodule_merge_without_a_gitlink_conflict(
    workspace: GitWorkspace,
) -> None:
    """文件写集不相交时，顶层 gitlink 不能制造一场假冲突。"""
    task_tree = workspace.checkout_isolated(TASK)
    for slot, filename in (("node-1", "left.txt"), ("node-2", "right.txt")):
        tree = workspace.checkout_isolated(TASK, slot=slot)
        target = tree / "repos/order-service" / filename
        target.write_text(f"{slot}\n", encoding="utf-8")

    conflict = workspace.merge_slots_into_task(TASK, ["node-1", "node-2"])

    assert conflict == ""
    assert (task_tree / "repos/order-service/left.txt").is_file()
    assert (task_tree / "repos/order-service/right.txt").is_file()
    assert not git_out(task_tree, "status", "--porcelain")


def test_a_node_that_changed_nothing_is_not_a_failure(workspace: GitWorkspace) -> None:
    """纯读的节点是合法的。造一个空提交或者判它失败,都是在惩罚一件正常的事。"""
    workspace.checkout_isolated(TASK)
    workspace.checkout_isolated(TASK, slot="node-1")

    assert workspace.squash_into_task(TASK, "node-1", "feat: 什么都没改") == ""
