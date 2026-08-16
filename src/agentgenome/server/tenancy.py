"""多工作区:一个实例管多个项目。

## 默认拒绝,不落到默认工作区

一个请求没说清楚要哪个工作区,应该**被拒绝**而不是落到某个默认值上。默认工作区是跨租户数据
泄漏最常见的来源:调用方忘了带参数,于是它读到了别人的任务——而这件事没有任何症状,直到有人
发现自己的需求出现在别人的看板上。

单工作区部署仍然要能跑,所以"只注册了一个工作区"时那一个就是答案——**那不是默认值,是唯一解**。
两者的区别在于:注册了两个之后,不带参数的请求会立刻开始报错,而不是悄悄挑一个。

## 名字不是路径

工作区用**名字**寻址,路径由注册表决定。让调用方直接传路径的话,`?workspace=/etc` 就是一次
任意目录读取。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: 注册表文件的缺省位置。它是**这台服务器的部署状态**(哪些项目在这台机器上),不进任何
#: 一个 Workspace 的 git——放进哪个项目,"注销那个项目"都会变成一个自我矛盾的操作。
DEFAULT_REGISTRY = Path.home() / ".agentgenome" / "registry.yaml"

#: 界面上建出来的项目住哪。受管根目录:调用方只说名字,路径由服务端拼——与
#: 「名字不是路径」同一条纪律,`?workspace=/etc` 那种口子在创建侧同样不开。
DEFAULT_WORKSPACES_HOME = Path.home() / ".agentgenome" / "workspaces"

#: 工作区名字的形状。路径分隔符与 `..` 一律不允许——它们是路径穿越的入口。
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class UnknownWorkspace(LookupError):
    """没有这个工作区。"""


def check_name(name: str) -> None:
    """名字形状不合法就抛 `ValueError`。

    独立成函数是因为**创建入口要在动磁盘之前校验**:名字进目录路径,不合法的名字要在
    还没有任何副作用的时候被拒——不该靠"先注册一个探针"这种绕法。
    """
    if not _NAME.match(name):
        raise ValueError(
            f"工作区名字不合法: {name!r}。只允许字母数字与 -/_ ——路径分隔符与 `..` "
            "是路径穿越的入口。"
        )


class AmbiguousWorkspace(LookupError):
    """注册了多个工作区,而请求没说要哪个。"""


@dataclass
class WorkspaceRegistry:
    """名字 → 路径。"""

    entries: dict[str, Path] = field(default_factory=dict)

    def register(self, name: str, root: Path) -> None:
        check_name(name)
        self.entries[name] = Path(root).resolve()

    def unregister(self, name: str) -> Path:
        """摘掉一个工作区,返回它的路径。**只动注册表,不动磁盘。**"""
        if name not in self.entries:
            raise UnknownWorkspace(
                f"没有这个工作区: {name}(有: {', '.join(self.names()) or '(空)'})"
            )
        return self.entries.pop(name)

    def names(self) -> list[str]:
        return sorted(self.entries)

    def resolve(self, name: str | None) -> tuple[str, Path]:
        """把请求里的工作区名解析成路径。返回 `(名字, 路径)`。

        没给名字时:只注册了一个就是它(**唯一解,不是默认值**);注册了多个就拒绝。
        """
        if name is None:
            if len(self.entries) == 1:
                only = next(iter(self.entries))
                return only, self.entries[only]
            raise AmbiguousWorkspace(
                f"注册了 {len(self.entries)} 个工作区,请求必须说明要哪一个"
                f"(有: {', '.join(self.names()) or '(空)'})。"
                "落到默认工作区是跨租户数据泄漏最常见的来源,所以这里直接拒绝。"
            )
        found = self.entries.get(name)
        if found is None:
            raise UnknownWorkspace(
                f"没有这个工作区: {name}(有: {', '.join(self.names()) or '(空)'})"
            )
        return name, found


def load_registry(path: Path) -> WorkspaceRegistry:
    """从文件读注册表。**文件不存在就是空注册表**——零项目是合法状态,不是错误。

    形状坏掉(不是映射、名字不合法)则抛:一个悄悄被忽略的条目,表现是"我的项目不见了",
    而那比启动失败难查得多。
    """
    registry = WorkspaceRegistry()
    if not Path(path).is_file():
        return registry
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"注册表 {path} 不是一个名字→路径的映射。")
    for name, root in payload.items():
        registry.register(str(name), Path(str(root)))
    return registry


def save_registry(path: Path, registry: WorkspaceRegistry) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: str(root) for name, root in sorted(registry.entries.items())}
    target.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=True), encoding="utf-8"
    )


__all__ = [
    "DEFAULT_REGISTRY",
    "DEFAULT_WORKSPACES_HOME",
    "AmbiguousWorkspace",
    "UnknownWorkspace",
    "WorkspaceRegistry",
    "check_name",
    "load_registry",
    "save_registry",
]
