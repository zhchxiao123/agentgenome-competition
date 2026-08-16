"""assisted 拓扑的 checker 失败语义。"""

from __future__ import annotations

from agentgenome.core.topology import (
    STOPPED_CHECKER_FAILED,
    NodeKind,
    NodeOutcome,
    TopologyNode,
    assisted_template,
    run_topology,
)


async def test_a_failed_confirmation_checker_cannot_approve_stale_payload() -> None:
    template = assisted_template("dev", "develop", assignee="alice")

    async def run_node(node: TopologyNode) -> NodeOutcome:
        if node.kind is NodeKind.CHECKER:
            return NodeOutcome(
                node=node,
                ok=False,
                payload={"passed": True, "approved": True},
                failure_kind="scope",
            )
        return NodeOutcome(node=node, ok=True, payload={"passed": True})

    run = await run_topology(template, run_node)

    assert run.stopped_because == STOPPED_CHECKER_FAILED
    assert run.failed == ("confirm",)
