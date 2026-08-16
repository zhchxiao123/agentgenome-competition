"""best-of-n 执行器:变异 + 选择 + 遗传。

**门禁即适应度,不是评审偏好。** 一个跑不过测试的方案再优雅也出局,而这条判据是确定性的、
不烧 token、不看脸色——judge 只在过闸集合里选,那是"适应度先于偏好"在代码里的落点。
"""

from __future__ import annotations

import pytest

from agentgenome.core.topology import (
    ATTEMPT,
    BEST_OF_N,
    GATE,
    JUDGE,
    STOPPED_CONVERGED,
    STOPPED_NODE_FAILED,
    ExecutionLimits,
    NodeKind,
    NodeOutcome,
    TopologyNode,
    UnknownTopology,
    Variant,
    best_of_n_template,
    run_topology,
)

VARIANTS = (
    Variant(key="minimal", hint="最小改动"),
    Variant(key="perf", hint="性能优先"),
    Variant(key="contract", hint="契约先行"),
)


def template(variants: tuple[Variant, ...] = VARIANTS):
    return best_of_n_template(
        employee="dev-employee",
        procedure="code-develop",
        gate_procedure="unit-gate",
        judge_employee="reviewer-employee",
        judge_procedure="code-critique",
        variants=variants,
    )


class Runner:
    """按脚本回答:哪几路过闸、judge 挑谁。"""

    def __init__(self, passes: frozenset[str] = frozenset(), pick: str = "") -> None:
        self.passes = passes
        self.pick = pick
        self.calls: list[str] = []

    async def __call__(self, node: TopologyNode) -> NodeOutcome:
        key = node.variants[0].key if node.variants else ""
        self.calls.append(f"{node.id}:{key}" if key else node.id)
        if node.id == GATE:
            return NodeOutcome(
                node=node, ok=True, payload={"passed": key in self.passes}
            )
        if node.id == JUDGE:
            return NodeOutcome(node=node, ok=True, payload={"passed": True, "winner": self.pick})
        return NodeOutcome(node=node, ok=True, payload={"passed": True})


async def test_every_variant_gets_its_own_attempt_and_its_own_gate() -> None:
    runner = Runner(passes=frozenset({"minimal", "perf"}), pick="perf")

    await run_topology(template(), runner, ExecutionLimits())

    assert runner.calls.count(f"{ATTEMPT}:minimal") == 1
    assert runner.calls.count(f"{GATE}:contract") == 1


async def test_the_judge_only_picks_from_those_that_passed_the_gate() -> None:
    """适应度先于偏好。"""
    runner = Runner(passes=frozenset({"minimal", "perf"}), pick="perf")

    run = await run_topology(template(), runner, ExecutionLimits())

    assert run.winner == "perf"
    assert set(run.survivors) == {"minimal", "perf"}
    assert run.stopped_because == STOPPED_CONVERGED


async def test_a_judge_that_picks_a_failing_variant_is_overruled() -> None:
    """"它挑了个跑不起来的"该是系统自己能兜住的事,不是一次事故。"""
    runner = Runner(passes=frozenset({"minimal"}), pick="contract")

    run = await run_topology(template(), runner, ExecutionLimits())

    assert run.winner == "minimal"


async def test_nobody_passing_the_gate_is_a_failure_not_an_indecision() -> None:
    """零过闸 = 全部失败,回开发态并带上 N 路失败对比——比单路失败报告信息量大得多。"""
    runner = Runner(passes=frozenset())

    run = await run_topology(template(), runner, ExecutionLimits())

    assert run.stopped_because == STOPPED_NODE_FAILED
    assert set(run.failed) == {"minimal", "perf", "contract"}
    assert JUDGE not in runner.calls, "没人过闸就不必付裁决那一次的钱"


async def test_one_variant_is_not_best_of_n() -> None:
    with pytest.raises(UnknownTopology):
        template(variants=(Variant(key="only"),))


async def test_the_judge_is_a_checker_and_writes_nothing() -> None:
    """裁决产出判定,不产出资产——一个能改代码的裁决者会不自觉地改成自己想选的样子。"""
    judge = template().nodes[-1]

    assert judge.kind is NodeKind.CHECKER
    assert judge.write_scope == ()


async def test_the_graph_is_legal_by_its_own_validator() -> None:
    """N 路变体按设计写同一批路径:写集冲突检验对同一节点的变体豁免。"""
    from agentgenome.core.topology_validate import validate_topology

    assert validate_topology(template()) == ()


async def test_the_template_id_is_the_one_the_registry_knows() -> None:
    assert template().id == BEST_OF_N


async def test_the_variant_decides_the_worktree_not_the_node() -> None:
    """同一路的"写代码"与"过门禁"必须看到同一棵树。

    按节点分的话,门禁会在一棵空树上跑——**而它会绿**。
    """
    from agentgenome.jobs.handlers import _worktree_of

    made, gate, judge = template().nodes
    one = VARIANTS[0]

    assert _worktree_of(template(), made.__class__(**{**made.__dict__, "variants": (one,)})) == (
        f"{ATTEMPT}-minimal"
    )
    assert _worktree_of(template(), gate.__class__(**{**gate.__dict__, "variants": (one,)})) == (
        f"{ATTEMPT}-minimal"
    )
    assert _worktree_of(template(), judge) == ""
