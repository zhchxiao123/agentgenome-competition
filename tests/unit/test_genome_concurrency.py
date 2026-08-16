"""基因组任务的并发闸与独立预算。

一次全量知识初始化会派发几十个并行深读作业。共用研发任务那个并发闸的话,它会把整条研发
流水线堵死——而堵住的那段时间里,系统在外面看起来就是"不干活了"。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from agentgenome.agents.pool import AgentPool
from agentgenome.agents.runtime import JobResult, JobSpec
from agentgenome.core.genome_task import (
    GenomeTaskKind,
    GenomeTaskState,
    GenomeTaskStore,
    ModuleBusy,
    Origin,
)
from agentgenome.core.task_ids import Lane, lane_of


class _Slow:
    """一个会停在那儿的运行时,用来观察闸门到底放了几个进来。"""

    name = "slow"

    def __init__(self) -> None:
        self.running = 0
        self.peak = 0
        self.release = asyncio.Event()

    def preflight(self) -> None:
        return None

    async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
        self.running += 1
        self.peak = max(self.peak, self.running)
        await self.release.wait()
        self.running -= 1
        return JobResult(ok=True)


def _spec(task_id: str, tmp_path: Path) -> JobSpec:
    # **每个任务一棵工作树。** 池的互斥锁按工作区算(图里的并行节点各写各的),
    # 共用一个目录的话这几条泳道测试量到的会是互斥而不是泳道。
    work = tmp_path / task_id
    work.mkdir(parents=True, exist_ok=True)
    (tmp_path / "ctx.md").write_text("# 上下文\n")
    return JobSpec(
        task_id=task_id,
        employee_id="arch",
        procedure_id="knowledge-init",
        procedure_version="1.0.0",
        round=1,
        workdir=work,
        context_file=tmp_path / "ctx.md",
        output_dir=tmp_path / "out" / task_id,
    )


# --- 两条泳道 ----------------------------------------------------------------


def test_the_id_prefix_tells_the_lane() -> None:
    """泳道从 id 前缀推出来。**前缀在 PRD 21-01 就已经是有含义的**,不是这里临时约定的。"""
    assert lane_of("gn-20260901-001") != lane_of("ag-20260901-001")


async def test_genome_tasks_do_not_eat_the_dev_lane(tmp_path: Path) -> None:
    """一次全量初始化不该把整条研发流水线堵死。"""
    runtime = _Slow()
    pool = AgentPool({"slow": runtime}, global_jobs=2, genome_jobs=1)

    genome = [
        asyncio.create_task(pool.submit(_spec(f"gn-20260901-{i:03d}", tmp_path), "slow"))
        for i in range(3)
    ]
    await asyncio.sleep(0.05)
    genome_peak = runtime.peak

    dev = [
        asyncio.create_task(pool.submit(_spec(f"ag-20260901-{i:03d}", tmp_path), "slow"))
        for i in range(2)
    ]
    await asyncio.sleep(0.05)

    assert genome_peak == 1, "基因组任务突破了自己的闸"
    assert runtime.peak == 3, "研发任务被基因组任务挡住了"
    runtime.release.set()
    await asyncio.gather(*genome, *dev)


async def test_dev_tasks_do_not_eat_the_genome_lane(tmp_path: Path) -> None:
    runtime = _Slow()
    pool = AgentPool({"slow": runtime}, global_jobs=1, genome_jobs=2)

    dev = [
        asyncio.create_task(pool.submit(_spec(f"ag-20260901-{i:03d}", tmp_path), "slow"))
        for i in range(3)
    ]
    await asyncio.sleep(0.05)
    genome = [
        asyncio.create_task(pool.submit(_spec(f"gn-20260901-{i:03d}", tmp_path), "slow"))
        for i in range(2)
    ]
    await asyncio.sleep(0.05)

    assert runtime.peak == 3
    runtime.release.set()
    await asyncio.gather(*dev, *genome)


# --- 同模块互斥 ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> GenomeTaskStore:
    return GenomeTaskStore(tmp_path)


def test_a_second_task_on_the_same_module_is_refused(store: GenomeTaskStore) -> None:
    """两次重建并行跑会互相覆盖。"""
    store.create(
        title="重建", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="order-service"
    )

    with pytest.raises(ModuleBusy) as caught:
        store.create(
            title="又一次", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="order-service"
        )

    assert "order-service" in str(caught.value)


def test_different_modules_run_in_parallel(store: GenomeTaskStore) -> None:
    store.create(
        title="a", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="order-service"
    )

    second = store.create(
        title="b", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="inventory-service"
    )

    assert second.subject == "inventory-service"


def test_a_finished_task_stops_blocking_its_module(store: GenomeTaskStore) -> None:
    """互斥管的是"同时",不是"这个模块这辈子只能重建一次"。"""
    first = store.create(
        title="a", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="order-service"
    )
    store.save(first.evolve(state=GenomeTaskState.SUBMITTED))

    again = store.create(
        title="b", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="order-service"
    )

    assert again.id != first.id


def test_tasks_without_a_module_never_block_each_other(store: GenomeTaskStore) -> None:
    """全量初始化与蒸馏都不作用在某一个模块上。拿 None 当键会让它们互相排斥。"""
    store.create(title="a", kind=GenomeTaskKind.DISTILL, origin=Origin.SYSTEM)

    second = store.create(title="b", kind=GenomeTaskKind.DISTILL, origin=Origin.SYSTEM)

    assert second.id


# --- 独立预算 ----------------------------------------------------------------


def test_the_genome_lane_gets_its_own_budget(tmp_path: Path) -> None:
    """**光有配置项不算数。** 一次接入不该吃掉整月额度——但只有真的被读到，这句话才成立。"""
    from agentgenome.cli import _lane_budget
    from agentgenome.config import Config

    config = Config()

    assert _lane_budget(config, "gn-20260901-001") == config.genome_tasks.per_task_tokens
    assert _lane_budget(config, "ag-20260901-001") == config.budgets.per_task_tokens


def test_the_lane_limits_are_wired_through_from_configuration() -> None:
    """闸门只在测试里够得着的话，生产里它就是写死的默认值。

    **比的是「构造了几个池」与「传了几次配置」，不是一个写死的数字。** 写死的话，加一条
    新的派发路径时这条断言会红——而修它最省事的做法是把数字改大一个，于是它守的东西就没了。
    """
    import inspect

    from agentgenome import cli

    source = inspect.getsource(cli)

    assert source.count("genome_jobs=config.genome_tasks.concurrent_jobs") == source.count(
        "AgentPool("
    ), "有 AgentPool 的构造点没把基因组泳道的配置传进去"


async def test_token_accounting_is_per_task_across_both_lanes(tmp_path: Path) -> None:
    """成本看板上没有账外开支：两条泳道共用一套记账，但额度各算各的。"""
    runtime = _Counting()
    pool = AgentPool({"slow": runtime}, global_jobs=2, genome_jobs=2)
    pool.set_task_budget("gn-20260901-001", 100_000)
    pool.set_task_budget("ag-20260901-001", 100_000)
    spec = _spec("gn-20260901-001", tmp_path)

    await pool.submit(replace(spec, max_tokens=1_000), "slow")

    assert pool.tokens_used("gn-20260901-001") == 700
    assert pool.tokens_used("ag-20260901-001") == 0
    assert pool.remaining_budget("ag-20260901-001") == 100_000


class _Counting:
    """一个会如实上报用量的运行时。"""

    name = "slow"

    def preflight(self) -> None:
        return None

    async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
        return JobResult(ok=True, tokens_used=700, tokens_available=True)


def test_a_lane_is_decided_by_the_prefix_not_by_a_second_parameter() -> None:
    assert lane_of("gn-20260901-001") is Lane.GENOME
    assert lane_of("ag-20260901-001") is Lane.DEV


def test_an_id_with_no_known_prefix_falls_to_the_dev_lane() -> None:
    """`agctl job run` 这类手工原语可以拿任意字符串当 id。**这条兜底是有意的，也有代价**：
    一个本该是基因组任务却还没有正式编号的作业会落在研发泳道上。"""
    assert lane_of("knowledge-init") is Lane.DEV


def test_a_waiting_task_can_be_cancelled_so_the_module_is_not_wedged(
    store: GenomeTaskStore, tmp_path: Path
) -> None:
    """**待确认不是终态。** 没有取消入口的话，一个没人回答的闸门会把那个模块永久堵住：
    既跑不完，也重建不了。"""
    from agentgenome.core.events import EventLog
    from agentgenome.core.genome_driver import GenomeDriver
    from agentgenome.core.genome_transitions import GenomeEvent

    stuck = store.save(
        store.create(
            title="重建", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="order-service"
        ).evolve(state=GenomeTaskState.AWAITING_CONFIRMATION)
    )
    with pytest.raises(ModuleBusy):
        store.create(
            title="再来", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="order-service"
        )

    GenomeDriver(store, EventLog(tmp_path)).deliver(stuck.id, GenomeEvent.CANCEL)

    assert store.create(
        title="再来", kind=GenomeTaskKind.REINIT, origin=Origin.HUMAN, subject="order-service"
    )
