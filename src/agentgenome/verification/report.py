"""模块验证执行的公共报告。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentgenome.core.verdict import VerdictKind
from agentgenome.verification.models import PendingVerification

GATE_REPORT = "gate-report.json"
GATE_LOGS_DIR = "logs"
LOG_TAIL_LINES = 200
PRODUCER = "unit-gate"


class GateReceipt(BaseModel):
    """从持久化报告里读取编排器真正需要的最小契约。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    passed: bool


def load_gate_receipt(output_dir: Path) -> GateReceipt:
    target = Path(output_dir) / GATE_REPORT
    try:
        return GateReceipt.model_validate_json(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"门禁报告不可读: {target}: {error}") from error


class GateOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"


GateReportKind = VerdictKind


@dataclass
class GateResult:
    id: str
    outcome: GateOutcome
    required: bool
    duration_s: float = 0.0
    exit_code: int | None = None
    failures: list[dict[str, Any]] = field(default_factory=list)
    log_tail: str = ""
    log_path: str | None = None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is GateOutcome.PASSED

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "passed": self.passed,
            "outcome": self.outcome.value,
            "required": self.required,
            "duration_s": round(self.duration_s, 3),
            "exit_code": self.exit_code,
            "failures": list(self.failures),
            "log_tail": self.log_tail,
            "log_path": self.log_path,
            "detail": self.detail,
        }


@dataclass
class GateReport:
    task_id: str
    module: str
    passed: bool
    kind: GateReportKind
    gates: list[GateResult] = field(default_factory=list)
    created_at: str = ""
    regressions: list[dict[str, Any]] = field(default_factory=list)
    fixed: list[dict[str, Any]] = field(default_factory=list)
    notes: tuple[str, ...] = ()
    verification_requests: tuple[PendingVerification, ...] = ()

    def gate(self, gate_id: str) -> GateResult:
        for result in self.gates:
            if result.id == gate_id:
                return result
        raise KeyError(f"报告里没有这一关: {gate_id}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "producer": PRODUCER,
            "created_at": self.created_at,
            "passed": self.passed,
            "module": self.module,
            "kind": self.kind.value,
            "notes": list(self.notes),
            "verification_requests": [
                request.model_dump(mode="json") for request in self.verification_requests
            ],
            "gates": [gate.as_dict() for gate in self.gates],
            "regressions": list(self.regressions),
            "fixed": list(self.fixed),
            "failures": self._flat_failures(),
        }

    def _flat_failures(self) -> list[dict[str, Any]]:
        flat: list[dict[str, Any]] = []
        for result in self.gates:
            if result.passed:
                continue
            if result.failures:
                flat += [{"gate": result.id, **failure} for failure in result.failures]
            else:
                flat.append(
                    {
                        "gate": result.id,
                        "message": result.detail or f"{result.id} 未通过({result.outcome.value})",
                        "evidence": {"log_tail": result.log_tail},
                    }
                )
        return flat

    def write(self, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / GATE_REPORT
        target.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target


__all__ = [
    "GATE_LOGS_DIR",
    "GATE_REPORT",
    "LOG_TAIL_LINES",
    "PRODUCER",
    "GateOutcome",
    "GateReceipt",
    "GateReport",
    "GateReportKind",
    "GateResult",
    "load_gate_receipt",
]
