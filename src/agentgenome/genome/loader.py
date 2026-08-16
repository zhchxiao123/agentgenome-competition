"""基因组资产的加载与校验。

加载器是纯函数式的:磁盘上的文本进,模型或 `GenomeValidationError` 出。
除了引用完整性检查需要看文件是否存在,不做任何其他 I/O。

通用的 YAML→模型原语住在 `genome.yamlio`,这里从那儿转出——它们与"基因组里有哪些资产"
无关,留在这里会让知识树与加载器互相 import。
"""

from __future__ import annotations

from pathlib import Path

from agentgenome import paths
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.features import KnowledgeTree, collect_features
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.tree import Assembled, assemble, is_flat_map
from agentgenome.genome.yamlio import parse_yaml_model, read_yaml


def _check_project_map_references(
    project_map: ProjectMap, workspace_root: Path, relative: str
) -> list[ValidationIssue]:
    """引用完整性:依赖指向存在的模块,文档与契约指向存在的文件。"""
    issues: list[ValidationIssue] = []
    known = project_map.module_ids()

    def require_file(value: str | None, location: str) -> None:
        if value and not (workspace_root / value).exists():
            issues.append(ValidationIssue(relative, f"引用的文件不存在: {value}", location))

    def require_module(value: str, location: str) -> None:
        if value not in known:
            issues.append(ValidationIssue(relative, f"引用的模块不存在: {value}", location))

    for index, module in enumerate(project_map.modules):
        for dep in module.depends_on:
            require_module(dep, f"modules.{index}.depends_on")
        require_file(module.doc, f"modules.{index}.doc")

    for index, interface in enumerate(project_map.interfaces):
        require_module(interface.provider, f"interfaces.{index}.provider")
        for consumer in interface.consumers:
            require_module(consumer, f"interfaces.{index}.consumers")
        require_file(interface.schema_path, f"interfaces.{index}.schema")

    for index, datastore in enumerate(project_map.datastores):
        require_module(datastore.owner, f"datastores.{index}.owner")
        require_file(datastore.migrations, f"datastores.{index}.migrations")

    return issues


def _assemble_checked(workspace_root: Path) -> tuple[Assembled, list[ValidationIssue]]:
    """装配 + 引用完整性。两个入口共用的那一半。"""
    relative = str(paths.PROJECT_MAP)
    if is_flat_map(workspace_root):
        # 严格模式会把旧文件报成"多了个 interfaces 字段",读起来像拼写错误。这条路径存在
        # 的唯一理由,就是让人看到"该迁移了"而不是"我是不是打错字了"。
        raise GenomeValidationError(
            [
                ValidationIssue(
                    relative,
                    "这是旧的单文件项目地图。跑 `agctl genome migrate` 把它拆成知识树。",
                )
            ]
        )
    assembled = assemble(workspace_root)
    return assembled, _check_project_map_references(assembled.project_map, workspace_root, relative)


def load_tree(workspace_root: Path) -> KnowledgeTree:
    """加载并校验整棵知识树,**含完备性**。

    每个功能点要么有一张卡片,要么有一条带理由的「无需卡片」声明——地图上不允许存在缺口
    (ADR-0003)。这是**门禁与人工校验**的入口。

    **引用问题与完备性问题一次报全。** 分两轮抛的话,修完第一批才看得见第二批,而修一个
    文件跑一次的反馈回路太长。
    """
    assembled, issues = _assemble_checked(workspace_root)
    index, cards, map_files, card_files = collect_features(
        workspace_root,
        assembled.project_map,
        assembled.module_features,
        assembled.module_submaps,
        issues,
    )
    if issues:
        raise GenomeValidationError(issues)
    return KnowledgeTree(
        project_map=assembled.project_map,
        feature_index=index,
        cards=cards,
        map_files=map_files,
        card_files=card_files,
    )


def load_project_map(workspace_root: Path) -> ProjectMap:
    """加载并校验项目地图。**运行期的入口,不查完备性。**

    分层是存储的事,消费面刻意没变——三十处消费方(上下文组装、集测判定、门禁配置、风险
    评级、图谱同步、REST 接口……)一行不改就继续工作。

    **刻意不跑功能点与卡片的校验。** 完备是知识变更要过的门禁,不是每个任务运行时要过的
    检查:一张卡片里的拼写错误不该让一个正在跑的开发任务当场死掉。要那一层的走 `load_tree`。
    """
    assembled, issues = _assemble_checked(workspace_root)
    if issues:
        raise GenomeValidationError(issues)
    return assembled.project_map


__all__ = [
    "GenomeValidationError",
    "ValidationIssue",
    "load_project_map",
    "load_tree",
    "parse_yaml_model",
    "read_yaml",
]
