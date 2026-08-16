"""staging:知识类工序的产物通道。**验证产物,不验证自述。**

知识类工序此前的契约是一个巨型 `result.json`:整篇 Markdown 转义后塞进 JSON 字符串,
编排器再解包写成真实文件。那是结构化输出的最坏形态——转义是 LLM 最易错的动作,长输出
会截断,而 50 张卡里 1 个转义错误会让整个 JSON 解析失败、全部重新生成(PRD 34)。

改造后员工在产物目录的 `staging/` 下直接写**真实的树文件**,目录形状与
`genome/knowledge/` 逐字节同构——员工写的就是最终形态,不存在中间表示。这个模块是
那条通道的全部确定性部分:

- **校验器**(`validate_knowledge_staging`):load 规则 + 完备性 + coverage + 预算,
  全部复用树自己的规则(`FeatureEntry` / `FeatureCard` / `ModuleMap` / 行数预算),
  不为 staging 再发明一套 schema。
- **逐文件报错**(`render_issues`):每条错误定位到文件、按文件分组。这是对 LLM 的
  接口设计,不只是日志美化——回注"cards/xxx.md: 缺 id"能让重试只修那一个文件,
  回注 JSON path 只会让第二次错得一模一样。
- **原子应用**(`apply_staged`):校验通过后把已验证的内容合进 `genome/knowledge/`,
  失败逐字节恢复原状。"半更新状态的树不合法"这条既有原则原样保留。

`result.json` 降级为小票(`RECEIPT_SCHEMA`):扁平小 JSON,notes / questions /
low_confidence 三个指针字段。**小票只补充裁决,不覆盖裁决**——它声称一切正常而
staging 校验失败,Job 照样失败;它缺失算格式失败(走契约重试),但树合法时它的
任何字段都不参与成败判定。

## 校验与装载共用一次遍历

`validate_knowledge_staging` 与 `load_knowledge_staging` 是同一个 `_read_fragment`
的两个出口。分成两个解析器的话,"什么算合法的 staging"迟早给出两个答案,而分叉的
表现是校验放过的东西装载时炸掉——正好落在两次尝试都花完之后。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentgenome import paths
from agentgenome.config import KnowledgeConfig
from agentgenome.core.scope import concrete_prefix
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.features import FeatureEntry, scope_exists, scope_hint
from agentgenome.genome.models import Datastore, Interface
from agentgenome.genome.tree import (
    MODULE_MAP,
    MODULE_OVERVIEW,
    ContractIndex,
    ModuleMap,
    RootModuleEntry,
    existing_features,
    module_dir,
    overview_path,
)
from agentgenome.genome.yamlio import (
    parse_yaml_model,
    read_yaml,
    split_front_matter,
)

#: 产物目录里放树片段的子目录。员工写它,校验器读它,原子应用搬它。
STAGING_DIR = "staging"

#: 卡片里出现它就不再被重跑覆盖(从 `knowledge` 搬来:它属于写入路径,而写入路径在这里)。
HUMAN_EDITED_MARKER = "<!-- human-edited -->"

#: 小票:`result.json` 降级后的形态。**小而平**——≤6 个标量/浅数组字段,无嵌套对象、
#: 无 Markdown 内容字段。小 JSON 与大嵌套 JSON 是两种可靠性完全不同的东西。
#:
#: 刻意没有 `passed`:自述是最弱证据,产物是最强证据,成败由 staging 校验裁决。
#: `additionalProperties: false` 不是洁癖——留着口子的话,"文件进信封"会从这里长回来。
RECEIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task_id", "producer"],
    "properties": {
        "task_id": {"type": "string"},
        "producer": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
        #: 指向 staging 里置信度低、要人复核的文件(相对 staging/ 的路径)。
        "low_confidence": {"type": "array", "items": {"type": "string"}},
    },
}

#: staged 模块地图里允许出现的认知字段。`summary` 不在其中——它住在根索引的模块条目上,
#: 由 `project-map.yaml` 补丁携带;写进 map.yaml 会被 `ModuleMap` 的严格模式当场拒掉。
_MODULE_FIELDS = (
    "lang",
    "entrypoints",
    "test_cmd",
    "build_cmd",
    "itest_cmd",
    "junit_xml_path",
    "depends_on",
    "confidence",
)


@dataclass(frozen=True)
class StagedFeature:
    """staging 里的一个功能点:清单条目 + 卡片正文(已从文件读出)。"""

    id: str
    summary: str = ""
    scope: tuple[str, ...] = ()
    #: 卡片文件的内容。与 `no_card` 互斥——互斥由树自己的 `FeatureEntry` 模型在校验时保证。
    card_text: str | None = None
    no_card: str | None = None


@dataclass(frozen=True)
class StagedModule:
    """staging 里的一个模块片段。"""

    id: str
    #: 来自 `project-map.yaml` 补丁。`None` 表示这次没报,保留现状。
    summary: str | None = None
    #: 认知字段(`_MODULE_FIELDS`),**按 staged map.yaml 里真实出现的键**收集——
    #: 没写的键不覆盖现状,与旧通道"字段为 None 不动"同一语义。
    fields: dict[str, Any] = field(default_factory=dict)
    #: 模块认知卡(overview.md)的内容。`None` 表示这次没写,保留现状。
    doc_text: str | None = None
    #: `None` 表示 staged map.yaml 里没有 `features` 键——功能点清单保持现状。
    #: 空列表则是一个明确的"这个模块没有功能点"的结论。
    features: tuple[StagedFeature, ...] | None = None


@dataclass(frozen=True)
class StagedTree:
    """一份(或多份合并后的)staging 片段,已经过校验、可直接应用。"""

    modules: tuple[StagedModule, ...] = ()
    #: `None` 表示 staging 里没有 interfaces.yaml——契约索引保持现状。
    interfaces: tuple[Interface, ...] | None = None
    datastores: tuple[Datastore, ...] | None = None


@dataclass
class KnowledgeUpdate:
    """一次知识更新的结果。"""

    version: int
    cards_written: list[str]
    cards_preserved: list[str]
    #: 这次写入真正碰过的文件,Workspace 相对路径。
    #:
    #: **提交时只提交这些。** 拿 `genome/**` 整个 add 的话,同一个检出里别人未提交的改动
    #: (人手改的规则、蒸馏刚写的经验卡片)会被一起打包进这次知识提交——署名成员工、标题写
    #: 着知识更新,而那不是它。
    files_written: list[str] = field(default_factory=list)


class _RootPatch(BaseModel):
    """`staging/project-map.yaml`:根索引补丁。

    条目形状就是根索引里的最终形态(`RootModuleEntry`),不另发明补丁语法——员工只列
    这次读过的模块那几行。根索引的其余部分(version、project)由编排器管,员工碰不到。
    """

    model_config = ConfigDict(extra="forbid")

    modules: list[RootModuleEntry] = Field(default_factory=list)


# --- 读取(校验与装载共用) ---------------------------------------------------


@dataclass
class _Fragment:
    """一次遍历的产出:装载出的片段 + 攒下的问题。"""

    tree: StagedTree
    issues: list[ValidationIssue]


def _relative(staging_root: Path, target: Path) -> str:
    """报错里的文件定位,统一带 `staging/` 前缀——回注给员工时它就是要改的那个路径。"""
    return str(PurePosixPath(STAGING_DIR) / target.relative_to(staging_root))


def _read_fragment(
    workspace_root: Path,
    staging_root: Path,
    focus: str = "",
    limits: KnowledgeConfig | None = None,
) -> _Fragment:
    """把 staging 读成片段,问题逐文件攒进清单。**一次报全,不撞到第一条就停。**"""
    limits = limits or KnowledgeConfig()
    issues: list[ValidationIssue] = []
    empty = StagedTree()

    files = (
        sorted(item for item in staging_root.rglob("*") if item.is_file())
        if staging_root.is_dir()
        else []
    )
    if not files:
        # 与 `contract.py` 里 "plan mode 退出码 0 无产物" 是同一课:没有 staging 树就是
        # 没有产物,判失败,理由指向"无产物"而不是一句含糊的"格式不对"。
        issues.append(
            ValidationIssue(
                f"{STAGING_DIR}/",
                "没有产物:staging/ 不存在或为空。认知必须写成 staging/ 下的真实树文件,"
                "不是对话里的总结",
            )
        )
        return _Fragment(empty, issues)

    from agentgenome.genome.loader import load_project_map

    project_map = load_project_map(workspace_root)
    known_paths = project_map.module_paths()

    by_module: dict[str, dict[str, Any]] = {}
    root_patch: Path | None = None
    contracts_file: Path | None = None
    for item in files:
        relative = PurePosixPath(item.relative_to(staging_root))
        parts = relative.parts
        if parts == ("project-map.yaml",):
            root_patch = item
            continue
        if parts == ("interfaces.yaml",):
            contracts_file = item
            continue
        if parts[0] == "modules" and len(parts) >= 3:
            slot = by_module.setdefault(parts[1], {"cards": []})
            if parts[2:] == (MODULE_MAP,):
                slot["map"] = item
                continue
            if parts[2:] == (MODULE_OVERVIEW,):
                slot["overview"] = item
                continue
            if len(parts) == 4 and parts[2] == "features" and parts[3].endswith(".md"):
                slot["cards"].append(item)
                continue
        # 认不出的文件报错而不是跳过:静默跳过的话,员工以为交付了的东西没进树,
        # 而这件事没有任何症状。
        issues.append(
            ValidationIssue(
                _relative(staging_root, item),
                "staging 里不认识的文件。只允许 project-map.yaml、interfaces.yaml、"
                "modules/<模块id>/map.yaml、modules/<模块id>/overview.md、"
                "modules/<模块id>/features/<功能点id>.md",
            )
        )

    if focus and focus not in by_module:
        # 这一趟的任务就是读它。别的都写了、唯独没写它,与"无产物"是同一件事,
        # 只是更具体——报得具体,重试才修得具体。
        issues.append(
            ValidationIssue(
                f"{STAGING_DIR}/modules/{focus}/",
                f"这一趟要读 {focus},staging 里却没有它的树片段",
            )
        )

    modules: list[StagedModule] = []
    summaries = _read_root_patch(
        staging_root, root_patch, set(by_module), known_paths, focus, issues
    )
    for module_id in sorted(by_module):
        staged = _read_module(
            workspace_root,
            staging_root,
            module_id,
            by_module[module_id],
            known_paths,
            focus,
            limits,
            issues,
        )
        if staged is not None:
            modules.append(
                StagedModule(
                    id=staged.id,
                    summary=summaries.get(module_id),
                    fields=staged.fields,
                    doc_text=staged.doc_text,
                    features=staged.features,
                )
            )

    interfaces, datastores = _read_contracts(
        staging_root, contracts_file, set(known_paths), issues
    )
    if not by_module and root_patch is None and contracts_file is None:
        # 文件全都认不出来时上面已经逐个报过;这里不再追加一条空泛的总结。
        pass
    return _Fragment(
        StagedTree(modules=tuple(modules), interfaces=interfaces, datastores=datastores),
        issues,
    )


def _read_root_patch(
    staging_root: Path,
    target: Path | None,
    staged_ids: set[str],
    known_paths: dict[str, str],
    focus: str,
    issues: list[ValidationIssue],
) -> dict[str, str]:
    """根索引补丁 → 模块 id 到 summary。id 与 path 不可变,这里就是那条边界的执行点。"""
    if target is None:
        return {}
    where = _relative(staging_root, target)
    try:
        patch = parse_yaml_model(_RootPatch, read_yaml(target, where), where)
    except GenomeValidationError as error:
        issues.extend(error.issues)
        return {}
    summaries: dict[str, str] = {}
    for entry in patch.modules:
        if entry.id not in known_paths:
            issues.append(
                ValidationIssue(
                    where,
                    f"未知模块: {entry.id}(已知: {', '.join(sorted(known_paths))})。"
                    "模块由 init 确定,员工不该发明",
                )
            )
            continue
        if focus and entry.id != focus:
            issues.append(ValidationIssue(where, f"这一趟只读 {focus},不该出现 {entry.id}"))
            continue
        if entry.path != known_paths[entry.id]:
            issues.append(
                ValidationIssue(
                    where,
                    f"{entry.id}: path 不可变(现为 {known_paths[entry.id]},"
                    f"报的是 {entry.path})",
                )
            )
        expected_map = f"modules/{entry.id}/{MODULE_MAP}"
        if entry.map != expected_map:
            issues.append(
                ValidationIssue(where, f"{entry.id}: map 指针必须是 {expected_map}")
            )
        if entry.id not in staged_ids:
            issues.append(
                ValidationIssue(
                    where,
                    f"{entry.id}: 补丁里提到了它,但 staging 里没有 modules/{entry.id}/",
                )
            )
            continue
        summaries[entry.id] = entry.summary
    return summaries


def _read_module(
    workspace_root: Path,
    staging_root: Path,
    module_id: str,
    slot: dict[str, Any],
    known_paths: dict[str, str],
    focus: str,
    limits: KnowledgeConfig,
    issues: list[ValidationIssue],
) -> StagedModule | None:
    """一个 staged 模块目录。返回 `None` 表示问题大到装载不出来。"""
    map_file: Path | None = slot.get("map")
    where = (
        _relative(staging_root, map_file)
        if map_file is not None
        else f"{STAGING_DIR}/modules/{module_id}/{MODULE_MAP}"
    )
    if module_id not in known_paths:
        issues.append(
            ValidationIssue(
                where,
                f"未知模块: {module_id}(已知: {', '.join(sorted(known_paths))})。"
                "模块由 init 确定,员工不该发明",
            )
        )
        return None
    if focus and module_id != focus:
        issues.append(
            ValidationIssue(
                where,
                f"这一趟只读 {focus},不该出现 {module_id}——其余模块由各自的作业负责",
            )
        )
        return None
    if map_file is None:
        issues.append(
            ValidationIssue(where, "缺 map.yaml——认知字段与功能点清单都在它里面")
        )
        return None

    try:
        raw = read_yaml(map_file, where)
        module_map = parse_yaml_model(ModuleMap, raw, where)
    except GenomeValidationError as error:
        issues.extend(error.issues)
        return None
    if module_map.id != module_id:
        issues.append(
            ValidationIssue(where, f"map.yaml 的 id 与目录不一致: {module_map.id}", "id")
        )
        return None
    if module_map.confidence is None:
        issues.append(
            ValidationIssue(where, "缺 confidence——不确定的认知标注置信度而非编造", "confidence")
        )
    if module_map.submaps:
        issues.append(
            ValidationIssue(where, "staging 暂不支持子地图分形,先写平的,超预算再谈", "submaps")
        )
    _check_doc(staging_root, module_id, module_map, slot.get("overview"), where, issues)
    _check_lines(map_file, where, limits.module_map_lines, "模块地图", issues)

    features = _read_features(
        workspace_root,
        staging_root,
        module_id,
        raw,
        slot["cards"],
        known_paths[module_id],
        limits,
        issues,
    )
    overview: Path | None = slot.get("overview")
    return StagedModule(
        id=module_id,
        fields={key: raw[key] for key in _MODULE_FIELDS if key in raw},
        doc_text=overview.read_text(encoding="utf-8") if overview is not None else None,
        features=features,
    )


def _check_doc(
    staging_root: Path,
    module_id: str,
    module_map: ModuleMap,
    overview: Path | None,
    where: str,
    issues: list[ValidationIssue],
) -> None:
    """认知卡与 doc 指针要成对。单给一边的话,要么写了没人指、要么指了个不存在的。"""
    expected = overview_path(module_id)
    if module_map.doc is not None and module_map.doc != expected:
        issues.append(
            ValidationIssue(where, f"doc 只能指向约定位置 {expected}", "doc")
        )
        return
    if overview is not None and module_map.doc is None:
        issues.append(
            ValidationIssue(
                where, f"staging 里有 overview.md,但 map.yaml 的 doc 没指向 {expected}", "doc"
            )
        )
    if overview is None and module_map.doc is not None:
        issues.append(
            ValidationIssue(where, "doc 指向认知卡,但 staging 里没有 overview.md", "doc")
        )


def _read_features(
    workspace_root: Path,
    staging_root: Path,
    module_id: str,
    raw_map: dict[str, Any],
    card_files: list[Path],
    module_path: str,
    limits: KnowledgeConfig,
    issues: list[ValidationIssue],
) -> tuple[StagedFeature, ...] | None:
    """staged 功能点清单 + 卡片文件。**没有 `features` 键与空列表是两个意思**,前者保留现状。"""
    map_where = f"{STAGING_DIR}/modules/{module_id}/{MODULE_MAP}"
    if "features" not in raw_map:
        for orphan in card_files:
            issues.append(
                ValidationIssue(
                    _relative(staging_root, orphan),
                    "map.yaml 没有 features 键,这张卡片不会被任何功能点指向",
                )
            )
        return None

    locked = _locked_feature_ids(workspace_root, module_id)
    by_name = {item.name: item for item in card_files}
    referenced: set[str] = set()
    staged: list[StagedFeature] = []
    for index, item in enumerate(raw_map.get("features") or []):
        if not isinstance(item, dict):
            issues.append(
                ValidationIssue(map_where, "功能点条目必须是一个映射", f"features.{index}")
            )
            continue
        feature_id = str(item.get("id") or "").strip()
        if not feature_id:
            issues.append(ValidationIssue(map_where, "缺少 id", f"features.{index}"))
            continue
        if feature_id in locked:
            # 人工定过稿的整条 entry 由人定案,员工这一轮报了什么都会被丢弃——
            # 报告不必合规,但它指到的卡片文件也一并当作已引用,免得误报孤儿。
            card = item.get("card")
            if isinstance(card, str):
                referenced.add(PurePosixPath(card).name)
            continue
        if "/" in feature_id or ".." in feature_id:
            # id 会被拼成 features/<id>.md;不查的话一个 ../../ 开头的 id 能把文件写到
            # Workspace 之外去,"只写 genome/**"那条承诺当场作废。
            issues.append(
                ValidationIssue(
                    map_where, f"{feature_id}: id 不能带路径分隔符或 ..", f"features.{index}"
                )
            )
            continue
        if item.get("human_edited"):
            issues.append(
                ValidationIssue(
                    map_where,
                    f"{feature_id}: human_edited 是人工定稿的标记,员工不能自己置位",
                    f"features.{index}",
                )
            )
            continue
        try:
            entry = parse_yaml_model(FeatureEntry, item, map_where)
        except GenomeValidationError as error:
            issues.extend(error.issues)
            pointer = item.get("card")
            if isinstance(pointer, str):
                referenced.add(PurePosixPath(pointer).name)
            continue

        scopes = [
            normalize_scope(workspace_root, module_path, str(scope)) for scope in entry.scope
        ]
        if not scopes:
            issues.append(
                ValidationIssue(
                    map_where,
                    f"{feature_id}: 没有 scope,路由不到任何代码",
                    f"features.{index}",
                )
            )
        for scope in scopes:
            if not scope_exists(workspace_root, scope):
                hint = scope_hint(workspace_root, scope)
                issues.append(
                    ValidationIssue(
                        map_where,
                        f"{feature_id}: 覆盖的代码路径不存在: {scope}{hint}",
                        f"features.{index}",
                    )
                )

        card_text: str | None = None
        if entry.card is not None:
            expected = f"features/{feature_id}.md"
            if entry.card != expected:
                issues.append(
                    ValidationIssue(
                        map_where,
                        f"{feature_id}: card 指针必须是 {expected}",
                        f"features.{index}",
                    )
                )
                continue
            referenced.add(f"{feature_id}.md")
            card_file = by_name.get(f"{feature_id}.md")
            if card_file is None:
                issues.append(
                    ValidationIssue(
                        map_where,
                        f"{feature_id}: 卡片文件不在 staging 里: {expected}",
                        f"features.{index}",
                    )
                )
                continue
            card_text = _read_card(
                workspace_root, staging_root, feature_id, card_file, module_path, limits, issues
            )
            if card_text is None:
                continue
        staged.append(
            StagedFeature(
                id=feature_id,
                summary=entry.summary,
                scope=tuple(scopes),
                card_text=card_text,
                no_card=entry.no_card,
            )
        )

    for unreferenced in sorted(set(by_name) - referenced):
        issues.append(
            ValidationIssue(
                _relative(staging_root, by_name[unreferenced]),
                "孤儿卡片:staging 的功能点清单里没有任何条目指向它",
            )
        )
    return tuple(staged)


def _read_card(
    workspace_root: Path,
    staging_root: Path,
    feature_id: str,
    card_file: Path,
    module_path: str,
    limits: KnowledgeConfig,
    issues: list[ValidationIssue],
) -> str | None:
    """一张 staged 功能卡片。走树自己的模型(`FeatureCard`),不为 staging 放松半分。"""
    from agentgenome.genome.features import FeatureCard

    where = _relative(staging_root, card_file)
    text = card_file.read_text(encoding="utf-8")
    try:
        front, body = split_front_matter(text, where)
        front["body"] = body
        card = parse_yaml_model(FeatureCard, front, where)
    except GenomeValidationError as error:
        issues.extend(error.issues)
        return None
    if card.id != feature_id:
        issues.append(
            ValidationIssue(where, f"卡片 id 与功能点不一致: {card.id} != {feature_id}", "id")
        )
        return None
    for scope in card.scope:
        if not scope_exists(workspace_root, scope):
            issues.append(
                ValidationIssue(
                    where,
                    f"覆盖的代码路径不存在: {scope}{scope_hint(workspace_root, scope)}",
                    "scope",
                )
            )
    _check_anchors(workspace_root, module_path, card.body, where, issues)
    _check_lines(card_file, where, limits.card_lines, "功能卡片", issues)
    return text


#: 行内代码里"像一条文件引用"的形状:路径 + 可选 `:行号` 或 `::符号名` 锚点。
#: 整个行内代码段必须完全是这个形状才算引用——glob(带 `*`)、命令行(带空格)、
#: 占位符(带 `<>`)都匹配不上,于是天然被跳过。
_ANCHOR = re.compile(
    r"^(?P<path>[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)(?P<suffix>:\d+(?:-\d+)?|::[A-Za-z0-9_.]+)?$"
)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def _inline_spans(body: str) -> Iterator[str]:
    """卡片正文里的行内代码段,不含围栏代码块——围栏里是示例,不是引用。"""
    fenced = False
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            continue
        if not fenced:
            yield from _INLINE_CODE.findall(line)


def _check_anchors(
    workspace_root: Path,
    module_path: str,
    body: str,
    where: str,
    issues: list[ValidationIssue],
) -> None:
    """证据锚点必真(PRD 41):正文引用的仓内路径必须存在,行号允许漂移。

    只查机器可查的那一半——存在性。"不变量写得对不对"留给评审与对抗 QA。
    基准与 scope 同一套:Workspace 相对,或模块目录相对(见 `normalize_scope` 的理由)。
    只认最后一段带扩展名的(文件引用);目录归 scope 字段管,这里不重复查。

    **不带斜杠的裸文件名,只在带了 `:行号`/`::符号` 锚点时才算引用。** 卡片正文提到
    `task.json` 这种运行态概念是合法的散文,不是引用;但 `runner.py:12` 明确是在指认
    一处代码——纪律教的"模块目录相对"允许指到模块根,校验就得认同样的形状(ADR-0011:
    提示词教什么,这里就查什么)。
    """
    base = module_path.rstrip("/")
    missing: dict[str, None] = {}
    for span in _inline_spans(body):
        matched = _ANCHOR.match(span)
        if matched is None:
            continue
        path = matched.group("path")
        if "." not in path.rsplit("/", 1)[-1]:
            continue
        if "/" not in path and matched.group("suffix") is None:
            continue
        if (Path(workspace_root) / path).exists():
            continue
        if base and (Path(workspace_root) / base / path).exists():
            continue
        missing.setdefault(path, None)
    for path in missing:
        issues.append(
            ValidationIssue(
                where,
                f"引用的仓内路径不存在: {path}(按 Workspace 根与模块目录都没找到)。"
                "锚点写 `路径:行号` 或 `路径::符号名`,行号允许漂移、存在性不允许;"
                "不是文件引用的话,不要把它写成行内代码里的路径形状",
            )
        )


def _read_contracts(
    staging_root: Path,
    target: Path | None,
    known_ids: set[str],
    issues: list[ValidationIssue],
) -> tuple[tuple[Interface, ...] | None, tuple[Datastore, ...] | None]:
    """staged 契约索引。形状就是最终的 `interfaces.yaml`,每条要带 confidence。"""
    if target is None:
        return None, None
    where = _relative(staging_root, target)
    try:
        contracts = parse_yaml_model(ContractIndex, read_yaml(target, where), where)
    except GenomeValidationError as error:
        issues.extend(error.issues)
        return None, None
    for index, item in enumerate(contracts.interfaces):
        if item.confidence is None:
            issues.append(
                ValidationIssue(
                    where,
                    f"{item.id}: 缺 confidence——每条结论标注置信度,契约尤其",
                    f"interfaces.{index}",
                )
            )
        for name in (item.provider, *item.consumers):
            if name not in known_ids:
                issues.append(
                    ValidationIssue(
                        where, f"{item.id}: 引用的模块不存在: {name}", f"interfaces.{index}"
                    )
                )
    for index, store in enumerate(contracts.datastores):
        if store.confidence is None:
            issues.append(
                ValidationIssue(
                    where, f"{store.id}: 缺 confidence", f"datastores.{index}"
                )
            )
        if store.owner not in known_ids:
            issues.append(
                ValidationIssue(
                    where, f"{store.id}: 引用的模块不存在: {store.owner}", f"datastores.{index}"
                )
            )
    return tuple(contracts.interfaces), tuple(contracts.datastores)


def _check_lines(
    target: Path, where: str, limit: int, what: str, issues: list[ValidationIssue]
) -> None:
    """行数预算在 staging 就查。等合进树再查的话,超限的回滚发生在整棵树写完之后——
    员工白烧一轮,人也白等一轮。判定口径与 `budgets.check_budgets` 完全一致。"""
    actual = len(target.read_text(encoding="utf-8").splitlines())
    if actual > limit:
        issues.append(
            ValidationIssue(
                where, f"{what}超出行数预算: {actual} 行 > {limit} 行。拆开它(子目录 + 子地图)。"
            )
        )


def check_card_file(
    workspace_root: Path,
    staging_root: Path,
    feature_id: str,
    card_file: Path,
    module_path: str,
    limits: KnowledgeConfig | None = None,
) -> tuple[str | None, list[ValidationIssue]]:
    """对一张 staged 卡片文件跑树的全部卡片规则(front matter、scope、锚点、行数)。

    深化工序(PRD 41)的通道只装一张卡,但纪律必须与整树 staging 完全相同——
    这里是那套规则的唯一出口,深化不自己再养一套。
    """
    issues: list[ValidationIssue] = []
    text = _read_card(
        Path(workspace_root),
        Path(staging_root),
        feature_id,
        Path(card_file),
        module_path,
        limits or KnowledgeConfig(),
        issues,
    )
    return text, issues


# --- 对外入口 -----------------------------------------------------------------


def validate_knowledge_staging(
    workspace_root: Path,
    staging_root: Path,
    focus: str = "",
    limits: KnowledgeConfig | None = None,
) -> list[ValidationIssue]:
    """对一份知识 staging 跑确定性校验。空清单即全绿。"""
    return _read_fragment(Path(workspace_root), Path(staging_root), focus, limits).issues


def load_knowledge_staging(
    workspace_root: Path,
    staging_root: Path,
    focus: str = "",
    limits: KnowledgeConfig | None = None,
) -> StagedTree:
    """装载一份 staging。**不合法就抛**——装载不做第二套宽松标准。"""
    fragment = _read_fragment(Path(workspace_root), Path(staging_root), focus, limits)
    if fragment.issues:
        raise GenomeValidationError(fragment.issues)
    return fragment.tree


def render_issues(issues: list[ValidationIssue]) -> str:
    """逐文件报错的渲染。**这是回注上下文的质量下限**:每条错误定位到文件、按文件分组、
    一句话说清哪条约束没过——含糊的"格式不对"只会让第二次错得一模一样。"""
    by_file: dict[str, list[ValidationIssue]] = {}
    for issue in issues:
        by_file.setdefault(issue.file, []).append(issue)
    lines = ["staging 校验未通过。只修下面列出的文件,已通过的文件不要重写:"]
    for name in sorted(by_file):
        for issue in by_file[name]:
            where = f"{name}:{issue.location}" if issue.location else name
            lines.append(f"- {where}: {issue.message}")
    return "\n".join(lines)


def knowledge_output_check(
    workspace_root: Path, focus: str = "", limits: KnowledgeConfig | None = None
) -> Callable[[Path], str | None]:
    """给 `JobSpec.output_check` 用的裁决闭包。返回 `None` 即通过,否则是拒绝原因。

    这一步是 Job 成败的判据本身,不是事后检查:它跑在契约重试外环**里面**,失败按
    格式失败处理——回注逐文件报错、在同一个 output_dir 上重试(增量修复,见 PRD 34 D3)。
    """
    root = Path(workspace_root)

    def check(output_dir: Path) -> str | None:
        issues = validate_knowledge_staging(root, output_dir / STAGING_DIR, focus, limits)
        return render_issues(issues) if issues else None

    return check


def read_receipt(target: Path) -> dict[str, Any]:
    """读一张小票文件。读不出来给空——小票只补充校对清单,不参与裁决,坏了不该炸汇总。"""
    if not Path(target).is_file():
        return {}
    try:
        loaded = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


# --- 经验卡片(distill)的 staging ---------------------------------------------

#: 蒸馏产出在 staging 里的位置:`staging/lessons/<slug>.md`。
LESSONS_DIR = "lessons"


class _StagedLesson(BaseModel):
    """一张 staged 经验卡片的 front matter。

    形状就是 `LessonCard` 减去编排器专属的字段:`id` 由入库时按 `next_number` 分配
    (只增不复用,证据链接要能一直指得住),`hits`/`archived`/`created_from` 是
    生命周期账,员工碰不到。**证据与适用条件的防污染过滤不在这里**——那两道是
    `parse_cards`/`lint` 的活,被过滤要留痕,而格式校验的拒绝走契约重试。
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    level: str
    applies_to: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


def _read_lessons(staging_root: Path) -> tuple[list[dict[str, Any]], list[ValidationIssue]]:
    """staged 经验卡片:一次遍历,同时装载与校验。

    **staging 缺失或为空是合法的**——"一次任务提炼出零条经验"是正常结果(宁可少写),
    这一点与知识树的"无产物即失败"刻意不同。
    """
    from agentgenome.genome.evolution.cards import Level

    issues: list[ValidationIssue] = []
    candidates: list[dict[str, Any]] = []
    if not Path(staging_root).is_dir():
        return candidates, issues
    for item in sorted(Path(staging_root).rglob("*")):
        if not item.is_file():
            continue
        relative = PurePosixPath(item.relative_to(staging_root))
        where = str(PurePosixPath(STAGING_DIR) / relative)
        if len(relative.parts) != 2 or relative.parts[0] != LESSONS_DIR or item.suffix != ".md":
            issues.append(
                ValidationIssue(where, "staging 里不认识的文件。经验卡片只能是 lessons/<名字>.md")
            )
            continue
        try:
            front, body = split_front_matter(item.read_text(encoding="utf-8"), where)
            lesson = parse_yaml_model(_StagedLesson, front, where)
        except GenomeValidationError as error:
            issues.extend(error.issues)
            continue
        if not lesson.title.strip():
            issues.append(ValidationIssue(where, "title 是空的", "title"))
            continue
        if lesson.level not in {item.value for item in Level}:
            issues.append(
                ValidationIssue(
                    where,
                    f"level 不认识: {lesson.level}"
                    f"(允许: {', '.join(item.value for item in Level)})",
                    "level",
                )
            )
            continue
        if not body.strip():
            issues.append(ValidationIssue(where, "结论正文是空的——front matter 之后要写结论"))
            continue
        candidate: dict[str, Any] = {
            "title": lesson.title,
            "applies_to": lesson.applies_to,
            "conclusion": body,
            "evidence": lesson.evidence,
            "level": lesson.level,
        }
        if lesson.confidence is not None:
            candidate["confidence"] = lesson.confidence
        candidates.append(candidate)
    return candidates, issues


def validate_lessons_staging(staging_root: Path) -> list[ValidationIssue]:
    """对蒸馏的 staging 跑确定性校验。空清单即全绿(零张卡片也算绿)。"""
    return _read_lessons(staging_root)[1]


def load_lesson_candidates(staging_root: Path) -> list[dict[str, Any]]:
    """把 staged 经验卡片装载成 `parse_cards` 吃的候选形状。不合法就抛。"""
    candidates, issues = _read_lessons(staging_root)
    if issues:
        raise GenomeValidationError(issues)
    return candidates


def lessons_output_check() -> Callable[[Path], str | None]:
    """给 `JobSpec.output_check` 用的蒸馏裁决闭包。"""

    def check(output_dir: Path) -> str | None:
        issues = validate_lessons_staging(output_dir / STAGING_DIR)
        return render_issues(issues) if issues else None

    return check


def output_check_for(
    kind: str | None, workdir: Path, limits: KnowledgeConfig | None = None
) -> Callable[[Path], str | None] | None:
    """按工序声明的 staging 种类(`outputs.staging`)给出对应的裁决。

    认不出的种类直接抛——静默给 `None` 等于"声明了校验却没有校验",而那正是这条
    声明存在要防的事。种类枚举由 `procedures.Outputs` 在加载时把关,这里的抛错只
    防两处枚举漂移。
    """
    if kind is None:
        return None
    if kind == "lessons":
        return lessons_output_check()
    if kind == "knowledge-tree":
        return knowledge_output_check(workdir, limits=limits)
    raise ValueError(f"不认识的 staging 种类: {kind}")


# --- 原子应用 -----------------------------------------------------------------


def apply_staged(
    workspace_root: Path, staged: StagedTree, limits: KnowledgeConfig | None = None
) -> KnowledgeUpdate:
    """把已验证的 staging 合并进项目地图与认知卡片。

    **这一步是知识写入路径上唯一的门禁点。** 输入应当来自 `load_knowledge_staging`,
    但产物目录在校验与应用之间可能被手改过,所以路径安全那几条(未知模块、越界 id)
    在这里再拦一次——它们失守的代价是写到 Workspace 之外,不是"知识差一点"。
    """
    from agentgenome.genome.budgets import check_budgets
    from agentgenome.genome.loader import load_tree
    from agentgenome.genome.tree import write_tree

    project_map = load_tree(workspace_root).project_map
    known_ids = project_map.module_ids()
    issues: list[ValidationIssue] = []
    relative = str(paths.PROJECT_MAP)
    for entry in staged.modules:
        if entry.id not in known_ids:
            issues.append(
                ValidationIssue(
                    relative,
                    f"结果里出现未知模块: {entry.id}"
                    f"(已知: {', '.join(sorted(known_ids))})。模块由 init 确定,员工不该发明",
                )
            )
            continue
        for feature in entry.features or ():
            if "/" in feature.id or ".." in feature.id:
                issues.append(
                    ValidationIssue(relative, f"{feature.id}: id 不能带路径分隔符或 ..")
                )
    if issues:
        raise GenomeValidationError(issues)

    cards_written: list[str] = []
    cards_preserved: list[str] = []
    by_id = {module.id: module for module in project_map.modules}
    features: dict[str, list[dict[str, Any]]] = {}
    # **写之前留底。** 校验在写之后跑(它要看整棵树),而拦下来的那一次已经改了磁盘——
    # 留在半更新状态的树是不合法的,于是**下一次写入在第一行的 `load_tree` 就失败**:
    # 一次被拒的产出,把整条知识路径永久卡死。
    restore = Snapshot(workspace_root)

    for entry in staged.modules:
        module = by_id[entry.id]
        if entry.summary is not None:
            module.summary = entry.summary
        for name in _MODULE_FIELDS:
            if name in entry.fields and entry.fields[name] is not None:
                setattr(module, name, entry.fields[name])
        # 认知卡住在模块子目录里。约定只有 `tree.overview_path` 一处说得出来。
        doc_relative = overview_path(module.id)
        module.doc = doc_relative
        if _write_card(Path(workspace_root) / doc_relative, entry.doc_text):
            cards_written.append(doc_relative)
        else:
            cards_preserved.append(doc_relative)
        if entry.features is not None:
            found, wrote, kept = _write_features(workspace_root, module.id, entry.features)
            features[module.id] = found
            cards_written += wrote
            cards_preserved += kept

    if staged.interfaces is not None:
        project_map.interfaces = list(staged.interfaces)
    if staged.datastores is not None:
        project_map.datastores = list(staged.datastores)

    from datetime import UTC, datetime

    project_map.version += 1
    project_map.updated_at = datetime.now(UTC)
    # 写回知识树。**不是覆盖一个文件**——`write_tree` 会保住各模块已有的功能点清单。
    write_tree(workspace_root, project_map, features=dict(features))

    # 写完立刻整体校验一遍:产出必须是合法状态,不能带病入库。
    #
    # **走 `load_tree` 而不是 `load_project_map`,并且查行数预算。** staging 校验查的是
    # 片段自身;这里查的是"合并之后的整棵树"——孤儿、引用完整性、跨片段的问题只有在
    # 这里才看得见,而这两道不是同一道。
    try:
        tree = load_tree(workspace_root)
        over = check_budgets(workspace_root, tree, limits or KnowledgeConfig())
        if over:
            raise GenomeValidationError(over)
    except GenomeValidationError:
        restore.rollback()
        raise
    return KnowledgeUpdate(project_map.version, cards_written, cards_preserved, restore.touched)


class Snapshot:
    """写之前给整棵知识树留一份底,失败时还原;成功时说出哪些文件真的变了。

    **不用 `git checkout` 还原。** 那会把同一个检出里别人未提交的改动一起还原掉,而这一层
    根本不知道那些改动是谁的。

    **也是提交清单的来源。** 拿 `genome/**` 整个 add 去提交的话,别人未提交的改动(人手改的
    规则、蒸馏刚写的经验卡片)会被一起打包进这次知识提交——署名成员工、标题写着知识更新,
    而那不是它。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._before = {path: path.read_text(encoding="utf-8") for path in self._files()}

    def _files(self) -> list[Path]:
        base = self.root / paths.KNOWLEDGE
        return sorted(item for item in base.rglob("*") if item.is_file()) if base.is_dir() else []

    @property
    def touched(self) -> list[str]:
        """写完之后与写之前不一样的那些(含新增)。"""
        found = []
        for path in self._files():
            before = self._before.get(path)
            if before is None or before != path.read_text(encoding="utf-8"):
                found.append(str(path.relative_to(self.root)))
        return sorted(found)

    def rollback(self) -> None:
        for path in self._files():
            if path not in self._before:
                path.unlink(missing_ok=True)
        for path, before in self._before.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(before, encoding="utf-8")


def normalize_scope(workspace_root: Path, module_path: str, scope: str) -> str:
    """把一条覆盖范围补成 **Workspace 相对**。补不了就原样返回,交给校验去报。

    `scope` 是 Workspace 相对的(路由拿来匹配的变更清单就是这个基准),而架构员工是在
    **深读单个模块**——它自然会写 `src/order/app.py`。让它同时记住两套基准本来就不合理:
    同一份 YAML 里 `card` 还是模块相对的。"这条路径在模块目录下存在"是机器一查就知道的
    事——能算出来的不该让人重跑。

    只在**原路径不存在、而补上模块前缀之后存在**时才补。两边都存在时不动:那说明员工写的
    就是 Workspace 相对的,改它反而会指错地方。
    """
    prefix = concrete_prefix(scope)
    if not prefix or (Path(workspace_root) / prefix).exists():
        return scope
    base = module_path.rstrip("/")
    if not base:
        return scope
    if (Path(workspace_root) / base / prefix).exists():
        return f"{base}/{scope}"
    return scope


def _locked_feature_ids(workspace_root: Path, module_id: str) -> set[str]:
    """已被人工定稿(`human_edited: true`)的功能点 id。

    这些 id 的整条 entry 由人定案,员工这一轮不管报了什么都会被丢弃——校验也照这个口径:
    员工对锁定 id 的报告不必合规,反正不会落盘。
    """
    existing = existing_features(module_dir(workspace_root, module_id) / MODULE_MAP)
    return {
        str(item["id"])
        for item in existing
        if isinstance(item, dict) and item.get("human_edited") and item.get("id")
    }


def _write_card(path: Path, markdown: str | None) -> bool:
    """写一张认知卡片。返回是否真的写了。

    带人工编辑标记的卡片不被覆盖——不然"我改过的东西下次扫描就没了",
    没人会愿意去改基因组。
    """
    if markdown is None:
        return False
    if path.is_file() and HUMAN_EDITED_MARKER in path.read_text(encoding="utf-8"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return True


def _write_features(
    workspace_root: Path,
    module_id: str,
    reported: tuple[StagedFeature, ...],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """把一个模块的功能点写成清单条目 + 卡片文件。产出已经过 staging 校验。

    **这一轮没再报的功能点,它的卡片要一起处理。** 只换清单不动文件的话,那张卡片会变成
    没有任何功能点指向它的孤儿——而孤儿卡片会让整棵树校验不过,于是下一次写入在第一行就
    失败:一次重新拆分模块的正常操作,把知识树永久卡死。

    **人手改过的卡片正文不删也不覆盖**(见 `HUMAN_EDITED_MARKER`)。**人工定过稿的整条
    entry(`human_edited: true`)原样留下,连 `scope` 都不换**——锁定护的是清单条目本身,
    标记只护得住卡片文件里的文字。
    """
    directory = module_dir(workspace_root, module_id)
    locked = _locked_feature_ids(workspace_root, module_id)
    existing_by_id = {
        str(item["id"]): item
        for item in existing_features(directory / MODULE_MAP)
        if isinstance(item, dict) and item.get("id")
    }
    entries: list[dict[str, Any]] = []
    written: list[str] = []
    preserved: list[str] = []
    seen: set[str] = set()
    for item in reported:
        seen.add(item.id)
        entry: dict[str, Any] = {
            "id": item.id,
            "summary": item.summary,
            "scope": list(item.scope),
        }
        if item.no_card:
            entry["no_card"] = item.no_card
        else:
            relative = f"features/{item.id}.md"
            entry["card"] = relative
            target = directory / relative
            if _write_card(target, item.card_text):
                written.append(str(target.relative_to(workspace_root)))
            else:
                preserved.append(str(target.relative_to(workspace_root)))
        entries.append(entry)

    for feature_id in sorted(locked):
        # 锁定的条目原样保留。staging 校验已经把员工对它们的报告整条丢掉了,
        # 这里按现状回填——不回填的话,一次重跑就把人定的稿从清单里挤出去了。
        if feature_id in seen:
            continue
        locked_entry = existing_by_id.get(feature_id)
        if locked_entry is None:
            continue
        seen.add(feature_id)
        entries.append(locked_entry)
        card = locked_entry.get("card")
        if card:
            preserved.append(str((directory / str(card)).relative_to(workspace_root)))

    kept, removed = _retire_features(workspace_root, module_id, seen)
    return [*entries, *kept], written, [*preserved, *removed]


def _retire_features(
    workspace_root: Path, module_id: str, seen: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """这一轮没再报的功能点怎么办。

    人工定过稿的(`human_edited: true`)留着,不管卡片文件里有没有标记——`no_card` 的
    锁定 entry 根本没有卡片文件可查。人手改过卡片正文的也留着(`HUMAN_EDITED_MARKER`)。
    其余的删掉卡片文件、条目也不再保留——留着文件不留条目就是孤儿,而孤儿会把整棵树
    判成不合法。
    """
    directory = module_dir(workspace_root, module_id)
    existing = existing_features(directory / MODULE_MAP)
    kept: list[dict[str, Any]] = []
    preserved: list[str] = []
    for item in existing:
        if not isinstance(item, dict) or str(item.get("id") or "") in seen:
            continue
        if item.get("human_edited"):
            kept.append(item)
            card = item.get("card")
            if card:
                preserved.append(str((directory / str(card)).relative_to(workspace_root)))
            continue
        card = item.get("card")
        target = directory / str(card) if card else None
        if target is not None and target.is_file():
            if HUMAN_EDITED_MARKER in target.read_text(encoding="utf-8"):
                kept.append(item)
                preserved.append(str(target.relative_to(workspace_root)))
                continue
            target.unlink()
    return kept, preserved


__all__ = [
    "HUMAN_EDITED_MARKER",
    "LESSONS_DIR",
    "RECEIPT_SCHEMA",
    "STAGING_DIR",
    "KnowledgeUpdate",
    "Snapshot",
    "StagedFeature",
    "StagedModule",
    "StagedTree",
    "apply_staged",
    "check_card_file",
    "knowledge_output_check",
    "lessons_output_check",
    "load_knowledge_staging",
    "load_lesson_candidates",
    "normalize_scope",
    "output_check_for",
    "read_receipt",
    "render_issues",
    "validate_knowledge_staging",
    "validate_lessons_staging",
]
