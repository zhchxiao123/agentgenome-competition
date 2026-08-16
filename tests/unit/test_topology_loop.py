"""critique-loop 执行器:生成一次,批判到收敛。

**修复轮次是重试环,critique-loop 是精化环**——前者由失败驱动,后者由批判驱动。这里验的
全部是"什么时候停、停了之后交出什么",因为那正是一个环与一次失控的全部区别。
"""

from __future__ import annotations

import pytest

from agentgenome.core.topology import (
    CRITIQUE_LOOP,
    STOPPED_BUDGET,
    STOPPED_CHECKER_FAILED,
    STOPPED_CONVERGED,
    STOPPED_MAX_ROUNDS,
    STOPPED_NODE_FAILED,
    ExecutionLimits,
    NodeKind,
    NodeOutcome,
    Termination,
    TopologyNode,
    TopologyRefused,
    TopologyTemplate,
    UnknownTopology,
    run_topology,
)

GENERATE = TopologyNode(id="generate", employee="dev", procedure="code-develop", produces=("diff",))
CRITIQUE = TopologyNode(
    id="critique",
    kind=NodeKind.CHECKER,
    employee="reviewer",
    procedure="code-critique",
    needs=("diff",),
    produces=("critique.json",),
)
REFINE = TopologyNode(
    id="refine",
    employee="dev",
    procedure="code-develop",
    needs=("critique.json",),
    produces=("diff",),
)


def template(
    max_rounds: int = 2, stop_when: str | None = "approved", budget_share: float = 0.0
) -> TopologyTemplate:
    return TopologyTemplate(
        id=CRITIQUE_LOOP,
        nodes=(GENERATE, CRITIQUE, REFINE),
        edges=(("generate", "critique"), ("critique", "refine")),
        termination=Termination(
            max_rounds=max_rounds, stop_when=stop_when, budget_share=budget_share
        ),
    )


class Runner:
    """按脚本回答的节点跑法。`verdicts` 逐轮给出批判结论。"""

    def __init__(
        self,
        verdicts: list[bool] | None = None,
        tokens: int = 0,
        checker_ok: bool = True,
        tokens_available: bool = True,
    ) -> None:
        self.verdicts = list(verdicts or [])
        self.tokens = tokens
        self.checker_ok = checker_ok
        self.tokens_available = tokens_available
        self.calls: list[str] = []

    async def __call__(self, node: TopologyNode) -> NodeOutcome:
        self.calls.append(node.id)
        if node.kind is NodeKind.CHECKER:
            approved = self.verdicts.pop(0) if self.verdicts else False
            payload = None if not self.checker_ok else {"passed": True, "approved": approved}
            return NodeOutcome(
                node=node,
                ok=self.checker_ok,
                payload=payload,
                tokens_used=self.tokens,
                tokens_available=self.tokens_available,
            )
        return NodeOutcome(
            node=node,
            ok=True,
            payload={"passed": True, "changed_files": []},
            tokens_used=self.tokens,
            tokens_available=self.tokens_available,
        )


async def test_a_first_round_approval_skips_refine() -> None:
    """批判通过就停,不多花一轮精化的钱。"""
    runner = Runner(verdicts=[True])

    run = await run_topology(template(), runner)

    assert runner.calls == ["generate", "critique"]
    assert run.stopped_because == STOPPED_CONVERGED
    assert run.rounds == 1


async def test_a_failed_seed_stops_before_starting_the_reviewer() -> None:
    """没有有效开发产出时，评审是一次必然无效的额外调用。"""

    async def failed_seed(node: TopologyNode) -> NodeOutcome:
        if node.id == "generate":
            return NodeOutcome(node=node, ok=False, failure_kind="contract")
        raise AssertionError(f"失败 seed 后不应调用 {node.id}")

    run = await run_topology(template(), failed_seed)

    assert run.stopped_because == STOPPED_NODE_FAILED
    assert run.failed == ("generate",)
    assert [item.node.id for item in run.outcomes] == ["generate"]


async def test_a_rejection_leads_to_refine_and_another_critique() -> None:
    runner = Runner(verdicts=[False, True])

    run = await run_topology(template(), runner)

    assert runner.calls == ["generate", "critique", "refine", "critique"]
    assert run.stopped_because == STOPPED_CONVERGED
    assert run.rounds == 2


async def test_the_round_cap_stops_the_loop_and_keeps_the_last_work() -> None:
    """达上限即停,**带最后一版意见送门禁**——环不是闸门,不升级人工。"""
    runner = Runner(verdicts=[False, False, False])

    run = await run_topology(template(max_rounds=2), runner)

    assert runner.calls == ["generate", "critique", "refine", "critique"]
    assert run.stopped_because == STOPPED_MAX_ROUNDS
    last = run.last_work()
    assert last is not None and last.node.id == "refine"


async def test_the_loop_budget_stops_it_before_the_next_round() -> None:
    """轮次封顶管得住次数,管不住单轮的花费。"""
    runner = Runner(verdicts=[False, False], tokens=40)

    run = await run_topology(
        template(max_rounds=5, budget_share=0.1), runner, ExecutionLimits(budget_tokens=1000)
    )

    # 批判(40)+ 精化(40)= 80 < 100;第二轮批判之后到 120,再往下走之前触顶。
    assert runner.calls == ["generate", "critique", "refine", "critique"]
    assert run.stopped_because == STOPPED_BUDGET
    assert run.tokens_used == 120


async def test_the_seed_job_is_not_charged_to_the_loop_budget() -> None:
    """生成那一次是这个状态本来就要跑的 Job,环只是在它之后追加批判与精化。

    把它算进环的账里,一次正常开发烧掉份额的任务会在"零轮批判"处停住,而事件面只会说
    一句"预算用尽"——看上去像环坏了,其实是账算错了。
    """
    runner = Runner(verdicts=[True], tokens=100)

    run = await run_topology(
        template(max_rounds=2, budget_share=0.1), runner, ExecutionLimits(budget_tokens=1000)
    )

    assert runner.calls == ["generate", "critique"]
    assert run.stopped_because == STOPPED_CONVERGED
    assert run.tokens_used == 100


async def test_all_three_stops_hand_back_the_last_work_node() -> None:
    """三种停法的出路完全相同:交出工作节点的最后一版产出。"""
    for runner, limits in (
        (Runner(verdicts=[True]), ExecutionLimits()),
        (Runner(verdicts=[False, False]), ExecutionLimits()),
        (Runner(verdicts=[False, False], tokens=90), ExecutionLimits(budget_tokens=200)),
    ):
        run = await run_topology(template(max_rounds=2, budget_share=0.5), runner, limits)

        last = run.last_work()
        assert last is not None and last.node.kind is NodeKind.WORK


async def test_a_failed_critique_does_not_discard_the_work() -> None:
    """代码现场要保留，但失败的 checker 必须阻断本轮继续过门禁。"""
    runner = Runner(checker_ok=False)

    run = await run_topology(template(), runner)

    assert run.stopped_because == STOPPED_CHECKER_FAILED
    assert run.failed == ("critique",)
    assert runner.calls == ["generate", "critique"]
    last = run.last_work()
    assert last is not None and last.node.id == "generate"


async def test_unavailable_usage_does_not_starve_the_loop() -> None:
    """拿不到用量时不能当成 0,也不能当成花完了——这条判据直接不生效。"""
    runner = Runner(verdicts=[False, False], tokens=0, tokens_available=False)

    run = await run_topology(
        template(max_rounds=2, budget_share=0.01), runner, ExecutionLimits(budget_tokens=1000)
    )

    assert run.stopped_because == STOPPED_MAX_ROUNDS
    assert run.tokens_available is False


async def test_a_template_with_the_wrong_shape_is_refused() -> None:
    """执行器只跑它懂的那种图,不猜。"""
    bad = TopologyTemplate(id=CRITIQUE_LOOP, nodes=(GENERATE, CRITIQUE))

    with pytest.raises(UnknownTopology) as error:
        await run_topology(bad, Runner())

    assert "checker" in str(error.value)


async def test_a_stop_when_expression_is_refused_not_silently_false() -> None:
    """当成"没通过"静默跑满轮次的话,症状是"这个环怎么每次都跑满两轮"。"""
    with pytest.raises(TopologyRefused):
        await run_topology(template(stop_when="critique.approved == true"), Runner())


async def test_without_a_stop_predicate_only_rounds_and_budget_stop_it() -> None:
    runner = Runner(verdicts=[True, True])

    run = await run_topology(template(stop_when=None, max_rounds=1), runner)

    assert run.stopped_because == STOPPED_MAX_ROUNDS
    assert run.rounds == 1
