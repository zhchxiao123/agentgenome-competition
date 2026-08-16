"""agentteams 适配器的产物合同:schema 随任务下发,交作业手艺教 Worker 按合同交付。

契约校验与重试语义来自共用件(issue 01)——这里只测适配器把合同**送到位**了,
以及违约时的行为与子进程运行时逐字对齐。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from agentgenome.agents.agentteams import AgentTeamsRuntime, TransportOutcome
from agentgenome.agents.runtime import FailureKind, JobSpec
from tests.fixtures.fake_agentteams import FakeTransport
from tests.unit.test_agentteams_runtime import GOOD_RESULT, _ok_outcome

RESULT_SCHEMA = {
    "type": "object",
    "required": ["task_id", "producer", "created_at", "passed"],
    "properties": {
        "task_id": {"type": "string"},
        "producer": {"type": "string"},
        "created_at": {"type": "string"},
        "passed": {"type": "boolean"},
    },
}


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
        output_schema=RESULT_SCHEMA,
        timeout_s=5,
    )
    return replace(spec, **overrides) if overrides else spec


async def test_the_output_schema_travels_with_the_job(tmp_path: Path) -> None:
    transport = FakeTransport([_ok_outcome()])

    await AgentTeamsRuntime(transport).run_job(_spec(tmp_path))

    assert transport.jobs[0].output_schema == RESULT_SCHEMA


async def test_the_delivery_craft_teaches_the_result_convention(tmp_path: Path) -> None:
    """Worker 要知道产物写到哪、按什么 schema——这份手艺随每个 Job 下发。"""
    transport = FakeTransport([_ok_outcome()])

    await AgentTeamsRuntime(transport).run_job(_spec(tmp_path))

    craft = transport.jobs[0].craft
    assert "result.json" in craft
    assert "passed" in craft, "schema 本体要在手艺里,Worker 不猜字段"


async def test_the_craft_is_delivered_even_without_a_schema(tmp_path: Path) -> None:
    """没有 schema 也要交 result.json——产物存在性本身就是合同的一部分。"""
    transport = FakeTransport([_ok_outcome()])

    await AgentTeamsRuntime(transport).run_job(_spec(tmp_path, output_schema={}))

    assert "result.json" in transport.jobs[0].craft


async def test_a_schema_violation_does_not_hide_a_second_full_attempt(tmp_path: Path) -> None:
    bad = TransportOutcome(ok=True, artifacts={"result.json": json.dumps({"task_id": "ag-1"})})
    transport = FakeTransport([bad, _ok_outcome()])

    result = await AgentTeamsRuntime(transport).run_job(_spec(tmp_path))

    assert result.ok is False
    assert result.attempts == 1
    assert len(transport.jobs) == 1


async def test_a_schema_violation_fails_with_contract(tmp_path: Path) -> None:
    bad = TransportOutcome(ok=True, artifacts={"result.json": json.dumps({"task_id": "ag-1"})})
    transport = FakeTransport([bad])

    result = await AgentTeamsRuntime(transport).run_job(_spec(tmp_path))

    assert result.ok is False
    assert result.failure_kind is FailureKind.CONTRACT
    assert result.attempts == 1
    assert "passed" in (result.failure_detail or ""), "错误信息要说清哪条约束没过"


async def test_a_valid_result_passes_the_shared_contract_check(tmp_path: Path) -> None:
    transport = FakeTransport(
        [TransportOutcome(ok=True, artifacts={"result.json": json.dumps(GOOD_RESULT)})]
    )

    result = await AgentTeamsRuntime(transport).run_job(_spec(tmp_path))

    assert result.ok is True
    assert result.result_path is not None
    assert json.loads(result.result_path.read_text())["passed"] is True
