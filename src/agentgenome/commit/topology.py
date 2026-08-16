"""双层提交拓扑。

一个跨模块任务的变更落在两层:业务仓各自的 PR,以及顶层 Workspace 上"这一组
子模块指针"的 PR。合并顺序是固定的,而且这个顺序不是实现细节:

    1. 全部子仓 PR 合并(按依赖拓扑,被依赖方先合)
    2. 顶层 Workspace 分支上把各子模块指针前移,生成一个指针提交
    3. 合并顶层 Workspace PR

意义在于:顶层 PR 合并之前,主 Workspace 的指针仍指向旧版本,此刻 clone 顶层拿到
的是一致的旧状态;合并之后拿到的是一致的新状态。中间态不存在。

## 关于原子性,必须说清楚

Git 平台不支持跨仓事务。**这套顺序是在逼近原子语义,不是提供事务保证。**
如果第二个子仓 PR 在第一个合并之后失败了,第一个的改动已经在主干上了——系统会
中止并报告"已经合了哪些",但主干上确实短暂存在过一个孤立的改动。

缓解措施是把子仓合并放在同一个短时间窗内、并按依赖拓扑排序(被依赖方先合,
这样中间态窗口里至少是"新接口已提供、调用方还没用",比反过来安全)。

不要让使用者以为这里有事务保证。

本模块交付的是这套编排的原语与一次可运行的验证。真正驱动它的提交流水线
(风险评级、审批、状态机)在 PRD 08。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.loader import load_project_map
from agentgenome.genome.models import ProjectMap
from agentgenome.space.forge import Forge, PRRef, Rev
from agentgenome.space.git_ws import GitWorkspace, branch_name, submodule_paths
from agentgenome.space.gitcmd import EMPLOYEE_IDENTITY, git, git_out, published_branch_exists


@dataclass
class SubmoduleStep:
    """一个子仓的合并结果。"""

    module_path: str
    pr: PRRef | None = None
    merged_rev: Rev | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "module_path": self.module_path,
            "pr": self.pr.as_dict() if self.pr else None,
            "merged_rev": self.merged_rev,
        }


@dataclass
class DualLayerResult:
    """一次双层合并的完整记录。

    每一步的完成状态都在这里,调用方要把它落盘——后续 PRD 的崩溃恢复用它做幂等键,
    避免重放时重复合并。
    """

    task_id: str
    submodules: list[SubmoduleStep] = field(default_factory=list)
    #: 本次没有任务分支、因而无需合并的子仓。显式记录而非静默跳过——
    #: "这个模块本来就没改"和"员工忘了 push"长得一样,得让人看得见。
    skipped: list[str] = field(default_factory=list)
    pointer_rev: Rev | None = None
    workspace_pr: PRRef | None = None
    workspace_rev: Rev | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DualLayerResult:
        """从落盘的记录读回来。崩溃恢复靠它认出"哪些子仓已经合过了"。"""
        result = cls(task_id=str(payload.get("task_id", "")))
        for item in payload.get("submodules") or []:
            pr = item.get("pr")
            result.submodules.append(
                SubmoduleStep(
                    module_path=str(item.get("module_path", "")),
                    pr=PRRef(**pr) if isinstance(pr, dict) else None,
                    merged_rev=item.get("merged_rev"),
                )
            )
        result.skipped = [str(item) for item in payload.get("skipped") or []]
        result.pointer_rev = payload.get("pointer_rev")
        result.workspace_rev = payload.get("workspace_rev")
        top = payload.get("workspace_pr")
        result.workspace_pr = PRRef(**top) if isinstance(top, dict) else None
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "submodules": [step.as_dict() for step in self.submodules],
            "skipped": list(self.skipped),
            "pointer_rev": self.pointer_rev,
            "workspace_pr": self.workspace_pr.as_dict() if self.workspace_pr else None,
            "workspace_rev": self.workspace_rev,
        }


@dataclass
class SubmoduleMergeFailed(RuntimeError):
    """某个子仓 PR 合并失败,整体中止。

    `already_merged` 是不可逆的事实——那些子仓的改动已经在主干上了。人接手时
    必须看得到这份清单,否则他不知道系统把世界改成了什么样。
    """

    module_path: str
    cause: Exception
    already_merged: list[SubmoduleStep] = field(default_factory=list)

    def __post_init__(self) -> None:
        merged = ", ".join(step.module_path for step in self.already_merged) or "无"
        super().__init__(
            f"子仓 {self.module_path} 合并失败: {self.cause}(本次已合并且不可回退的子仓: {merged})"
        )


def order_by_dependency(module_paths: list[str], project_map: ProjectMap | None) -> list[str]:
    """按依赖拓扑排序:被依赖方先合。

    跨仓不是事务,所以中间态窗口是真实存在的。被依赖方先合意味着窗口里的状态是
    "新接口已提供、调用方还没用"——比反过来("调用方已用新接口、提供方还没上线")
    安全得多。

    项目地图缺失或存在环时退回到给定顺序:排序是优化,不该成为失败点。
    """
    if project_map is None:
        return list(module_paths)

    path_to_id = {module.path.rstrip("/"): module.id for module in project_map.modules}
    id_to_path = {module_id: path for path, module_id in path_to_id.items()}
    deps = {
        module.id: [dep for dep in module.depends_on if dep in id_to_path]
        for module in project_map.modules
    }

    pending = [path.rstrip("/") for path in module_paths]
    ordered: list[str] = []
    settled: set[str] = set()
    while pending:
        ready = [
            path
            for path in pending
            if all(id_to_path[dep] in settled for dep in deps.get(path_to_id.get(path, ""), []))
        ]
        if not ready:
            # 有环。排序失败不该阻断合并,按原顺序把剩下的接上。
            ordered.extend(pending)
            break
        ordered.extend(ready)
        settled.update(ready)
        pending = [path for path in pending if path not in settled]
    return ordered


def _load_project_map(workspace: GitWorkspace) -> ProjectMap | None:
    try:
        return load_project_map(workspace.root)
    except GenomeValidationError:
        return None


def publish_task_branches(
    workspace: GitWorkspace,
    task_id: str,
    touched: list[str],
    message: str,
    slug: str | None = None,
) -> list[str]:
    """把员工在各子仓里做的改动提交到任务分支并推到远端。返回真的推出去的子仓。

    ## 推送凭证只由编排器持有

    员工负责产出提交,**推送是编排器的动作**。员工进程被攻破也拿不到推送能力——这条是
    "机器人账号在机制上无法直接推主干"那道保护的另一半:主干由平台侧保护,推任务分支的
    能力则根本不在员工手里。

    ## 为什么现在才推

    此前子仓任务分支只建在本地 checkout,没推到远端;而 `merge_submodules` 按"远端上存在
    同名分支"判断该仓要不要参与合并——于是一个真的改过的仓会被静默跳过,任务照样被标成
    完成。推送属于提交流水线的动作,放在 Job 结束后的检查里是错的位置(那时改动可能还要
    再改几轮)。

    没有改动的子仓不推:留一个空分支等于让一个没改过的仓也去开 PR。
    """
    worktree = workspace.checkout_isolated(task_id, slug)
    branch = branch_name(task_id, slug)
    pushed = []
    for module_path in sorted(submodule_paths(worktree)):
        if not any(item == module_path or item.startswith(f"{module_path}/") for item in touched):
            continue
        target = worktree / module_path
        if not (target / ".git").exists():
            continue
        git(target, "checkout", "--quiet", "-B", branch)
        git(target, "add", "-A")
        if git_out(target, "diff", "--cached", "--name-only"):
            git(target, *EMPLOYEE_IDENTITY, "commit", "--quiet", "-m", message)
        git(target, "push", "--quiet", "--force", "origin", f"HEAD:{branch}")
        pushed.append(module_path)
    return pushed


def merge_submodules(
    workspace: GitWorkspace,
    forge: Forge,
    task_id: str,
    submodule_remotes: dict[str, Path],
    workspace_remote: Path,
    base: str = "main",
    title: str = "",
    body: str = "",
    slug: str | None = None,
    known: DualLayerResult | None = None,
) -> DualLayerResult:
    """双层合并的前半段:子仓 PR 全合 + 顶层指针前移 + 开出顶层 PR。

    **拆成两段而不是一个带开关的函数。** 真实调用方是状态机,而状态机天然分两步走:
    `MERGING` 先跑这半段,顶层 PR 合并要等审批结果落定。此前那个
    `stop_before_workspace_merge` 参数是为了让测试观察中间态而存在的语义开关——它不表达
    任何业务概念,而带语义开关的函数迟早长出第二个、第三个开关。

    `known` 是上一次跑到一半留下的记录。传进来的话,里面已经合过的子仓不再重复开 PR、
    重复合并——**平台上重复开 PR 会得到两个**,这是整套崩溃恢复里最不能马虎的一处。
    """
    branch = branch_name(task_id, slug)
    result = DualLayerResult(task_id=task_id)
    project_map = _load_project_map(workspace)
    done = {step.module_path: step for step in (known.submodules if known else [])}

    for module_path in order_by_dependency(list(submodule_remotes), project_map):
        if module_path in done:
            # 上一次已经合过它了。再合一遍不会出错(merge_pr 幂等),但会再开一个 PR。
            result.submodules.append(done[module_path])
            continue
        remote = submodule_remotes[module_path]
        if not published_branch_exists(remote, branch):
            # 这个子仓本次没被碰过,没有东西要合。
            result.skipped.append(module_path)
            continue
        step = SubmoduleStep(module_path=module_path)
        try:
            step.pr = forge.open_pr(
                remote,
                head=branch,
                base=base,
                title=title or f"{task_id}: {module_path}",
                body=body,
            )
            step.merged_rev = forge.merge_pr(step.pr)
        except Exception as exc:
            raise SubmoduleMergeFailed(
                module_path=module_path,
                cause=exc,
                already_merged=list(result.submodules),
            ) from exc
        result.submodules.append(step)

    result.pointer_rev = _advance_pointers(workspace, task_id, branch, result.submodules)
    result.workspace_pr = forge.open_pr(
        workspace_remote,
        head=branch,
        base=base,
        title=title or f"{task_id}: 子模块指针前移",
        body=body,
    )
    return result


def merge_workspace(forge: Forge, result: DualLayerResult) -> DualLayerResult:
    """双层合并的后半段:合并顶层 PR。**任务完成的原子提交点。**

    在这一刻之前,clone 顶层拿到的是一致的旧状态;之后是一致的新状态。
    """
    if result.workspace_pr is None:
        raise ValueError(f"{result.task_id} 还没有顶层 PR,前半段没跑完")
    result.workspace_rev = forge.merge_pr(result.workspace_pr)
    return result


def merge_task(
    workspace: GitWorkspace,
    forge: Forge,
    task_id: str,
    submodule_remotes: dict[str, Path],
    workspace_remote: Path,
    base: str = "main",
    title: str = "",
    body: str = "",
    slug: str | None = None,
) -> DualLayerResult:
    """一口气走完两段。给不需要在中间停下的调用方(演示、手工重跑)。"""
    return merge_workspace(
        forge,
        merge_submodules(
            workspace,
            forge,
            task_id,
            submodule_remotes,
            workspace_remote,
            base=base,
            title=title,
            body=body,
            slug=slug,
        ),
    )


def _advance_pointers(
    workspace: GitWorkspace, task_id: str, branch: str, steps: list[SubmoduleStep]
) -> Rev:
    """把各子模块指针前移到合并后的 commit,并推到顶层远端。

    用 `commit_staged` 而非 `commit`:后者会 `git add`,按工作树里子模块当前
    checkout 的 HEAD 重新暂存 gitlink,把前移覆盖回去(见 `commit_staged` 的说明)。
    """
    worktree = workspace.checkout_isolated(task_id)
    for step in steps:
        if step.merged_rev:
            workspace.advance_submodule(task_id, step.module_path, step.merged_rev)
    rev = workspace.commit_staged(task_id, f"chore({task_id}): 子模块指针前移")
    git(worktree, "push", "--quiet", "--force", "origin", f"HEAD:{branch}")
    return rev
