"""只消费已确认规格的通用验证执行 Module。"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agentgenome.gates.parsers import ParseContext, parse_failures
from agentgenome.verification.environments import (
    AdapterRegistry,
    AdapterUnavailable,
    PrepareContext,
    PreparedEnvironment,
    default_registry,
    process_environment,
)
from agentgenome.verification.evidence import validate_spec_evidence
from agentgenome.verification.models import VerificationGate, VerificationSpec
from agentgenome.verification.report import (
    GATE_LOGS_DIR,
    LOG_TAIL_LINES,
    GateOutcome,
    GateReport,
    GateReportKind,
    GateResult,
)


@dataclass(frozen=True)
class VerificationContext:
    task_id: str
    module_root: Path
    output_dir: Path
    environment: Mapping[str, str] | None = None


def run_verification(
    spec: VerificationSpec,
    context: VerificationContext,
    *,
    registry: AdapterRegistry | None = None,
) -> GateReport:
    """执行一份已确认规格；工具链差异全部留在 Adapter 后面。"""
    evidence_issue = validate_spec_evidence(spec, Path(context.module_root))
    if evidence_issue is not None:
        return GateReport(
            task_id=context.task_id,
            module=spec.module,
            passed=False,
            kind=GateReportKind.TAMPERED,
            gates=[
                GateResult(
                    id=gate.id,
                    outcome=GateOutcome.REFUSED,
                    required=gate.required,
                    detail=evidence_issue,
                )
                for gate in spec.gates
            ],
            created_at=datetime.now(UTC).isoformat(),
        )
    adapters = registry or default_registry()
    base_env = process_environment(context.environment)
    results: list[GateResult] = []
    with (
        tempfile.TemporaryDirectory(prefix="agentgenome-verification-") as scratch,
        ExitStack() as stack,
    ):
        prepare_context = PrepareContext(
            module_root=Path(context.module_root),
            scratch_root=Path(scratch),
            base_env=base_env,
        )
        prepared: dict[str, PreparedEnvironment] = {}
        preparation_errors: dict[str, str] = {}
        for environment_name in dict.fromkeys(gate.environment for gate in spec.gates):
            reference = spec.environments.get(environment_name)
            if reference is None:
                preparation_errors[environment_name] = (
                    f"规格引用了不存在的环境: {environment_name}"
                )
                continue
            try:
                adapter = adapters.require(reference.adapter)
                item = stack.enter_context(adapter.prepare(reference, prepare_context))
                preparation_error = _prepare_environment(item, context)
                if preparation_error is not None:
                    preparation_errors[environment_name] = preparation_error
                else:
                    prepared[environment_name] = item
            except AdapterUnavailable as error:
                preparation_errors[environment_name] = str(error)

        for gate in spec.gates:
            preparation_error = preparation_errors.get(gate.environment)
            if preparation_error is not None:
                results.append(_unavailable(gate, preparation_error))
                continue
            results.append(_run_gate(gate, context, prepared[gate.environment].env))

    passed = all(result.passed for result in results if result.required)
    return GateReport(
        task_id=context.task_id,
        module=spec.module,
        passed=passed,
        kind=_kind(results, passed),
        gates=results,
        created_at=datetime.now(UTC).isoformat(),
    )


def _run_gate(
    gate: VerificationGate,
    context: VerificationContext,
    environment: Mapping[str, str],
) -> GateResult:
    path = environment.get("PATH", "")
    executable = gate.command.argv[0]
    if shutil.which(executable, path=path) is None:
        return _unavailable(gate, f"命令不存在: {executable}。这是环境问题,改代码解决不了。")

    module_root = Path(context.module_root).resolve()
    cwd = (module_root / gate.command.cwd).resolve()
    if not cwd.is_relative_to(module_root):
        return _unavailable(gate, f"命令 cwd 越出模块目录: {gate.command.cwd}")

    started = time.monotonic()
    output, exit_code, timed_out = _execute(
        gate.command.argv,
        cwd,
        dict(environment),
        gate.timeout_s,
    )
    duration = time.monotonic() - started
    log_path = _write_log(Path(context.output_dir), gate.id, output)
    outcome = (
        GateOutcome.TIMEOUT
        if timed_out
        else (GateOutcome.PASSED if exit_code == 0 else GateOutcome.FAILED)
    )
    failures = (
        []
        if outcome is GateOutcome.PASSED
        else parse_failures(
            gate.parser,
            ParseContext(
                gate_id=gate.id,
                module_dir=module_root,
                junit_xml_path=(
                    str(module_root / gate.junit_xml_path)
                    if gate.junit_xml_path is not None
                    else None
                ),
                output=output,
            ),
        )
    )
    return GateResult(
        id=gate.id,
        outcome=outcome,
        required=gate.required,
        duration_s=duration,
        exit_code=exit_code,
        failures=failures,
        log_tail=_tail(output),
        log_path=str(Path(GATE_LOGS_DIR) / log_path.name),
        detail=f"超过 {gate.timeout_s} 秒被中止" if timed_out else "",
    )


def _prepare_environment(
    prepared: PreparedEnvironment, context: VerificationContext
) -> str | None:
    for argv in prepared.setup:
        executable = argv[0]
        path = prepared.env.get("PATH", "")
        if shutil.which(executable, path=path) is None:
            return f"依赖准备命令不存在: {executable}"
        output, exit_code, timed_out = _execute(
            argv,
            Path(context.module_root),
            dict(prepared.env),
            600,
        )
        if timed_out:
            return f"依赖准备超时: {' '.join(argv)}"
        if exit_code != 0:
            return f"依赖准备失败({exit_code}): {_tail(output)}"
    return None


def _execute(
    argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout_s: int
) -> tuple[str, int | None, bool]:
    process = subprocess.Popen(  # noqa: S603 — argv 来自已确认且带证据的验证规格
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout_s)
        return output or "", process.returncode, False
    except subprocess.TimeoutExpired as expired:
        _kill_group(process)
        leftover, _ = process.communicate()
        partial = expired.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        return partial + (leftover or ""), None, True


def _unavailable(gate: VerificationGate, detail: str) -> GateResult:
    return GateResult(
        id=gate.id,
        outcome=GateOutcome.UNAVAILABLE,
        required=gate.required,
        detail=detail,
    )


def _write_log(output_dir: Path, gate_id: str, output: str) -> Path:
    directory = output_dir / GATE_LOGS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{gate_id}.log"
    target.write_text(output, encoding="utf-8")
    return target


def _tail(output: str) -> str:
    return "\n".join(output.splitlines()[-LOG_TAIL_LINES:])


def _kill_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


def _kind(results: list[GateResult], passed: bool) -> GateReportKind:
    if passed:
        return GateReportKind.NONE
    blocking = [result for result in results if result.required]
    if any(result.outcome is GateOutcome.UNAVAILABLE for result in blocking):
        return GateReportKind.ENVIRONMENT
    return GateReportKind.SEMANTIC


__all__ = ["VerificationContext", "run_verification"]
