"""风险评级:这次改动要不要人来看一眼。

加一行日志和改一次数据库迁移,风险差着数量级,但对系统来说都只是"测试通过的一个 diff"。
这一层负责分辨轻重。

## 低风险自动合并是效率的全部来源

少了它,这套系统的速度优势会被审批消耗干净——每个改动都等人点一下的话,人就是新的瓶颈。
所以内置模式要**克制**:宁可漏判成低风险,也不要把什么都评成高。评成高的代价是人被打扰,
而被打扰太多次的人会开始不看内容直接点同意——那时这道关卡就彻底失效了。

## 五种内置模式

都可以被项目的 `protected.yaml` 覆盖或扩展:

- 命中任一 `datastores[].migrations`——不可逆;
- 命中认证/授权相关路径——安全边界;
- 命中部署与 CI 配置——发布配置;
- 净删除行数超过阈值——"顺手清理"不该静默发生;
- 触碰任一 `interfaces[].schema`——跨模块契约。

前后两条与影响规则(`itest.decide`)看的是同一批路径,但**判的是不同的问题**:那边问"要不要
跑集成测试",这边问"要不要人来看"。合并成一套的话,两个问题会被迫共用一个阈值。

## 删除行数按净删除算

`deleted - added` 而不是 `deleted`。一次大重构会删掉几千行同时加回几千行,那不是"顺手清理",
按总删除行数算会把每一次重命名都评成高风险,然后人就开始不看了。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agentgenome.core.scope import compile_glob, normalize_path
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.rules import HighRiskPattern, ProtectedRules

#: 净删除行数的内置阈值。
DEFAULT_DELETED_LINES_GT = 500

#: 中途扩过权即命中。**它刻意不是一条可覆盖的 `HighRiskPattern`**:项目可以放宽"改了迁移
#: 要不要人看",但不该能关掉"扩过权要人看"——那条是扩权自动放行的对价,关掉它就等于让
#: 员工能自助扩权而无人过目。
SCOPE_WIDENED_RULE = "scope-widened"

#: 内置的高风险路径模式。项目可以在 `protected.yaml` 里用同名 id 覆盖它们。
BUILTIN_PATTERNS: tuple[HighRiskPattern, ...] = (
    HighRiskPattern(
        id="auth",
        description="认证/授权相关代码",
        path_globs=[
            "**/auth/**",
            "**/authn/**",
            "**/authz/**",
            "**/security/**",
            "**/*permission*",
        ],
    ),
    HighRiskPattern(
        id="deploy",
        description="部署与 CI 配置",
        path_globs=[
            ".github/**",
            ".gitlab-ci.yml",
            "deploy/**",
            "**/Dockerfile",
            "docker-compose*.y*ml",
            "**/k8s/**",
            "**/helm/**",
        ],
    ),
    HighRiskPattern(
        id="mass-deletion",
        description=f"净删除超过 {DEFAULT_DELETED_LINES_GT} 行",
        deleted_lines_gt=DEFAULT_DELETED_LINES_GT,
    ),
)


class RiskLevel(StrEnum):
    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True)
class DiffStat:
    """这次改动的规模。"""

    added: int = 0
    deleted: int = 0

    @property
    def net_deleted(self) -> int:
        """净删除。一次大重构删几千行也加回几千行,那不是"顺手清理"。"""
        return max(0, self.deleted - self.added)


@dataclass(frozen=True)
class RiskAssessment:
    """评级结论。命中的规则 id 一起走——"这次为什么要我审"必须可回答。"""

    level: RiskLevel
    matched: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_high(self) -> bool:
        return self.level is RiskLevel.HIGH

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk": self.level.value,
            "matched_rules": list(self.matched),
            "reason": self.reason,
        }


def effective_patterns(
    protected: ProtectedRules, project_map: ProjectMap | None = None
) -> list[HighRiskPattern]:
    """这个项目实际生效的高风险模式:内置 + 从项目地图推导 + 项目自定义。

    **同 id 的项目规则整条覆盖内置那条**,不是逐字段合并——半份来自内置半份来自项目的规则
    没人能推理。项目想放宽某条内置规则,写一条同 id 的就行。
    """
    derived = list(BUILTIN_PATTERNS) + _from_project_map(project_map)
    custom = {pattern.id: pattern for pattern in protected.high_risk}
    merged = [custom.pop(item.id, item) for item in derived]
    return merged + list(custom.values())


def assess(
    changed: Sequence[str],
    stat: DiffStat,
    protected: ProtectedRules,
    project_map: ProjectMap | None = None,
    widened: Sequence[str] = (),
) -> RiskAssessment:
    """对这次改动评级。**纯函数**——不跑 git、不读配置文件。

    `widened` 是这次任务**中途扩过权的模块**。它不是路径模式,所以不走 `protected.yaml`
    那套可覆盖的规则——项目可以放宽"改了迁移要不要人看",但不该能关掉"扩过权要人看":
    那条是扩权自动放行的对价。
    """
    paths = [normalize_path(item) for item in changed]
    matched = [
        pattern.id
        for pattern in effective_patterns(protected, project_map)
        if _fires(pattern, paths, stat)
    ]
    reasons = [f"命中高风险模式: {', '.join(matched)}"] if matched else []
    if widened:
        matched = [*matched, SCOPE_WIDENED_RULE]
        reasons.append(f"本任务中途扩权到: {', '.join(widened)}")
    if not matched:
        return RiskAssessment(RiskLevel.LOW, reason="没有命中任何高风险模式")
    return RiskAssessment(RiskLevel.HIGH, tuple(matched), reason="；".join(reasons))


def _from_project_map(project_map: ProjectMap | None) -> list[HighRiskPattern]:
    """从项目地图推导两条:迁移目录与跨模块契约。

    推导而不是让人手写:这两处的位置已经在地图里声明过了,再让人在 `protected.yaml` 里抄
    一遍的话,两份迟早对不上——而对不上的那一次就是漏判。
    """
    if project_map is None:
        return []
    patterns = []
    migrations = [store.migrations for store in project_map.datastores if store.migrations]
    if migrations:
        patterns.append(
            HighRiskPattern(
                id="migrations",
                description="数据库迁移(不可逆)",
                path_globs=[f"{normalize_path(item).rstrip('/')}/**" for item in migrations],
            )
        )
    schemas = [face.schema_path for face in project_map.interfaces if face.schema_path]
    if schemas:
        patterns.append(
            HighRiskPattern(
                id="interface-schema",
                description="跨模块契约",
                path_globs=[normalize_path(item) for item in schemas],
            )
        )
    return patterns


def _fires(pattern: HighRiskPattern, paths: Sequence[str], stat: DiffStat) -> bool:
    """这条模式成不成立。

    路径与行数之间是**或**,不是与:`mass-deletion` 那条只写了行数,而一条同时写了两者的
    规则最自然的读法是"这些路径下删太多才算"。前者靠"没写的条件不参与"自然成立。
    """
    if pattern.path_globs and _matches(paths, pattern.path_globs):
        if pattern.deleted_lines_gt is None:
            return True
        return stat.net_deleted > pattern.deleted_lines_gt
    if not pattern.path_globs and pattern.deleted_lines_gt is not None:
        return stat.net_deleted > pattern.deleted_lines_gt
    return False


def _matches(paths: Sequence[str], globs: Sequence[str]) -> bool:
    return any(compile_glob(glob).match(path) for glob in globs for path in paths)


__all__ = [
    "SCOPE_WIDENED_RULE",
    "BUILTIN_PATTERNS",
    "DEFAULT_DELETED_LINES_GT",
    "DiffStat",
    "RiskAssessment",
    "RiskLevel",
    "assess",
    "effective_patterns",
]
