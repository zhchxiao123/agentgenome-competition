"""功能点与功能卡片:知识树的第三层,以及「完备」这条不变式。

模块地图能答"订单服务是干什么的",答不了"下单预占库存这个流程怎么走、超时了会发生什么、
以前在哪踩过坑"——而后者才是员工真正需要的东西。

## 完备的含义不是写得好,是地图上没有未知区域

见 [ADR-0003](../../../docs/adr/0003-knowledge-map-is-complete-upfront-not-lazily-grown.md)。
每个功能点要么有一张卡片,要么有一条**带理由的**「无需卡片」声明。

「这里没知识」是缺口;「这里不需要知识」是一个被记录下来的判断。两者必须能分开——用"指针
为空就当作不需要"这种约定的第一个后果,就是没人分得清那是漏填还是有意为之。而分开的唯一
办法,是逼人把后者写出来。

**这条不变式的软肋是理由的质量。** 校验挡得住空理由,挡不住「不需要」三个字。唯一有效的补强
是把全部声明列出来让人扫一眼(`no_card_declarations`),而不是再加一层自动校验。

## 逐条读、逐条报

功能点在模块地图里是原样带过的原始数据(见 `genome.tree`),到这一层才逐条过模型。整份一起
过的话,一个手写的拼写错误会让那个模块的其余问题全部看不见——而修一个文件跑一次的反馈回路
太长。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentgenome import paths
from agentgenome.core.scope import compile_glob, concrete_prefix
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.tree import MODULE_MAP, module_dir
from agentgenome.genome.yamlio import parse_yaml_model, read_yaml, split_front_matter

_STRICT = ConfigDict(extra="forbid")

#: 功能卡片目录,相对模块目录。
FEATURES_DIR = "features"


class Confidence(StrEnum):
    """对一条认知有多大把握。

    **不确定的认知标注置信度而不是编造**——这是架构员工提示词里的铁律,这里是它的落地。
    枚举而不是自由字符串:`high` 与 `HIGH` 与 `很高` 会在筛选时变成三个不同的桶。
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FeatureEntry(BaseModel):
    """模块地图里的一个功能点。

    **卡片指针与「无需卡片」声明二选一,而且互斥在模型层面就成立。** 放在校验函数里的话,
    直接构造出来的实例绕得过去——而写回路径上就有直接构造。
    """

    model_config = _STRICT

    id: str
    summary: str = ""
    #: 这个功能点覆盖哪些代码路径(Workspace 相对的 glob)。路由靠它命中。
    scope: list[str] = Field(default_factory=list)
    #: 功能卡片,相对模块目录。
    card: str | None = None
    #: 判断这里不值得写卡片时,**带理由**写在这儿。空理由等于没写。
    no_card: str | None = None
    #: 人工定过稿。**锁的是整条 entry**(尤其是 `scope`),不只是卡片正文。
    #: `HUMAN_EDITED_MARKER` 只护得住卡片文件里的文字,护不住这条 entry 本身——
    #: 于是人手改对的 `scope`,下一次架构员工重新报告同一个 id 时会被原样盖掉。
    #: 置位之后,`knowledge-init` 重跑对这个 id 的报告整条忽略,直到有人手动摘掉它。
    human_edited: bool = False

    @model_validator(mode="after")
    def _exactly_one_kind_of_knowledge(self) -> FeatureEntry:
        declared = self.no_card is not None
        if self.card and declared:
            raise ValueError(f"{self.id}: 卡片指针与「无需卡片」声明只能给一个")
        if not self.card and not declared:
            raise ValueError(f"{self.id}: 既没有卡片也没有「无需卡片」声明——地图上不允许存在缺口")
        if declared and not (self.no_card or "").strip():
            raise ValueError(f"{self.id}: 「无需卡片」必须写明理由")
        return self


class FeatureCard(BaseModel):
    """一个功能点的细节知识:时序、坑点、不变量、测试要点。

    正文是 Markdown 而不是 YAML——它是给人和 Agent 读的散文,YAML 装散文会把缩进变成噪声。
    身份与路由信息放 front matter,那部分才是机器要消费的。
    """

    model_config = _STRICT

    id: str
    summary: str = ""
    #: 覆盖范围。与功能点条目上的 scope 并存:卡片可以比条目声明得更细。
    scope: list[str] = Field(default_factory=list)
    confidence: Confidence | None = None
    #: 被命中且该任务成功的次数。自然选择的依据——加载时丢掉它,长期零命中的卡片就永远
    #: 淘汰不掉。增减逻辑属于渐进式加载,这一层只负责不弄丢。
    hits: int = 0
    links: list[str] = Field(default_factory=list)
    updated_at: date | None = None
    #: front matter 之后的正文。
    body: str = ""


@dataclass(frozen=True)
class FeatureRef:
    """一个功能点连同它属于哪个模块。

    功能点 id 只在模块内唯一,所以任何跨模块传递它的地方都得带上模块——分开传两个值的话,
    迟早有一处把它们配错。
    """

    module_id: str
    feature: FeatureEntry


@dataclass(frozen=True)
class KnowledgeTree:
    """整棵树:装配好的项目地图,加上项目地图装不下的那一层。

    项目地图这个类型被三十处消费,刻意保持不变;功能点与卡片挂在它旁边而不是塞进它——
    塞进去就等于让那三十处全部跟着改。
    """

    project_map: ProjectMap
    #: 模块 id → 功能点清单。
    feature_index: dict[str, list[FeatureEntry]] = field(default_factory=dict)
    #: (模块 id, 功能点 id) → 卡片。声明为无需卡片的功能点不在这里。
    cards: dict[tuple[str, str], FeatureCard] = field(default_factory=dict)
    #: 装配这棵树真正读过的地图文件(模块地图 + 分形出去的子地图)。
    #:
    #: **预算按它算,不按目录约定猜。** 子地图指针可以指到任何地方,靠"扫 features/ 目录"
    #: 找它们的话,把内容挪进目录外的子地图就成了绕过行数门禁的办法——而门禁要拦的正是
    #: "某一层膨胀"。
    map_files: tuple[Path, ...] = ()
    #: 装配这棵树真正读过的卡片文件。同上。
    card_files: tuple[Path, ...] = ()

    def features(self, module_id: str) -> list[FeatureEntry]:
        return self.feature_index.get(module_id, [])

    def card(self, module_id: str, feature_id: str) -> FeatureCard | None:
        return self.cards.get((module_id, feature_id))


def _parse_card(text: str, relative: str) -> FeatureCard:
    front, body = split_front_matter(text, relative)
    front["body"] = body
    return parse_yaml_model(FeatureCard, front, relative)


def scope_exists(workspace_root: Path, scope: str) -> bool:
    """覆盖范围指到的代码还在吗。

    看的是这条 glob 的具体前缀:整条 glob 拿去匹配的话,一个空目录会被判成"路径不存在",
    而空目录是合法的。
    """
    prefix = concrete_prefix(scope)
    if not prefix:
        return True
    return (Path(workspace_root) / prefix).exists()


def scope_hint(workspace_root: Path, scope: str) -> str:
    """这条路径在哪个挂载仓底下能找到。找得到就把正确写法说出来。

    **"路径不存在"这句话本身指导不了任何人。** 而少了模块前缀是这里最常见的写法错误
    (`scope` 是 Workspace 相对,而写它的员工正在深读单个模块),偏偏又是机器一查就知道
    答案的——知道却不说,等于让人自己去把整个仓翻一遍。
    """
    prefix = concrete_prefix(scope)
    if not prefix:
        return ""
    root = Path(workspace_root)
    for mount in sorted(entry.name for entry in root.iterdir() if entry.is_dir()):
        if (root / mount / prefix).exists():
            return f",是不是想写 {mount}/{scope}?"
    return ""


def _card_paths(target: Path) -> list[Path]:
    root = target / FEATURES_DIR
    return sorted(root.rglob("*.md")) if root.is_dir() else []


class _SubMap(BaseModel):
    """子地图。**结构与模块地图相同**——分形不引入第二套模型,规则原样递归。"""

    model_config = _STRICT

    features: list[Any] = Field(default_factory=list)
    submaps: list[str] = Field(default_factory=list)


def _collect_level(
    workspace_root: Path,
    module_id: str,
    base: Path,
    relative: str,
    raw_features: list[Any],
    issues: list[ValidationIssue],
    referenced: set[Path],
    loaded_cards: list[Path],
) -> tuple[list[FeatureEntry], dict[str, FeatureCard]]:
    """一层功能点:逐条过模型、逐条校验。卡片路径相对 `base`,**不是相对模块目录**。

    分形之后这两个不再是同一个目录,给 `base` 起个叫 `target` 的别名会让人以为它还是模块根。
    """
    entries: list[FeatureEntry] = []
    cards: dict[str, FeatureCard] = {}

    for index, raw in enumerate(raw_features):
        where = f"features.{index}"
        if not isinstance(raw, dict):
            issues.append(ValidationIssue(relative, "功能点条目必须是一个映射", where))
            continue
        try:
            feature = parse_yaml_model(FeatureEntry, raw, relative)
        except GenomeValidationError as error:
            issues.extend(error.issues)
            # **仍然记下这条卡片被引用过。** 不记的话,一个条目写错就会让它指向的那张
            # 合法卡片被顺带报成孤儿——两条错里有一条是假的。
            pointer = raw.get("card")
            if isinstance(pointer, str):
                referenced.add((base / pointer).resolve())
            continue
        entries.append(feature)

        # **覆盖范围一律检查,不受上面任何一条的影响。** 早退会让"这个功能点的 scope 指向
        # 已删掉的代码"在它同时有别的毛病时静默逃掉,而那两个问题是独立的。
        for scope in feature.scope:
            if not scope_exists(workspace_root, scope):
                hint = scope_hint(workspace_root, scope)
                issues.append(
                    ValidationIssue(
                        relative, f"{feature.id}: 覆盖的代码路径不存在: {scope}{hint}", where
                    )
                )

        if feature.card is None:
            continue
        path = base / feature.card
        referenced.add(path.resolve())
        card_relative = str(
            paths.MODULES / module_id / path.relative_to(module_dir(workspace_root, module_id))
        )
        if not path.is_file():
            issues.append(
                ValidationIssue(relative, f"{feature.id}: 卡片不存在: {feature.card}", where)
            )
            continue
        try:
            card = _parse_card(path.read_text(encoding="utf-8"), card_relative)
        except GenomeValidationError as error:
            issues.extend(error.issues)
            continue
        if card.id != feature.id:
            issues.append(
                ValidationIssue(
                    card_relative, f"卡片 id 与功能点不一致: {card.id} != {feature.id}", "id"
                )
            )
            continue
        cards[feature.id] = card
        loaded_cards.append(path)

    return entries, cards


def _collect_module(
    workspace_root: Path,
    module_id: str,
    raw_features: list[Any],
    submaps: list[str],
    issues: list[ValidationIssue],
) -> tuple[list[FeatureEntry], dict[str, FeatureCard], list[Path], list[Path]]:
    """一个模块的全部功能点,含分形出去的那些。

    **孤儿判定按模块整棵子树算。** 只看顶层的话,分形之后每一张住在子目录里的卡片都会被
    误报成孤儿——而人会照着报告把它们删掉。
    """
    target = module_dir(workspace_root, module_id)
    referenced: set[Path] = set()
    entries: list[FeatureEntry] = []
    cards: dict[str, FeatureCard] = {}

    top = target / MODULE_MAP
    map_files: list[Path] = [top] if top.is_file() else []
    loaded_cards: list[Path] = []
    pending: list[tuple[Path, str, list[Any], list[str]]] = [
        (target, str(paths.MODULES / module_id / MODULE_MAP), raw_features, submaps)
    ]
    seen: set[Path] = set()
    while pending:
        base, relative, features, children = pending.pop(0)
        level_entries, level_cards = _collect_level(
            workspace_root, module_id, base, relative, features, issues, referenced, loaded_cards
        )
        entries.extend(level_entries)
        cards.update(level_cards)
        for pointer in children:
            child = (base / pointer).resolve()
            if not child.is_relative_to(target.resolve()):
                # 指到模块目录之外的子地图。**这是员工可写的内容**,不拦的话下面那句
                # `relative_to` 会抛一个裸 ValueError——报错变成一段堆栈,而不是一条能改的
                # 校验问题。
                issues.append(
                    ValidationIssue(relative, f"子地图指到模块目录之外: {pointer}", "submaps")
                )
                continue
            child_relative = str(paths.MODULES / module_id / child.relative_to(target.resolve()))
            if child in seen:
                # 子地图互指会转不出来。
                issues.append(ValidationIssue(child_relative, "子地图指针成环"))
                continue
            seen.add(child)
            if not child.is_file():
                issues.append(ValidationIssue(relative, f"子地图不存在: {pointer}", "submaps"))
                continue
            referenced.add(child)
            map_files.append(child)
            try:
                sub = parse_yaml_model(_SubMap, read_yaml(child, child_relative), child_relative)
            except GenomeValidationError as error:
                issues.extend(error.issues)
                continue
            pending.append((child.parent, child_relative, sub.features, sub.submaps))

    for orphan in _card_paths(target):
        if orphan.resolve() not in referenced:
            # 相对模块目录报,不是只报文件名:分形之后子目录里会有同名卡片,只报名字的话
            # 「哪一张」答不出来。
            issues.append(
                ValidationIssue(
                    str(paths.MODULES / module_id / orphan.relative_to(target)),
                    "孤儿卡片:没有任何功能点指向它",
                )
            )
    return entries, cards, map_files, loaded_cards


def collect_features(
    workspace_root: Path,
    project_map: ProjectMap,
    module_features: dict[str, list[Any]],
    module_submaps: dict[str, list[str]],
    issues: list[ValidationIssue],
) -> tuple[
    dict[str, list[FeatureEntry]],
    dict[tuple[str, str], FeatureCard],
    tuple[Path, ...],
    tuple[Path, ...],
]:
    """读出全部功能点与卡片,把问题攒进 `issues` 而不是当场抛。

    **攒而不抛**,是为了让引用问题与完备性问题一次报全:分两轮抛的话,修完第一批才看得见
    第二批。组装成完整入口是 `loader.load_tree` 的事。

    `module_features` 由装配那一步顺带带出来——在这里重新读一遍模块地图的话,读的会是
    **约定路径**而不是根索引里那个指针,两者一旦不同(分形、改名)就会读错文件,而且那次
    读取抛出去会把此前攒的问题全丢掉。
    """
    index: dict[str, list[FeatureEntry]] = {}
    cards: dict[tuple[str, str], FeatureCard] = {}
    map_files: list[Path] = []
    card_files: list[Path] = []
    for module in project_map.modules:
        entries, module_cards, module_maps, module_card_files = _collect_module(
            workspace_root,
            module.id,
            module_features.get(module.id, []),
            module_submaps.get(module.id, []),
            issues,
        )
        index[module.id] = entries
        map_files.extend(module_maps)
        card_files.extend(module_card_files)
        for feature_id, card in module_cards.items():
            cards[(module.id, feature_id)] = card
    return index, cards, tuple(map_files), tuple(card_files)


def no_card_declarations(tree: KnowledgeTree) -> list[FeatureRef]:
    """全部「无需卡片」声明,供人一次扫完。

    校验只挡得住空理由,挡不住「不需要」三个字。**把它们单独列出来让人扫一眼,比任何自动
    校验都有效**——这是这条不变式唯一的软肋,也是唯一的补强。
    """
    return [
        FeatureRef(module_id, feature)
        for module_id, features in tree.feature_index.items()
        for feature in features
        if feature.no_card is not None
    ]


#: 卡片正文里那一节的标题。**不变量不是一种新的卡片类型**——它是功能卡片的一个小节,
#: 而"哪条线碰不得"这份清单本来就该住在那儿。
INVARIANT_HEADING = "不变量"


def invariants_of(card: FeatureCard) -> list[str]:
    """卡片正文里列出的不变量,逐条。没有那一节就是空。

    **逐条取,不整段取。** 攻击是"对着每一条问一句真的碰不得吗",而一整段散文没法逐条问——
    红队会挑最显眼的那句下手,剩下的永远没人验过。

    条目认 `- `、`* ` 与 `1. ` 三种写法:卡片是人写的散文,强求一种写法的结果是那一节
    在一半卡片上读不出来,而读不出来是静默的。
    """
    found: list[str] = []
    inside = False
    for raw in card.body.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            inside = INVARIANT_HEADING in line
            continue
        if not inside or not line:
            continue
        item = _list_item(line)
        if item:
            found.append(item)
    return found


def _list_item(line: str) -> str:
    if line[:2] in ("- ", "* "):
        return line[2:].strip()
    head, _, rest = line.partition(". ")
    return rest.strip() if head.isdigit() and rest else ""


def features_covering(tree: KnowledgeTree, path: str) -> list[FeatureRef]:
    """哪些功能点覆盖了这条代码路径。**路由的地基。**

    命中多个时全部返回。挑一张会漏掉关键信息,而"哪几张值得注入"是预算的事,不是这一层的事。
    """
    target = str(path).replace("\\", "/")
    return [
        FeatureRef(module_id, feature)
        for module_id, features in tree.feature_index.items()
        for feature in features
        # 用仓里那套 glob 翻译,不用 `fnmatch`:后者不区分 `*` 与 `**`,于是
        # `src/order/*` 会连 `src/order/reserve/service.py` 一起吃掉。
        if any(compile_glob(scope).match(target) for scope in feature.scope)
    ]


__all__ = [
    "FEATURES_DIR",
    "INVARIANT_HEADING",
    "Confidence",
    "FeatureCard",
    "FeatureEntry",
    "FeatureRef",
    "KnowledgeTree",
    "collect_features",
    "features_covering",
    "invariants_of",
    "no_card_declarations",
    "scope_exists",
    "scope_hint",
]
