"""处理器泛化之后的崩溃恢复不变式。

**幂等约定是整套崩溃恢复的前提。** 它从"这个 stage 的第 k 个产物目录就是它的第 k 轮"
改写成了"这个**节点**的第 k 个目录就是它的第 k 轮"——`single` 下两者等价,而等价这件事
必须当场证明,不能靠推理。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentgenome.agents.runtime import FailureKind
from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.topology import NodeKind, NodeOutcome, TopologyNode, TopologyRun
from agentgenome.core.transitions import PlanFailureCause
from agentgenome.jobs.artifacts import ArtifactBus
from agentgenome.jobs.handlers import STAGE_DEVELOP, Outcome, ProcedureHandler

if TYPE_CHECKING:
    from agentgenome.jobs.orchestrator import JobContext

HANDLER = ProcedureHandler(
    state=TaskState.DEVELOPING,
    employee_id="dev-employee",
    procedure_id="code-develop",
    stage=STAGE_DEVELOP,
    on_success=TaskEvent.DEV_DONE,
    on_failure=TaskEvent.DEV_DONE,
)


@pytest.fixture
def bus(tmp_path: Path) -> ArtifactBus:
    return ArtifactBus(tmp_path / "tasks" / "ag-1")


def land(bus: ArtifactBus, passed: bool, node: str = "", variant: str = "") -> None:
    slot = bus.allocate(STAGE_DEVELOP, node, variant)
    (slot.path / "result.json").write_text(
        json.dumps({"task_id": "ag-1", "passed": passed}), encoding="utf-8"
    )


def test_the_first_round_is_recognised(bus: ArtifactBus) -> None:
    land(bus, passed=True)

    outcome = HANDLER.existing_outcome(bus, attempt=1)

    assert outcome is not None and outcome.replayed


def test_a_second_round_is_not_satisfied_by_the_first_rounds_artifact(bus: ArtifactBus) -> None:
    """只问"这个阶段产出过东西吗"的话,第二轮压根不跑,任务在两个状态之间空转到轮次耗尽。"""
    land(bus, passed=False)

    assert HANDLER.existing_outcome(bus, attempt=2) is None


def test_the_second_rounds_artifact_is_the_one_read(bus: ArtifactBus) -> None:
    land(bus, passed=False)
    land(bus, passed=True)

    outcome = HANDLER.existing_outcome(bus, attempt=2)

    assert outcome is not None
    assert outcome.payload is not None and outcome.payload["passed"] is True


def test_another_nodes_slots_do_not_shift_this_nodes_rounds(bus: ArtifactBus) -> None:
    """按 stage 数的话,一个节点的产物会把另一个节点的轮次顶上去——于是它的第一轮被跳过。"""
    land(bus, passed=True, node="inventory")

    assert HANDLER.existing_outcome(bus, attempt=1) is None

    land(bus, passed=True)

    outcome = HANDLER.existing_outcome(bus, attempt=1)
    assert outcome is not None and outcome.replayed


def test_a_directory_without_a_valid_result_means_that_run_never_finished(
    bus: ArtifactBus,
) -> None:
    bus.allocate(STAGE_DEVELOP)

    assert HANDLER.existing_outcome(bus, attempt=1) is None


def test_node_replay_restores_the_original_failure_metadata(bus: ArtifactBus) -> None:
    """崩溃恢复不能把 reviewer 越权的小票重建成成功。"""
    slot = bus.allocate(STAGE_DEVELOP, "critique")
    (slot.path / "result.json").write_text(
        json.dumps({"task_id": "ag-1", "passed": True, "approved": True}), encoding="utf-8"
    )
    slot.write_manifest(
        producer="reviewer-employee",
        outputs=["result.json"],
        task_attempt=1,
        result_ok=False,
        failure_kind="scope",
        failure_detail="reviewer 越权",
    )
    node = TopologyNode(
        id="critique", kind=NodeKind.CHECKER, employee="reviewer-employee", procedure="review"
    )

    context = cast("JobContext", SimpleNamespace(bus=bus))
    outcome = HANDLER._already_done(  # noqa: SLF001 - 直接验证崩溃恢复缝
        context, node, "critique", "", 1, {"critique": 1}
    )

    assert outcome is not None
    assert outcome.ok is False
    assert outcome.failure_kind == "scope"
    assert outcome.failure_detail == "reviewer 越权"


def test_single_replay_does_not_turn_a_scope_failure_into_success(bus: ArtifactBus) -> None:
    slot = bus.allocate(STAGE_DEVELOP)
    (slot.path / "result.json").write_text(
        json.dumps({"task_id": "ag-1", "passed": True}), encoding="utf-8"
    )
    slot.write_manifest(
        producer="dev-employee",
        outputs=["result.json"],
        task_attempt=1,
        result_ok=False,
        failure_kind="scope",
        failure_detail="开发改动越权",
    )

    outcome = HANDLER.existing_outcome(bus, attempt=1)

    assert outcome is not None
    assert outcome.replayed is True
    assert outcome.redo is True
    assert "开发改动越权" in outcome.note


def test_aggregate_preserves_a_missing_receipts_failure_kind() -> None:
    node = TopologyNode(
        id="plan",
        kind=NodeKind.WORK,
        employee="decision-employee",
        procedure="requirement-analysis",
    )
    run = TopologyRun(
        outcomes=(
            NodeOutcome(
                node=node,
                ok=False,
                failure_kind="protocol",
                failure_detail="没有 structured_output",
            ),
        ),
        failed=("plan",),
    )

    outcome = HANDLER.aggregate(run)

    assert outcome.valid is False
    assert outcome.failure_kind is FailureKind.PROTOCOL
    assert "structured_output" in outcome.note


@pytest.mark.parametrize(
    ("kind", "cause"),
    [
        (FailureKind.NONE, PlanFailureCause.NONE),
        (FailureKind.PROTOCOL, PlanFailureCause.DELIVERY),
        (FailureKind.CONTRACT, PlanFailureCause.CONTRACT),
        (FailureKind.PROCESS, PlanFailureCause.RUNTIME),
        (FailureKind.TIMEOUT, PlanFailureCause.RUNTIME),
        (FailureKind.BUDGET, PlanFailureCause.LIMIT),
        (FailureKind.TASK_BUDGET, PlanFailureCause.LIMIT),
        (FailureKind.SCOPE, PlanFailureCause.SCOPE),
        (FailureKind.UNKNOWN, PlanFailureCause.UNKNOWN),
    ],
)
def test_every_job_failure_maps_to_a_plan_failure_cause(
    kind: FailureKind, cause: PlanFailureCause
) -> None:
    assert Outcome(event=TaskEvent.PLAN_FAILED, failure_kind=kind).plan_failure_cause is cause


def test_an_unknown_persisted_failure_kind_is_not_guessed_as_process() -> None:
    node = TopologyNode(
        id="plan",
        kind=NodeKind.WORK,
        employee="decision-employee",
        procedure="requirement-analysis",
    )
    run = TopologyRun(
        outcomes=(NodeOutcome(node=node, ok=False, failure_kind="future-kind"),),
    )

    outcome = HANDLER.aggregate(run)

    assert outcome.failure_kind is FailureKind.UNKNOWN
