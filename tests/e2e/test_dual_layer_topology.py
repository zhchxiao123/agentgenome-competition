"""双层提交拓扑:一致性与原子回滚。

这是 PRD 01 核心主张的验证——一个跨模块任务的整体状态是一个可原子回滚的对象,
任何时刻 clone 顶层仓拿到的都是一组互相兼容的版本。全流程跑在 examples/mall
双模块夹具 + LocalForge 上,真实 git 合并。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.commit.topology import (
    DualLayerResult,
    SubmoduleMergeFailed,
    merge_submodules,
    merge_task,
)
from agentgenome.space.forge import MergeConflict
from agentgenome.space.git_ws import GitWorkspace, branch_name
from agentgenome.space.local_forge import LocalForge
from tests.fixtures.mall import materialize_mall
from tests.fixtures.tree import write_flat_as_tree

runner = CliRunner()
TASK = "ag-20260901-001"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def scene(tmp_path: Path):
    """一个完整的双层现场:顶层 Workspace(带 bare 远端)+ 两个业务仓。"""
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

    workspace_remote = tmp_path / "ws.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(workspace_remote)],
        check=True,
        capture_output=True,
    )
    _git(root, "remote", "add", "origin", str(workspace_remote))
    _git(root, "push", "-q", "-u", "origin", "main")

    return {
        "root": root,
        "workspace_remote": workspace_remote,
        "mall": mall,
        "ws": GitWorkspace(root, worktrees_home=tmp_path / "worktrees"),
        "forge": LocalForge(),
        "tmp": tmp_path,
    }


def _change_submodule(scene, module_path: str, filename: str, content: str = "改动\n") -> None:
    """在隔离工作区的某个子模块里改点东西,提交并推到该业务仓的任务分支。"""
    worktree = scene["ws"].checkout_isolated(TASK)
    submodule = worktree / module_path
    branch = branch_name(TASK)
    _git(submodule, "checkout", "-q", "-B", branch)
    (submodule / filename).write_text(content)
    _git(submodule, "add", "-A")
    _git(submodule, "commit", "-m", f"feat: {filename}")
    _git(submodule, "push", "-q", "origin", branch)


def _push_workspace_branch(scene) -> None:
    worktree = scene["ws"].checkout_isolated(TASK)
    _git(worktree, "push", "-q", "origin", f"HEAD:{branch_name(TASK)}")


def _clone_top(scene, name: str) -> Path:
    """clone 顶层仓并拉起子模块——这是"任意时刻拿到的是不是一致状态"的检验方式。"""
    target = scene["tmp"] / name
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--quiet",
            "--recurse-submodules",
            str(scene["workspace_remote"]),
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    return Path(target)


def _run_topology(scene) -> DualLayerResult:
    return merge_task(
        workspace=scene["ws"],
        forge=scene["forge"],
        task_id=TASK,
        submodule_remotes={
            "repos/order-service": scene["mall"]["order-service"].remote,
            "repos/inventory-service": scene["mall"]["inventory-service"].remote,
        },
        workspace_remote=scene["workspace_remote"],
        base="main",
        title="跨模块任务",
        body="订单与库存一起改",
    )


# --- 正常路径 ---------------------------------------------------------------


def test_merges_submodule_prs_before_the_workspace_pr(scene) -> None:
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _change_submodule(scene, "repos/inventory-service", "inventory_change.txt")
    _push_workspace_branch(scene)

    result = _run_topology(scene)

    assert [step.module_path for step in result.submodules] == [
        "repos/order-service",
        "repos/inventory-service",
    ]
    assert all(step.merged_rev for step in result.submodules)
    assert result.workspace_rev


def test_submodules_merge_in_dependency_order(scene) -> None:
    """被依赖方先合:中间态窗口里至少是"新接口已提供、调用方还没用",反过来更危险。"""
    write_flat_as_tree(
        scene["root"],
        {
            "version": 1,
            "project": {"name": "example-mall"},
            "modules": [
                {
                    "id": "order-service",
                    "path": "repos/order-service/",
                    "depends_on": ["inventory-service"],
                },
                {"id": "inventory-service", "path": "repos/inventory-service/"},
            ],
        },
    )
    _git(scene["root"], "add", "-A")
    _git(scene["root"], "commit", "-m", "chore: 声明依赖方向")
    _git(scene["root"], "push", "-q", "origin", "main")

    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _change_submodule(scene, "repos/inventory-service", "inventory_change.txt")
    _push_workspace_branch(scene)

    result = _run_topology(scene)

    assert [step.module_path for step in result.submodules] == [
        "repos/inventory-service",
        "repos/order-service",
    ]


def test_pointer_lands_exactly_on_the_merged_revision(scene) -> None:
    """指针必须落在合并后的 commit 上,不是任务分支的 tip。

    两者的内容看起来一样(tip 是合并提交的祖先),所以只看文件内容的断言会空过。
    但停在 tip 上意味着顶层记录的是"合并前的那个版本",一旦子仓主干后续前进,
    顶层就指向了一个游离于 main 之外的提交。必须逐字比对 revision。
    """
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _change_submodule(scene, "repos/inventory-service", "inventory_change.txt")
    _push_workspace_branch(scene)

    result = _run_topology(scene)

    pointers = scene["ws"].submodule_pointers(TASK)
    for step in result.submodules:
        assert pointers[step.module_path] == step.merged_rev, (
            f"{step.module_path} 的指针停在 {pointers[step.module_path]},"
            f"而合并后的 revision 是 {step.merged_rev}"
        )


def test_pointer_advance_survives_a_fresh_worktree(scene) -> None:
    """重放场景:工作区被回收后重跑,指针仍然要前移到合并后的 commit。

    这是最容易破的一条——新工作区里子模块 checkout 在旧版本上,任何走 `git add`
    的提交路径都会把前移覆盖回旧指针,而且失败得完全无声。
    """
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _push_workspace_branch(scene)
    stale_pointer = scene["ws"].submodule_pointers(TASK)["repos/order-service"]
    scene["ws"].cleanup(TASK)

    result = _run_topology(scene)

    merged_rev = result.submodules[0].merged_rev
    assert scene["ws"].submodule_pointers(TASK)["repos/order-service"] == merged_rev
    assert merged_rev != stale_pointer
    clone = _clone_top(scene, "after-replay")
    assert (clone / "repos/order-service" / "order_change.txt").is_file()


def test_pointer_commit_contains_only_gitlink_changes(scene) -> None:
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _push_workspace_branch(scene)

    result = _run_topology(scene)

    worktree = scene["ws"].worktree_path(TASK)
    assert result.pointer_rev is not None
    changed = _git(worktree, "show", "--name-only", "--pretty=", result.pointer_rev).split()
    assert changed == ["repos/order-service"]


# --- 一致性:核心主张 -------------------------------------------------------


def test_untouched_submodules_are_skipped_and_recorded(scene) -> None:
    """任务只碰部分模块是常态。跳过要显式记录——"没改过"和"忘了 push"长得一样。"""
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _push_workspace_branch(scene)

    result = _run_topology(scene)

    assert [step.module_path for step in result.submodules] == ["repos/order-service"]
    assert result.skipped == ["repos/inventory-service"]


def test_top_level_clone_is_consistent_before_the_workspace_pr_merges(scene) -> None:
    """顶层 PR 合并之前,clone 顶层拿到的是一致的旧状态。"""
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _change_submodule(scene, "repos/inventory-service", "inventory_change.txt")
    _push_workspace_branch(scene)

    # 只走前半段:子仓全合并 + 指针前移 + 开出顶层 PR,但不合它。
    merge_submodules(
        workspace=scene["ws"],
        forge=scene["forge"],
        task_id=TASK,
        submodule_remotes={
            "repos/order-service": scene["mall"]["order-service"].remote,
            "repos/inventory-service": scene["mall"]["inventory-service"].remote,
        },
        workspace_remote=scene["workspace_remote"],
        base="main",
        title="t",
        body="b",
    )

    clone = _clone_top(scene, "before")
    assert not (clone / "repos/order-service" / "order_change.txt").exists()
    assert not (clone / "repos/inventory-service" / "inventory_change.txt").exists()


def test_top_level_clone_is_consistent_after_the_workspace_pr_merges(scene) -> None:
    """顶层 PR 合并之后,clone 顶层拿到的是一致的新状态。中间态不存在。"""
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _change_submodule(scene, "repos/inventory-service", "inventory_change.txt")
    _push_workspace_branch(scene)

    _run_topology(scene)

    clone = _clone_top(scene, "after")
    assert (clone / "repos/order-service" / "order_change.txt").is_file()
    assert (clone / "repos/inventory-service" / "inventory_change.txt").is_file()


def test_reverting_the_pointer_commit_rolls_back_the_whole_task(scene) -> None:
    """revert 顶层的一个指针提交即回滚整个任务,子仓历史一行不改。"""
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _change_submodule(scene, "repos/inventory-service", "inventory_change.txt")
    _push_workspace_branch(scene)
    order_history_before = _git(scene["mall"]["order-service"].remote, "log", "--pretty=%H", "main")

    result = _run_topology(scene)

    rollback = scene["tmp"] / "rollback"
    subprocess.run(
        ["git", "clone", "--quiet", str(scene["workspace_remote"]), str(rollback)],
        check=True,
        capture_output=True,
    )
    assert result.workspace_rev is not None
    _git(rollback, "revert", "--no-edit", "-m", "1", result.workspace_rev)
    _git(rollback, "push", "-q", "origin", "main")

    clone = _clone_top(scene, "rolled-back")
    assert not (clone / "repos/order-service" / "order_change.txt").exists()
    assert not (clone / "repos/inventory-service" / "inventory_change.txt").exists()
    # 子仓历史未被改写。
    assert (
        _git(scene["mall"]["order-service"].remote, "log", "--pretty=%H", "main")
        != order_history_before
    )  # 子仓确实合并过
    assert order_history_before.splitlines()[0] in _git(
        scene["mall"]["order-service"].remote, "log", "--pretty=%H", "main"
    )


# --- 失败与重放 -------------------------------------------------------------


def test_a_failing_submodule_merge_aborts_the_whole_run(scene) -> None:
    _change_submodule(scene, "repos/order-service", "shared.txt", "来自任务\n")
    _change_submodule(scene, "repos/inventory-service", "inventory_change.txt")
    _push_workspace_branch(scene)
    # 在 repos/order-service 的 main 上制造一个冲突改动。
    order = scene["mall"]["order-service"]
    _git(order.worktree, "checkout", "-q", "main")
    (order.worktree / "shared.txt").write_text("来自主干\n")
    _git(order.worktree, "add", "-A")
    _git(order.worktree, "commit", "-m", "chore: 主干也改了")
    _git(order.worktree, "push", "-q", "origin", "main")

    with pytest.raises(SubmoduleMergeFailed) as excinfo:
        _run_topology(scene)

    failure = excinfo.value
    assert failure.module_path == "repos/order-service"
    assert isinstance(failure.cause, MergeConflict)
    assert failure.cause.files == ["shared.txt"]


def test_an_aborted_run_reports_which_submodules_already_merged(scene) -> None:
    """中止时要说清"已经合了哪些"——它是不可逆的事实,人接手时必须看得到。"""
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _change_submodule(scene, "repos/inventory-service", "shared.txt", "来自任务\n")
    _push_workspace_branch(scene)
    inventory = scene["mall"]["inventory-service"]
    _git(inventory.worktree, "checkout", "-q", "main")
    (inventory.worktree / "shared.txt").write_text("来自主干\n")
    _git(inventory.worktree, "add", "-A")
    _git(inventory.worktree, "commit", "-m", "chore: 主干也改了")
    _git(inventory.worktree, "push", "-q", "origin", "main")

    with pytest.raises(SubmoduleMergeFailed) as excinfo:
        _run_topology(scene)

    assert [step.module_path for step in excinfo.value.already_merged] == ["repos/order-service"]


def test_an_aborted_run_leaves_the_workspace_pointer_untouched(scene) -> None:
    """顶层没动,所以 clone 顶层仍然是一致的旧状态。"""
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _change_submodule(scene, "repos/inventory-service", "shared.txt", "来自任务\n")
    _push_workspace_branch(scene)
    inventory = scene["mall"]["inventory-service"]
    _git(inventory.worktree, "checkout", "-q", "main")
    (inventory.worktree / "shared.txt").write_text("来自主干\n")
    _git(inventory.worktree, "add", "-A")
    _git(inventory.worktree, "commit", "-m", "chore: 主干也改了")
    _git(inventory.worktree, "push", "-q", "origin", "main")

    with pytest.raises(SubmoduleMergeFailed):
        _run_topology(scene)

    clone = _clone_top(scene, "aborted")
    assert not (clone / "repos/order-service" / "order_change.txt").exists()


def test_rerunning_after_an_interruption_does_not_merge_twice(scene) -> None:
    """崩溃恢复会重放。已合并的子仓 PR 不能被重复合并。"""
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _change_submodule(scene, "repos/inventory-service", "inventory_change.txt")
    _push_workspace_branch(scene)

    first = _run_topology(scene)
    order_history = _git(scene["mall"]["order-service"].remote, "log", "--pretty=%H", "main")

    second = _run_topology(scene)

    assert [step.merged_rev for step in second.submodules] == [
        step.merged_rev for step in first.submodules
    ]
    assert (
        _git(scene["mall"]["order-service"].remote, "log", "--pretty=%H", "main") == order_history
    )


def test_result_exposes_every_pr_reference_for_the_caller_to_record(scene) -> None:
    """每一步的完成状态要能被外部记录,后续 PRD 的崩溃恢复靠它做幂等键。"""
    _change_submodule(scene, "repos/order-service", "order_change.txt")
    _push_workspace_branch(scene)

    result = _run_topology(scene)

    assert result.workspace_pr is not None
    assert all(step.pr is not None for step in result.submodules)
    assert result.as_dict()["submodules"][0]["pr"]["number"] > 0
