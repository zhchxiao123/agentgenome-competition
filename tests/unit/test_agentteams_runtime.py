"""AgentTeams 适配器的任务语义层:经假传输驱动,不起任何真实平台。

测试缝是平台传输层(PRD 31 唯一的新缝)。适配器的翻译、落盘、契约、超时
全走真实路径。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from agentgenome.agents.agentteams import AgentTeamsRuntime, TransportOutcome
from agentgenome.agents.runtime import FailureKind, JobSpec
from tests.fixtures.fake_agentteams import FakeTransport

GOOD_RESULT = {
    "task_id": "ag-1",
    "producer": "dev-employee",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
}


def _spec(tmp_path: Path, **overrides: Any) -> JobSpec:
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    context = tmp_path / "context.md"
    context.write_text("# 上下文包\n请完成任务。\n", encoding="utf-8")
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


def _ok_outcome(**overrides: Any) -> TransportOutcome:
    fields: dict[str, Any] = {
        "ok": True,
        "artifacts": {"result.json": json.dumps(GOOD_RESULT, ensure_ascii=False)},
        "events": [{"kind": "text", "text": "干完了"}],
    }
    fields.update(overrides)
    return TransportOutcome(**fields)


# --- 正常路径 ---------------------------------------------------------------


async def test_successful_job_lands_the_result_in_the_output_dir(tmp_path: Path) -> None:
    runtime = AgentTeamsRuntime(FakeTransport([_ok_outcome()]))

    result = await runtime.run_job(_spec(tmp_path))

    assert result.ok is True
    assert result.failure_kind is FailureKind.NONE
    assert result.attempts == 1
    assert result.result_path is not None
    assert json.loads(result.result_path.read_text(encoding="utf-8"))["task_id"] == "ag-1"


async def test_the_platform_receives_the_assembled_context(tmp_path: Path) -> None:
    """容器员工必须拿到与本地员工同一份上下文包——不因跑在远端而少看东西。"""
    transport = FakeTransport([_ok_outcome()])
    runtime = AgentTeamsRuntime(transport)

    await runtime.run_job(_spec(tmp_path))

    assert len(transport.jobs) == 1
    job = transport.jobs[0]
    assert "请完成任务" in job.context_text
    assert job.task_id == "ag-1"
    assert job.procedure_ref == "code-develop@1.0.0"


async def test_events_from_the_platform_land_in_the_log_plane(tmp_path: Path) -> None:
    """事件面记录与本地 Job 同构:审计包不因运行时不同而残缺。"""
    runtime = AgentTeamsRuntime(FakeTransport([_ok_outcome()]))
    spec = _spec(tmp_path)

    result = await runtime.run_job(spec)

    assert result.log_path is not None
    lines = [json.loads(line) for line in result.log_path.read_text().splitlines()]
    assert {"kind": "text", "text": "干完了"} in lines


async def test_tools_and_limits_travel_with_the_job(tmp_path: Path) -> None:
    transport = FakeTransport([_ok_outcome()])
    runtime = AgentTeamsRuntime(transport)
    spec = _spec(tmp_path, tools_allow=["read_file"], tools_deny=["run_command"], max_tokens=42)

    await runtime.run_job(spec)

    job = transport.jobs[0]
    assert job.tools_allow == ("read_file",)
    assert job.tools_deny == ("run_command",)
    assert job.max_tokens == 42


# --- 失败路径 ---------------------------------------------------------------


async def test_platform_unavailability_fails_fast_and_points_at_the_platform(
    tmp_path: Path,
) -> None:
    """错误要指向平台,不是员工或工序——故障归因不绕弯路。"""
    runtime = AgentTeamsRuntime(FakeTransport(unavailable="连接被拒绝"))

    result = await runtime.run_job(_spec(tmp_path))

    assert result.ok is False
    assert result.failure_kind is FailureKind.PROCESS
    assert "AgentTeams" in (result.failure_detail or "")
    assert "连接被拒绝" in (result.failure_detail or "")


async def test_worker_side_failure_is_a_process_failure(tmp_path: Path) -> None:
    runtime = AgentTeamsRuntime(
        FakeTransport([TransportOutcome(ok=False, detail="Worker 容器崩了")])
    )

    result = await runtime.run_job(_spec(tmp_path))

    assert result.ok is False
    assert result.failure_kind is FailureKind.PROCESS
    assert "Worker 容器崩了" in (result.failure_detail or "")


async def test_a_hung_platform_call_times_out(tmp_path: Path) -> None:
    """到点判超时失败,不无限等一个失联的容器。"""
    runtime = AgentTeamsRuntime(FakeTransport([_ok_outcome()], delay_s=30.0))

    result = await runtime.run_job(_spec(tmp_path, timeout_s=1))

    assert result.ok is False
    assert result.failure_kind is FailureKind.TIMEOUT


async def test_missing_result_is_a_single_contract_failure(tmp_path: Path) -> None:
    """契约语义与子进程运行时同源:产物缺失失败，但不暗中重跑完整 Agent。"""
    transport = FakeTransport([TransportOutcome(ok=True, artifacts={}), _ok_outcome()])
    runtime = AgentTeamsRuntime(transport)

    result = await runtime.run_job(_spec(tmp_path))

    assert result.ok is False
    assert result.failure_kind is FailureKind.CONTRACT
    assert result.attempts == 1
    assert len(transport.jobs) == 1


async def test_direct_credentials_are_a_configuration_contradiction(tmp_path: Path) -> None:
    """容器员工拿不到真实凭证。声明了必须直连凭证的配置显式报错,不静默丢弃。"""
    transport = FakeTransport([_ok_outcome()])
    runtime = AgentTeamsRuntime(transport)

    result = await runtime.run_job(_spec(tmp_path, credentials={"GITHUB_TOKEN": "ghp_x"}))

    assert result.ok is False
    assert result.failure_kind is FailureKind.PROCESS
    assert "凭证" in (result.failure_detail or "")
    assert transport.jobs == [], "配置矛盾不该把任务发到平台"


async def test_credentials_never_reach_the_transport(tmp_path: Path) -> None:
    """凭证不下发是结构性的:TransportJob 里根本没有凭证字段。"""
    transport = FakeTransport([_ok_outcome()])
    runtime = AgentTeamsRuntime(transport)

    await runtime.run_job(_spec(tmp_path))

    assert "credentials" not in transport.jobs[0].as_dict()


# --- dry-run 与预检 ---------------------------------------------------------


async def test_dry_run_records_the_context_without_calling_the_platform(
    tmp_path: Path,
) -> None:
    transport = FakeTransport([_ok_outcome()])
    runtime = AgentTeamsRuntime(transport)
    spec = _spec(tmp_path)

    result = await runtime.run_job(spec, dry_run=True)

    assert result.dry_run is True
    assert result.ok is True
    assert transport.jobs == []
    assert (spec.output_dir / "context-attempt-1.md").is_file()


def test_preflight_delegates_to_the_transport(tmp_path: Path) -> None:
    transport = FakeTransport([_ok_outcome()])
    AgentTeamsRuntime(transport).preflight()

    assert transport.preflights == 1


def test_preflight_surfaces_platform_unavailability(tmp_path: Path) -> None:
    import pytest

    from agentgenome.agents.agentteams import PlatformUnavailable

    runtime = AgentTeamsRuntime(FakeTransport(unavailable="401 未授权"))

    with pytest.raises(PlatformUnavailable, match="401"):
        runtime.preflight()
