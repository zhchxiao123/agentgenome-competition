"""版本化项目空间的抽象。

为兑现"版本化原生、不绑 Git",空间层把"开隔离工作区、看改了什么、提交、
查子模块指针"收敛到这个接口后面。当前唯一实现是 `GitWorkspace`。

刻意**不包含** PR 相关操作——那属于独立的 `Forge` 窄口(见 `space.forge`)。
拆分理由:git 本地操作确定性高、跑得快,测试里应该用真的;只有代码托管平台的
调用需要在测试时替换。揉进一个接口会导致测试时连真 git 也一起被 mock 掉,
那样这一层就从来没有被真正测过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from agentgenome.space.gitcmd import Rev


class ChangeKind(StrEnum):
    """一次改动的形态。

    六类都要能被识别——重命名与子模块指针是最容易漏的两类,漏了会让越权检查误判。
    """

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    UNTRACKED = "untracked"
    SUBMODULE = "submodule"


@dataclass(frozen=True)
class ChangeEntry:
    """变更集里的一条。路径一律是 Workspace 相对路径。"""

    path: str
    kind: ChangeKind
    #: 仅重命名时有值,指向改名前的路径。
    old_path: str | None = None


@dataclass
class ChangeSet:
    """一次改动的完整描述。"""

    entries: list[ChangeEntry] = field(default_factory=list)
    #: 删除的行数,高风险评级的内置判据之一。
    deleted_lines: int = 0
    added_lines: int = 0

    def paths(self) -> set[str]:
        """改动后的路径集合。"""
        return {entry.path for entry in self.entries}

    def touched_paths(self) -> set[str]:
        """被这次改动碰过的全部路径,含重命名的两端。

        越权检查用的是这个而不是 `paths()`——把文件从受保护目录移出去,
        源路径同样是"碰过"。
        """
        touched = self.paths()
        touched.update(entry.old_path for entry in self.entries if entry.old_path)
        return touched

    def submodule_paths(self) -> set[str]:
        return {entry.path for entry in self.entries if entry.kind is ChangeKind.SUBMODULE}

    def entry(self, path: str) -> ChangeEntry:
        for entry in self.entries:
            if entry.path == path:
                return entry
        raise KeyError(f"变更集中没有这条路径: {path}")

    def is_empty(self) -> bool:
        return not self.entries


@runtime_checkable
class VersionedWorkspace(Protocol):
    """版本化项目空间。

    ## 键是 `(task_id, slot)`,不是 `task_id`

    一个任务可以同时有多个工作树:图里的并行节点各一个,多方案尝试各一个。所以每个按任务
    取工作区的方法都带一个 `slot`,**缺省 `None` 就是任务主工作树**——既有调用方一个字不改。

    键先立后用:并行执行器落地时再改这一层,等于把"重构空间层"和"第一次真并行"两个风险叠在
    同一次合入里,出问题时分不清是键错了还是调度错了。
    """

    root: Path

    def checkout_isolated(
        self, task_id: str, slug: str | None = None, slot: str | None = None
    ) -> Path:
        """为一个任务(的一个槽)开出隔离工作区,返回其根目录。必须幂等。"""
        ...

    def diff(self, task_id: str, slot: str | None = None) -> ChangeSet:
        """任务分支相对其基线的全部改动,含未跟踪文件。"""
        ...

    def commit(
        self,
        task_id: str,
        message: str,
        paths: list[str] | None = None,
        slot: str | None = None,
    ) -> Rev:
        """在任务的隔离工作区里提交,返回新的 revision。无改动时返回当前 revision。"""
        ...

    def protected_paths(self) -> list[str]:
        """受保护路径的 glob 列表,来自规则层。"""
        ...

    def submodule_pointers(
        self, task_id: str | None = None, slot: str | None = None
    ) -> dict[str, Rev]:
        """各子模块当前指向的 revision。不给 task_id 时看主 checkout。"""
        ...

    def advance_submodule(
        self, task_id: str, module_path: str, rev: Rev, slot: str | None = None
    ) -> None:
        """把某个子模块的指针前移到指定 revision。"""
        ...

    def cleanup(self, task_id: str, delete_branch: bool = False) -> None:
        """回收任务的隔离工作区,**含它的全部槽**。对不存在的任务是空操作。"""
        ...


__all__ = [
    "ChangeEntry",
    "ChangeKind",
    "ChangeSet",
    "Rev",
    "VersionedWorkspace",
]
