"""agentteams 适配器的用量记账与预算语义。

平台网关按 Job 粒度给用量;拿不到时显式标"不可得",**不填 0**——填 0 会让
成本看板悄悄少算,比缺数据更糟。派发前的任务预算拒绝在池里,与运行时无关,
这里只确认它对容器员工照常生效。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from agentgenome.agents.agentteams import AgentTeamsRuntime, TransportOutcome
from agentgenome.agents.capabilities import capabilities_of
from agentgenome.agents.pool import AgentPool
from agentgenome.agents.runtime import FailureKind, JobSpec
from tests.fixtures.fake_agentteams import FakeTransport
from tests.unit.test_agentteams_runtime import _ok_outcome


def _spec(tmp_path: Path, **overrides: Any) -> JobSpec:
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    context = tmp_path / "context.md"
    context.write_text("# 上下文包\n", encoding="utf-8")
    spec = JobSpec(
        task_id="ag-1",
        employee_id="dev-employee",
        procedure_id="code-develop",
        procedure_version="1.0.0",
        round=1,
        workdir=workdir,
        context_file=context,
        output_dir=tmp_path / "out",
        timeout_s=5,
    )
    return replace(spec, **overrides) if overrides else spec


async def test_gateway_usage_lands_in_the_job_result(tmp_path: Path) -> None:
    runtime = AgentTeamsRuntime(FakeTransport([_ok_outcome(tokens_used=12_345)]))

    result = await runtime.run_job(_spec(tmp_path))

    assert result.ok is True
    assert result.tokens_used == 12_345
    assert result.tokens_available is True


async def test_missing_gateway_usage_is_marked_unavailable_not_zero(tmp_path: Path) -> None:
    """账面不出现虚假的 0:缺口要看得见、查得着。"""
    runtime = AgentTeamsRuntime(FakeTransport([_ok_outcome(tokens_used=None)]))

    result = await runtime.run_job(_spec(tmp_path))

    assert result.ok is True
    assert result.tokens_available is False


async def test_usage_is_accounted_even_when_the_worker_fails(tmp_path: Path) -> None:
    """失败的 Job 钱也已经烧了——不结账的表现是成本看板长期少算。"""
    outcome = TransportOutcome(ok=False, detail="Worker 崩了", tokens_used=777)
    runtime = AgentTeamsRuntime(FakeTransport([outcome]))

    result = await runtime.run_job(_spec(tmp_path))

    assert result.ok is False
    assert result.tokens_used == 777
    assert result.tokens_available is True


async def test_the_pool_charges_the_task_ledger_for_container_jobs(tmp_path: Path) -> None:
    """成本口径与本地 Job 一致:池按 JobResult 记账,运行时无关。"""
    runtime = AgentTeamsRuntime(FakeTransport([_ok_outcome(tokens_used=5_000)]))
    pool = AgentPool({"agentteams": runtime})
    pool.set_task_budget("ag-1", total=100_000)

    await pool.submit(_spec(tmp_path, max_tokens=50_000), "agentteams")

    assert pool.tokens_used("ag-1") == 5_000


async def test_budget_refusal_happens_before_the_platform_is_called(tmp_path: Path) -> None:
    """剩余额度装不下这个 Job 就不开工——对容器员工照常生效,且不打扰平台。"""
    transport = FakeTransport([_ok_outcome()])
    pool = AgentPool({"agentteams": AgentTeamsRuntime(transport)})
    pool.set_task_budget("ag-1", total=10_000)

    result = await pool.submit(_spec(tmp_path, max_tokens=50_000), "agentteams")

    assert result.ok is False
    assert result.failure_kind is FailureKind.TASK_BUDGET
    assert transport.jobs == [], "预算拒绝不该把任务发到平台"


def test_the_capability_matrix_declares_gateway_usage_available() -> None:
    profile = capabilities_of("agentteams")

    assert profile is not None
    assert profile.usage_available is True
