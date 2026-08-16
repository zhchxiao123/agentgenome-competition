"""基因组资产的校验错误。

校验失败要给人看，所以错误按"哪个文件、哪个位置、什么原因"三元组累积，
一次报全部而不是撞到第一条就退出——否则修一条跑一次，反馈回路太长。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ValidationIssue:
    """一条可读的校验问题。"""

    file: str
    message: str
    location: str | None = None

    def render(self) -> str:
        where = f"{self.file}:{self.location}" if self.location else self.file
        return f"{where}: {self.message}"


@dataclass
class GenomeValidationError(Exception):
    """一个或多个基因组资产不合法。"""

    issues: list[ValidationIssue] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(self.render())

    def render(self) -> str:
        return "\n".join(issue.render() for issue in self.issues)

    def __str__(self) -> str:
        return self.render()
