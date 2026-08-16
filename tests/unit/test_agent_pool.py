"""AgentPool:并发闸、任务串行与任务级预算。

并发断言用**可手动放行的假 runtime**,不用 sleep 做时序判断——sleep 式的测试在慢
机器上会随机失败,然后被人加长 sleep,最后变成又慢又不可靠的东西。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentgenome.agents.pool import AgentPool, RuntimeNotRegistered
from agentgenome.agents.runtime import FailureKind, JobResult, JobSpec
from agentgenome.core.scope import ScopePolicy
from agentgenome.space.scope_guard import SCOPE_REPORT


class GatedRuntime:
    """一个只有被测试明确放行才会返回的运行时。"""

    name = "gated"

    def __init__(self, tokens_per_job: int = 0) -> None:
        self.started = asyncio.Semaphore(0)
        self.gate = asyncio.Event()
        self.in_flight = 0
        self.peak = 0
        self.tokens_per_job = tokens_per_job
        self.calls: list[str] = []

    async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
        self.calls.append(spec.task_id)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.started.release()
        try:
            await self.gate.wait()
        finally:
            self.in_flight -= 1
        return JobResult(ok=True, tokens_used=self.tokens_per_job, tokens_available=True)

    def preflight(self) -> None:
        return None

    async def wait_for_starts(self, count: int) -> None:
        for _ in range(count):
            await self.started.acquire()

    def release(self) -> None:
        self.gate.set()


class ExplodingRuntime:
    name = "boom"

    async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
        raise RuntimeError("运行时炸了")

    def preflight(self) -> None:
        return None


def _spec(
    tmp_path: Path, task_id: str = "ag-1", worktree: str = "", **overrides: Any
) -> JobSpec:
    """一份 JobSpec。`worktree` 给不同的名字就是"另一棵工作树"——池的互斥锁按它算。"""
    work = tmp_path / (worktree or "work")
    work.mkdir(parents=True, exist_ok=True)
    (tmp_path / "ctx.md").write_text("# 上下文\n")
    spec = JobSpec(
        task_id=task_id,
        employee_id="dev",
        procedure_id="code-develop",
        procedure_version="1.0.0",
        round=1,
        workdir=work,
        context_file=tmp_path / "ctx.md",
        output_dir=tmp_path / "out" / task_id,
    )
    return replace(spec, **overrides) if overrides else spec


# --- 并发闸 -----------------------------------------------------------------


async def test_peak_concurrency_never_exceeds_the_global_limit(tmp_path: Path) -> None:
    """子进程既吃机器也吃 API 配额,不设上限时多任务并行会把两者一起打爆。"""
    runtime = GatedRuntime()
    pool = AgentPool({"gated": runtime}, global_jobs=2)
    jobs = [
        asyncio.create_task(
            pool.submit(_spec(tmp_path, f"ag-{i}", worktree=f"ws-{i}"), runtime_name="gated")
        )
        for i in range(5)
    ]

    await runtime.wait_for_starts(2)
    await asyncio.sleep(0)  # 让还在排队的协程有机会跑(如果闸门是坏的)

    assert runtime.peak == 2

    runtime.release()
    await asyncio.gather(*jobs)
    assert runtime.peak == 2


async def test_jobs_sharing_a_worktree_run_serially(tmp_path: Path) -> None:
    """互斥的理由**从来是工作区,不是任务**:两个进程同时往一棵工作树里写会把它写坏。"""
    runtime = GatedRuntime()
    pool = AgentPool({"gated": runtime}, global_jobs=8)
    jobs = [
        asyncio.create_task(pool.submit(_spec(tmp_path, "ag-same"), runtime_name="gated"))
        for _ in range(3)
    ]

    await runtime.wait_for_starts(1)
    await asyncio.sleep(0)

    assert runtime.in_flight == 1

    runtime.release()
    await asyncio.gather(*jobs)


async def test_jobs_of_the_same_task_in_different_worktrees_run_in_parallel(
    tmp_path: Path,
) -> None:
    """图里的并行节点各有各的工作树。

    按任务上锁的话它们会被串回去,而症状是"并行了但一点没变快"——原因在三层之外的一把锁上。
    """
    runtime = GatedRuntime()
    pool = AgentPool({"gated": runtime}, global_jobs=8)
    jobs = [
        asyncio.create_task(
            pool.submit(_spec(tmp_path, "ag-same", worktree=f"node-{i}"), runtime_name="gated")
        )
        for i in range(3)
    ]

    await runtime.wait_for_starts(3)

    assert runtime.in_flight == 3

    runtime.release()
    await asyncio.gather(*jobs)


async def test_jobs_of_different_tasks_run_in_parallel(tmp_path: Path) -> None:
    """互斥不该把不相干的任务也堵住。"""
    runtime = GatedRuntime()
    pool = AgentPool({"gated": runtime}, global_jobs=8)
    jobs = [
        asyncio.create_task(
            pool.submit(_spec(tmp_path, f"ag-{i}", worktree=f"ws-{i}"), runtime_name="gated")
        )
        for i in range(3)
    ]

    await runtime.wait_for_starts(3)

    assert runtime.in_flight == 3

    runtime.release()
    await asyncio.gather(*jobs)


# --- 任务级预算 -------------------------------------------------------------


async def test_a_job_is_refused_once_the_task_budget_is_exhausted(tmp_path: Path) -> None:
    """Job 级上限管单次执行,任务级管一个需求总共能烧多少。

    一个任务可能跑十几个 Job,只有 Job 级上限的话总额无人看管。
    """
    runtime = GatedRuntime(tokens_per_job=600)
    runtime.release()
    pool = AgentPool({"gated": runtime}, global_jobs=4, task_budgets={"ag-1": 1000})
    spec = _spec(tmp_path, "ag-1", max_tokens=500)

    first = await pool.submit(spec, runtime_name="gated")
    second = await pool.submit(spec, runtime_name="gated")

    assert first.ok is True
    assert second.ok is False
    assert second.failure_kind is FailureKind.TASK_BUDGET


async def test_a_job_whose_ceiling_does_not_fit_is_refused(tmp_path: Path) -> None:
    """**任务预算是硬上限。** 剩余额度装不下这个 Job 的上限就直接拒,不夹。

    夹住上限听起来省钱:剩 400 时把 Job 上限压到 400 照样能跑。但那意味着"这个
    Job 能烧多少"由剩余额度决定而不是由角色决定,员工会在一个被悄悄压缩的预算下
    干活并因超限而失败——表现为"最近它老是干到一半就断",极难归因。而且运行中的
    预算闸是**事后**发现越线的(用量得先被上报),压缩后的尾额那一段刚好最容易
    冲过头,任务总额于是不再是硬的。
    """
    runtime = GatedRuntime(tokens_per_job=600)
    runtime.release()
    pool = AgentPool({"gated": runtime}, global_jobs=4, task_budgets={"ag-1": 1000})

    await pool.submit(_spec(tmp_path, "ag-1", max_tokens=600), runtime_name="gated")
    second = await pool.submit(_spec(tmp_path, "ag-1", max_tokens=600), runtime_name="gated")

    assert second.ok is False
    assert second.failure_kind is FailureKind.TASK_BUDGET
    assert "600" in (second.failure_detail or ""), "要说清楚差多少"
    assert "400" in (second.failure_detail or "")


async def test_a_job_that_still_fits_is_allowed_through(tmp_path: Path) -> None:
    """拒的是装不下的,不是所有的——只测拦得住不测放得过,等于没测。"""
    runtime = GatedRuntime(tokens_per_job=600)
    runtime.release()
    pool = AgentPool({"gated": runtime}, global_jobs=4, task_budgets={"ag-1": 1000})

    await pool.submit(_spec(tmp_path, "ag-1", max_tokens=400), runtime_name="gated")
    second = await pool.submit(_spec(tmp_path, "ag-1", max_tokens=400), runtime_name="gated")

    assert second.ok is True


async def test_the_job_ceiling_is_handed_to_the_runtime_untouched(tmp_path: Path) -> None:
    """Job 上限由角色定义决定,池不改它——改了的话员工是在一个它不知道的预算下干活。"""
    seen: list[int] = []

    class Recording(GatedRuntime):
        async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
            seen.append(spec.max_tokens)
            return await super().run_job(spec, dry_run)

    runtime = Recording(tokens_per_job=100)
    runtime.release()
    pool = AgentPool({"gated": runtime}, global_jobs=4, task_budgets={"ag-1": 1000})

    await pool.submit(_spec(tmp_path, "ag-1", max_tokens=500), runtime_name="gated")
    await pool.submit(_spec(tmp_path, "ag-1", max_tokens=500), runtime_name="gated")

    assert seen == [500, 500]


async def test_a_refused_job_never_reaches_the_runtime(tmp_path: Path) -> None:
    """预算耗尽时不该拉起子进程——那正是要省下来的东西。"""
    runtime = GatedRuntime(tokens_per_job=2000)
    runtime.release()
    pool = AgentPool({"gated": runtime}, global_jobs=4, task_budgets={"ag-1": 1000})

    await pool.submit(_spec(tmp_path, "ag-1", max_tokens=1000), runtime_name="gated")
    calls_after_first = list(runtime.calls)
    await pool.submit(_spec(tmp_path, "ag-1", max_tokens=1000), runtime_name="gated")

    assert runtime.calls == calls_after_first


async def test_usage_accumulates_into_the_task_budget(tmp_path: Path) -> None:
    runtime = GatedRuntime(tokens_per_job=300)
    runtime.release()
    pool = AgentPool({"gated": runtime}, global_jobs=4, task_budgets={"ag-1": 10_000})
    spec = _spec(tmp_path, "ag-1", max_tokens=1000)

    await pool.submit(spec, runtime_name="gated")
    await pool.submit(spec, runtime_name="gated")

    assert pool.tokens_used("ag-1") == 600


async def test_tasks_without_a_declared_budget_are_unlimited(tmp_path: Path) -> None:
    runtime = GatedRuntime(tokens_per_job=10_000)
    runtime.release()
    pool = AgentPool({"gated": runtime}, global_jobs=4)

    for _ in range(3):
        result = await pool.submit(_spec(tmp_path, "ag-free"), runtime_name="gated")
        assert result.ok is True


# --- 运行时选择与异常 -------------------------------------------------------


async def test_unknown_runtime_is_a_readable_error(tmp_path: Path) -> None:
    """不静默回退到默认实现——那会让一个配置错误变成"最近它好像变笨了"。"""
    pool = AgentPool({"gated": GatedRuntime()}, global_jobs=1)

    with pytest.raises(RuntimeNotRegistered, match="qwen-code"):
        await pool.submit(_spec(tmp_path), runtime_name="qwen-code")


async def test_slots_are_released_when_a_job_raises(tmp_path: Path) -> None:
    """不释放的话跑几次之后系统就卡死了,而且卡得毫无征兆。"""
    pool = AgentPool({"boom": ExplodingRuntime()}, global_jobs=1)

    for _ in range(3):
        with pytest.raises(RuntimeError, match="运行时炸了"):
            await pool.submit(_spec(tmp_path, "ag-1"), runtime_name="boom")

    # 名额与任务锁都没漏:同一个任务还能继续提交。
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(pool.submit(_spec(tmp_path, "ag-1"), runtime_name="boom"), 2)


async def test_preflight_checks_every_registered_runtime() -> None:
    class Failing:
        name = "failing"

        async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
            return JobResult(ok=True)

        def preflight(self) -> None:
            raise RuntimeError("缺依赖")

    pool = AgentPool({"gated": GatedRuntime(), "failing": Failing()}, global_jobs=1)

    with pytest.raises(RuntimeError, match="缺依赖"):
        pool.preflight()


# --- 越权检查:每个 Job 结束后无条件执行 -------------------------------------
#
# 放在池里而不是让调用方记得调:池是执行 Job 的唯一入口,放在这里"忘了检查"
# 就不再是一种可能的状态。


class WritingRuntime:
    """往工作区里写几个文件的假运行时——真实写盘是这组测试成立的前提。"""

    name = "writer"

    def __init__(self, files: dict[str, str], ok: bool = True) -> None:
        self.files = files
        self._ok = ok

    async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
        for relative, content in self.files.items():
            target = spec.workdir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return JobResult(
            ok=self._ok,
            failure_kind=FailureKind.NONE if self._ok else FailureKind.CONTRACT,
        )

    def preflight(self) -> None:
        return None


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    (root / "app").mkdir(exist_ok=True)
    (root / "app" / "main.py").write_text("print('hi')\n")
    (root / "genome").mkdir(exist_ok=True)
    (root / "genome" / "rules.md").write_text("# 规则\n原样\n")
    for args in (
        ("init", "--initial-branch=main"),
        ("config", "user.email", "fixture@agentgenome.test"),
        ("config", "user.name", "Fixture"),
        ("add", "-A"),
        ("commit", "-m", "chore: 初始化"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _scoped(tmp_path: Path, **overrides: Any) -> JobSpec:
    _repo(tmp_path)
    return _spec(
        tmp_path,
        scope=ScopePolicy(write_paths=("app/**",), forbid_paths=("genome/**",)),
        **overrides,
    )


async def test_a_job_writing_out_of_scope_is_failed(tmp_path: Path) -> None:
    runtime = WritingRuntime({"elsewhere.py": "x = 1\n"})
    pool = AgentPool({"writer": runtime})

    result = await pool.submit(_scoped(tmp_path), runtime_name="writer")

    assert result.ok is False
    assert result.failure_kind is FailureKind.SCOPE
    assert "elsewhere.py" in (result.failure_detail or "")


async def test_a_job_writing_out_of_scope_is_rolled_back(tmp_path: Path) -> None:
    runtime = WritingRuntime({"genome/rules.md": "# 我给自己开绿灯\n"})
    pool = AgentPool({"writer": runtime})

    await pool.submit(_scoped(tmp_path), runtime_name="writer")

    assert (tmp_path / "work" / "genome" / "rules.md").read_text() == "# 规则\n原样\n"


async def test_a_compliant_job_is_left_alone(tmp_path: Path) -> None:
    """只测拦得住不测放得过,等于没测。"""
    runtime = WritingRuntime({"app/feature.py": "# 新功能\n"})
    pool = AgentPool({"writer": runtime})

    result = await pool.submit(_scoped(tmp_path), runtime_name="writer")

    assert result.ok is True
    assert (tmp_path / "work" / "app" / "feature.py").exists()


async def test_the_scope_report_is_always_written(tmp_path: Path) -> None:
    """ "这个 Job 碰了什么"要能被回答,不管它有没有越权。"""
    runtime = WritingRuntime({"app/feature.py": "# 新功能\n"})
    pool = AgentPool({"writer": runtime})
    spec = _scoped(tmp_path)

    result = await pool.submit(spec, runtime_name="writer")

    assert result.scope_report_path == spec.output_dir / SCOPE_REPORT
    payload = json.loads(result.scope_report_path.read_text(encoding="utf-8"))
    assert payload["touched"] == ["app/feature.py"]


async def test_a_job_that_already_failed_is_still_checked(tmp_path: Path) -> None:
    """失败的 Job 照样可能已经写出去了。检查是无条件的。"""
    runtime = WritingRuntime({"elsewhere.py": "x = 1\n"}, ok=False)
    pool = AgentPool({"writer": runtime})

    result = await pool.submit(_scoped(tmp_path), runtime_name="writer")

    assert result.failure_kind is FailureKind.SCOPE
    assert not (tmp_path / "work" / "elsewhere.py").exists()


async def test_a_spec_without_a_policy_needs_no_git(tmp_path: Path) -> None:
    """`agctl job run` 这类手工原语可以指向任意目录,不该因此炸掉。"""
    runtime = WritingRuntime({"elsewhere.py": "x = 1\n"})
    pool = AgentPool({"writer": runtime})

    result = await pool.submit(_spec(tmp_path), runtime_name="writer")

    assert result.ok is True


async def test_a_dry_run_is_not_checked(tmp_path: Path) -> None:
    """空跑什么都不写,拿它去跑一遍 git 纯属浪费。"""
    runtime = WritingRuntime({})
    pool = AgentPool({"writer": runtime})

    result = await pool.submit(_scoped(tmp_path), runtime_name="writer", dry_run=True)

    assert result.scope_report_path is None
