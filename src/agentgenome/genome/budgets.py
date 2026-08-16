"""行数预算与碎片化告警。

分层解决了"一个文件装不下",没解决"某一层自己膨胀"。一个有两百个功能点的模块,它的模块地图
会把单文件地图的全部问题重演一遍,只是换了个位置——膨胀到没人愿意打开,于是开始撒谎。

## 预算是门禁项,不是建议

超限的知识变更不予通过。**报错要说清楚是哪个文件、超了多少行**——只说"不让过"的话,收到
拒绝的架构员工只知道被拦了,不知道该拆哪里,于是它会去改预算而不是去分形。

超限的正确响应通常是**分形**(子目录 + 子地图,见 `genome.features`)。把默认值调大等于
取消这条门禁。

## 碎片化只提示,不拦截

同一条代码路径被太多张卡片同时覆盖,说明知识被切碎了——路由会一次命中一堆,而真正相关的
那张淹没在里面。但切碎有时是对的,所以这里只发信号:它应该是**被注意到的**,不是被禁止的。

## 与加载分开

加载器是纯的、不认识根配置;预算与阈值是"这一次部署怎么跑"。把它们塞进加载器,等于让每个
读地图的地方都被迫拿着一份配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentgenome import paths
from agentgenome.config import KnowledgeConfig
from agentgenome.core.scope import concrete_prefix
from agentgenome.genome.errors import ValidationIssue
from agentgenome.genome.features import KnowledgeTree


@dataclass(frozen=True)
class Fragmentation:
    """一处被切碎的知识。

    `scope_prefix` 是一批覆盖范围**共同的具体前缀**,不是一个保证存在的目录——叫它 `path`
    会让告警读起来像一条文件系统事实。
    """

    scope_prefix: str
    count: int
    feature_ids: tuple[str, ...]


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _over(path: Path, relative: str, limit: int, what: str) -> ValidationIssue | None:
    if not path.is_file():
        return None
    actual = _lines(path)
    if actual <= limit:
        return None
    return ValidationIssue(
        relative, f"{what}超出行数预算: {actual} 行 > {limit} 行。拆开它(子目录 + 子地图)。"
    )


def check_budgets(
    workspace_root: Path, tree: KnowledgeTree, limits: KnowledgeConfig
) -> list[ValidationIssue]:
    """每一层都在预算内吗。返回全部超限项,不是遇到第一个就停。"""
    root = Path(workspace_root)
    issues: list[ValidationIssue] = []

    issue = _over(
        root / paths.PROJECT_MAP, str(paths.PROJECT_MAP), limits.root_index_lines, "根索引"
    )
    if issue is not None:
        issues.append(issue)

    # **按树真正加载到的文件算,不按目录约定猜。** 子地图指针可以指到模块目录里的任何
    # 位置,靠"扫 features/ 目录"找它们的话,把内容挪进别处的子地图就成了绕过行数门禁的
    # 办法——而门禁要拦的正是"某一层膨胀"。
    for path in tree.map_files:
        issue = _over(path, _relative(root, path), limits.module_map_lines, "模块地图")
        if issue is not None:
            issues.append(issue)
    for path in tree.card_files:
        issue = _over(path, _relative(root, path), limits.card_lines, "功能卡片")
        if issue is not None:
            issues.append(issue)
    return issues


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def fragmentation_warnings(tree: KnowledgeTree, limits: KnowledgeConfig) -> list[Fragmentation]:
    """哪几处的知识被切得太碎。**只提示,不拦截。**

    **按具体前缀分桶,不按"谁匹配得上这个前缀"数。** 后者有两个反向的毛病,两个都真出现过:

    - 漏报:四条 `src/order/*.py` 的前缀都是 `src/order`,而 `src/order/*.py` 匹配不上
      `src/order` 本身(它要求一个 `.py` 结尾),于是覆盖数是零——四张卡片压在同一个目录上
      却一声不吭。
    - 误报:一条模块级的 `repos/order/**` 加三条 `repos/order/src/reserve/**`,前者匹配得上后者
      的前缀,于是报"四个功能点覆盖同一处、考虑合并"——而把模块级卡片并进叶子卡片恰恰是
      反的。粗细分层正是这棵树鼓励的写法。

    **只数有卡片的功能点。** 声明了「无需卡片」的地方没有知识可切,把它算进来等于在零张
    卡片上报"知识被切碎了"。
    """
    buckets: dict[str, set[str]] = {}
    for module_id, features in tree.feature_index.items():
        for feature in features:
            if feature.card is None:
                continue
            for scope in feature.scope:
                prefix = concrete_prefix(scope)
                if prefix:
                    buckets.setdefault(prefix, set()).add(f"{module_id}/{feature.id}")
    return [
        Fragmentation(prefix, len(ids), tuple(sorted(ids)))
        for prefix, ids in sorted(buckets.items())
        if len(ids) > limits.fragmentation_threshold
    ]


__all__ = ["Fragmentation", "check_budgets", "fragmentation_warnings"]
