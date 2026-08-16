"""`VersionedWorkspace` 的 Git 实现。

隔离用 `git worktree`,落在 Workspace 之外的目录——员工在自己的目录里干活,
物理上碰不到主 checkout。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
from pathlib import Path

from agentgenome.genome.rules import load_rules
from agentgenome.paths import WORKTREES_HOME
from agentgenome.space.gitcmd import (
    ORCHESTRATOR_IDENTITY,
    GitError,
    branch_exists,
    git,
    git_lines,
    git_out,
    git_z,
)
from agentgenome.space.workspace import ChangeEntry, ChangeKind, ChangeSet, Rev

#: 任务分支的命名。同一个任务在 Workspace 与所有受影响子仓上用同名分支,
#: 这样一个任务散落在几个仓的分支能被一眼追踪,清理时也不会漏。
BRANCH_PREFIX = "task"

#: 会话自己那条分支的前缀。**与 `task/` 分开**:按前缀认任务分支的地方(`_task_branches`、
#: 任务清理)不该把一个会话的改动当成某个任务的历史扫进去。
SESSION_BRANCH_PREFIX = "session"


#: 隔离工作区存放根的环境变量覆盖。
WORKTREES_HOME_ENV = "AGENTGENOME_WORKTREES_HOME"


def default_worktrees_home() -> Path:
    """隔离工作区放哪。

    策略住在这里而不是各个调用方:漏读环境变量的那个调用方会把 worktree 建到使用者的
    家目录里,而测试尤其容易漏——表现是跑完测试之后 `~/.agentgenome` 里多了一堆垃圾。
    """
    override = os.environ.get(WORKTREES_HOME_ENV)
    return Path(override) if override else WORKTREES_HOME


def scoped_worktrees_home(workspace_root: Path, scope: str) -> Path:
    """为共享隔离根里的特殊工作流分区；Workspace 命名空间由 ``GitWorkspace`` 追加。"""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", scope):
        raise ValueError(f"worktree scope 不合法: {scope!r}")
    # 保留 workspace_root 参数是为了兼容既有调用方。真正的 Workspace 身份统一由
    # GitWorkspace 计算，特殊工作流不能再各自发明一套路径规则。
    return default_worktrees_home() / scope


def _workspace_identity(workspace_root: Path) -> str:
    """稳定且不泄露本机完整路径的 Workspace 目录名。"""
    return hashlib.sha256(str(Path(workspace_root).resolve()).encode()).hexdigest()[:16]


def branch_name(task_id: str, slug: str | None = None) -> str:
    return f"{BRANCH_PREFIX}/{task_id}/{slug}" if slug else f"{BRANCH_PREFIX}/{task_id}"


def slot_branch(task_id: str, slot: str) -> str:
    """槽的分支名。

    **分隔符是点不是斜杠。** git 的 ref 是文件系统上的路径:`task/<id>` 存在时,
    `task/<id>/node-1` 建不出来(`cannot lock ref: 'task/<id>' exists`)——而任务分支
    恰恰总是存在。这条约束只有真跑一次 git 才撞得到,所以它值得写在这里:并行执行器
    的分支命名不能沿用"任务分支底下再挂一层"的直觉。
    """
    check_slot(slot)
    return f"{BRANCH_PREFIX}/{task_id}.{slot}"


#: 槽名同样进路径与分支名,所以受同一套白名单约束——放任 `../` 进去等于让工作树建到
#: 隔离根之外。
_SLOT_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def check_slot(slot: str | None) -> None:
    """槽名合法性。**在派生路径之前查**,别等 git 报一条看不懂的错。"""
    if slot is not None and not _SLOT_NAME.match(slot):
        raise ValueError(f"slot 名只能是小写字母、数字、`-`、`_` 与 `.`: {slot!r}")


class GitWorkspace:
    """基于 git worktree 的版本化项目空间。"""

    def __init__(self, root: Path, worktrees_home: Path | None = None) -> None:
        self.root = root.resolve()
        self._worktrees_home = (worktrees_home or default_worktrees_home()).resolve()
        self._workspace_home = self._worktrees_home / _workspace_identity(self.root)

    # --- 隔离工作区 ----------------------------------------------------------

    def worktree_path(self, task_id: str, slot: str | None = None) -> Path:
        """这个任务(的这个槽)的隔离工作区在哪。

        **槽的工作树是任务工作树的兄弟,不是它的子目录。** 建在里面的话,任务工作树的
        `git status` 会把它整棵看成未跟踪文件——于是越权检查判它碰了不该碰的东西,
        而实际上那是系统自己建的目录。这条是并行执行器落地前最容易踩、也最难归因的一脚。
        """
        check_slot(slot)
        name = f"{task_id}.{slot}" if slot else task_id
        return self._scoped_path(name)

    def checkout_isolated(
        self, task_id: str, slug: str | None = None, slot: str | None = None
    ) -> Path:
        """为一个任务(的一个槽)开出隔离工作区。

        幂等:已经存在就直接返回,不覆盖里面已经干了一半的活——崩溃恢复会重放
        处理器,重复建工作区必须是安全的。

        槽有自己的分支(`task/<id>.<slot>`,分隔符为什么是点见 `slot_branch`):共用一条
        分支的话,两个并行的槽会互相看见对方写到一半的提交,而并行的全部意义就是它们
        互不干扰。
        """
        return self._add_worktree(
            self.worktree_path(task_id, slot),
            slot_branch(task_id, slot) if slot else branch_name(task_id, slug),
        )

    # --- 会话的隔离工作区 ------------------------------------------------------

    def session_worktree_path(self, session_id: str) -> Path:
        """一个没关联任务的可写会话在哪儿改代码。

        **与任务工作区并列,不是它的子目录、也不借用它的键。** 拿 session id 去调
        `worktree_path` 也能算出一个路径,但那样分支会叫 `task/sess-...`——按前缀识别
        任务分支的地方(`_task_branches`、清理路径)会把它当成一个任务的分支,而它不是。
        """
        return self._scoped_path(session_id)

    def _scoped_path(self, name: str) -> Path:
        """返回当前 Workspace 的路径，并只兼容确实属于它的旧版平铺 worktree。"""
        scoped = self._workspace_home / name
        if scoped.exists():
            self._assert_owned(scoped)
            return scoped

        legacy = self._worktrees_home / name
        if legacy.exists() and self._is_owned(legacy):
            return legacy
        return scoped

    def _is_owned(self, path: Path) -> bool:
        if not path.is_dir():
            return False
        try:
            return _common_git_dir(path) == _common_git_dir(self.root)
        except (GitError, OSError):
            return False

    def _assert_owned(self, path: Path) -> None:
        if not self._is_owned(path):
            raise ValueError(f"隔离工作区路径不属于当前 Workspace，拒绝使用: {path}")

    def checkout_session(self, session_id: str) -> Path:
        """为一个没关联任务的可写会话开出隔离工作区。分支 `session/<id>`。

        改动落在这条分支上,**不落在主线的工作树上**。出口是把它转成任务,由任务接管后
        照常过门禁与审批——会话不提供任何直接进主线的路径。

        幂等,理由同 `checkout_isolated`:重启后重建句柄会再走一遍创建路径。
        """
        return self._add_worktree(
            self.session_worktree_path(session_id), f"{SESSION_BRANCH_PREFIX}/{session_id}"
        )

    def cleanup_session(self, session_id: str) -> None:
        """回收会话的工作树。**分支留着**——里面是还没被任何任务接管的改动,是证据不是垃圾。"""
        path = self.session_worktree_path(session_id)
        if path.exists():
            git(self.root, "worktree", "remove", "--force", str(path), check=False)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        git(self.root, "worktree", "prune", check=False)

    def _add_worktree(self, path: Path, branch: str) -> Path:
        """`git worktree add` 的公共部分。任务与会话共用——两份的话下一次改只会改到一份。"""
        if path.is_dir():
            expected = _common_git_dir(self.root)
            try:
                actual = _common_git_dir(path)
            except (GitError, OSError) as error:
                raise ValueError(f"隔离工作区路径已被非 Git 目录占用: {path}") from error
            if actual != expected:
                raise ValueError(f"隔离工作区属于另一个 Workspace，拒绝复用: {path}")
            return path

        path.parent.mkdir(parents=True, exist_ok=True)
        base = self._current_head()
        existing = branch_exists(self.root, branch)
        args = ["worktree", "add"]
        if not existing:
            args += ["-b", branch]
        args += [str(path), branch if existing else base]
        git(self.root, *args)
        # 子模块在新 worktree 里是空的,得单独初始化——否则集成测试与门禁看不到业务代码。
        self._init_submodules(path)
        return path

    def cleanup_slot(self, task_id: str, slot: str) -> None:
        """只回收一个槽的工作树(分支留着——落选方案的历史是证据,不是垃圾)。"""
        path = self.worktree_path(task_id, slot)
        if path.exists():
            git(self.root, "worktree", "remove", "--force", str(path), check=False)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        git(self.root, "worktree", "prune", check=False)

    def reset_slot(self, task_id: str, slot: str) -> Path:
        """让一个重试槽回到当前任务分支，丢弃上一轮失败尝试但保留槽分支身份。"""
        task_tree = self.worktree_path(task_id)
        slot_tree = self.checkout_isolated(task_id, slot=slot)
        git(slot_tree, "reset", "--hard", branch_name(task_id))
        git(slot_tree, "clean", "-fdx")
        for relative in submodule_paths(task_tree):
            source = task_tree / relative
            target = slot_tree / relative
            if not source.is_dir() or not target.is_dir():
                continue
            git(target, "fetch", str(source), "HEAD", allow_file_protocol=True)
            git(target, "checkout", "--detach", "FETCH_HEAD")
            git(target, "reset", "--hard", "FETCH_HEAD")
            git(target, "clean", "-fdx")
        return slot_tree

    def cleanup(self, task_id: str, delete_branch: bool = False) -> None:
        """回收这个任务的隔离工作区,**含它的全部槽**。

        漏掉槽的话会留下孤儿工作树,而下一次同名分支创建会以一条难懂的 git 报错失败
        ("already checked out"),现场却在另一个目录里。
        """
        for path in (self.worktree_path(task_id), *self._slot_paths(task_id)):
            if path.exists():
                git(self.root, "worktree", "remove", "--force", str(path), check=False)
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
        git(self.root, "worktree", "prune", check=False)
        if delete_branch:
            for branch in self._task_branches(task_id):
                git(self.root, "branch", "-D", branch, check=False)

    def commit_slot(self, task_id: str, slot: str, message: str) -> None:
        """把一个槽里的改动落成提交——**两层都要**。

        业务代码住在子模块里,而每个工作树有自己的子模块 checkout(各自的 gitdir)。只提交
        顶层的话,提交里只有一个"子模块指针动了",而指针指向的那个 commit **在这棵工作树的
        子模块里根本不存在**——合并过去之后顶层是坏的。
        """
        tree = self.worktree_path(task_id, slot)
        for relative in submodule_paths(tree):
            module = tree / relative
            if not module.is_dir():
                continue
            git(module, "add", "-A", check=False)
            if git_out(module, "diff", "--cached", "--name-only"):
                git(module, *ORCHESTRATOR_IDENTITY, "commit", "-m", message)
        self.commit(task_id, message, slot=slot)

    def merge_slots_into_task(self, task_id: str, slots: list[str]) -> str:
        """把一组节点原子地合进任务分支；失败时任务工作树逐字回到合并前。

        节点分支上的提交是审计证据，失败时保留；事务边界只覆盖任务分支及其子模块。
        """
        task_tree = self.worktree_path(task_id)
        task_rev = self._head(task_tree)
        module_revs = {
            relative: self._head(task_tree / relative)
            for relative in submodule_paths(task_tree)
            if (task_tree / relative).is_dir()
        }
        active_slot = ""
        try:
            for active_slot in slots:
                message = f"feat({active_slot}): 节点产出"
                self.commit_slot(task_id, active_slot, message)
                conflict = self.squash_into_task(task_id, active_slot, message)
                if conflict:
                    self._restore_task_tree(task_tree, task_rev, module_revs)
                    return f"节点 {active_slot} 的改动合不回任务分支: {conflict}"
        except (GitError, OSError) as error:
            self._restore_task_tree(task_tree, task_rev, module_revs)
            return f"节点 {active_slot or '(未开始)'} 的改动合不回任务分支: {error}"
        return ""

    def squash_into_task(self, task_id: str, slot: str, message: str) -> str:
        """把一个槽的分支 squash 合并回任务分支。冲突返回冲突路径,顺利返回空串。

        **squash 而不是 merge**:顶层要的是"这个节点干了什么"这一件事,而不是它内部的几次
        试错——双层提交拓扑里,节点内部的历史属于节点分支,任务分支上留一条就够。

        **冲突是异常而非常态**:写集不相交由图校验器事先保证,所以撞车说明声明与实际不符。
        这里不自动解决——自动解决的合并是下一个人排查"这行代码哪来的"时最难回答的那种。
        """
        check_slot(slot)
        task_tree = self.worktree_path(task_id)
        slot_tree = self.worktree_path(task_id, slot)

        # **先合子模块那一层。** 两棵工作树各有各的子模块 gitdir(对象库不共享),所以要
        # 显式把节点那边的提交取过来再合;顶层的指针随后由 `add -A` 一起带上。
        for relative in submodule_paths(task_tree):
            source = slot_tree / relative
            target = task_tree / relative
            if not source.is_dir() or not target.is_dir():
                continue
            git(target, "fetch", str(source), "HEAD", check=False, allow_file_protocol=True)
            merged = git(
                target,
                *ORCHESTRATOR_IDENTITY,
                "merge",
                "--squash",
                "FETCH_HEAD",
                check=False,
                allow_file_protocol=True,
            )
            if merged.returncode != 0:
                conflicts = git_lines(target, "diff", "--name-only", "--diff-filter=U")
                git(target, "merge", "--abort", check=False)
                git(target, "reset", "--hard", check=False)
                return ", ".join(f"{relative}/{item}" for item in conflicts) or f"{relative} 合不上"
            if git_out(target, "diff", "--cached", "--name-only"):
                git(target, *ORCHESTRATOR_IDENTITY, "commit", "-m", message)

        # 身份要带上:`merge` 在某些路径上也要记东西,而 CI 机器上通常没有全局 git 身份
        # ——不带的话失败信息会是"你是谁",而真正的冲突被它盖住。
        result = git(
            task_tree,
            *ORCHESTRATOR_IDENTITY,
            "merge",
            "--squash",
            slot_branch(task_id, slot),
            check=False,
        )
        if result.returncode != 0:
            conflicts = git_lines(task_tree, "diff", "--name-only", "--diff-filter=U")
            # 子模块内容已经在上面那层完成三方合并。顶层这里只是两个 gitlink 都前移造成的
            # 假冲突，以任务工作树里刚合好的 HEAD 为准即可；普通文件冲突仍然拒绝。
            ordinary = [
                item
                for item in conflicts
                if item.rstrip("/") not in submodule_paths(task_tree)
            ]
            if ordinary or not conflicts:
                git(task_tree, "merge", "--abort", check=False)
                git(task_tree, "reset", "--hard", check=False)
                return ", ".join(ordinary or conflicts) or (result.stderr or "合并失败").strip()
            git(task_tree, "add", "--", *conflicts)
        # 顶层的子模块指针要跟着走:合完子模块之后它变了,而 `merge --squash` 只暂存了
        # 分支那一侧的改动。
        git(task_tree, "add", "-A", check=False)
        if not git_out(task_tree, "diff", "--cached", "--name-only"):
            # 这个节点什么都没改:不造空提交,也不算失败——一个纯读的节点是合法的。
            return ""
        git(task_tree, *ORCHESTRATOR_IDENTITY, "commit", "-m", message)
        return ""

    @staticmethod
    def _restore_task_tree(task_tree: Path, task_rev: Rev, module_revs: dict[str, Rev]) -> None:
        """回滚一次节点集成尝试，包括已经产生提交的子模块工作树。"""
        git(task_tree, "merge", "--abort", check=False)
        git(task_tree, "reset", "--hard", task_rev)
        for relative, rev in module_revs.items():
            module = task_tree / relative
            if module.is_dir():
                git(module, "merge", "--abort", check=False)
                git(module, "reset", "--hard", rev)

    # --- 变更 ----------------------------------------------------------------

    def diff(self, task_id: str, slot: str | None = None) -> ChangeSet:
        """任务(或它某个槽)分支相对基线的全部改动。

        用 `--porcelain=v1 -z` 而非 `diff`,因为它同时覆盖已提交、未提交与未跟踪
        三种状态——员工新写但还没 add 的文件也已经改变了工作区,越权检查必须看得见。
        """
        path = self.worktree_path(task_id, slot)
        base = self._merge_base(path)
        entries = self._committed_changes(path, base)
        entries += self._working_tree_changes(path)
        added, deleted = self._line_stats(path, base)
        return ChangeSet(entries=_dedupe(entries), added_lines=added, deleted_lines=deleted)

    def commit(
        self,
        task_id: str,
        message: str,
        paths: list[str] | None = None,
        slot: str | None = None,
    ) -> Rev:
        path = self.worktree_path(task_id, slot)
        git(path, "add", "-A", "--", *(paths or ["."]))
        return self.commit_staged(task_id, message, slot=slot)

    def commit_staged(self, task_id: str, message: str, slot: str | None = None) -> Rev:
        """只提交当前索引,不做 `git add`。

        `advance_submodule` 精确摆好的 gitlink 必须用这个提交:`git add` 会按
        **工作树里子模块当前 checkout 的 HEAD** 重新暂存 gitlink,把前移覆盖回去。
        那会让顶层指针停在旧版本上,而合并后 clone 顶层拿到的就是不一致的组合——
        整套双层拓扑的一致性保证正是断在这里。
        """
        path = self.worktree_path(task_id, slot)
        if not git_out(path, "diff", "--cached", "--name-only"):
            # 无改动时不造空提交——处理器重放时不该炸也不该污染历史。
            return self._head(path)
        git(path, *ORCHESTRATOR_IDENTITY, "commit", "-m", message)
        return self._head(path)

    # --- 子模块 --------------------------------------------------------------

    def submodule_pointers(
        self, task_id: str | None = None, slot: str | None = None
    ) -> dict[str, Rev]:
        """各子模块当前指向的 revision。

        读索引而非 HEAD:`advance_submodule` 之后、`commit` 之前,调用方关心的是
        "指针现在是什么",而不是"上次提交时是什么"。索引在没有暂存改动时等于 HEAD,
        所以这个语义严格更有用。
        """
        target = self.worktree_path(task_id, slot) if task_id else self.root
        pointers = {}
        for line in git_lines(target, "ls-files", "--stage"):
            meta, _, path = line.partition("\t")
            mode, rev, _stage = meta.split()
            if mode == "160000":  # gitlink
                pointers[path] = rev
        return pointers

    def advance_submodule(
        self, task_id: str, module_path: str, rev: Rev, slot: str | None = None
    ) -> None:
        """把某个子模块的 gitlink 前移到指定 revision。

        直接改索引里的 gitlink 而不是在子模块里 checkout——前移是顶层仓的事,
        不该要求子模块工作树处于任何特定状态。
        """
        path = self.worktree_path(task_id, slot)
        git(
            path,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{rev},{module_path.rstrip('/')}",
        )

    # --- 规则 ----------------------------------------------------------------

    def protected_paths(self) -> list[str]:
        return list(load_rules(self.root).protected.all_paths)

    # --- 内部 ----------------------------------------------------------------

    def _current_head(self) -> str:
        return git_out(self.root, "rev-parse", "HEAD")

    @staticmethod
    def _head(path: Path) -> Rev:
        return git_out(path, "rev-parse", "HEAD")

    def _task_branches(self, task_id: str) -> list[str]:
        prefix = f"{BRANCH_PREFIX}/{task_id}"
        # 槽分支用点分隔(见 `slot_branch`),所以两种分隔符都要认——只认斜杠的话
        # `cleanup(delete_branch=True)` 会把槽分支留在仓里,下一个同 id 的任务就撞名。
        return [
            line
            for line in git_lines(
                self.root, "for-each-ref", "--format=%(refname:short)", "refs/heads"
            )
            if line == prefix or line.startswith(f"{prefix}/") or line.startswith(f"{prefix}.")
        ]

    def _init_submodules(self, path: Path) -> None:
        gitmodules = path / ".gitmodules"
        if not gitmodules.is_file():
            return
        # 子模块拉不起来不该让整个隔离工作区失败——业务仓可能暂时不可达,
        # 而基因组相关的任务根本不需要它们。
        with contextlib.suppress(GitError):
            git(path, "submodule", "update", "--init", allow_file_protocol=True)

    def merge_base(self, task_id: str, slot: str | None = None) -> str:
        """任务分支相对基线的分叉点。越权检查与门禁都拿它当"这一轮从哪儿开始"。"""
        return self._merge_base(self.worktree_path(task_id, slot))

    def _slot_paths(self, task_id: str) -> list[Path]:
        """这个任务已经建出来的全部槽工作树。"""
        found: set[Path] = set()
        # 第一处是新布局；第二处只为升级前已经存在的平铺 worktree 保留。所有权检查
        # 是清理路径的硬边界，不能让同名任务删除另一个 Workspace 的槽。
        for home in (self._workspace_home, self._worktrees_home):
            if not home.is_dir():
                continue
            found.update(
                entry
                for entry in home.iterdir()
                if entry.is_dir()
                and entry.name.startswith(f"{task_id}.")
                and self._is_owned(entry)
            )
        return sorted(found)

    def _merge_base(self, path: Path) -> str:
        """任务分支相对基线的分叉点。

        基线取主 checkout 当前所在分支;算不出来时退回到分支的第一个提交之前。
        """
        default = git_out(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        result = git(path, "merge-base", default, "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else self._head(path)

    def _committed_changes(self, path: Path, base: str) -> list[ChangeEntry]:
        """已提交的改动。开启 `-M` 让 git 把改名识别成改名而非增+删。"""
        fields = git_z(path, "diff", "--name-status", "-M", "-z", base, "HEAD")
        return _parse_name_status(fields, self._submodule_paths(path))

    def _working_tree_changes(self, path: Path) -> list[ChangeEntry]:
        fields = git_z(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        return _parse_porcelain(fields, self._submodule_paths(path))

    def _submodule_paths(self, path: Path) -> set[str]:
        return submodule_paths(path)

    def _line_stats(self, path: Path, base: str) -> tuple[int, int]:
        added = deleted = 0
        for line in git_lines(path, "diff", "--numstat", base, "HEAD"):
            parts = line.split("\t")
            if len(parts) < 2 or "-" in parts[:2]:
                continue  # 二进制文件,git 用 `-` 占位
            added += int(parts[0])
            deleted += int(parts[1])
        return added, deleted


def working_tree_touched_paths(root: Path) -> set[str]:
    """工作树里被碰过的全部路径,含未跟踪文件与子模块指针变化。

    越权检查用的就是它:员工新写但还没 add 的文件同样改变了工作区,只看已提交的
    改动会漏掉最需要被拦住的那一类。
    """
    fields = git_z(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    submodules = submodule_paths(root)
    touched: set[str] = set()
    for entry in _parse_porcelain(fields, submodules):
        touched.add(entry.path)
        if entry.old_path:
            touched.add(entry.old_path)
    return touched


def committed_touched_paths(root: Path, baseline: str) -> set[str]:
    """`baseline..HEAD` 之间被碰过的路径,含重命名的两端。

    越权检查必须把它和工作树状态并起来看:**员工自己 `git commit` 一把,工作树就干净了**,
    只看 `git status` 的话一次越权改动会因为被提交而彻底隐形——而"提交它"恰恰是任何
    一个正常干活的员工都会做的事。

    基线在这个仓里不存在(比如子仓刚被挂上来)时按"没有已提交改动"处理,不报错:
    这一层的职责是把改动找全,判定交给上层。
    """
    result = git(root, "diff", "--name-status", "-M", "-z", baseline, "HEAD", check=False)
    if result.returncode != 0:
        return set()
    fields = [field for field in result.stdout.split("\0") if field]
    touched: set[str] = set()
    for entry in _parse_name_status(fields, submodule_paths(root)):
        touched.add(entry.path)
        if entry.old_path:
            touched.add(entry.old_path)
    return touched


def submodule_paths(root: Path) -> set[str]:
    """`.gitmodules` 里声明的子模块挂载点。"""
    if not (root / ".gitmodules").is_file():
        return set()
    raw = git_out(root, "config", "-f", ".gitmodules", "--get-regexp", r"submodule\..*\.path")
    return {line.split(" ", 1)[1].rstrip("/") for line in raw.splitlines() if " " in line}


def _classify(path: str, submodules: set[str], fallback: ChangeKind) -> ChangeKind:
    """子模块指针变化在 porcelain 里长得跟普通修改一样,必须按路径单独识别。"""
    return ChangeKind.SUBMODULE if path.rstrip("/") in submodules else fallback


_NAME_STATUS = {
    "A": ChangeKind.ADDED,
    "M": ChangeKind.MODIFIED,
    "D": ChangeKind.DELETED,
    "T": ChangeKind.MODIFIED,
}


def _parse_name_status(fields: list[str], submodules: set[str]) -> list[ChangeEntry]:
    """解析 `diff --name-status -M -z` 的输出。

    `-z` 用 NUL 分隔字段,重命名占三段(状态、旧路径、新路径),其余占两段。
    用 NUL 而非换行是因为路径里可以有换行。
    """
    entries: list[ChangeEntry] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        if status.startswith(("R", "C")):
            old_path, new_path = fields[index + 1], fields[index + 2]
            entries.append(
                ChangeEntry(
                    path=new_path,
                    kind=_classify(new_path, submodules, ChangeKind.RENAMED),
                    old_path=old_path,
                )
            )
            index += 3
            continue
        path = fields[index + 1]
        kind = _NAME_STATUS.get(status[0], ChangeKind.MODIFIED)
        entries.append(ChangeEntry(path=path, kind=_classify(path, submodules, kind)))
        index += 2
    return entries


_PORCELAIN = {
    "?": ChangeKind.UNTRACKED,
    "A": ChangeKind.ADDED,
    "M": ChangeKind.MODIFIED,
    "D": ChangeKind.DELETED,
    "T": ChangeKind.MODIFIED,
}


def _parse_porcelain(fields: list[str], submodules: set[str]) -> list[ChangeEntry]:
    """解析 `status --porcelain=v1 -z` 的输出。

    每条以 `XY path` 开头;重命名额外跟一段旧路径。
    """
    entries: list[ChangeEntry] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        status, path = record[:2], record[3:]
        index += 1
        if "R" in status or "C" in status:
            old_path = fields[index]
            index += 1
            entries.append(
                ChangeEntry(
                    path=path,
                    kind=_classify(path, submodules, ChangeKind.RENAMED),
                    old_path=old_path,
                )
            )
            continue
        marker = status.replace(" ", "")[:1] or "M"
        kind = _PORCELAIN.get(marker, ChangeKind.MODIFIED)
        entries.append(ChangeEntry(path=path, kind=_classify(path, submodules, kind)))
    return entries


def _dedupe(entries: list[ChangeEntry]) -> list[ChangeEntry]:
    """同一路径既在已提交改动里又在工作树里时,保留信息量更大的那条。

    未跟踪是最弱的信号(它只说明文件存在),其余形态都比它更具体。
    """
    by_path: dict[str, ChangeEntry] = {}
    for entry in entries:
        existing = by_path.get(entry.path)
        if existing is None or existing.kind is ChangeKind.UNTRACKED:
            by_path[entry.path] = entry
    return list(by_path.values())


def _common_git_dir(root: Path) -> Path:
    value = Path(git_out(root, "rev-parse", "--git-common-dir"))
    return (value if value.is_absolute() else Path(root) / value).resolve()
