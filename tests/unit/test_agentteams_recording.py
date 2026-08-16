"""传输层的录制与回放:录一次真实交互,CI 里回放驱动适配器,不起真实平台。

与既有录制机制同一原则:只录成功的运行;素材位置由环境变量给;敏感信息
结构性地进不了素材。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentgenome.agents.agentteams import (
    AgentTeamsRuntime,
    PlatformUnavailable,
    TransportJob,
    TransportOutcome,
)
from agentgenome.agents.agentteams.recording import RecordingTransport, ReplayTransport
from tests.fixtures.fake_agentteams import FakeTransport


def _job(**overrides: Any) -> TransportJob:
    fields: dict[str, Any] = {
        "task_id": "ag-1",
        "employee_id": "dev-employee",
        "procedure_ref": "code-develop@1.0.0",
        "round": 1,
        "attempt": 1,
        "subject": "",
        "context_text": "# 上下文包\n",
        "workspace": {"src/app.py": "print(1)\n"},
    }
    fields.update(overrides)
    return TransportJob(**fields)


def _outcome() -> TransportOutcome:
    return TransportOutcome(
        ok=True,
        artifacts={"result.json": json.dumps({"task_id": "ag-1"})},
        changed_files={"src/app.py": "print(2)\n"},
        events=[{"kind": "text", "text": "干完了"}],
    )


async def test_a_recorded_exchange_replays_to_the_same_outcome(tmp_path: Path) -> None:
    """落盘素材可直接被回放传输消费,复现同一次执行——这就是素材的存在意义。"""
    library = tmp_path / "lib"
    recorder = RecordingTransport(FakeTransport([_outcome()]), library)

    recorded = await recorder.run_job(_job())
    replayed = await ReplayTransport(library).run_job(_job())

    assert replayed == recorded


async def test_only_successful_outcomes_are_recorded(tmp_path: Path) -> None:
    """失败的落成录制之后,回放重现的原因是素材声明的而不是真实发生的——录制库很快没法信任。"""
    library = tmp_path / "lib"
    recorder = RecordingTransport(
        FakeTransport([TransportOutcome(ok=False, detail="Worker 崩了")]), library
    )

    await recorder.run_job(_job())

    assert not list(library.rglob("outcome.json"))


async def test_replay_without_a_matching_recording_names_the_missing_key(
    tmp_path: Path,
) -> None:
    library = tmp_path / "lib"
    library.mkdir()

    with pytest.raises(PlatformUnavailable, match="dev-employee__code-develop__r1"):
        await ReplayTransport(library).run_job(_job())


async def test_the_consumer_token_cannot_land_in_the_recording(tmp_path: Path) -> None:
    """脱敏不靠记得擦——TransportJob 里结构性地没有凭证与 token 字段。"""
    library = tmp_path / "lib"
    recorder = RecordingTransport(FakeTransport([_outcome()]), library)

    await recorder.run_job(_job())

    payloads = [json.loads(p.read_text()) for p in library.rglob("job.json")]
    assert payloads, "成功的交互要落盘"
    for payload in payloads:
        assert "token" not in payload
        assert "credentials" not in payload


async def test_attempts_and_subjects_get_distinct_recordings(tmp_path: Path) -> None:
    """同一 Job 的第二次尝试与不同模块的并行作业不能共用一份素材——回放会全撞在一起。"""
    library = tmp_path / "lib"
    first = TransportOutcome(ok=True, artifacts={"result.json": '{"n": 1}'})
    second = TransportOutcome(ok=True, artifacts={"result.json": '{"n": 2}'})
    recorder = RecordingTransport(FakeTransport([first, second]), library)
    await recorder.run_job(_job(attempt=1))
    await recorder.run_job(_job(attempt=2))

    replay = ReplayTransport(library)

    assert (await replay.run_job(_job(attempt=1))).artifacts["result.json"] == '{"n": 1}'
    assert (await replay.run_job(_job(attempt=2))).artifacts["result.json"] == '{"n": 2}'


def test_preflight_requires_the_library_to_exist(tmp_path: Path) -> None:
    with pytest.raises(PlatformUnavailable, match="录制库"):
        ReplayTransport(tmp_path / "不存在").preflight()


async def test_the_adapter_runs_end_to_end_on_a_replayed_transport(tmp_path: Path) -> None:
    """适配器全链路(翻译、同步、契约)跑在回放传输上——CI 的标准姿势。"""
    from agentgenome.agents.runtime import JobSpec

    library = tmp_path / "lib"
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "src").mkdir()
    (workdir / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
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
    recorder = RecordingTransport(FakeTransport([_outcome()]), library)
    first = await AgentTeamsRuntime(recorder).run_job(spec)
    (workdir / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")

    replayed = await AgentTeamsRuntime(ReplayTransport(library)).run_job(spec)

    assert first.ok is True
    assert replayed.ok is True
    assert (workdir / "src" / "app.py").read_text() == "print(2)\n"
