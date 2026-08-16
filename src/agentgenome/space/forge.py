"""代码托管平台的窄口(全仓次测试缝)。

这里只收口一件事:与代码托管平台交互。它是整个系统里仅有的外部网络依赖,
因此单独成一个接口——`VersionedWorkspace` 里的 git 本地操作确定性高、跑得快,
测试里应该用真的;只有这一层需要在测试时替换。

两个实现:
- `CliForge` 封装 gh/glab,生产用,不写自动化测试(测它等于测 gh)
- `LocalForge` 对本地 bare 仓库的真实现,测试用,行为是真的、只是没有网络
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from agentgenome.space.gitcmd import Rev


class PRState(StrEnum):
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


@dataclass(frozen=True)
class PRRef:
    """一个 PR 的稳定引用。可序列化——崩溃恢复要能从任务记录里读回来。"""

    repo: str
    number: int
    head: str
    base: str

    def as_dict(self) -> dict[str, str | int]:
        return {"repo": self.repo, "number": self.number, "head": self.head, "base": self.base}


@dataclass(frozen=True)
class PRStatus:
    state: PRState
    title: str = ""
    merged_rev: Rev | None = None


class ForgeError(RuntimeError):
    """与代码托管平台交互失败。"""


class PRNotFound(ForgeError):
    """引用的 PR 或分支不存在。"""


class ProtectedBranchError(ForgeError):
    """目标分支受保护,不接受这次操作。"""


@dataclass
class MergeConflict(ForgeError):
    """合并冲突。

    带上冲突文件清单而不是抛裸异常——上层要把这份清单注入下一轮开发上下文,
    让员工知道该处理哪几个文件。
    """

    files: list[str]
    pr: PRRef | None = None

    def __post_init__(self) -> None:
        super().__init__(f"合并冲突,涉及 {len(self.files)} 个文件: {', '.join(self.files)}")


@runtime_checkable
class Forge(Protocol):
    """代码托管平台。"""

    def open_pr(self, repo: Path | str, head: str, base: str, title: str, body: str) -> PRRef:
        """开一个 PR。同一 head→base 已有开启中的 PR 时返回它,不重复创建。"""
        ...

    def merge_pr(self, pr: PRRef) -> Rev:
        """合并一个 PR,返回基线上的新 revision。

        **必须幂等**:已合并的 PR 再合一次返回同一 revision 而非报错。双层提交
        拓扑的中断重放与后续的崩溃恢复都建立在这条上。
        """
        ...

    def pr_status(self, pr: PRRef) -> PRStatus: ...

    def is_protected(self, repo: Path | str, branch: str) -> bool: ...


def select(git_host: str) -> Forge:
    """按 `platform.git_host` 选实现。**三处开 PR 的地方共用这一条判据**——编排器的合并
    流水线、CLI 的 `agctl pr` 系列命令、REST 的规则提案——写死成某一个平台的话,示例与
    端到端测试就跑不起来了。

    延迟导入:`LocalForge`/`CliForge` 各自导入本模块,模块级导入会成环。
    """
    from agentgenome.space.cli_forge import CliForge
    from agentgenome.space.local_forge import LocalForge

    if git_host == "local":
        return LocalForge()
    return CliForge(host=git_host)  # type: ignore[arg-type]


__all__ = [
    "Forge",
    "ForgeError",
    "MergeConflict",
    "PRNotFound",
    "PRRef",
    "PRState",
    "PRStatus",
    "ProtectedBranchError",
    "Rev",
    "select",
]
