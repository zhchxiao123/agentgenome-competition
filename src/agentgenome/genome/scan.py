"""① 确定性扫描:知识初始化里不烧 token 的那一半。

挂了哪些业务仓、每个仓用什么语言、构建文件在哪、依赖清单里有什么、最近半年哪些路径改得最
勤——**全是脚本能算的**。同一件事,脚本几秒钟、零成本;Agent 几分钟、真金白银。而现在让一个
架构员工一行行读出来。

## 热区有两个用途,而且只有两个

给模块划分做依据(常一起改的东西多半属于同一个域),给深读排优先序(先读常改的地方,中途出
问题时已经建好的也是最有价值的部分)。

**它不决定哪些功能点建卡片。** 那条路(热区优先建卡、其余标待补)在 ADR-0003 里被明确否掉
了:知识覆盖率会因此变成任务历史的函数。看到热区数据很自然会想拿它去筛卡片,所以这句话写在
这里而不是提交信息里。

## 候选不等于模块

这一层只说"这几个仓看起来像模块",最终划分由人在闸门上拍板(见 22-02)。所以宁可漏报也不
多报:一个放图片的目录被列成候选,只会让人在闸门上多划掉一行。

## 候选来自挂载声明,不是目录树

**判据是"有没有被挂载",不是"目录里有没有构建文件"。** 后者曾经是判据,它有两个毛病:
一是把"业务仓是 Workspace 根的一级子目录"焊死进了发现逻辑,挂载点一搬家就扫出空;二是
纯配置仓、文档仓这类没有可识别构建文件的业务仓会被**静默丢弃**——而闸门存在的全部理由
就是让人看一眼,人看不到被悄悄扔掉的东西。

构建文件仍然读,但只用来判语言与依赖,不再决定去留。
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agentgenome.space.git_ws import submodule_paths

#: 热区默认往回看多久。半年:更长会把早就重构掉的老热点算进来,更短则一次版本迭代就能
#: 把结论带偏。
DEFAULT_SINCE_DAYS = 180

#: 构建文件 → 语言。**只用来判语言,不决定去留**——去留看的是有没有被挂载。
_BUILD_FILES: dict[str, str] = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "requirements.txt": "python",
    "package.json": "node",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java",
    "build.gradle": "java",
}


class MountState(StrEnum):
    """挂载点现在是什么样。

    **一个"空"的挂载点目录可以是三件完全不同的事**,而它们要求的动作正好相反:
    未就绪要修环境,空要继续干活,有内容要做判断。合并任意两个,闸门就在问一个人答不了的问题。

    最容易被合并掉的是前两个:两者都表现为"目录里没有代码",但一个是环境坏了、一个是项目
    正常起步。合并的代价是绿地用户每次规划都被要求去修一个没坏的东西——足以让人放弃这条路。
    """

    #: 声明了但没 checkout 出来。**判据是挂载点下有没有 `.git`,不是目录空不空**——
    #: git 会为子模块建空目录占位,所以"目录在不在"完全判不出这件事。
    UNREADY = "unready"
    #: 已 checkout,但除版本控制元数据外什么都没有。绿地新仓的正常状态。
    EMPTY = "empty"
    #: 有内容。有没有可识别的构建文件只影响语言那一栏,不影响状态。
    POPULATED = "populated"


@dataclass(frozen=True)
class Candidate:
    """一个看起来像模块的业务仓。

    `path` 是它在 `.gitmodules` 里的挂载点,Workspace 相对——**不保证是根的一级子目录**。
    """

    path: str
    language: str | None
    build_files: tuple[str, ...]
    dependencies: tuple[str, ...]
    #: 见 `MountState`。判定住在扫描层,**解释住在闸门层**:扫描是确定性的事实采集,
    #: "这个状态该让人做什么"是产品判断,分开才不至于每加一个消费方就复制一遍判断。
    #:
    #: **没有默认值。** 给一个的话,漏传的地方会静默变成"有内容"——而这个类型存在的全部理由
    #: 就是这三者不能被混为一谈。
    state: MountState


@dataclass(frozen=True)
class HotPath:
    """一条常改的路径。"""

    path: str
    changes: int


@dataclass(frozen=True)
class ScanResult:
    candidates: tuple[Candidate, ...] = ()
    hot_paths: tuple[HotPath, ...] = ()
    since_days: int = DEFAULT_SINCE_DAYS

    def as_dict(self) -> dict[str, Any]:
        return {
            "since_days": self.since_days,
            "candidates": [
                {
                    "path": item.path,
                    "language": item.language,
                    "build_files": list(item.build_files),
                    "dependencies": list(item.dependencies),
                    "state": item.state.value,
                }
                for item in self.candidates
            ],
            "hot_paths": [{"path": item.path, "changes": item.changes} for item in self.hot_paths],
        }


def _dependencies(path: Path, name: str) -> list[str]:
    """从构建文件里读出依赖。读不出来就当没有——**扫描不许因为一个畸形文件整个失败**。"""
    try:
        if name == "pyproject.toml":
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            found = payload.get("project", {}).get("dependencies", [])
            return [str(item).split()[0].split(">")[0].split("=")[0] for item in found]
        if name == "package.json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            return sorted({*payload.get("dependencies", {}), *payload.get("devDependencies", {})})
        if name == "requirements.txt":
            return [
                line.split("=")[0].split(">")[0].strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            ]
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return []
    return []


def _mount_state(entry: Path) -> MountState:
    """这个挂载点现在是什么样。见 `MountState`。"""
    if not entry.is_dir() or not (entry / ".git").exists():
        return MountState.UNREADY
    # `.git` 之外还有别的东西才算有内容。它自己在子模块里是个文件、在独立 clone 里是个目录,
    # 两种都不算"代码"。
    has_content = any(item.name != ".git" for item in entry.iterdir())
    return MountState.POPULATED if has_content else MountState.EMPTY


def _candidates(root: Path) -> list[Candidate]:
    """挂载声明里的每个业务仓一条。

    **声明过的一个都不跳过。** 拿不到内容的仓照样要出现在闸门上——人看不到被悄悄扔掉的
    东西,而"这个仓怎么不见了"是一个没有任何线索可查的问题。它的状态会说明为什么它是空的。
    """
    found: list[Candidate] = []
    for mount in sorted(submodule_paths(root)):
        entry = root / mount
        state = _mount_state(entry)
        builds = (
            [name for name in sorted(_BUILD_FILES) if (entry / name).is_file()]
            if state is MountState.POPULATED
            else []
        )
        deps: list[str] = []
        for name in builds:
            deps += _dependencies(entry / name, name)
        found.append(
            Candidate(
                path=mount,
                # 认不出语言不是排除的理由,只是这一栏留空。
                language=_BUILD_FILES[builds[0]] if builds else None,
                build_files=tuple(builds),
                dependencies=tuple(dict.fromkeys(deps)),
                state=state,
            )
        )
    return found


def _hot_paths(root: Path, since_days: int, limit: int = 50) -> list[HotPath]:
    """近期改得最勤的路径。

    **没有 git 历史不是错误**:一个刚 init 出来的 Workspace 照样要能扫,它只是还没有热区。
    """
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                f"--since={since_days}.days",
                "--name-only",
                "--pretty=format:",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    counted = Counter(line.strip() for line in out.splitlines() if line.strip())
    return [
        HotPath(path=path, changes=count)
        # 次数相同时按路径排,**否则同一份仓库两次扫描会给出不同的顺序**——而这一阶段
        # 存在的前提就是它是确定性的。
        for path, count in sorted(counted.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def scan_workspace(root: Path, since_days: int = DEFAULT_SINCE_DAYS) -> ScanResult:
    """扫一遍 Workspace。完全确定性,不经过任何 Agent。"""
    target = Path(root)
    return ScanResult(
        candidates=tuple(_candidates(target)),
        hot_paths=tuple(_hot_paths(target, since_days)),
        since_days=since_days,
    )


__all__ = [
    "DEFAULT_SINCE_DAYS",
    "Candidate",
    "HotPath",
    "MountState",
    "ScanResult",
    "scan_workspace",
]
