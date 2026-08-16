"""运行时契约与执行骨架:拉起真实子进程、校验结果契约、返回 JobResult。"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentgenome.agents.runtime import FailureKind, JobSpec
from agentgenome.agents.subprocess_runtime import SubprocessRuntime
from tests.fixtures import fake_agent
from tests.fixtures.fake_agent import SCRIPT_ENV

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

GOOD_RESULT = {
    "task_id": "ag-1",
    "producer": "dev-employee",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
}


def _spec(tmp_path: Path, script: dict[str, Any], **overrides: Any) -> JobSpec:
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    context = tmp_path / "context.md"
    context.write_text("# 上下文包\n请完成任务。\n")
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
        timeout_s=20,
        max_tokens=1_000_000,
        credentials={
            SCRIPT_ENV: json.dumps(
                {**script, "workdir": str(tmp_path / "work"), "output_dir": str(tmp_path / "out")},
                ensure_ascii=False,
            )
        },
    )
    return replace(spec, **overrides) if overrides else spec


@pytest.fixture
def runtime() -> SubprocessRuntime:
    """拉起"真实的假子进程"——真进程管理,只有被拉起的是谁被替换了。

    给脚本路径而非 `-m`:子进程环境是从白名单构造的,不带 PYTHONPATH,按模块名
    找不到它——而"环境不继承父进程"正是被测行为之一,不该为了测试方便破掉。
    """
    return SubprocessRuntime(argv=[sys.executable, fake_agent.__file__])


# --- 正常路径 ---------------------------------------------------------------


async def test_successful_job_returns_ok_and_points_at_the_result(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    spec = _spec(tmp_path, {"result": GOOD_RESULT})

    result = await runtime.run_job(spec)

    assert result.ok is True
    assert result.failure_kind is FailureKind.NONE
    assert result.result_path is not None
    assert json.loads(result.result_path.read_text())["task_id"] == "ag-1"


async def test_job_result_records_exit_code_and_duration(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    result = await runtime.run_job(_spec(tmp_path, {"result": GOOD_RESULT}))

    assert result.exit_code == 0
    assert result.duration_s >= 0


async def test_agent_writes_files_into_the_workdir(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    spec = _spec(tmp_path, {"result": GOOD_RESULT, "files": {"src/new.py": "print(1)\n"}})

    await runtime.run_job(spec)

    assert (spec.workdir / "src" / "new.py").read_text() == "print(1)\n"


# --- 结果契约 ---------------------------------------------------------------


async def test_missing_result_json_does_not_rerun_the_whole_job_by_default(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    """缺小票不能让一轮开发在同一个 Job 里不透明地重做一遍。"""
    spec = _spec(tmp_path, {"result": None, "succeed_on_retry": True})

    result = await runtime.run_job(spec)

    assert result.ok is False
    assert result.failure_kind is FailureKind.CONTRACT
    assert result.attempts == 1


async def test_missing_result_json_twice_fails_with_contract(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    result = await runtime.run_job(_spec(tmp_path, {"result": None}))

    assert result.ok is False
    assert result.failure_kind is FailureKind.CONTRACT
    assert result.attempts == 1
    assert "result.json" in (result.failure_detail or "")


async def test_result_violating_the_schema_fails_with_contract(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    result = await runtime.run_job(_spec(tmp_path, {"result": {"task_id": "ag-1"}}))

    assert result.failure_kind is FailureKind.CONTRACT
    detail = result.failure_detail or ""
    assert "passed" in detail, "错误信息要说清哪条约束没过"


async def test_malformed_result_json_fails_with_contract(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    result = await runtime.run_job(_spec(tmp_path, {"result": "{ 这不是 JSON"}))

    assert result.failure_kind is FailureKind.CONTRACT


# --- 进程失败 ---------------------------------------------------------------


async def test_nonzero_exit_is_reported_as_process_failure(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    result = await runtime.run_job(_spec(tmp_path, {"exit_code": 3, "result": GOOD_RESULT}))

    assert result.ok is False
    assert result.failure_kind is FailureKind.PROCESS
    assert result.exit_code == 3


async def test_a_process_that_exits_zero_without_doing_anything_still_fails(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    """真实观察到的形态:Agent 进 plan mode,退出码 0、无报错、也无任何产物。

    退出码完全不足以判断 Job 是否真的完成——这正是结果契约必须是硬的理由。
    """
    result = await runtime.run_job(_spec(tmp_path, {"exit_code": 0, "result": None}))

    assert result.ok is False
    assert result.failure_kind is FailureKind.CONTRACT


# --- dry-run ----------------------------------------------------------------


async def test_dry_run_does_not_spawn_and_records_the_context(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    spec = _spec(tmp_path, {"result": GOOD_RESULT, "files": {"should-not-exist.txt": "x"}})

    result = await runtime.run_job(spec, dry_run=True)

    assert result.dry_run is True
    assert result.ok is True
    assert not (spec.workdir / "should-not-exist.txt").exists(), "dry-run 不该拉起子进程"
    assert (spec.output_dir / "context-attempt-1.md").is_file()


# --- preflight --------------------------------------------------------------


def test_preflight_rejects_a_missing_executable() -> None:
    with pytest.raises(RuntimeError, match="不存在|未找到"):
        SubprocessRuntime(argv=["/nonexistent/agent-binary"]).preflight()


def test_preflight_passes_for_an_available_executable(runtime: SubprocessRuntime) -> None:
    runtime.preflight()


# --- 扩权申请是可选字段 -------------------------------------------------------


def test_a_develop_result_without_a_scope_request_is_still_valid(tmp_path: Path) -> None:
    """**既有的录制回放产物不带这个字段。**

    加成必填等于让全部历史录制一夜之间失效——而它们是主缝的全部依据,重录一遍的代价
    远大于这个字段能换来的严格。
    """
    from agentgenome.genome.procedures import load_procedure
    from agentgenome.genome.roster import scaffold_roster

    scaffold_roster(tmp_path)
    schema = load_procedure(tmp_path / "genome" / "procedures" / "code-develop").output_schema

    assert "scope_request" not in (schema or {}).get("required", [])
    assert "scope_request" in (schema or {}).get("properties", {})


# --- 产物裁决(output_check,PRD 34) -----------------------------------------


def test_output_check_failure_is_a_contract_failure(tmp_path: Path) -> None:
    """树产出类工序的成败判据是对产物目录的确定性校验——验证产物,不验证自述。"""
    from agentgenome.agents.contract import check_result_contract

    (tmp_path / "result.json").write_text(
        json.dumps({"task_id": "t", "producer": "p"}), encoding="utf-8"
    )

    check = check_result_contract(
        tmp_path, {}, output_check=lambda _out: "staging/modules/x/map.yaml: 缺 confidence"
    )

    assert check.ok is False
    assert check.detail is not None and "map.yaml" in check.detail


def test_output_check_and_missing_receipt_are_reported_together(tmp_path: Path) -> None:
    """两半的失败一次报全:契约重试只有一次,分两轮报的话第二条错误到达时额度已花完。"""
    from agentgenome.agents.contract import check_result_contract

    check = check_result_contract(tmp_path, {}, output_check=lambda _out: "没有产物")

    assert check.ok is False
    assert check.detail is not None
    assert "没有产物" in check.detail
    assert "result.json" in check.detail


def test_a_receipt_cannot_overrule_the_product_verdict(tmp_path: Path) -> None:
    """小票声称一切正常而产物校验失败 → 失败。自述是最弱证据,产物是最强证据。"""
    from agentgenome.agents.contract import check_result_contract
    from agentgenome.genome.staging import RECEIPT_SCHEMA

    (tmp_path / "result.json").write_text(
        json.dumps({"task_id": "t", "producer": "p", "notes": ["一切正常"]}), encoding="utf-8"
    )

    check = check_result_contract(
        tmp_path, RECEIPT_SCHEMA, output_check=lambda _out: "staging 校验未通过"
    )

    assert check.ok is False


def test_a_flat_receipt_with_extra_fields_is_refused(tmp_path: Path) -> None:
    """小票小而平是 schema 强制的——留着口子的话,"文件进信封"会从多余字段长回来。"""
    from agentgenome.agents.contract import check_result_contract
    from agentgenome.genome.staging import RECEIPT_SCHEMA

    (tmp_path / "result.json").write_text(
        json.dumps({"task_id": "t", "producer": "p", "doc_body": "# 整篇文档"}),
        encoding="utf-8",
    )

    check = check_result_contract(tmp_path, RECEIPT_SCHEMA)

    assert check.ok is False
