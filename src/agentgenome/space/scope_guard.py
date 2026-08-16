"""越权检查、工作区回滚与受影响子仓的任务分支。

让"员工只能改授权范围内的东西"成为**机制**,而不是提示词里的一句话。

我告诉开发员工"不要改规则文件",它大概率会听。但"大概率"在一个能自动提 PR 的系统里
是不够的:提示注入、目标漂移、单纯的理解偏差,任何一次都可能让它把架构规则改成对自己
有利的样子,然后自己给自己开绿灯。

## 为什么检查发生在每个 Job 之后,而不是提交前

它把越权的爆炸半径限制在单个 Job 内,并且能把越权信息立即回注给下一轮。只在提交前查
的话,一个越权改动可能已经跟三轮正常改动混在一起,回滚代价大得多。

## 为什么看的是 git 而不是员工的自述

员工手里有 Write 与 Bash,想写哪儿写哪儿。最小权限不靠自觉,靠事后有人真的去看——
这里看的是真实工作树,含未跟踪文件、重命名的两端与子模块指针变化。

## 关于 workdir 的约定

回滚是 `reset --hard` + `clean`,它会连带清掉 Job 开始前就存在的未提交改动。这在
**为该任务专开的隔离工作区**里是对的语义(那里除了这个 Job 什么都没有),把 Job 指向
一个人正在用的 checkout 则不是。`agctl procedure run` 这类手工原语要留意这一点。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentgenome.core.scope import ScopePolicy, Violation
from agentgenome.space.git_ws import (
    branch_name,
    committed_touched_paths,
    submodule_paths,
    working_tree_touched_paths,
)
from agentgenome.space.gitcmd import Rev, git, git_lines, git_out

#: 越权 diff 的落盘文件名。回滚之后现场就没了,这份是唯一的证。
SCOPE_DIFF = "scope-violation.diff"

#: 结构化越权报告的文件名。
SCOPE_REPORT = "scope-report.json"


@dataclass
class ScopeReport:
    """一次越权检查的完整记录。

    结构化而非一行错误信息:它既要进审计,又要在下一轮被回注给员工——两个消费者
    都需要"碰了哪些路径、越了哪条界",而不是一句"越权了"。
    """

    ok: bool
    task_id: str
    employee_id: str
    policy: ScopePolicy = field(default_factory=ScopePolicy)
    violations: list[Violation] = field(default_factory=list)
    #: 本次改动碰过的全部路径,含合规的那些——回滚之后现场就没了,得留证。
    touched: list[str] = field(default_factory=list)
    #: 回滚前存下的越权 diff。
    diff_path: str | None = None
    #: 工作区被回滚到哪个 revision。没发生回滚时为空。
    rolled_back_to: str | None = None
    #: 本次因改动而建出任务分支的子仓。
    submodule_branches: list[str] = field(default_factory=list)

    def render(self) -> str:
        return "; ".join(item.render() for item in self.violations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "task_id": self.task_id,
            "employee_id": self.employee_id,
            "policy": self.policy.as_dict(),
            "violations": [item.as_dict() for item in self.violations],
            "touched": list(self.touched),
            "diff_path": self.diff_path,
            "rolled_back_to": self.rolled_back_to,
            "submodule_branches": list(self.submodule_branches),
        }


def capture_baseline(workdir: Path) -> Rev:
    """Job 开始前的 revision。回滚的目标就是它。"""
    return git_out(workdir, "rev-parse", "HEAD")


def capture_preexisting(workdir: Path, baseline: Rev | None) -> dict[str, str]:
    """Job 开始时**已经脏了**的路径,以及它们那一刻的内容指纹。

    基线是一个 revision,而工作树在 Job 开始那一刻可能已经不干净了——同一个隔离工作区
    会被这个任务的多个员工先后使用,前一个留下的未提交改动对后一个来说是既成事实。

    不减掉它的话,后跑的那个员工会被判成"动了前一个员工改的文件":一次纯读的判断类
    Job 会因为上一轮开发留下的改动而判越权,然后触发回滚,把上一轮的成果一起抹掉。

    **减的是"没再动过的",不是"路径在名单里的"。** 只按路径减的话,一个 Job 在本来就脏
    的文件上接着改会被一起放过——而顺序模板下那正是要拦的那件事:出题人刚写下的用例,
    实现人在同一棵工作树里顺手改掉。指纹让"我没碰它"与"我又改了一遍"分得开,而"纯读的
    判断类 Job 不该被前一个人的改动连累"这条原意一个字没变:没改内容就仍然减掉。
    """
    # `baseline` 不能省：前序 Job 可能已经在子仓提交、却还没提交顶层 gitlink。
    # 不带基线时只能看到脏的子仓目录；带上它才能下钻到真正改过的文件，并与
    # `enforce_scope()` 结束时使用的路径粒度保持一致。
    return {path: _fingerprint(workdir / path) for path in touched_paths(workdir, baseline)}


def _fingerprint(path: Path) -> str:
    """一条路径此刻的内容指纹。读不出内容的一律给稳定的哨兵值。

    目录(子模块挂载点)不下钻:它的指针变化由 git 那一侧的比对负责,在这里再算一遍
    会得到第二个答案。给哨兵值等于保持今天的行为——本来就脏的目录仍然被减掉。
    """
    try:
        if path.is_dir():
            return "<dir>"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<unreadable>"


def touched_paths(workdir: Path, baseline: Rev | None = None) -> set[str]:
    """Job 开始以来被碰过的全部路径,下钻到子模块内部。

    两个来源并起来看:

    - **工作树**(含未跟踪文件与重命名的两端)——员工新写但还没 add 的文件同样
      改变了工作区;
    - **`baseline..HEAD` 的已提交改动**——员工自己 commit 一把工作树就干净了,
      只看 `git status` 的话一次越权会因为被提交而彻底隐形。

    顶层的 `git status` 把子仓的改动折叠成一个目录名(` M repos/order-service`)。只看顶层的
    话,一条 `repos/**` 的授权会连带放行 `repos/order-service/.github/**`——而后者恰恰是禁止的。
    所以逐个子仓下钻,并按顶层基线里记的 gitlink 算出子仓自己的基线。

    下钻不出东西时保留子仓路径本身:那说明**只有指针动了**,同样是一处改动,不能
    因为工作树干净就当作没发生。
    """
    submodules = submodule_paths(workdir)
    top = working_tree_touched_paths(workdir)
    if baseline:
        top |= committed_touched_paths(workdir, baseline)

    found: set[str] = set()
    for path in top:
        if path.rstrip("/") not in submodules:
            found.add(path)
            continue
        inner = touched_paths(workdir / path, _submodule_baseline(workdir, baseline, path))
        if inner:
            found.update(f"{path}/{item}" for item in inner)
        else:
            found.add(path)
    return found


def _submodule_baseline(workdir: Path, baseline: Rev | None, module_path: str) -> Rev | None:
    """基线那一刻这个子仓指向哪。子仓自己的"Job 开始前"就是它。"""
    if not baseline:
        return None
    result = git(workdir, "rev-parse", f"{baseline}:{module_path}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def enforce_scope(
    workdir: Path,
    policy: ScopePolicy,
    task_id: str,
    employee_id: str,
    output_dir: Path,
    baseline: Rev | None,
    preexisting: Mapping[str, str] | None = None,
) -> ScopeReport:
    """Job 结束后的越权检查。越权则回滚,合规则建出受影响子仓的任务分支。

    拿不到工作树状态时**直接抛错**,不装作"没有越权"——那正好是最该被拦住的情形。

    `preexisting` 是 Job 开始时就已经脏了的路径及其内容指纹,由 `capture_preexisting` 取。
    检查的是"**这个 Job** 碰了什么",不是"这个工作区现在有什么改动"——两者在多员工先后进
    同一个工作区时会分叉。**指纹没变才减掉**:变了说明这个 Job 又动了它一次。
    """
    before = dict(preexisting or {})
    touched = sorted(
        path
        for path in touched_paths(workdir, baseline)
        if path not in before or before[path] != _fingerprint(workdir / path)
    )
    violations = policy.violations(touched)
    report = ScopeReport(
        ok=not violations,
        task_id=task_id,
        employee_id=employee_id,
        policy=policy,
        violations=violations,
        touched=touched,
    )

    if violations:
        report.diff_path = str(_save_diff(workdir, output_dir, baseline))
        if baseline:
            _rollback(workdir, baseline, output_dir)
            report.rolled_back_to = baseline
    else:
        report.submodule_branches = ensure_submodule_task_branches(workdir, task_id, touched)

    _write_report(output_dir, report)
    return report


def ensure_submodule_task_branches(workdir: Path, task_id: str, touched: list[str]) -> list[str]:
    """在被碰过的子仓上建出与 Workspace 同名的任务分支。

    **"哪些子仓被碰了"只有一个答案来源**,就是越权检测算出的那份变更集。放到别处
    推导就会出现第二份答案,然后两份迟早对不上。

    没被碰过的子仓不建分支:双层提交拓扑按"子仓上存在同名分支"判断该仓是否参与合并,
    留一个空分支等于让一个没改过的仓也去开 PR。

    用 `checkout -B` 而非 `branch`:子模块默认处于游离 HEAD,建了分支却不切过去的话
    员工后续的提交仍然落在游离 HEAD 上,等于没建。同一 commit 上重建是幂等的,
    崩溃恢复重放安全。
    """
    branch = branch_name(task_id)
    created = []
    for module_path in sorted(submodule_paths(workdir)):
        if not _within(module_path, touched):
            continue
        target = workdir / module_path
        if not (target / ".git").exists():
            continue
        git(target, "checkout", "--quiet", "-B", branch)
        created.append(module_path)
    return created


# --- 内部 --------------------------------------------------------------------


def _within(module_path: str, touched: list[str]) -> bool:
    prefix = f"{module_path}/"
    return any(path == module_path or path.startswith(prefix) for path in touched)


def _save_diff(workdir: Path, output_dir: Path, baseline: Rev | None) -> Path:
    """回滚前把越权 diff 存一份。审计与下一轮的上下文回注都要用它。

    先做一次 intent-to-add:**越权最常见的形态就是凭空写一个新文件**,而未跟踪文件
    在普通 diff 里根本不出现。索引的这点改动随后会被回滚一并抹掉。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / SCOPE_DIFF
    for root in [workdir, *(workdir / sub for sub in sorted(submodule_paths(workdir)))]:
        if (root / ".git").exists() or root == workdir:
            git(root, "add", "--all", "--intent-to-add", ".", check=False)
    args = ["diff", "--submodule=diff"]
    if baseline:
        args.append(baseline)
    target.write_text(git(workdir, *args).stdout, encoding="utf-8")
    return target


def _rollback(workdir: Path, baseline: Rev, output_dir: Path) -> None:
    """把工作区恢复到 Job 开始前。

    顺序是有讲究的:先把顶层索引连同 gitlink 拉回基线,再按基线里的 revision 把各个
    子仓拉回去。反过来做的话,子仓刚回滚完就被旧的 gitlink 再次带偏。
    """
    git(workdir, "reset", "--quiet", "--hard", baseline)
    for module_path, rev in _gitlink_revs(workdir).items():
        target = workdir / module_path
        if not (target / ".git").exists():
            continue
        git(target, "reset", "--quiet", "--hard", rev, check=False)
        git(target, "clean", "--quiet", "-fd", check=False)
    git(workdir, "clean", "--quiet", "-fd", *_clean_excludes(workdir, output_dir))


def _clean_excludes(workdir: Path, output_dir: Path) -> list[str]:
    """产物目录不能被 `clean` 一起带走——刚存下的越权 diff 就在里面。

    实际的 Workspace 里 `tasks/` 已被 gitignore,`clean` 不带 `-x` 本就碰不到它;
    这条是给非标准 workdir 兜底的。
    """
    try:
        relative = output_dir.resolve().relative_to(workdir.resolve())
    except ValueError:
        return []
    return ["-e", str(relative)]


def _gitlink_revs(root: Path) -> dict[str, Rev]:
    revs = {}
    for line in git_lines(root, "ls-files", "--stage"):
        meta, _, path = line.partition("\t")
        mode, rev, _stage = meta.split()
        if mode == "160000":
            revs[path] = rev
    return revs


def _write_report(output_dir: Path, report: ScopeReport) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / SCOPE_REPORT).write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


__all__ = [
    "SCOPE_DIFF",
    "ScopeReport",
    "SCOPE_REPORT",
    "capture_baseline",
    "enforce_scope",
    "ensure_submodule_task_branches",
    "touched_paths",
]
