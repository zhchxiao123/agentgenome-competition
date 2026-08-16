"""`agctl procedure run`:三种 kind 各走一条,走 CLI 入口 + 回放运行时。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from tests.fixtures.procedures import GOOD_SCRIPT, INTERMEDIATE_SCRIPT, RESULT, write_procedure

runner = CliRunner()


def _procedure(
    root: Path,
    procedure_id: str,
    kind: str,
    *,
    script: str | None = None,
    prompt: str | None = None,
    trigger: str | None = None,
) -> None:
    write_procedure(
        root,
        procedure_id,
        kind,
        script=script,
        prompt=prompt,
        trigger=[trigger] if trigger else None,
    )


def _recording(library: Path, procedure_id: str) -> None:
    directory = library / f"operator__{procedure_id}__r1"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps(RESULT), encoding="utf-8")


#: 派发一律以某个员工的身份发生,所以夹具里得有一个真的员工。
OPERATOR = """\
id: operator
runtime: claude-code
prompt: prompts/operator.md
procedures: [unit-gate, code-develop, itest-run]
tools:
  allow: [Bash, Read]
permissions:
  write_paths: ["**"]
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
    (tmp_path / "global").mkdir()
    (tmp_path / "lib").mkdir()
    root = tmp_path / "ws"
    (root / "genome" / "procedures").mkdir(parents=True)
    (root / "agentgenome.yaml").write_text("platform: {git_host: local}\n")
    (root / "employees" / "prompts").mkdir(parents=True)
    (root / "employees" / "prompts" / "operator.md").write_text("你是操作员。\n", encoding="utf-8")
    (root / "employees" / "operator.yaml").write_text(OPERATOR, encoding="utf-8")
    # 派发出去的 Job 带着授权范围,而越权检查看的是真实 git 工作树。
    for args in (("init", "--initial-branch=main"), ("commit", "--allow-empty", "-m", "base")):
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@t.test", *args],
            cwd=root,
            check=True,
            capture_output=True,
        )
    return root


def _run(workspace: Path, procedure_id: str, *extra: str):
    return runner.invoke(
        app,
        [
            "procedure",
            "run",
            procedure_id,
            "--workspace",
            str(workspace),
            "--runtime",
            "replay",
            "--json",
            *extra,
        ],
    )


def test_deterministic_procedure_runs_without_any_recording(workspace: Path) -> None:
    """确定性 Procedure 不碰运行时,所以录制库是空的也照样跑。"""
    _procedure(
        workspace / "genome" / "procedures", "unit-gate", "deterministic", script=GOOD_SCRIPT
    )

    result = _run(workspace, "unit-gate")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["stages"] == ["script"]
    assert payload["procedure_ref"] == "unit-gate@1.0.0"


def test_agentic_procedure_goes_through_the_runtime(workspace: Path, tmp_path: Path) -> None:
    _procedure(workspace / "genome" / "procedures", "code-develop", "agentic", prompt="去写代码\n")
    _recording(tmp_path / "lib", "code-develop")

    result = _run(workspace, "code-develop")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["stages"] == ["agent"]


def test_hybrid_procedure_runs_both_stages(workspace: Path, tmp_path: Path) -> None:
    _procedure(
        workspace / "genome" / "procedures",
        "itest-run",
        "hybrid",
        script=INTERMEDIATE_SCRIPT,
        prompt="诊断一下\n",
    )
    _recording(tmp_path / "lib", "itest-run")

    result = _run(workspace, "itest-run")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stages"] == ["script", "agent"]


def test_a_refused_dispatch_exits_nonzero_without_a_traceback(workspace: Path) -> None:
    _procedure(
        workspace / "genome" / "procedures",
        "unit-gate",
        "deterministic",
        script=GOOD_SCRIPT,
        trigger="UNIT_TESTING",
    )

    result = _run(workspace, "unit-gate", "--state", "DEVELOPING")

    assert result.exit_code != 0
    assert "DEVELOPING" in result.output
    assert "Traceback" not in result.output


def test_an_unknown_procedure_lists_what_is_registered(workspace: Path) -> None:
    _procedure(
        workspace / "genome" / "procedures", "unit-gate", "deterministic", script=GOOD_SCRIPT
    )

    result = _run(workspace, "ghost")

    assert result.exit_code != 0
    assert "unit-gate" in result.output


def test_an_unknown_state_is_rejected_with_the_allowed_values(workspace: Path) -> None:
    _procedure(
        workspace / "genome" / "procedures", "unit-gate", "deterministic", script=GOOD_SCRIPT
    )

    result = _run(workspace, "unit-gate", "--state", "NOT_A_STATE")

    assert result.exit_code != 0
    assert "UNIT_TESTING" in result.output


def test_a_procedure_off_the_employee_whitelist_is_refused(workspace: Path) -> None:
    """员工能力边界:不在白名单里的 Procedure 连拉起都不该发生。"""
    _procedure(
        workspace / "genome" / "procedures", "secrets-scan", "deterministic", script=GOOD_SCRIPT
    )

    result = _run(workspace, "secrets-scan")

    assert result.exit_code != 0
    assert "operator" in result.output
    assert "secrets-scan" in result.output
    assert "unit-gate" in result.output, "拒绝信息要说明白名单里有什么"
    assert "Traceback" not in result.output
    assert not (workspace / "tasks").exists(), "被拒的派发不该留下任何痕迹"


def test_an_unknown_employee_lists_what_is_defined(workspace: Path) -> None:
    _procedure(
        workspace / "genome" / "procedures", "unit-gate", "deterministic", script=GOOD_SCRIPT
    )

    result = _run(workspace, "unit-gate", "--employee", "ghost")

    assert result.exit_code != 0
    assert "operator" in result.output
