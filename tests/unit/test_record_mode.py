"""录制模式:真实运行落盘成一份可直接回放的录制。"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentgenome.agents.recording import RecordingLibrary, changed_files
from agentgenome.agents.replay import ReplayRuntime
from agentgenome.agents.runtime import JobSpec
from agentgenome.agents.subprocess_runtime import RECORD_MODE_ENV, RECORDINGS_ENV, SubprocessRuntime
from tests.fixtures import fake_agent
from tests.fixtures.fake_agent import SCRIPT_ENV

GOOD_RESULT = {
    "task_id": "ag-1",
    "producer": "dev",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
}
SCHEMA = {"type": "object", "required": ["task_id", "passed"]}


def _spec(tmp_path: Path, script: dict[str, Any], **overrides: Any) -> JobSpec:
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ctx.md").write_text("# 上下文\n")
    spec = JobSpec(
        task_id="ag-1",
        employee_id="dev",
        procedure_id="code-develop",
        procedure_version="1.0.0",
        round=1,
        workdir=tmp_path / "work",
        context_file=tmp_path / "ctx.md",
        output_dir=tmp_path / "out",
        output_schema=SCHEMA,
        credentials={
            SCRIPT_ENV: json.dumps(
                {
                    **script,
                    "workdir": str(tmp_path / "work"),
                    "output_dir": str(tmp_path / "out"),
                }
            )
        },
    )
    return replace(spec, **overrides) if overrides else spec


@pytest.fixture
def runtime() -> SubprocessRuntime:
    return SubprocessRuntime(argv=[sys.executable, fake_agent.__file__])


def _enable(monkeypatch: pytest.MonkeyPatch, library: Path) -> None:
    monkeypatch.setenv(RECORD_MODE_ENV, "1")
    monkeypatch.setenv(RECORDINGS_ENV, str(library))


# --- 开关 -------------------------------------------------------------------


async def test_nothing_is_recorded_when_the_switch_is_off(
    tmp_path: Path, runtime: SubprocessRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTGENOME_RECORD", raising=False)
    monkeypatch.setenv(RECORDINGS_ENV, str(tmp_path / "lib"))

    await runtime.run_job(_spec(tmp_path, {"result": GOOD_RESULT}))

    assert not (tmp_path / "lib").exists()


async def test_a_successful_run_is_recorded_when_enabled(
    tmp_path: Path, runtime: SubprocessRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch, tmp_path / "lib")

    await runtime.run_job(_spec(tmp_path, {"result": GOOD_RESULT, "files": {"new.py": "x\n"}}))

    recording = tmp_path / "lib" / "dev__code-develop__r1"
    assert (recording / "result.json").is_file()
    assert (recording / "files" / "new.py").read_text() == "x\n"


async def test_a_failed_run_is_not_recorded(
    tmp_path: Path, runtime: SubprocessRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败的运行落成录制后,回放会重现那次失败——但重现的原因是录制里声明的
    而不是真实发生的。两者混在一起,录制库很快就没法信任了。
    """
    _enable(monkeypatch, tmp_path / "lib")

    await runtime.run_job(_spec(tmp_path, {"result": None}))

    assert not (tmp_path / "lib" / "dev__code-develop__r1").exists()


async def test_recording_without_a_configured_library_is_a_no_op(
    tmp_path: Path, runtime: SubprocessRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RECORD_MODE_ENV, "1")
    monkeypatch.delenv(RECORDINGS_ENV, raising=False)

    result = await runtime.run_job(_spec(tmp_path, {"result": GOOD_RESULT}))

    assert result.ok is True, "没配录制库不该让正常运行失败"


async def test_the_result_points_at_the_recording_for_human_review(
    tmp_path: Path, runtime: SubprocessRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实输出里常带环境相关的绝对路径,产出必须提示人工过一遍。"""
    _enable(monkeypatch, tmp_path / "lib")

    result = await runtime.run_job(_spec(tmp_path, {"result": GOOD_RESULT}))

    assert result.recorded_to == tmp_path / "lib" / "dev__code-develop__r1"


# --- 只收改动,不是整个工作区 ------------------------------------------------


def test_changed_files_only_reports_what_this_run_touched(tmp_path: Path) -> None:
    """在一个挂了业务子模块的 Workspace 上,拷整个工作区会把整个仓库塞进录制。"""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "pre-existing.py").write_text("本来就有的\n")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
        cwd=work,
        check=True,
        capture_output=True,
    )

    (work / "written-by-the-agent.py").write_text("这次写的\n")

    assert changed_files(work) == {"written-by-the-agent.py"}


def test_changed_files_falls_back_to_everything_outside_a_repo(tmp_path: Path) -> None:
    """不是 git 仓时没有基线可比。宁可多收也不要漏——录制不完整比录制冗余更糟。"""
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("x\n")

    assert changed_files(work) == {"a.py"}


async def test_a_recording_excludes_files_the_run_did_not_touch(
    tmp_path: Path, runtime: SubprocessRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "untouched.py").write_text("本来就有的\n")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
        cwd=work,
        check=True,
        capture_output=True,
    )
    _enable(monkeypatch, tmp_path / "lib")

    await runtime.run_job(_spec(tmp_path, {"result": GOOD_RESULT, "files": {"new.py": "x\n"}}))

    files = tmp_path / "lib" / "dev__code-develop__r1" / "files"
    assert (files / "new.py").is_file()
    assert not (files / "untouched.py").exists()


# --- 录制—回放闭环 -----------------------------------------------------------


async def test_a_recorded_run_replays_into_a_clean_workdir(
    tmp_path: Path, runtime: SubprocessRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """录下来的东西必须能被原样读回去,否则录制模式只是在写垃圾。"""
    _enable(monkeypatch, tmp_path / "lib")
    await runtime.run_job(
        _spec(tmp_path, {"result": GOOD_RESULT, "files": {"made.py": "真实跑出来的\n"}})
    )

    replayed = tmp_path / "replayed"
    (replayed / "work").mkdir(parents=True)
    (replayed / "ctx.md").write_text("# 上下文\n")
    result = await ReplayRuntime(RecordingLibrary(tmp_path / "lib")).run_job(
        _spec(tmp_path, {}, workdir=replayed / "work", output_dir=replayed / "out")
    )

    assert result.ok is True
    assert (replayed / "work" / "made.py").read_text() == "真实跑出来的\n"
