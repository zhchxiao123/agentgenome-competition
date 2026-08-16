"""Job 执行的唯一入口。

上层不直接持有运行时实例,一律经过这里——因为四件事必须在一个地方统一管住:
同时能跑几个、同一个任务能不能并发、这个任务的钱花完了没有,以及**它有没有写到
授权范围之外**。

越权检查放在这里而不是让调用方记得调:池是执行 Job 的唯一入口,放在这里"忘了检查"
就不再是一种可能的状态。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping

from agentgenome.agents.runtime import AgentRuntime, FailureKind, JobResult, JobSpec
from agentgenome.core.task_ids import Lane, lane_of
from agentgenome.space.scope_guard import (
    SCOPE_REPORT,
    capture_baseline,
    capture_preexisting,
    enforce_scope,
)


class RuntimeNotRegistered(LookupError):
    """请求的运行时没有注册。

    不静默回退到默认实现:一个员工被配成跑在不存在的运行时上,应该立刻报错,
    而不是悄悄换一个跑起来——那会让配置错误表现为"最近它好像变笨了"这种
    极难归因的现象。
    """


class AgentPool:
    """带并发闸与任务级预算的执行池。"""

    def __init__(
        self,
        runtimes: dict[str, AgentRuntime],
        global_jobs: int = 3,
        genome_jobs: int = 2,
        task_budgets: dict[str, int | None] | None = None,
    ) -> None:
        self._runtimes = dict(runtimes)
        #: 并发闸,**每条泳道一把**。子进程既吃机器也吃 API 配额,不设上限会把两者一起打爆。
        #: 为什么分两条泳道见 `core.task_ids.Lane`。
        self._slots = {
            Lane.DEV: asyncio.Semaphore(global_jobs),
            Lane.GENOME: asyncio.Semaphore(genome_jobs),
        }
        #: **每个工作区一把锁,不是每个任务一把。**
        #:
        #: 理由从来就是"同一个隔离工作区并发写会把它写坏",而不是"同一个任务不能并行"——
        #: 图里的并行节点各有各的工作树,按任务上锁会把它们串回去,症状是"并行了但一点
        #: 没变快",而原因在三层之外的一把锁上。
        self._workspace_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._budgets = {
            task_id: total
            for task_id, total in (task_budgets or {}).items()
            if total is not None
        }
        self._spent: dict[str, int] = defaultdict(int)

    def preflight(self) -> None:
        """逐个自检已注册的运行时。"""
        for runtime in self._runtimes.values():
            runtime.preflight()

    def runtime_names(self) -> tuple[str, ...]:
        """返回实际可派发的运行时；编排层的自动兜底不能只看静态配置猜。"""
        return tuple(self._runtimes)

    def _lookup(self, name: str) -> AgentRuntime:
        """按名字取运行时。未注册即报错,不回退到默认实现。"""
        runtime = self._runtimes.get(name)
        if runtime is None:
            registered = ", ".join(sorted(self._runtimes)) or "(空)"
            raise RuntimeNotRegistered(f"运行时未注册: {name}(已注册: {registered})")
        return runtime

    def tokens_used(self, task_id: str) -> int:
        return self._spent[task_id]

    def set_task_budget(self, task_id: str, total: int | None, spent: int = 0) -> None:
        """告诉池这个任务的预算与已用量。

        池是**进程内**的:重启之后它对"已经烧了多少"一无所知。真相在任务表里,
        所以每次派发前由编排器把它同步进来——不同步的话,重启就等于把预算清零。
        """
        if total is None:
            self._budgets.pop(task_id, None)
            return
        self._budgets[task_id] = total
        self._spent[task_id] = max(self._spent[task_id], spent)

    def remaining_budget(self, task_id: str) -> int | None:
        """任务剩余额度。没声明预算的任务不受限。"""
        total = self._budgets.get(task_id)
        return None if total is None else total - self._spent[task_id]

    def _refuse_if_over_budget(self, spec: JobSpec) -> JobResult | None:
        """剩余额度装不下这个 Job 的上限就拒绝,且**不拉起子进程**。

        **任务预算是硬上限。** 另一种做法是把 Job 上限夹到剩余额度,尾额就不会浪费。
        但那样"这个 Job 能烧多少"就由剩余额度决定,而不是由角色定义决定:员工在一个
        被悄悄压缩的预算下干活、干到一半因超限而断,表现为"最近它老是干不完",极难
        归因。而且运行中的预算闸是**事后**发现越线的(用量得先被上报才看得见),被
        压缩的那一小段尾额刚好最容易冲过头——于是任务总额也不再是硬的。

        代价是尾额确实会剩下来。这是刻意的:预算的意义是"到此为止",不是"尽量花完"。
        """
        remaining = self.remaining_budget(spec.task_id)
        if remaining is None or remaining >= spec.max_tokens:
            return None
        return JobResult(
            ok=False,
            failure_kind=FailureKind.TASK_BUDGET,
            failure_detail=(
                f"任务 {spec.task_id} 的 token 预算不足以再跑一个 Job:"
                f"这次需要 {spec.max_tokens},只剩 {max(remaining, 0)}"
                f"(已用 {self._spent[spec.task_id]} / {self._budgets[spec.task_id]})"
            ),
        )

    async def submit(self, spec: JobSpec, runtime_name: str, dry_run: bool = False) -> JobResult:
        runtime = self._lookup(runtime_name)

        refused = self._refuse_if_over_budget(spec)
        if refused is not None:
            return refused

        # 两把闸都用 async with:Job 抛异常时名额与任务锁都会释放。不释放的话
        # 跑几次之后系统就卡死,而且卡得毫无征兆。
        #
        # 越权检查与回滚也在锁里:回滚会动整个工作区,同任务的下一个 Job 必须等它
        # 做完,否则它会看到一个改到一半的现场。
        checked = spec.scope is not None and not dry_run
        async with self._workspace_locks[str(spec.workdir)], self._slots[lane_of(spec.task_id)]:
            baseline = capture_baseline(spec.workdir) if checked else None
            # 同一个隔离工作区会被这个任务的多个员工先后使用。开工前就脏的路径
            # 不算这个 Job 的账——算了的话,一个纯读的判断类 Job 会因为上一轮开发
            # 留下的改动被判越权,然后回滚把上一轮的成果一起抹掉。
            preexisting = capture_preexisting(spec.workdir, baseline) if checked else {}
            result = await runtime.run_job(spec, dry_run=dry_run)
            if checked:
                _apply_scope(spec, result, baseline, preexisting)

        if result.tokens_available:
            self._spent[spec.task_id] += result.tokens_used
        return result


def _apply_scope(
    spec: JobSpec, result: JobResult, baseline: str | None, preexisting: Mapping[str, str]
) -> None:
    """无条件跑一遍越权检查,越权则把结果改判为 SCOPE 失败。

    **已经失败的 Job 照样要查**:它失败之前完全可能已经写出去了,而"反正它失败了"
    这个理由不会让那些文件消失。

    越权覆盖原来的失败原因,因为处置不同:契约失败该重试,越权该回滚加升级——
    保留原因会让状态机走错分支。原因本身在报告里,不会丢。
    """
    assert spec.scope is not None
    report = enforce_scope(
        workdir=spec.workdir,
        policy=spec.scope,
        task_id=spec.task_id,
        employee_id=spec.employee_id,
        output_dir=spec.output_dir,
        baseline=baseline,
        preexisting=preexisting,
    )
    result.scope_report_path = spec.output_dir / SCOPE_REPORT
    if report.ok:
        return
    result.ok = False
    result.failure_kind = FailureKind.SCOPE
    result.failure_detail = f"{spec.employee_id} 的改动越出授权范围: {report.render()}"


__all__ = ["AgentPool", "RuntimeNotRegistered"]
