"""dag 执行器:按拓扑序调度,无依赖的节点同时跑。

**独立的子改动在互相等待**——不是因为有依赖,是因为执行器只会跑一条线。这组测试盯的是三件
事:该并的真并了、该等的真等了、有人挂了之后剩下的怎么办。
"""

from __future__ import annotations

import asyncio

import pytest

from agentgenome.core.topology import (
    DAG,
    STOPPED_CONVERGED,
    STOPPED_NODE_FAILED,
    ExecutionLimits,
    NodeOutcome,
    TopologyNode,
    TopologyTemplate,
    UnknownTopology,
    run_topology,
)


def node(node_id: str, needs: tuple[str, ...] = (), produces: tuple[str, ...] = ()) -> TopologyNode:
    return TopologyNode(
        id=node_id,
        employee="dev",
        procedure="code-develop",
        needs=needs,
        produces=produces or (f"{node_id}.diff",),
    )


#: 扇出-收敛。plan 的拆分手艺教的就是这个形状。
DIAMOND = TopologyTemplate(
    id=DAG,
    nodes=(
        node("plan", produces=("spec.md",)),
        node("order", needs=("spec.md",)),
        node("inventory", needs=("spec.md",)),
        node("wire", needs=("order.diff", "inventory.diff")),
    ),
    edges=(("plan", "order"), ("plan", "inventory"), ("order", "wire"), ("inventory", "wire")),
)


class Runner:
    """记账的节点跑法:谁在什么时候开跑、同时在跑几个。"""

    def __init__(self, fails: frozenset[str] = frozenset(), delay: float = 0.01) -> None:
        self.fails = fails
        self.delay = delay
        self.started: list[str] = []
        self.running = 0
        self.peak = 0

    async def __call__(self, node: TopologyNode) -> NodeOutcome:
        self.started.append(node.id)
        self.running += 1
        self.peak = max(self.peak, self.running)
        await asyncio.sleep(self.delay)
        self.running -= 1
        if node.id in self.fails:
            return NodeOutcome(node=node, ok=False, payload=None)
        return NodeOutcome(node=node, ok=True, payload={"passed": True})


async def test_independent_nodes_really_run_at_the_same_time() -> None:
    """"跑完了两个"与"同时跑了两个"是两回事,而后者才是这一层存在的理由。"""
    runner = Runner()

    await run_topology(DIAMOND, runner, ExecutionLimits(max_parallel=3))

    assert runner.peak >= 2


async def test_a_node_waits_for_what_it_consumes() -> None:
    runner = Runner()

    await run_topology(DIAMOND, runner, ExecutionLimits(max_parallel=3))

    assert runner.started[0] == "plan"
    assert runner.started[-1] == "wire"


async def test_the_parallel_cap_is_respected() -> None:
    """并行的钱不是新钱,而机器与 API 配额是有限的。"""
    runner = Runner()

    await run_topology(DIAMOND, runner, ExecutionLimits(max_parallel=1))

    assert runner.peak == 1


async def test_a_failure_freezes_its_downstream_but_not_the_rest() -> None:
    """依赖失败节点的那些不派发:它们要读的产物不存在,派出去只是把钱花在一次必然失败上。"""
    runner = Runner(fails=frozenset({"order"}))

    run = await run_topology(DIAMOND, runner, ExecutionLimits(max_parallel=3))

    assert "inventory" in runner.started, "旁支该跑完——已经花的钱要产出价值"
    assert "wire" not in runner.started
    assert run.stopped_because == STOPPED_NODE_FAILED
    assert run.failed == ("order",)
    assert run.frozen == ("wire",)


async def test_frozen_is_not_failed() -> None:
    """被冻结的节点没有失败现场。算成失败的话,下一轮的诊断材料里会多出几条不存在的失败。"""
    runner = Runner(fails=frozenset({"plan"}))

    run = await run_topology(DIAMOND, runner, ExecutionLimits())

    assert run.failed == ("plan",)
    assert set(run.frozen) == {"order", "inventory", "wire"}


async def test_an_all_green_graph_converges() -> None:
    runner = Runner()

    run = await run_topology(DIAMOND, runner, ExecutionLimits())

    assert run.stopped_because == STOPPED_CONVERGED
    assert run.failed == () and run.frozen == ()
    assert len(run.outcomes) == 4


async def test_a_single_node_graph_degenerates_to_one_dispatch() -> None:
    """串行退化等价:一条线也是合法的图,不为并行而并行。"""
    only = TopologyTemplate(id=DAG, nodes=(node("main"),))
    runner = Runner()

    run = await run_topology(only, runner, ExecutionLimits())

    assert runner.started == ["main"]
    assert run.stopped_because == STOPPED_CONVERGED


async def test_an_empty_graph_is_refused() -> None:
    with pytest.raises(UnknownTopology):
        await run_topology(TopologyTemplate(id=DAG), Runner(), ExecutionLimits())
