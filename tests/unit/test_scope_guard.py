"""越权检查、工作区回滚与子仓任务分支——跑在真实 git 仓上。

这一层没有缝:真实的 `git status`、真实的 `reset --hard`、真实的子模块。员工
"想不想守规矩"完全不影响结果,而这个性质只有对着真的 git 才测得出来。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentgenome.core.scope import ScopePolicy, ViolationKind
from agentgenome.space.git_ws import branch_name
from agentgenome.space.scope_guard import (
    SCOPE_DIFF,
    SCOPE_REPORT,
    capture_baseline,
    capture_preexisting,
    enforce_scope,
    touched_paths,
)

TASK = "ag-20260901-001"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.email", "fixture@agentgenome.test")
    _git(path, "config", "user.name", "Fixture")
    return path


def _commit(path: Path, message: str = "chore: 初始化") -> str:
    _git(path, "add", "-A")
    # 身份用 -c 传:`submodule add` clone 出来的子仓没有本地身份配置。
    _git(
        path,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@agentgenome.test",
        "commit",
        "-m",
        message,
    )
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个带受保护目录与业务目录的工作区。"""
    root = _init(tmp_path / "ws")
    (root / "genome" / "rules").mkdir(parents=True)
    (root / "genome" / "rules" / "architecture.md").write_text("# 架构规则\n原样\n")
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text("print('hi')\n")
    _commit(root)
    return root


@pytest.fixture
def with_submodules(tmp_path: Path, repo: Path) -> Path:
    """挂两个业务子仓,模拟真实 Workspace 的形状。"""
    for name in ("repos/order-service", "repos/inventory-service"):
        upstream = _init(tmp_path / "upstream" / name)
        (upstream / "src").mkdir()
        (upstream / "src" / "app.py").write_text(f"# {name}\n")
        _commit(upstream)
        _git(repo, "submodule", "add", str(upstream), name)
    _commit(repo, "chore: 挂载子仓")
    return repo


def _policy(write: list[str], forbid: list[str] | None = None) -> ScopePolicy:
    return ScopePolicy(write_paths=tuple(write), forbid_paths=tuple(forbid or []))


def _enforce(repo: Path, policy: ScopePolicy, output_dir: Path, baseline: str | None):
    return enforce_scope(
        workdir=repo,
        policy=policy,
        task_id=TASK,
        employee_id="dev-employee",
        output_dir=output_dir,
        baseline=baseline,
    )


# --- 看的是真实工作树 -------------------------------------------------------


def test_untracked_files_are_seen(repo: Path) -> None:
    """员工新写但还没 add 的文件同样改变了工作区。"""
    (repo / "sneaky.py").write_text("x = 1\n")

    assert "sneaky.py" in touched_paths(repo)


def test_deletions_are_seen(repo: Path) -> None:
    (repo / "app" / "main.py").unlink()

    assert "app/main.py" in touched_paths(repo)


def test_both_ends_of_a_rename_are_seen(repo: Path) -> None:
    """把文件从受保护目录移出去,源路径同样是"碰过"。"""
    _git(repo, "mv", "genome/rules/architecture.md", "app/architecture.md")

    touched = touched_paths(repo)

    assert "genome/rules/architecture.md" in touched
    assert "app/architecture.md" in touched


def test_changes_inside_a_submodule_are_seen_by_file(with_submodules: Path) -> None:
    """顶层的 git status 把子仓改动折叠成一个目录名。

    只看顶层的话,一条 `repos/**` 的授权会连带放行 `repos/order-service/.github/**`——
    而后者恰恰是禁止的。
    """
    (with_submodules / "repos/order-service" / "src" / "new.py").write_text("# 新文件\n")

    touched = touched_paths(with_submodules)

    assert "repos/order-service/src/new.py" in touched


def test_a_commit_inside_a_submodule_is_seen_by_file(with_submodules: Path, tmp_path: Path) -> None:
    """员工在子仓里提交、顶层指针跟着前移,是**正常干活的形态**。

    这条路径上两个仓的工作树都是干净的,只看 `git status` 会一个字都看不到——
    于是子仓里的越权改动全部隐形。
    """
    baseline = capture_baseline(with_submodules)
    (with_submodules / "repos/order-service" / "src" / "new.py").write_text("# 新功能\n")
    _commit(with_submodules / "repos/order-service", "feat: 新功能")
    _commit(with_submodules, "chore: 指针前移")

    report = _enforce(
        with_submodules, _policy(["repos/inventory-service/**"]), tmp_path / "out", baseline
    )

    assert report.ok is False
    assert [v.path for v in report.violations] == ["repos/order-service/src/new.py"]


# --- 合规改动不被误判 -------------------------------------------------------


def test_a_compliant_change_passes(repo: Path, tmp_path: Path) -> None:
    baseline = capture_baseline(repo)
    (repo / "app" / "feature.py").write_text("# 新功能\n")

    report = _enforce(repo, _policy(["app/**"]), tmp_path / "out", baseline)

    assert report.ok is True
    assert report.violations == []
    assert (repo / "app" / "feature.py").exists(), "合规改动不该被回滚"


def test_a_job_that_changed_nothing_passes(repo: Path, tmp_path: Path) -> None:
    baseline = capture_baseline(repo)

    report = _enforce(repo, _policy([]), tmp_path / "out", baseline)

    assert report.ok is True


# --- 越权:判失败、回滚、留证 -----------------------------------------------


def test_touching_a_forbidden_path_fails_the_job(repo: Path, tmp_path: Path) -> None:
    baseline = capture_baseline(repo)
    (repo / "genome" / "rules" / "architecture.md").write_text("# 我给自己开绿灯\n")

    report = _enforce(repo, _policy(["**"], ["genome/rules/**"]), tmp_path / "out", baseline)

    assert report.ok is False
    assert [v.kind for v in report.violations] == [ViolationKind.FORBIDDEN]
    assert report.violations[0].rule == "genome/rules/**"


def test_writing_outside_the_authorized_paths_fails_the_job(repo: Path, tmp_path: Path) -> None:
    baseline = capture_baseline(repo)
    (repo / "elsewhere.py").write_text("x = 1\n")

    report = _enforce(repo, _policy(["app/**"]), tmp_path / "out", baseline)

    assert report.ok is False
    assert [v.path for v in report.violations] == ["elsewhere.py"]


def test_the_workspace_is_rolled_back_to_the_job_baseline(repo: Path, tmp_path: Path) -> None:
    """越权的 Job 整份产出都不可信,不是只回滚越权的那一条。"""
    baseline = capture_baseline(repo)
    (repo / "genome" / "rules" / "architecture.md").write_text("# 篡改\n")
    (repo / "app" / "feature.py").write_text("# 顺手写的\n")

    report = _enforce(repo, _policy(["app/**"], ["genome/rules/**"]), tmp_path / "out", baseline)

    assert report.rolled_back_to == baseline
    assert (repo / "genome" / "rules" / "architecture.md").read_text() == "# 架构规则\n原样\n"
    assert not (repo / "app" / "feature.py").exists(), "未跟踪文件也要被清掉"


def test_a_committed_violation_is_rolled_back_too(repo: Path, tmp_path: Path) -> None:
    """员工自己 commit 一把并不能把越权洗白。"""
    baseline = capture_baseline(repo)
    (repo / "genome" / "rules" / "architecture.md").write_text("# 篡改\n")
    _commit(repo, "feat: 顺手改了规则")

    report = _enforce(repo, _policy(["**"], ["genome/rules/**"]), tmp_path / "out", baseline)

    assert report.ok is False
    assert _git(repo, "rev-parse", "HEAD") == baseline


def test_the_violating_diff_is_saved_before_the_rollback(repo: Path, tmp_path: Path) -> None:
    """回滚之后现场就没了。审计与下一轮的上下文回注都要用这份 diff。"""
    baseline = capture_baseline(repo)
    (repo / "genome" / "rules" / "architecture.md").write_text("# 我给自己开绿灯\n")

    report = _enforce(repo, _policy(["**"], ["genome/rules/**"]), tmp_path / "out", baseline)

    assert report.diff_path is not None
    saved = (tmp_path / "out" / SCOPE_DIFF).read_text(encoding="utf-8")
    assert "我给自己开绿灯" in saved


def test_the_diff_covers_untracked_files_too(repo: Path, tmp_path: Path) -> None:
    """越权最常见的形态就是凭空写一个新文件,它在普通 diff 里是看不见的。"""
    baseline = capture_baseline(repo)
    (repo / "backdoor.py").write_text("# 后门\n")

    _enforce(repo, _policy(["app/**"]), tmp_path / "out", baseline)

    assert "后门" in (tmp_path / "out" / SCOPE_DIFF).read_text(encoding="utf-8")


def test_the_report_lands_on_disk_and_survives_the_rollback(repo: Path, tmp_path: Path) -> None:
    baseline = capture_baseline(repo)
    (repo / "elsewhere.py").write_text("x = 1\n")
    output_dir = repo / "tasks" / TASK / "artifacts"

    report = _enforce(repo, _policy(["app/**"]), output_dir, baseline)

    payload = json.loads((output_dir / SCOPE_REPORT).read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["task_id"] == TASK
    assert payload["employee_id"] == "dev-employee"
    assert payload["violations"][0]["path"] == "elsewhere.py"
    assert report.render()


def test_the_report_records_what_was_touched(repo: Path, tmp_path: Path) -> None:
    """回滚之后"它到底动了什么"只剩报告能回答。"""
    baseline = capture_baseline(repo)
    (repo / "elsewhere.py").write_text("x = 1\n")
    (repo / "app" / "ok.py").write_text("x = 1\n")

    report = _enforce(repo, _policy(["app/**"]), tmp_path / "out", baseline)

    assert report.touched == ["app/ok.py", "elsewhere.py"]


def test_a_violation_inside_a_submodule_is_caught_and_rolled_back(
    with_submodules: Path, tmp_path: Path
) -> None:
    baseline = capture_baseline(with_submodules)
    (with_submodules / "repos/inventory-service" / "src" / "app.py").write_text("# 不该动这个仓\n")

    report = _enforce(
        with_submodules, _policy(["repos/order-service/**"]), tmp_path / "out", baseline
    )

    assert report.ok is False
    assert [v.path for v in report.violations] == ["repos/inventory-service/src/app.py"]
    assert (
        with_submodules / "repos/inventory-service" / "src" / "app.py"
    ).read_text() == "# repos/inventory-service\n"


# --- 受影响子仓的任务分支 ---------------------------------------------------


def test_a_touched_submodule_gets_the_task_branch(with_submodules: Path, tmp_path: Path) -> None:
    """ "哪些子仓被碰了"只有一个答案来源,就是越权检测算出的那份变更集。"""
    baseline = capture_baseline(with_submodules)
    (with_submodules / "repos/order-service" / "src" / "new.py").write_text("# 新功能\n")

    report = _enforce(
        with_submodules, _policy(["repos/order-service/**"]), tmp_path / "out", baseline
    )

    branches = _git(with_submodules / "repos/order-service", "branch", "--list", branch_name(TASK))
    assert branch_name(TASK) in branches
    assert report.submodule_branches == ["repos/order-service"]


def test_an_untouched_submodule_gets_no_branch(with_submodules: Path, tmp_path: Path) -> None:
    """不留空分支:空分支会让双层提交拓扑以为这个仓参与了本次任务。"""
    baseline = capture_baseline(with_submodules)
    (with_submodules / "repos/order-service" / "src" / "new.py").write_text("# 新功能\n")

    _enforce(with_submodules, _policy(["repos/order-service/**"]), tmp_path / "out", baseline)

    assert not _git(
        with_submodules / "repos/inventory-service", "branch", "--list", branch_name(TASK)
    )


def test_the_task_branch_is_checked_out_so_later_commits_land_on_it(
    with_submodules: Path, tmp_path: Path
) -> None:
    """建了分支却不切过去,员工后续的提交仍然落在游离 HEAD 上,等于没建。"""
    baseline = capture_baseline(with_submodules)
    (with_submodules / "repos/order-service" / "src" / "new.py").write_text("# 新功能\n")

    _enforce(with_submodules, _policy(["repos/order-service/**"]), tmp_path / "out", baseline)

    assert _git(
        with_submodules / "repos/order-service", "rev-parse", "--abbrev-ref", "HEAD"
    ) == branch_name(TASK)


def test_the_employee_changes_survive_the_branch_creation(
    with_submodules: Path, tmp_path: Path
) -> None:
    """切分支不能把它刚写的东西弄丢。"""
    baseline = capture_baseline(with_submodules)
    (with_submodules / "repos/order-service" / "src" / "new.py").write_text("# 新功能\n")

    _enforce(with_submodules, _policy(["repos/order-service/**"]), tmp_path / "out", baseline)

    assert (with_submodules / "repos/order-service" / "src" / "new.py").read_text() == "# 新功能\n"


def test_a_violating_job_creates_no_branches(with_submodules: Path, tmp_path: Path) -> None:
    """整份产出都被判不可信了,不该顺手在子仓上留一个分支。"""
    baseline = capture_baseline(with_submodules)
    (with_submodules / "repos/order-service" / "src" / "new.py").write_text("# 新功能\n")
    (with_submodules / "elsewhere.py").write_text("x = 1\n")

    report = _enforce(
        with_submodules, _policy(["repos/order-service/**"]), tmp_path / "out", baseline
    )

    assert report.ok is False
    assert report.submodule_branches == []
    assert not _git(with_submodules / "repos/order-service", "branch", "--list", branch_name(TASK))


def test_creating_the_branch_twice_is_safe(with_submodules: Path, tmp_path: Path) -> None:
    """崩溃恢复会重放处理器,重复建分支必须是安全的。"""
    baseline = capture_baseline(with_submodules)
    (with_submodules / "repos/order-service" / "src" / "new.py").write_text("# 第一轮\n")
    _enforce(with_submodules, _policy(["repos/order-service/**"]), tmp_path / "out", baseline)

    (with_submodules / "repos/order-service" / "src" / "more.py").write_text("# 第二轮\n")
    report = _enforce(
        with_submodules, _policy(["repos/order-service/**"]), tmp_path / "out2", baseline
    )

    assert report.ok is True
    assert report.submodule_branches == ["repos/order-service"]


# --- 不是 git 仓时 ----------------------------------------------------------


def test_a_workdir_outside_git_is_reported_not_silently_passed(tmp_path: Path) -> None:
    """拿不到工作树状态时不能装作"没有越权"——那正好是最该被拦住的情形。"""
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(Exception):  # noqa: B017 — 具体异常类型不该被测试固化
        enforce_scope(
            workdir=plain,
            policy=_policy(["**"]),
            task_id=TASK,
            employee_id="dev-employee",
            output_dir=tmp_path / "out",
            baseline=None,
        )


# --- 开工前就脏的路径 -------------------------------------------------------
#
# 同一个隔离工作区会被这个任务的多个员工先后使用。前一个留下的未提交改动对后一个是既成
# 事实——但"我没碰它"与"我又改了一遍"必须分得开,否则顺序模板下后一个人可以随手改掉
# 前一个人刚写的东西而不留痕迹。


def test_a_path_left_untouched_is_not_charged_to_this_job(repo: Path, tmp_path: Path) -> None:
    """纯读的判断类 Job 不该被上一个人的改动连累——连累的代价是回滚把那份成果一起抹掉。"""
    baseline = capture_baseline(repo)
    (repo / "left-by-someone-else.py").write_text("# 上一个 Job 写的\n", encoding="utf-8")
    preexisting = capture_preexisting(repo, baseline)

    report = enforce_scope(
        workdir=repo,
        policy=_policy(["nothing/**"]),
        task_id=TASK,
        employee_id="reviewer-employee",
        output_dir=tmp_path / "out",
        baseline=baseline,
        preexisting=preexisting,
    )

    assert report.ok is True
    assert report.touched == []


def test_a_committed_submodule_change_left_untouched_is_not_charged_to_the_checker(
    with_submodules: Path, tmp_path: Path
) -> None:
    """前序 Job 在子仓提交后，顶层只剩一个脏 gitlink；只读 checker 不该替它背锅。"""
    baseline = capture_baseline(with_submodules)
    target = with_submodules / "repos/order-service" / "src" / "new.py"
    target.write_text("# 上一个 Job 提交的\n", encoding="utf-8")
    _commit(with_submodules / "repos/order-service", "feat: 前序 Job 的提交")
    preexisting = capture_preexisting(with_submodules, baseline)

    report = enforce_scope(
        workdir=with_submodules,
        policy=_policy([], ["repos/**"]),
        task_id=TASK,
        employee_id="reviewer-employee",
        output_dir=tmp_path / "out",
        baseline=baseline,
        preexisting=preexisting,
    )

    assert report.ok is True
    assert report.touched == []
    assert target.read_text(encoding="utf-8") == "# 上一个 Job 提交的\n"


def test_changing_a_path_that_was_already_dirty_is_charged_to_this_job(
    repo: Path, tmp_path: Path
) -> None:
    """**这一条是写集分离能不能成立的全部。**

    只按路径减的话,出题人刚写下的用例会因为"本来就脏"而对实现人完全开放——而那正是
    分离要拦的那件事。
    """
    baseline = capture_baseline(repo)
    target = repo / "tests" / "test_x.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# 出题人写的\n", encoding="utf-8")
    preexisting = capture_preexisting(repo, baseline)

    target.write_text("# 实现人顺手改的\n", encoding="utf-8")
    report = enforce_scope(
        workdir=repo,
        policy=_policy(["**"], ["tests/**"]),
        task_id=TASK,
        employee_id="dev-employee",
        output_dir=tmp_path / "out",
        baseline=baseline,
        preexisting=preexisting,
    )

    assert report.ok is False
    assert [item.kind for item in report.violations] == [ViolationKind.FORBIDDEN]
