"""手艺(Craft Skill)的物化。

工序是**作业契约**(编排器读),手艺是**技艺包**(员工读)。两者的分界是谁来裁决成败:
工序有 schema 校验与门禁,手艺没有直接裁决,只经结果指标间接体现。

这个模块只负责一件事:**把手艺送到员工能读到的地方**。

## 为什么是每 Job 物化,而不是初始化拷贝一次

`genome/procedures/` 是唯一事实源,worktree 里的 `.claude/skills/` 是派生视图。初始化时拷贝
一次的话,genome 里改了手艺要重建工作区才生效——而工作区是按任务建的,等于改一次手艺要等
下一个任务。每 Job 物化的成本是几个小文件的复制,可以忽略。

## 为什么先清空再复制

不清空的话,上一个 Job 的手艺会残留在这一个 Job 的工作区里。而"开发员工不该看到架构员工的
手艺"正是角色定制要保证的事——增量合并会让这个保证在第二个 Job 之后就不成立。

## 为什么复制而不是软链

软链会让员工顺着链接改到 genome 里去,而"员工篡改不持久化"这条就没了。跨平台行为也更稳。
手艺都是小文件,复制的成本可以忽略。
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: 工序目录下放手艺的子目录名。
CRAFT_DIR = "craft"

#: 一个手艺包的入口文件。**这是 Claude Code 的规范,不是我们的命名**——改了就挂不上。
CRAFT_MANIFEST = "SKILL.md"

#: 物化目标,相对工作区根。同样是 Claude Code 的规范。
MOUNT_SUBPATH = Path(".claude") / "skills"

#: 通用手艺(不属于任何单个工序)在基因组里的位置,相对 `genome/procedures/`。
COMMON_DIR = "_common"

#: 单个手艺的行数预算。**超了只告警不拒绝**——写作规范是内容质量问题,不是配置错误。
#: 拒绝加载会让一份稍长的手艺把整个工序打挂,那是完全不成比例的后果。
LINE_BUDGET = 200


class CraftMountError(RuntimeError):
    """手艺没能物化。

    **不静默跳过。** 跳过的话"手艺没挂上"会表现为"最近它好像变笨了"——这是最难归因的
    一类现象,因为没有任何地方会报错。
    """


@dataclass
class MountReport:
    """这次物化都挂了什么。进 Job 事件,审计据此复现"当时带着哪版手艺干的活"。"""

    target: Path
    #: 挂上的手艺名,已排序。
    crafts: list[str] = field(default_factory=list)
    #: 超出行数预算的手艺名。告警用,不影响成败。
    oversized: list[str] = field(default_factory=list)
    #: 运行时不支持技艺包时,改为内联进上下文包的那些手艺名。
    #: 与 `crafts` 分开记:审计要能区分"员工自己按需读的"与"我们塞进提示词的"。
    inlined: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "target": str(self.target),
            "crafts": list(self.crafts),
            "oversized": list(self.oversized),
            "inlined": list(self.inlined),
        }


def common_root(workspace_root: Path) -> Path | None:
    """通用手艺库的位置。**只此一处**——三个调用方各自拼一遍的话,项目级与全局级会算出
    不同的库,而"这个员工到底带了哪份手艺"就没有唯一答案了。
    """
    from agentgenome import paths

    root = Path(workspace_root) / paths.PROCEDURES / COMMON_DIR / CRAFT_DIR
    return root if root.is_dir() else None


def discover(root: Path) -> dict[str, Path]:
    """列出一个 `craft/` 目录下的手艺。目录不存在时返回空。

    **`craft/` 缺失是正常情况,不是错误。** 冷启动时工序只靠 `prompt.md` 也能跑——手艺是
    增强不是前置依赖。
    """
    if not root.is_dir():
        return {}
    found: dict[str, Path] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if (entry / CRAFT_MANIFEST).is_file():
            found[entry.name] = entry
    return found


def validate(root: Path) -> tuple[list[str], list[str]]:
    """校验一个 `craft/` 目录。返回 (错误, 告警)。

    只有两条规则:子目录必须含 `SKILL.md`(否则它不是一个手艺包,挂上去也没用);
    `SKILL.md` 超出行数预算只告警。
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not root.is_dir():
        return errors, warnings

    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        manifest = entry / CRAFT_MANIFEST
        if not manifest.is_file():
            errors.append(f"{CRAFT_DIR}/{entry.name} 缺少 {CRAFT_MANIFEST}")
            continue
        lines = manifest.read_text(encoding="utf-8").count("\n") + 1
        if lines > LINE_BUDGET:
            warnings.append(
                f"{CRAFT_DIR}/{entry.name}/{CRAFT_MANIFEST} 有 {lines} 行,超出 {LINE_BUDGET} 行预算"
            )
    return errors, warnings


def materialize(
    workdir: Path,
    sources: dict[str, Path],
) -> MountReport:
    """把手艺复制进工作区,让员工能原生渐进式加载。

    `sources` 是"手艺名 → 源目录",调用方负责按角色算好这个集合(见 `collect`)。
    """
    target = workdir / MOUNT_SUBPATH
    report = MountReport(target=target)

    try:
        # 先整体清空:残留会让"这个员工只看得到自己的手艺"在第二个 Job 之后就不成立。
        if target.exists():
            shutil.rmtree(target)
        if not sources:
            # 没有手艺要挂也要保证目标是干净的——上一个 Job 的残留必须消失。
            return report
        target.mkdir(parents=True, exist_ok=True)
        for name, source in sorted(sources.items()):
            shutil.copytree(source, target / name)
            report.crafts.append(name)
    except OSError as error:
        raise CraftMountError(f"手艺物化失败({target}): {error}") from error

    _, warnings = validate(workdir / MOUNT_SUBPATH)
    report.oversized = [w.split("/")[1] for w in warnings if "/" in w]
    return report


def collect(
    procedure_craft: Path | None, common_root: Path | None, wanted: Sequence[str] = ()
) -> dict[str, Path]:
    """算出这次该挂哪些手艺。

    集合 = 本次工序自带的 `craft/` + 员工声明的通用手艺。**同名时工序级优先**——工序对
    这次作业的了解比员工级声明多。

    `wanted` 是员工 `crafts:` 里声明的名字;空表示不要任何通用手艺(不是"要全部")。
    默认给空而不是给全,理由与员工 `procedures:` 白名单一致:能力要显式授予。
    """
    names = tuple(wanted) if wanted else ()
    merged: dict[str, Path] = {}

    if common_root is not None:
        available = discover(common_root)
        for name in names:
            if name in available:
                merged[name] = available[name]

    if procedure_craft is not None:
        merged.update(discover(procedure_craft))

    return merged


def missing_common(common_root: Path | None, wanted: Sequence[str] = ()) -> list[str]:
    """员工声明了但通用手艺库里没有的那些。

    在**加载期**报出来,不是派发时——配置写错该在启动时暴露,而不是任务跑一半才发现员工
    少带了一份手艺。
    """
    names = tuple(wanted) if wanted else ()
    if not names:
        return []
    available = discover(common_root) if common_root is not None else {}
    return [name for name in names if name not in available]


__all__ = [
    "COMMON_DIR",
    "common_root",
    "CRAFT_DIR",
    "CRAFT_MANIFEST",
    "LINE_BUDGET",
    "MOUNT_SUBPATH",
    "CraftMountError",
    "MountReport",
    "collect",
    "discover",
    "materialize",
    "missing_common",
    "validate",
]
