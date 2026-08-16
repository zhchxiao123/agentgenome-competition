"""知识地图的三层结构:根索引、契约索引、模块地图。

## 为什么不是一个文件

单文件项目地图在两个模块的示例仓里工作得很好,在五十个模块、几千个功能点的中台上必然失效
——要么膨胀成一份没人愿意打开的两千行 YAML,要么被整份塞进每一个员工的上下文包,把预算
吃光。两种失效都不是"慢",是"错":地图开始撒谎而没有任何东西会告诉你,员工带着一屏无关
知识上岗而真正相关的那几十行被淹没在里面。

## 每层只回答一个问题

- **根索引**(`project-map.yaml`):项目是什么、有哪些模块、去哪找它们。模块条目只有身份
  与指针——`id` / `path` / `summary` / 指向模块地图的 `map`。
- **契约索引**(`interfaces.yaml`):跨模块契约与数据存储。它们是**跨模块的事实**,挂在
  任何一个模块名下都是错的。
- **模块地图**(`modules/<id>/map.yaml`):这个模块怎么跑——语言、入口、测试与构建命令、
  依赖、认知卡。

## 装配出来的东西刻意不变

上层有三十处消费项目地图这个类型(上下文组装、集测判定、门禁配置、风险评级、图谱同步、
REST 接口……)。这个模块只改**它从哪儿装配出来**,不改它装配出来长什么样——否则一次知识
分层的改动会波及全仓,而那和分层本身毫无关系。

功能点与功能卡片(第三层)不在这里——见 `genome.features`。这个模块只管"树在磁盘上怎么摆、
怎么装配回上层要的那个扁平模型",那一层管"知识本身完不完备"。两件事的变化原因不同。

装配与写出是一对互逆操作,`migrate_flat_map` 骑在这对操作上:用旧模型读进来,再用新结构
写出去。它额外要做的只有一件事——旧约定把认知卡平铺成 `modules/<id>.md`,树把一个模块的
东西收在一处。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from agentgenome import paths
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.models import Datastore, Interface, Module, ProjectInfo, ProjectMap
from agentgenome.genome.yamlio import parse_yaml_model, read_yaml

_STRICT = ConfigDict(extra="forbid")

#: 模块地图与模块认知卡在模块子目录里的文件名。
MODULE_MAP = "map.yaml"
MODULE_OVERVIEW = "overview.md"

_ROOT_HEADER = (
    "# 知识地图 L0 根索引:项目是什么、有哪些模块、去哪找它们。\n"
    "# 「这个模块怎么跑」属于 modules/<id>/map.yaml;跨模块契约属于 interfaces.yaml。\n"
)
_CONTRACTS_HEADER = "# 跨模块契约与数据存储。它们是跨模块的事实,挂在任何一个模块名下都是错的。\n"
_MODULE_HEADER = "# 模块地图 L1:这个模块怎么跑、依赖谁。\n"


class RootModuleEntry(BaseModel):
    """根索引里的一个模块:**只有身份与指针**。"""

    model_config = _STRICT

    id: str
    path: str
    summary: str = ""
    #: 指向模块地图,相对知识目录。
    map: str


class RootIndex(BaseModel):
    model_config = _STRICT

    version: int = 1
    updated_at: datetime | None = None
    project: ProjectInfo
    modules: list[RootModuleEntry] = Field(default_factory=list)


class ContractIndex(BaseModel):
    model_config = _STRICT

    interfaces: list[Interface] = Field(default_factory=list)
    datastores: list[Datastore] = Field(default_factory=list)


class ModuleMap(BaseModel):
    """一个模块怎么跑,以及它被拆成了哪几个功能点。"""

    model_config = _STRICT

    id: str
    lang: str | None = None
    entrypoints: list[str] = Field(default_factory=list)
    test_cmd: str | None = None
    build_cmd: str | None = None
    itest_cmd: str | None = None
    junit_xml_path: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    #: 面向人的模块认知卡,**Workspace 相对路径**。
    #:
    #: 存基名会更"整洁",但那要求装配时把目录拼回去,而拼回去的目录只能是约定的那一个——
    #: 于是任何指到别处的认知卡在一次写回之后就指向了一个不存在的文件。往返必须逐字段
    #: 相等,这一层才敢说"只改存储、不改语义"。
    doc: str | None = None
    confidence: float | None = None
    #: 功能点清单。**这一层原样带过,不解析。**
    #:
    #: 解析成模型的话,一个手写的拼写错误会让整份模块地图读不出来——而这一层的调用方
    #: (`assemble`、`write_tree`)只想知道"这个模块怎么跑",不关心功能点对不对。
    #: 校验属于 `genome.features`,那里能逐条报而不是整份炸掉。
    features: list[Any] = Field(default_factory=list)
    #: 子地图指针(相对模块目录)。**超大模块继续分形:子目录 + 子地图,规则原样递归。**
    #: 不为分形引入第二套模型——为大模块新增例外的话,"每层有预算"这条就有了逃逸口。
    submaps: list[str] = Field(default_factory=list)


def module_dir(workspace_root: Path, module_id: str) -> Path:
    return Path(workspace_root) / paths.MODULES / module_id


def overview_path(module_id: str) -> str:
    """一个模块的认知卡按约定住在哪(Workspace 相对路径)。

    **只有这一处**说得出这个约定。此前它被抄在五个地方——写卡片的、迁移的、装配的、
    两处夹具——而抄五遍的东西改一遍是改不完的。
    """
    return str(paths.MODULES / module_id / MODULE_OVERVIEW)


def is_flat_map(workspace_root: Path) -> bool:
    """这个 Workspace 里的还是旧的单文件地图吗。

    靠"根索引里没有的键"识别:旧文件把契约与数据存储放在顶层,模块条目里带 `test_cmd`
    这类运行信息。严格模式会把它们报成"多了个字段",而那读起来像拼写错误,不像"该迁移了"。
    """
    path = Path(workspace_root) / paths.PROJECT_MAP
    if not path.is_file():
        return False
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return False
    if not isinstance(loaded, dict):
        return False
    if "interfaces" in loaded or "datastores" in loaded:
        return True
    modules = loaded.get("modules") or []
    return any(isinstance(item, dict) and "map" not in item for item in modules)


@dataclass(frozen=True)
class Assembled:
    """一次装配的产出。

    功能点跟着出来,是因为**读它们的那次 I/O 已经发生了**。让下一层自己再读一遍的话,它读
    的会是约定路径而不是根索引里那个指针——两者一旦不同(分形、改名)就读错文件,而且那次
    读取抛出去会把此前攒的问题全丢掉。
    """

    project_map: ProjectMap
    #: 模块 id → 原始功能点条目。这一层不解析它们,见 `ModuleMap.features`。
    module_features: dict[str, list[Any]] = dataclass_field(default_factory=dict)
    #: 模块 id → 子地图指针。分形的入口。
    module_submaps: dict[str, list[str]] = dataclass_field(default_factory=dict)


def assemble(workspace_root: Path) -> Assembled:
    """把树读成上层要的那个项目地图。

    **一次报全。** 缺三个模块地图时报三条,而不是让人修一个跑一次。
    """
    root = Path(workspace_root)
    knowledge = root / paths.KNOWLEDGE
    relative = str(paths.PROJECT_MAP)
    root_index = parse_yaml_model(
        RootIndex, read_yaml(root / paths.PROJECT_MAP, relative), relative
    )

    issues: list[ValidationIssue] = []
    contracts = ContractIndex()
    contracts_relative = str(paths.INTERFACES)
    try:
        contracts = parse_yaml_model(
            ContractIndex,
            read_yaml(root / paths.INTERFACES, contracts_relative),
            contracts_relative,
        )
    except GenomeValidationError as error:
        issues.extend(error.issues)

    modules: list[Module] = []
    module_features: dict[str, list[Any]] = {}
    module_submaps: dict[str, list[str]] = {}
    for entry in root_index.modules:
        relative = str(paths.KNOWLEDGE / entry.map)
        try:
            module_map = parse_yaml_model(
                ModuleMap, read_yaml(knowledge / entry.map, relative), relative
            )
        except GenomeValidationError as error:
            issues.extend(error.issues)
            continue
        if module_map.id != entry.id:
            issues.append(
                ValidationIssue(relative, f"模块地图的 id 与根索引不一致: {module_map.id}", "id")
            )
            continue
        modules.append(_to_module(entry, module_map))
        module_features[entry.id] = module_map.features
        module_submaps[entry.id] = module_map.submaps

    if issues:
        raise GenomeValidationError(issues)
    return Assembled(
        project_map=ProjectMap(
            version=root_index.version,
            updated_at=root_index.updated_at,
            project=root_index.project,
            modules=modules,
            interfaces=contracts.interfaces,
            datastores=contracts.datastores,
        ),
        module_features=module_features,
        module_submaps=module_submaps,
    )


def _to_module(entry: RootModuleEntry, module_map: ModuleMap) -> Module:
    return Module(
        id=entry.id,
        path=entry.path,
        lang=module_map.lang,
        summary=entry.summary,
        entrypoints=module_map.entrypoints,
        test_cmd=module_map.test_cmd,
        build_cmd=module_map.build_cmd,
        itest_cmd=module_map.itest_cmd,
        junit_xml_path=module_map.junit_xml_path,
        depends_on=module_map.depends_on,
        doc=module_map.doc,
        confidence=module_map.confidence,
    )


def _dump(payload: dict[str, Any], header: str) -> str:
    return header + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def write_tree(
    workspace_root: Path,
    project_map: ProjectMap,
    features: dict[str, list[Any]] | None = None,
) -> None:
    """把一个装配好的项目地图写回树。`assemble` 的逆操作。

    **已有的功能点清单被保留。** 项目地图这个类型不带功能点,直接覆盖模块地图会把它们
    静默抹掉——而"知识莫名其妙少了一块"是最难查的那种故障。

    `features` 给出某个模块的新清单时才覆盖那一个模块的;没给的模块照旧原样带过。**按模块
    覆盖而不是整棵树覆盖**:一次只读了一个模块的重建,不该把其余模块的功能点一起清掉——
    那正是上一段说的那种故障,只是从另一条路进来。
    """
    root = Path(workspace_root)
    knowledge = root / paths.KNOWLEDGE
    knowledge.mkdir(parents=True, exist_ok=True)

    entries = []
    for module in project_map.modules:
        target = module_dir(root, module.id)
        target.mkdir(parents=True, exist_ok=True)
        # **`in` 而不是 `or`。** 用 `or` 的话,一个"这个模块的功能点全删了"的合法结论会
        # 落回磁盘上的旧清单——写入方以为清空了,树上却原样留着。
        existing = (
            features[module.id]
            if features and module.id in features
            else existing_features(target / MODULE_MAP)
        )
        existing_submaps = _existing_key(target / MODULE_MAP, "submaps")
        module_map = ModuleMap(
            id=module.id,
            lang=module.lang,
            entrypoints=module.entrypoints,
            test_cmd=module.test_cmd,
            build_cmd=module.build_cmd,
            itest_cmd=module.itest_cmd,
            junit_xml_path=module.junit_xml_path,
            depends_on=module.depends_on,
            doc=module.doc,
            confidence=module.confidence,
            features=existing,
            submaps=[str(item) for item in existing_submaps],
        )
        (target / MODULE_MAP).write_text(
            _dump(module_map.model_dump(mode="json", exclude_none=True), _MODULE_HEADER),
            encoding="utf-8",
        )
        entries.append(
            RootModuleEntry(
                id=module.id,
                path=module.path,
                summary=module.summary,
                map=str(Path(paths.MODULES.name) / module.id / MODULE_MAP),
            )
        )

    root_index = RootIndex(
        version=project_map.version,
        updated_at=project_map.updated_at,
        project=project_map.project,
        modules=entries,
    )
    (root / paths.PROJECT_MAP).write_text(
        _dump(root_index.model_dump(mode="json", exclude_none=True), _ROOT_HEADER), encoding="utf-8"
    )

    contracts = ContractIndex(interfaces=project_map.interfaces, datastores=project_map.datastores)
    (root / paths.INTERFACES).write_text(
        _dump(
            contracts.model_dump(mode="json", by_alias=True, exclude_none=True), _CONTRACTS_HEADER
        ),
        encoding="utf-8",
    )


class NothingToMigrate(RuntimeError):
    """这里没有可迁移的单文件地图。"""


def migrate_flat_map(workspace_root: Path) -> tuple[ProjectMap, list[str]]:
    """把旧的单文件地图拆成树。返回迁移后的地图与被搬动的认知卡模块清单。

    **确定性,不经 Agent。** 它只搬位置,一个字的知识都不改。

    **不建任何功能点。** 旧地图里没有那一层,凭空捏造就是污染——功能点由知识初始化读代码
    之后产出。

    旧约定把认知卡平铺成 `modules/<id>.md`;树把一个模块的东西收在一处。搬完连同新指针
    **一次写出**,分两次写的话中间那一刻的树是不自洽的。
    """
    root = Path(workspace_root)
    relative = str(paths.PROJECT_MAP)
    if not (root / paths.PROJECT_MAP).is_file():
        raise NothingToMigrate(f"没有 {relative},这里还不是一个 Workspace。")
    if not is_flat_map(root):
        raise NothingToMigrate("已经是知识树,无需迁移。")

    flat = parse_yaml_model(ProjectMap, read_yaml(root / paths.PROJECT_MAP, relative), relative)

    moved: list[str] = []
    modules: list[Module] = []
    for module in flat.modules:
        legacy = root / paths.MODULES / f"{module.id}.md"
        if legacy.is_file():
            target = root / overview_path(module.id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
            legacy.unlink()
            moved.append(module.id)
            module = module.model_copy(update={"doc": overview_path(module.id)})
        modules.append(module)

    migrated = flat.model_copy(update={"modules": modules})
    write_tree(root, migrated)
    return migrated, moved


def _existing_key(path: Path, key: str) -> list[Any]:
    """模块地图里某个"这一层不管、但绝不能弄丢"的键。"""
    if not path.is_file():
        return []
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return []
    if not isinstance(loaded, dict):
        return []
    return list(loaded.get(key) or [])


def existing_features(path: Path) -> list[Any]:
    """已有的功能点清单,**原样读出、原样写回**。

    项目地图这个类型不带功能点,直接覆盖模块地图会把它们抹掉——而"知识莫名其妙少了一块"
    是最难查的那种故障。

    **不在这里过模型。** 过模型意味着一个手写的拼写错误会让那条功能点在下一次写回时被
    悄悄删掉,正好是这段代码存在要防的事情本身。坏数据留着,由 `genome.features` 逐条报。
    """
    return _existing_key(path, "features")


__all__ = [
    "MODULE_MAP",
    "existing_features",
    "MODULE_OVERVIEW",
    "Assembled",
    "ContractIndex",
    "ModuleMap",
    "NothingToMigrate",
    "RootIndex",
    "RootModuleEntry",
    "assemble",
    "is_flat_map",
    "migrate_flat_map",
    "module_dir",
    "overview_path",
    "write_tree",
]
