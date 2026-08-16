"""授权范围:一个 Job 允许碰哪些路径。

这一层是纯的——路径进,越权清单出。不碰 git、不碰配置文件,于是最危险的那部分
逻辑可以被逐条钉死。

## 为什么自己翻译 glob 而不是用 fnmatch

`fnmatch` 不区分 `*` 与 `**`:两者都被当作"匹配任意字符,含斜杠",于是 `genome/*`
会匹配 `genome/rules/architecture.md`。**在权限检查里,过度匹配的方向是放行越权。**

## forbid 优先于 write

两者都命中时以禁止为准。反过来的话,一条宽泛的 `write_paths: ["**"]` 会悄悄把全部
禁止规则吃掉,而"我明明写了 forbid"这种排查极其耗时。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any


class ViolationKind(StrEnum):
    """越了哪一类界。

    分开是因为处置不同:碰受保护路径通常意味着提示注入或目标漂移,值得升级人工;
    写到授权目录之外多半只是理解偏差,回注一轮就能修好。
    """

    #: 命中禁写规则(含叠加进来的受保护路径)。
    FORBIDDEN = "forbidden"
    #: 不在任何授权可写路径内。
    OUTSIDE = "outside_write_paths"


@dataclass(frozen=True)
class Violation:
    path: str
    kind: ViolationKind
    #: 命中的那条 glob。越权是"越了哪条界",不写清楚人得自己拿规则逐条比。
    rule: str | None = None

    def render(self) -> str:
        if self.kind is ViolationKind.FORBIDDEN:
            return f"{self.path}:命中禁写规则 {self.rule}"
        return f"{self.path}:不在授权可写路径内"

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind.value, "rule": self.rule}


@dataclass(frozen=True)
class ScopePolicy:
    """这个 Job 允许碰什么。占位符必须在构造前就展开完毕。

    **不可变。** 一个 Job 叠加的受保护路径不该渗到下一个;而"权限在跑的过程中被改过"
    这种事一旦可能发生,事件流里记的责任归属就再也对不上实际发生的事。
    """

    write_paths: tuple[str, ...] = ()
    forbid_paths: tuple[str, ...] = ()
    #: 叠加在角色写范围上的额外收窄层。每一层都必须命中；层内多个 glob 是“或”。
    #: DAG 节点的 `write_scope` 就住在这里，不能用它替换角色权限，否则一张 LLM 产出的
    #: 图反而能把员工原本没有的路径授权给自己。
    write_limits: tuple[tuple[str, ...], ...] = ()

    def with_write_limit(self, paths: Sequence[str]) -> ScopePolicy:
        """增加一层必须同时满足的写范围；空层表示只读。"""
        limit = tuple(dict.fromkeys(paths))
        return ScopePolicy(
            write_paths=self.write_paths,
            forbid_paths=self.forbid_paths,
            write_limits=self.write_limits + (limit,),
        )

    def with_forbidden(self, extra: Sequence[str]) -> ScopePolicy:
        """再叠一组禁写规则。**叠加而非替换,重复的只留一条。**

        叠加的来源有两类:项目的受保护路径(角色无关),与调用方按**这一个任务的事实**算出来
        的禁令(比如质量线的写集分离)。两类共用这一条实现——各写一遍的话,迟早有一处忘了
        去重或者忘了保留员工自己的禁令,而后者的表现是一条本该生效的规则静默消失。
        """
        added = tuple(item for item in extra if item not in self.forbid_paths)
        return ScopePolicy(
            write_paths=self.write_paths,
            forbid_paths=self.forbid_paths + added,
            write_limits=self.write_limits,
        )

    def with_protected(self, protected: Sequence[str]) -> ScopePolicy:
        """把项目的受保护路径叠加在员工规则之上。

        叠加而非替换:受保护路径是**任何员工**都不能碰的,员工自己的 forbid 是它这个
        角色额外的约束,两者都要生效。
        """
        return self.with_forbidden(protected)

    def matched_forbid(self, path: str) -> str | None:
        return _first_match(path, self.forbid_paths)

    def allows(self, path: str) -> bool:
        """这条路径能不能写。走 `violations` 而不是自己再写一遍那道级联:
        两处各写一遍的话,迟早有一处更宽——而更宽的那一处就是漏洞。"""
        return not self.violations([path])

    def violations(self, paths: Iterable[str]) -> list[Violation]:
        """按路径排序的越权清单。空表示这次改动完全合规。"""
        found = []
        for path in sorted(set(paths)):
            rule = self.matched_forbid(path)
            if rule is not None:
                found.append(Violation(path, ViolationKind.FORBIDDEN, rule))
            elif _first_match(path, self.write_paths) is None or any(
                _first_match(path, limit) is None for limit in self.write_limits
            ):
                found.append(Violation(path, ViolationKind.OUTSIDE))
        return found

    def as_dict(self) -> dict[str, Any]:
        return {
            "write_paths": list(self.write_paths),
            "forbid_paths": list(self.forbid_paths),
            "write_limits": [list(limit) for limit in self.write_limits],
        }


#: glob 里表示通配的那几个字符。**只有这一处**说得出这件事——抄两遍的话,哪天补上 `{}`
#: 只会修好其中一个调用方,另一个静默算错。
WILDCARDS = "*?["


def concrete_prefix(glob: str) -> str:
    """一条 glob 里不带通配的那一段。`src/order/reserve/**` → `src/order/reserve`。

    两个地方要它:判断"这条覆盖范围指到的代码还在不在"(整条 glob 拿去匹配的话,一个空目录
    会被判成不存在,而空目录是合法的),以及数"同一处被多少条覆盖范围盯着"。
    """
    parts: list[str] = []
    for part in PurePosixPath(glob).parts:
        if any(char in part for char in WILDCARDS):
            break
        parts.append(part)
    return "/".join(parts)


@lru_cache(maxsize=512)
def compile_glob(glob: str) -> re.Pattern[str]:
    """把路径 glob 翻译成正则。

    规则:
    - `**` 跨目录,`*` 不跨;
    - `repos/**` 同时匹配 `repos/` 本身与其下的一切——不然"禁写这个目录"会漏掉
      目录本身被删除或被换成文件的情形。
    """
    out = ["^"]
    index = 0
    while index < len(glob):
        char = glob[index]
        if glob.startswith("/**", index):
            # `a/**` 覆盖 `a` 自身与 `a/` 下的一切。
            out.append("(?:/.*)?")
            index += 3
        elif glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(char))
            index += 1
    out.append("$")
    return re.compile("".join(out))


def is_under(path: str, directory: str) -> bool:
    """这条路径是不是落在这个目录之下(目录本身也算)。

    **按目录边界比,不按字符串前缀。** `repos/api` 与 `repos/api-2` 是两个仓,前缀匹配会把
    后者的改动算到前者头上。这一对不是杜撰的——同名仓库挂第二次拿到的就是 `-2` 后缀,
    所以"前缀相同的兄弟目录"在这套挂载约定下是常规产物而不是巧合。

    **住在这里而不是各处各写一遍。** 此前门禁、集成测试判定各有一份自己的实现,而边界草案
    因为没有可用的共享实现,自己写了个只比第一段的版本——挂载点从一级变成两级之后它就永久
    失效了,且没有任何测试发现。三份实现里错一份,正是这种复制的必然结果。
    """
    target = normalize_path(directory).rstrip("/")
    return normalize_path(path) == target or normalize_path(path).startswith(f"{target}/")


def normalize_path(path: str) -> str:
    """把一条路径化成与 glob 同一套写法。

    用 `removeprefix` 而不是 `lstrip("./")`:后者删的是**字符集合**,会把
    `.github/workflows/ci.yml` 削成 `github/...`,于是 `.github/**` 这条禁写规则
    再也匹配不上——一条看起来写了的禁令实际是空的。
    """
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized.removeprefix("./")
    return normalized


def _first_match(path: str, globs: Sequence[str]) -> str | None:
    normalized = normalize_path(path)
    for glob in globs:
        if compile_glob(glob).match(normalized):
            return glob
    return None


__all__ = [
    "ScopePolicy",
    "Violation",
    "ViolationKind",
    "compile_glob",
    "is_under",
    "normalize_path",
]
