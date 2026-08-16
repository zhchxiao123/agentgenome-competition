"""回归测试:自进化管道真的被触发,不只是"接口存在"。

2026-08 的一次审查发现 `Orchestrator._pending_distill` 从 `__init__` 赋值成 `None` 之后
再没有第二处写它——`_trigger_evolution`(`Effect.TRIGGER_EVOLUTION` 唯一的处理者)之前只做
知识命中记账,从没往队列里放过任务。`drain_evolution` 自己没问题、`cli.py::_advance` 也按
预期调用它,但上游从没喂过东西给它:任务照常完成,只是从第一天起就没有真的蒸馏过一次,而且
没有任何症状。

这里断的是外部可观察行为——`drain_evolution()` 排过队之后必须真的返回一次蒸馏结果(哪怕因为
没有真实 Agent 运行时而空手而归),而不是内部私有字段的值。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from agentgenome.core.states import TaskState
from agentgenome.jobs.orchestrator import Orchestrator
from agentgenome.umodel.graph import InMemoryGraph
from tests.fixtures.mall import materialize_mall

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        cli_app,
        [
            "init",
            "--local-only",
            str(root),
            "--name",
            "mall",
            "--repo",
            mall["order-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    return root


def test_completing_a_task_actually_queues_it_for_distillation(workspace: Path) -> None:
    orchestrator = Orchestrator(workspace)
    task = orchestrator.store.create(title="t", requirement="r")
    completed = orchestrator.store.save(task.evolve(state=TaskState.COMPLETED))

    orchestrator._trigger_evolution(completed)
    result = asyncio.run(orchestrator.drain_evolution())

    # 修复前 `drain_evolution` 永远是 `None`(没有排过队);现在它必须真的跑一次蒸馏,
    # 哪怕这次蒸馏因为没有真实 Agent 运行时而空手而归。
    assert result is not None
    assert result.task_id == completed.id


def test_escalating_a_task_also_queues_it_for_distillation(workspace: Path) -> None:
    """ESCALATED 任务是最富矿的蒸馏素材(design doc §10.1)——不该只有 COMPLETED 排队。"""
    orchestrator = Orchestrator(workspace)
    task = orchestrator.store.create(title="t", requirement="r")
    escalated = orchestrator.store.save(
        task.evolve(state=TaskState.ESCALATED, escalate_reason="test")
    )

    orchestrator._trigger_evolution(escalated)
    result = asyncio.run(orchestrator.drain_evolution())

    assert result is not None
    assert result.task_id == escalated.id


def test_drain_evolution_is_a_no_op_when_nothing_was_queued(workspace: Path) -> None:
    """没有任务终结时不该凭空蒸馏——这条锁住"排队"和"消费"两端都对得上。"""
    orchestrator = Orchestrator(workspace)

    result = asyncio.run(orchestrator.drain_evolution())

    assert result is None


def test_escalating_through_the_budget_fallback_path_queues_distillation(
    workspace: Path,
) -> None:
    """`_escalate()` 是两条不经过迁移表的兜底升级路径共用的落地(任务预算不足、连续被
    守卫挡住)。这条路此前完全不调 `_trigger_evolution`——迁移表里补的 `TRIGGER_EVOLUTION`
    对它没有任何帮助,因为它压根不经过 `decide()`/`_run_effects()`。
    """
    orchestrator = Orchestrator(workspace)
    task = orchestrator.store.create(title="t", requirement="r")

    orchestrator._escalate(task.id, "任务预算不足以再跑一个 Job")
    result = asyncio.run(orchestrator.drain_evolution())

    assert result is not None
    assert result.task_id == task.id


def test_completing_a_task_syncs_the_semantic_graph(workspace: Path) -> None:
    """`_sync_graph` 定义了但此前没有任何调用方——语义图谱从没真的同步过一次。"""
    orchestrator = Orchestrator(workspace)
    orchestrator.graph = InMemoryGraph()
    task = orchestrator.store.create(title="t", requirement="r")
    completed = orchestrator.store.save(task.evolve(state=TaskState.COMPLETED))

    orchestrator._trigger_evolution(completed)

    assert orchestrator.graph.entity(f"task:{completed.id}") is not None


def test_escalating_a_task_does_not_sync_the_graph(workspace: Path) -> None:
    """ESCALATED 任务没有合入任何代码——同步它只会往图谱里写一条指向空改动的边。"""
    orchestrator = Orchestrator(workspace)
    orchestrator.graph = InMemoryGraph()
    task = orchestrator.store.create(title="t", requirement="r")
    escalated = orchestrator.store.save(
        task.evolve(state=TaskState.ESCALATED, escalate_reason="test")
    )

    orchestrator._trigger_evolution(escalated)

    assert orchestrator.graph.entity(f"task:{escalated.id}") is None


def test_a_distillation_shows_up_as_a_genome_task(workspace: Path) -> None:
    """它此前没有身份：失败与耗时没有地方安放，进行中时人完全看不见。"""
    from agentgenome.core.genome_task import GenomeTaskKind, GenomeTaskStore, Origin

    orchestrator = Orchestrator(workspace)
    task = orchestrator.store.create(title="t", requirement="r")
    completed = orchestrator.store.save(task.evolve(state=TaskState.COMPLETED))

    orchestrator._trigger_evolution(completed)
    asyncio.run(orchestrator.drain_evolution())

    records = GenomeTaskStore(workspace).all_tasks(kind=GenomeTaskKind.DISTILL)
    assert len(records) == 1
    assert records[0].origin is Origin.SYSTEM
    assert records[0].source_task_id == completed.id
    assert records[0].is_terminal, "蒸馏跑完了，记录却还停在非终态"


def test_the_source_task_timeline_points_at_it(workspace: Path) -> None:
    """「这个任务的经验变成了什么」仍然读得连贯。指针不是内容。"""
    from agentgenome.core.events import EventLog

    orchestrator = Orchestrator(workspace)
    task = orchestrator.store.create(title="t", requirement="r")
    completed = orchestrator.store.save(task.evolve(state=TaskState.COMPLETED))

    orchestrator._trigger_evolution(completed)
    asyncio.run(orchestrator.drain_evolution())

    pointers = [
        event
        for event in EventLog(workspace).events(completed.id)
        if event.payload.get("note") == "distillation"
    ]
    assert len(pointers) == 1
    assert pointers[0].payload["genome_task_id"].startswith("gn-")


def test_a_distillation_record_never_bothers_anyone(workspace: Path) -> None:
    """系统自发的失败算已了结——一次蒸馏失败不该变成一条需要处理的告警。"""
    from agentgenome.core.genome_task import GenomeTaskStore

    orchestrator = Orchestrator(workspace)
    task = orchestrator.store.create(title="t", requirement="r")
    completed = orchestrator.store.save(task.evolve(state=TaskState.COMPLETED))

    orchestrator._trigger_evolution(completed)
    asyncio.run(orchestrator.drain_evolution())

    store = GenomeTaskStore(workspace)
    assert store.needs_attention() == ()
    assert store.awaiting_confirmation() == ()


# --- 教训蒸发信号(PRD 41) ---------------------------------------------------


def test_a_fix_task_whose_distillation_yields_nothing_is_flagged_as_evaporating(
    workspace: Path,
) -> None:
    """fix 类任务(修复轮次 > 0)结项、蒸馏空手而归 → 一条教训正在蒸发,入可疑账。"""
    from agentgenome.genome.suspects import SuspectKind, pending_suspects

    orchestrator = Orchestrator(workspace)
    task = orchestrator.store.create(title="修一个超卖", requirement="r")
    completed = orchestrator.store.save(task.evolve(state=TaskState.COMPLETED, fix_rounds=2))

    orchestrator._trigger_evolution(completed)
    asyncio.run(orchestrator.drain_evolution())

    found = pending_suspects(workspace)
    assert [item.kind for item in found] == [SuspectKind.EVAPORATED]
    assert found[0].task_id == completed.id


def test_a_clean_first_try_completion_is_not_a_lost_lesson(workspace: Path) -> None:
    """一次过的任务没有"修复的教训"可言——非 fix 类不触发蒸发信号。"""
    from agentgenome.genome.suspects import pending_suspects

    orchestrator = Orchestrator(workspace)
    task = orchestrator.store.create(title="t", requirement="r")
    completed = orchestrator.store.save(task.evolve(state=TaskState.COMPLETED))

    orchestrator._trigger_evolution(completed)
    asyncio.run(orchestrator.drain_evolution())

    assert pending_suspects(workspace) == ()


def test_a_fix_task_that_did_yield_a_lesson_is_not_evaporating(workspace: Path) -> None:
    """对照:蒸馏产出了卡片提案的 fix 任务不入账——教训被捡起来了。"""
    from agentgenome.genome.evolution.pipeline import DistillResult
    from agentgenome.genome.suspects import pending_suspects

    orchestrator = Orchestrator(workspace)
    task = orchestrator.store.create(title="修一个超卖", requirement="r")
    completed = orchestrator.store.save(task.evolve(state=TaskState.COMPLETED, fix_rounds=1))

    orchestrator._record_evaporation(
        completed.id, DistillResult(task_id=completed.id, written=["lesson-0001"])
    )

    assert pending_suspects(workspace) == ()


def test_both_signal_kinds_share_the_ledger_without_clobbering(workspace: Path) -> None:
    """蒸发与可疑过期同账共存——第三本账只有一本,两类信号互不覆盖。"""
    from agentgenome.genome.suspects import (
        Suspect,
        SuspectKind,
        pending_suspects,
        record_suspects,
    )

    record_suspects(
        workspace,
        (
            Suspect(
                kind=SuspectKind.STALE,
                task_id="ag-1",
                card="order-service/reserve-flow",
                changed=("repos/order-service/src/x.py",),
            ),
        ),
    )
    orchestrator = Orchestrator(workspace)
    task = orchestrator.store.create(title="修一个超卖", requirement="r")
    completed = orchestrator.store.save(task.evolve(state=TaskState.COMPLETED, fix_rounds=1))

    orchestrator._trigger_evolution(completed)
    asyncio.run(orchestrator.drain_evolution())

    kinds = sorted(item.kind.value for item in pending_suspects(workspace))
    assert kinds == ["evaporated_lesson", "stale_card"]
