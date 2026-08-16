"""`agctl job run`:让 Job 执行这一层能被一条命令独立驱动。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from agentgenome.cli import app
from tests.fixtures import fake_agent
from tests.fixtures.fake_agent import SCRIPT_ENV

runner = CliRunner()

GOOD_RESULT = {
    "task_id": "ag-1",
    "producer": "dev",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
}
SCHEMA = {"type": "object", "required": ["task_id", "passed"]}


def _spec_file(tmp_path: Path, script: dict[str, Any]) -> Path:
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "ctx.md").write_text("# 上下文\n")
    spec = {
        "argv": [sys.executable, fake_agent.__file__],
        "task_id": "ag-1",
        "employee_id": "dev",
        "procedure_id": "code-develop",
        "procedure_version": "1.0.0",
        "round": 1,
        "workdir": str(tmp_path / "work"),
        "context_file": str(tmp_path / "ctx.md"),
        "output_dir": str(tmp_path / "out"),
        "output_schema": SCHEMA,
        "credentials": {
            SCRIPT_ENV: json.dumps(
                {**script, "workdir": str(tmp_path / "work"), "output_dir": str(tmp_path / "out")}
            )
        },
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return path


def test_run_reports_success(tmp_path: Path) -> None:
    result = runner.invoke(app, ["job", "run", str(_spec_file(tmp_path, {"result": GOOD_RESULT}))])

    assert result.exit_code == 0, result.output
    assert "成功" in result.output


def test_run_exits_nonzero_on_contract_failure(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["job", "run", str(_spec_file(tmp_path, {"result": None})), "--json"]
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["failure_kind"] == "contract"
    assert payload["attempts"] == 1


def test_dry_run_does_not_spawn(tmp_path: Path) -> None:
    spec = _spec_file(tmp_path, {"result": GOOD_RESULT, "files": {"nope.txt": "x"}})

    result = runner.invoke(app, ["job", "run", str(spec), "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["dry_run"] is True
    assert not (tmp_path / "work" / "nope.txt").exists()


def test_record_mode_tells_the_operator_to_review_the_recording(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """悄悄写完就算了的话,环境相关的绝对路径会跟着录制进库,然后在别人机器上回放失败。"""
    monkeypatch.setenv("AGENTGENOME_RECORD", "1")
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))

    result = runner.invoke(app, ["job", "run", str(_spec_file(tmp_path, {"result": GOOD_RESULT}))])

    assert result.exit_code == 0, result.output
    assert "已录制到" in result.output
    assert "人工过一遍" in result.output
