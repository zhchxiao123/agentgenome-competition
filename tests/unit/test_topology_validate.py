"""图校验器:派发前把一张图判死的四条确定性检验。

**纯函数,不依赖执行器。** 校验器要能在没有任何执行器的情况下单独成立——否则"派发前全绿"
这条承诺会变成"跑起来才知道"。
"""

from __future__ import annotations

import ast
from pathlib import Path

from agentgenome.core import topology_validate
from agentgenome.core.topology import NodeKind, TopologyNode, TopologyTemplate, single_template
from agentgenome.core.topology_validate import (
    CHECKER_WRITES,
    CYCLE,
    DUPLICATE_NODE,
    FAKE_EDGE,
    UNKNOWN_NODE,
    WRITE_CONFLICT,
    globs_overlap,
    validate_topology,
)


def codes(template: TopologyTemplate) -> list[str]:
    return [issue.code for issue in validate_topology(template)]


def node(node_id: str, **kwargs: object) -> TopologyNode:
    return TopologyNode(id=node_id, **kwargs)  # type: ignore[arg-type]


def test_single_passes_everything() -> None:
    assert validate_topology(single_template(employee="dev-employee", procedure="p")) == ()


def test_an_edge_backed_by_an_artifact_passes() -> None:
    template = TopologyTemplate(
        id="chain",
        nodes=(
            node("a", produces=("spec.md",)),
            node("b", needs=("spec.md",)),
        ),
        edges=(("a", "b"),),
    )

    assert validate_topology(template) == ()


def test_an_ordering_edge_without_an_artifact_is_refused() -> None:
    """"先后顺序"不构成边。等待一个不给你任何东西的上游是纯浪费。"""
    template = TopologyTemplate(
        id="ordering",
        nodes=(node("a", produces=("a.json",)), node("b", needs=("other.md",))),
        edges=(("a", "b"),),
    )

    issues = validate_topology(template)

    assert [issue.code for issue in issues] == [FAKE_EDGE]
    rendered = issues[0].render()
    assert "a" in rendered and "b" in rendered
    assert "other.md" in rendered


def test_parallel_nodes_with_overlapping_write_scope_are_refused() -> None:
    """并行节点的写集相交 = 拓扑序合并回任务分支时的冲突,静态可判。"""
    template = TopologyTemplate(
        id="parallel",
        nodes=(
            node("a", write_scope=("repos/order/**",)),
            node("b", write_scope=("repos/order/src/*.py",)),
        ),
    )

    issues = validate_topology(template)

    assert [issue.code for issue in issues] == [WRITE_CONFLICT]
    assert "repos/order/src/*.py" in issues[0].render()


def test_a_dependency_path_makes_an_overlapping_write_scope_legal() -> None:
    """有依赖就不并行,不并行就不会撞——规则管的是并行,不是写集本身。"""
    template = TopologyTemplate(
        id="serial",
        nodes=(
            node("a", produces=("a.json",), write_scope=("repos/order/**",)),
            node("b", needs=("a.json",), write_scope=("repos/order/src/*.py",)),
        ),
        edges=(("a", "b"),),
    )

    assert validate_topology(template) == ()


def test_a_transitive_dependency_also_counts() -> None:
    template = TopologyTemplate(
        id="transitive",
        nodes=(
            node("a", produces=("a.json",), write_scope=("repos/order/**",)),
            node("b", needs=("a.json",), produces=("b.json",)),
            node("c", needs=("b.json",), write_scope=("repos/order/**",)),
        ),
        edges=(("a", "b"), ("b", "c")),
    )

    assert validate_topology(template) == ()


def test_variants_of_one_node_may_share_a_write_scope() -> None:
    """N 路变体按设计写同一批路径:靠分支物理隔离,而且只有一路会被合并。

    豁免不是一条例外分支——规则比较的是**两个节点**,而变体属于同一个节点。
    """
    from agentgenome.core.topology import Variant

    template = TopologyTemplate(
        id="best-of-n",
        nodes=(
            node(
                "dev",
                write_scope=("repos/order/**",),
                variants=(Variant(key="minimal"), Variant(key="perf")),
            ),
        ),
    )

    assert validate_topology(template) == ()


def test_a_checker_may_not_declare_a_write_scope() -> None:
    """checker 产出的是判定,不是资产。它进产物面与事件面,不进版本面。"""
    template = TopologyTemplate(
        id="bad-checker",
        nodes=(
            node("c", kind=NodeKind.CHECKER, produces=("critique.json",), write_scope=("a/**",)),
        ),
    )

    assert codes(template) == [CHECKER_WRITES]


def test_a_checker_produces_and_its_outgoing_edge_is_not_fake() -> None:
    """critique(checker) → refine 这条边靠 critique.json 背书。

    把 checker 定义成"只评估不生产"的话,这条边会被自家校验器判成假边——规则与模板必须
    能同时成立。
    """
    template = TopologyTemplate(
        id="critique-loop",
        nodes=(
            node("generate", produces=("diff",)),
            node("critique", kind=NodeKind.CHECKER, needs=("diff",), produces=("critique.json",)),
            node("refine", needs=("critique.json",), produces=("diff2",)),
        ),
        edges=(("generate", "critique"), ("critique", "refine")),
    )

    assert validate_topology(template) == ()


def test_a_cycle_is_refused_with_its_path() -> None:
    """在加载那一刻拒绝,不在调度那一刻。

    调度时才发现的症状是"任务提交之后什么都没发生"的静默死锁,排查极贵。
    """
    template = TopologyTemplate(
        id="cyclic",
        nodes=(
            node("a", needs=("c.json",), produces=("a.json",)),
            node("b", needs=("a.json",), produces=("b.json",)),
            node("c", needs=("b.json",), produces=("c.json",)),
        ),
        edges=(("a", "b"), ("b", "c"), ("c", "a")),
    )

    issues = validate_topology(template)

    assert [issue.code for issue in issues] == [CYCLE]
    assert "a" in issues[0].render() and "c" in issues[0].render()


def test_an_edge_to_an_unknown_node_is_refused_before_the_graph_rules_run() -> None:
    """结构不成立时先报结构:拿着一个不存在的节点去查产物背书只会报出误导性的第二个问题。"""
    template = TopologyTemplate(id="broken", nodes=(node("a"),), edges=(("a", "ghost"),))

    assert codes(template) == [UNKNOWN_NODE]


def test_duplicate_node_ids_are_refused() -> None:
    """id 进产物编址,重复等于两个节点共用一个产物目录。"""
    template = TopologyTemplate(id="dup", nodes=(node("a"), node("a")))

    assert codes(template) == [DUPLICATE_NODE]


def test_every_issue_names_something_actionable() -> None:
    """报错沿用逐条可操作原则:LLM 拿着它要能直接改图。"""
    template = TopologyTemplate(
        id="messy",
        nodes=(node("a", produces=("a.json",)), node("b", needs=("nope.md",))),
        edges=(("a", "b"),),
    )

    for issue in validate_topology(template):
        assert issue.render().strip()
        assert issue.code in issue.render()


def test_the_validator_stays_a_pure_function() -> None:
    """校验器不许 import 执行器 / 编排器 / 存储 / 工作区。

    **"派发前全绿"这条承诺只有在它能独立跑起来时才成立。** 只写在文档里的话,某次顺手加一个
    `from ...jobs.orchestrator import` 不会有任何症状,直到它被一个还没有编排器的调用方需要。
    """
    source = Path(topology_validate.__file__).read_text(encoding="utf-8")
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("agentgenome")
    }

    assert imported == {"agentgenome.core.topology"}


class TestGlobOverlap:
    """glob 相交按**保守**判定:判不准就判相交。

    宁可少并行,不可放过一次静默的互相覆盖。
    """

    def test_identical_globs_overlap(self) -> None:
        assert globs_overlap("src/**", "src/**")

    def test_a_prefix_star_star_covers_a_deeper_path(self) -> None:
        assert globs_overlap("src/**", "src/order/main.py")

    def test_disjoint_directories_do_not_overlap(self) -> None:
        assert not globs_overlap("repos/order/**", "repos/inventory/**")

    def test_recursive_suffix_meets_a_directory_subtree(self) -> None:
        assert globs_overlap("**/*.py", "src/order/**")

    def test_extension_mismatch_does_not_overlap(self) -> None:
        assert not globs_overlap("src/*.py", "src/*.md")

    def test_single_star_does_not_cross_a_separator(self) -> None:
        assert not globs_overlap("src/*.py", "src/order/main.py")

    def test_question_mark_matches_one_character(self) -> None:
        assert globs_overlap("src/a?.py", "src/ab.py")
        assert not globs_overlap("src/a?.py", "src/abc.py")

    def test_character_classes_are_treated_as_overlapping(self) -> None:
        """判不准的一律判相交——保守方向是这条规则的全部价值。"""
        assert globs_overlap("src/[ab].py", "src/z.py")


def test_a_refused_graph_and_an_unknown_template_are_different_exceptions() -> None:
    """"配置写错了"与"这次拆出来的图不合法"要分得开。

    前者改配置,后者重拆图——而重拆图那条路会消耗需求解析重试。一个异常回答不了
    "下一步该干什么"。
    """
    from agentgenome.core.topology import TopologyRefused, UnknownTopology

    assert not issubclass(TopologyRefused, UnknownTopology)
    assert TopologyRefused(("fake-edge [a→b]: 没有产物背书",)).issues
