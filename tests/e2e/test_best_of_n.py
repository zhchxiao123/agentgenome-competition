"""best-of-n 端到端:三路取向并行,门禁筛选,胜者进流水线,落选进教材。

**显式 opt-in**:任务级把模板点成 `best-of-n`。N 倍成本必须是一个被看见的决定——自动启用
等于让系统替人决定花几倍的钱,而这类任务恰恰是错误成本最高的那批。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskStore
from agentgenome.core.topology import BEST_OF_N
from agentgenome.space.git_ws import GitWorkspace
from tests.e2e.test_critique_loop import record_node  # noqa: PLC2701 —— 同一套录制写法
from tests.e2e.test_dag_execution import arm_plan, topology_events, worktrees  # noqa: PLC2701
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    DEV_RESULT,
    FAILING_TEST,
    PASSING_TEST,
    _orchestrator,
    _submit,
    library,
    workspace,
)

__all__ = ["library", "workspace"]

VARIANTS = ("minimal", "perf", "contract")


def arm_attempts(library: Path, task_id: str, green: tuple[str, ...]) -> None:
    """每一路一份录制:`green` 里的那几路写出能过门禁的测试,其余写一个必挂的。

    **门禁跑的是真 pytest**,所以"过闸"在这条链路上不是一个开关,而是真的跑出来的。
    """
    for variant in VARIANTS:
        body = PASSING_TEST if variant in green else FAILING_TEST
        record_node(
            library,
            "dev-employee",
            "code-develop",
            f"attempt.{variant}.1",
            DEV_RESULT | {"task_id": task_id},
            {
                "repos/order-service/tests/test_reserve.py": body,
                f"repos/order-service/src/order/{variant}.py": f"# {variant} 这一路\n",
            },
        )
    record_node(
        library,
        "reviewer-employee",
        "code-critique",
        "judge.1",
        {
            "task_id": task_id,
            "producer": "reviewer-employee",
            "created_at": "2026-09-01T12:00:00Z",
            "passed": True,
            "approved": True,
            "findings": [],
            "winner": "perf",
            "notes": "性能优先那一路的热路径少一次拷贝",
        },
    )


def opt_in(workspace: Path, task_id: str) -> None:
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(topology=BEST_OF_N))


async def test_three_attempts_run_and_the_winner_lands_on_the_task_branch(
    workspace: Path, library: Path
) -> None:
    """三路并行 → 两路过闸 → judge 择优 → 胜者的改动合回任务分支。"""
    task_id = _submit(workspace, "重构预占逻辑")
    arm_plan(library, task_id, None)
    arm_attempts(library, task_id, green=("minimal", "perf"))
    opt_in(workspace, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    ran = [item for item in topology_events(orchestrator, task_id) if item.get("template_id")]
    assert ran and ran[0]["template_id"] == BEST_OF_N
    task_tree = GitWorkspace(workspace).worktree_path(task_id)
    assert (task_tree / "repos/order-service/src/order/perf.py").exists(), "胜者的改动要合进来"
    assert not (task_tree / "repos/order-service/src/order/contract.py").exists()


async def test_the_contrast_is_kept_and_the_losers_are_cleaned_up(
    workspace: Path, library: Path
) -> None:
    """**落选即教材**:对比先落盘,取完材才清理工作树。"""
    task_id = _submit(workspace, "重构预占逻辑")
    arm_plan(library, task_id, None)
    arm_attempts(library, task_id, green=("minimal", "perf"))
    opt_in(workspace, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    contrast = json.loads(
        (workspace / "tasks" / task_id / "contrast.json").read_text(encoding="utf-8")
    )
    assert contrast["winner"] == "perf"
    assert set(contrast["survivors"]) == {"minimal", "perf"}
    # 证据是**指针**:各路的产物目录,而不是把产物内容抄一份进来。
    assert all(item["slot"] for item in contrast["attempts"])
    assert any(
        item["variant"] == "contract" and not item["passed"] for item in contrast["attempts"]
    )
    # 落选的工作树清干净了,胜者那棵还在(它刚被合过)。
    assert f"{task_id}.attempt-contract" not in worktrees(workspace, task_id)


async def test_nobody_passing_the_gate_redoes_the_round_with_the_contrast(
    workspace: Path, library: Path
) -> None:
    """零过闸 = 全部失败,而**这次多花的钱唯一还能产出的东西就是那份对比**。"""
    task_id = _submit(workspace, "重构预占逻辑")
    arm_plan(library, task_id, None)
    arm_attempts(library, task_id, green=())
    opt_in(workspace, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.DEVELOPING
    assert task.fix_rounds == 1
    contrast = json.loads(
        (workspace / "tasks" / task_id / "contrast.json").read_text(encoding="utf-8")
    )
    assert contrast["winner"] == ""
    assert all(not item["passed"] for item in contrast["attempts"] if item["node"] == "gate")


async def test_a_budget_that_cannot_cover_n_attempts_is_refused_before_dispatch(
    workspace: Path, library: Path
) -> None:
    """跑到一半发现钱不够的话,前面几路的钱已经花掉了。"""
    task_id = _submit(workspace, "重构预占逻辑")
    arm_plan(library, task_id, None)
    arm_attempts(library, task_id, green=("perf",))
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(topology=BEST_OF_N))
    orchestrator = _orchestrator(workspace, library, enforce_budget=True)

    await orchestrator.advance(task_id)  # 需求解析先跑完(这一步的钱是够的)
    # 到开发这一步时,剩下的钱装不下三路。
    store.save(store.get(task_id).evolve(budget_tokens=400_000, tokens_used=100_000))
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.ESCALATED
    assert "降低路数" in (task.escalate_reason or "")


async def test_without_opting_in_nothing_changes(workspace: Path, library: Path) -> None:
    """无自动启用路径。"""
    task_id = _submit(workspace, "重构预占逻辑")
    arm_plan(library, task_id, None)
    from tests.e2e.test_orchestrator import _record  # noqa: PLC2701

    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        {"repos/order-service/tests/test_reserve.py": PASSING_TEST},
    )
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    assert not (workspace / "tasks" / task_id / "contrast.json").exists()
    assert worktrees(workspace, task_id) == [task_id]
