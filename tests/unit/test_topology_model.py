"""拓扑模板的数据形状与解析。

**数据形状在这里一次定死。** 后续执行器 PRD 不得修改这些字段——需要新字段即说明模型没
定对,回来改这里并说明理由。所以本文件的断言有一半是在给"冻结"这件事上锁。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agentgenome.core.topology import (
    STOPPED_NODE_FAILED,
    ExecutionLimits,
    Executor,
    NodeKind,
    NodeOutcome,
    Termination,
    TopologyNode,
    TopologyParseError,
    TopologyTemplate,
    Variant,
    parse_template,
    run_topology,
    single_template,
)


def test_a_node_defaults_to_an_auto_work_node() -> None:
    node = TopologyNode(id="main", employee="dev-employee", procedure="code-develop")

    assert node.kind is NodeKind.WORK
    assert node.executor is Executor.AUTO
    assert node.variants == ()
    assert node.budget_share == 1.0


def test_nodes_are_frozen() -> None:
    """模板是数据。可变的话,一次执行中途改了图,事件面记的就不是真正跑过的那张。"""
    node = TopologyNode(id="main")

    with pytest.raises(FrozenInstanceError):
        node.id = "other"  # type: ignore[misc]


def test_single_template_is_one_node_no_edges() -> None:
    template = single_template(employee="dev-employee", procedure="code-develop")

    assert template.id == "single"
    assert len(template.nodes) == 1
    assert template.edges == ()
    assert template.termination is None
    assert template.selection is None


async def test_a_failed_single_node_is_a_failed_topology_run() -> None:
    """运行时没交出产物时，这一轮要重做，不能伪装成一次可迁移的结论。"""
    template = single_template(employee="dev-employee", procedure="code-develop")

    async def fail(node: TopologyNode) -> NodeOutcome:
        return NodeOutcome(
            node=node,
            ok=False,
            payload=None,
            failure_kind="process",
            failure_detail="运行时达到 max_turns",
        )

    run = await run_topology(template, fail, ExecutionLimits())

    assert run.stopped_because == STOPPED_NODE_FAILED
    assert run.failed == ("main",)


async def test_a_failed_runtime_with_a_fallback_artifact_is_left_to_the_handler() -> None:
    """拓扑不解释业务产物；例如集成测试诊断失败时，脚本事实仍可决定状态。"""
    template = single_template(employee="itest-employee", procedure="itest-run")

    async def fail_with_fallback(node: TopologyNode) -> NodeOutcome:
        return NodeOutcome(
            node=node,
            ok=False,
            payload={"passed": False, "failures": [{"case": "cross-module"}]},
            failure_kind="contract",
            failure_detail="诊断产物不合契约，已读取测试脚本报告",
        )

    run = await run_topology(template, fail_with_fallback, ExecutionLimits())

    assert run.stopped_because == ""
    assert run.failed == ()


def test_termination_and_variant_exist_before_any_executor_consumes_them() -> None:
    """本期没有执行器消费它们。

    仍然现在定义:环的收敛谓词与每路变体的取向在后续 PRD 里都需要落点,而在一个已经声明
    "数据形状冻结"的结构里它们没有位置——那等于在写下冻结承诺的同一处预定了它的破产。
    """
    termination = Termination(max_rounds=2, stop_when="critique.approved == true")
    node = TopologyNode(id="dev", variants=(Variant(key="minimal", hint="最小改动"),))

    assert termination.max_rounds == 2
    assert termination.budget_share == 0.0
    assert node.variants[0].key == "minimal"


def test_parse_reads_a_full_template() -> None:
    template = parse_template(
        {
            "id": "diamond",
            "nodes": [
                {"id": "a", "employee": "dev-employee", "procedure": "p", "produces": ["a.json"]},
                {
                    "id": "b",
                    "kind": "checker",
                    "employee": "reviewer",
                    "procedure": "q",
                    "needs": ["a.json"],
                    "produces": ["b.json"],
                    "executor": "manual",
                    "assignee": "alice",
                    "variants": [{"key": "minimal", "hint": "最小改动"}],
                },
            ],
            "edges": [["a", "b"]],
            "termination": {"max_rounds": 3, "budget_share": 0.3},
            "selection": "judge",
        }
    )

    assert [node.id for node in template.nodes] == ["a", "b"]
    assert template.nodes[1].kind is NodeKind.CHECKER
    assert template.nodes[1].executor is Executor.MANUAL
    assert template.nodes[1].needs == ("a.json",)
    assert template.nodes[1].variants == (Variant(key="minimal", hint="最小改动"),)
    assert template.edges == (("a", "b"),)
    assert template.termination == Termination(max_rounds=3, budget_share=0.3)
    assert template.selection == "judge"


def test_unknown_keys_are_refused() -> None:
    """静默吞掉写错的键,等于让一个 `write_scopes:` 的笔误变成"这个节点没声明写集"。"""
    with pytest.raises(TopologyParseError) as error:
        parse_template({"id": "x", "nodes": [{"id": "a", "write_scopes": ["src/**"]}]})

    assert "write_scopes" in str(error.value)


def test_unknown_kind_or_executor_is_refused() -> None:
    with pytest.raises(TopologyParseError):
        parse_template({"id": "x", "nodes": [{"id": "a", "kind": "reviewer"}]})

    with pytest.raises(TopologyParseError):
        parse_template({"id": "x", "nodes": [{"id": "a", "executor": "assisted"}]})


def test_assisted_is_not_an_executor_value() -> None:
    """assisted 是组合(auto 节点 + 确认节点),不是新原语。

    把它做成第三个取值,就要在每个执行器里长出一条"这个节点是半自动的"分支。
    """
    assert [item.value for item in Executor] == ["auto", "manual"]


def test_an_edge_must_be_a_pair() -> None:
    with pytest.raises(TopologyParseError):
        parse_template({"id": "x", "nodes": [{"id": "a"}], "edges": [["a", "b", "c"]]})


def test_template_round_trips_through_payload() -> None:
    """事件面记的拓扑实例就是它。记进去读不回来的话,回放就断了。"""
    template = TopologyTemplate(
        id="single",
        nodes=(TopologyNode(id="main", employee="dev-employee", procedure="code-develop"),),
    )

    assert parse_template(template.to_payload()) == template
