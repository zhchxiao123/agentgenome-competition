"""MatrixMinioTransport:按平台真实约定的完整往返,经假 mc + stub HTTP 驱动。

"Worker"由测试里的一个后台协程扮演:看到任务目录出现就写终态与产物——
与真实平台的时序同构(推送、通知、异步完成、拉回),没有任何内部 mock。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentgenome.agents.agentteams.directory import FixedDirectory
from agentgenome.agents.agentteams.matrix_minio import MatrixMinioTransport
from agentgenome.agents.agentteams.mirror import MinioMirror
from agentgenome.agents.agentteams.taskdoc import task_ref
from agentgenome.agents.agentteams.transport import PlatformUnavailable, TransportJob
from tests.fixtures import fake_mc
from tests.fixtures.stub_http import StubServer

PREFIX = "myminio/agentteams"


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "remote"
    root.mkdir()
    monkeypatch.setenv("FAKE_MC_ROOT", str(root))
    monkeypatch.setenv("FAKE_MC_PREFIX", PREFIX)
    monkeypatch.delenv("FAKE_MC_FAIL", raising=False)
    return root


def _job(**overrides: Any) -> TransportJob:
    fields: dict[str, Any] = {
        "task_id": "ag-1",
        "employee_id": "dev-employee",
        "procedure_ref": "code-develop@1.0.0",
        "round": 1,
        "attempt": 1,
        "subject": "",
        "context_text": "# 上下文包\n请完成任务。\n",
        "craft": "## 交作业合同\n",
        "workspace": {"src/app.py": "print(1)\n"},
    }
    fields.update(overrides)
    return TransportJob(**fields)


def _transport(stub: StubServer) -> MatrixMinioTransport:
    return MatrixMinioTransport(
        controller_endpoint=stub.url,
        controller_token="sk-consumer-1",
        matrix_homeserver=stub.url,
        matrix_token="syt-matrix-1",
        directory=FixedDirectory(worker="alice", room_id="!room:example.com"),
        mirror=MinioMirror(mc_argv=[sys.executable, fake_mc.__file__], prefix=PREFIX),
        poll_interval_s=0.02,
    )


async def _play_worker(
    remote: Path,
    ref: str,
    status: str,
    result_md: str,
    edit: dict[str, str] | None = None,
    artifacts: dict[str, str] | None = None,
) -> None:
    """扮演 Worker:任务目录出现后改工作区、写产物、置终态。"""
    task_dir = remote / "tasks" / ref
    for _ in range(500):
        if (task_dir / "meta.json").is_file():
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("任务目录一直没出现——推送没发生")
    for relative, content in (edit or {}).items():
        target = task_dir / "workspace" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    artifact_root = task_dir / "artifacts"
    artifact_root.mkdir(exist_ok=True)
    returned = {"result.json": '{"passed": true}', **(artifacts or {})}
    for relative, content in returned.items():
        target = artifact_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (task_dir / "result.md").write_text(result_md, encoding="utf-8")
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    meta["status"] = status
    (task_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


async def test_a_full_round_trip_lands_changes_and_artifacts(remote: Path) -> None:
    with StubServer() as stub:
        transport = _transport(stub)
        job = _job()
        worker = asyncio.create_task(
            _play_worker(
                remote,
                "ag-1-dev-employee-code-develop-r1",
                "SUCCESS",
                "干完了。\n",
                edit={"src/app.py": "print(2)\n", "src/new.py": "print(3)\n"},
            )
        )

        outcome = await asyncio.wait_for(transport.run_job(job), 10)
        await worker

    assert outcome.ok is True
    assert outcome.changed_files == {"src/app.py": "print(2)\n", "src/new.py": "print(3)\n"}
    assert outcome.artifacts == {"result.json": '{"passed": true}'}
    assert outcome.tokens_used is None, "平台无逐任务计量,恒为不可得"
    assert any("干完了" in event.get("text", "") for event in outcome.events)


async def test_legacy_root_staging_is_collected_as_an_output_artifact(remote: Path) -> None:
    """旧 Worker 把“产物目录下 staging/”解释成任务根 staging/。

    回收器必须把这棵树迁回统一的 output_dir 命名空间；否则 Worker 明明交了
    知识文件，本地却只收到 result.json，蒸馏还会把缺失误判成合法的零产出。
    """
    ref = "ag-1-dev-employee-code-develop-r1"

    async def worker() -> None:
        task_dir = remote / "tasks" / ref
        for _ in range(500):
            if (task_dir / "meta.json").is_file():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("任务目录一直没出现——推送没发生")
        (task_dir / "artifacts").mkdir()
        (task_dir / "artifacts" / "result.json").write_text("{}", encoding="utf-8")
        lesson = task_dir / "staging" / "lessons" / "do-not-drop-me.md"
        lesson.parent.mkdir(parents=True)
        lesson.write_text("# 不能丢\n", encoding="utf-8")
        (task_dir / "meta.json").write_text(json.dumps({"status": "SUCCESS"}), encoding="utf-8")

    with StubServer() as stub:
        running = asyncio.create_task(worker())
        outcome = await asyncio.wait_for(_transport(stub).run_job(_job()), 10)
        await running

    assert outcome.artifacts == {
        "result.json": "{}",
        "staging/lessons/do-not-drop-me.md": "# 不能丢\n",
    }


async def test_canonical_artifacts_win_over_a_stale_legacy_staging_copy(remote: Path) -> None:
    """新旧形态同时存在时只信 artifacts/，避免旧副本覆盖本轮交付。"""
    ref = "ag-1-dev-employee-code-develop-r1"
    task_dir = remote / "tasks" / ref
    canonical = task_dir / "artifacts" / "staging" / "lessons" / "same.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("new\n", encoding="utf-8")
    legacy = task_dir / "staging" / "lessons" / "same.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old\n", encoding="utf-8")
    (task_dir / "artifacts" / "result.json").write_text("{}", encoding="utf-8")
    (task_dir / "meta.json").write_text(json.dumps({"status": "SUCCESS"}), encoding="utf-8")

    with StubServer() as stub:
        outcome = await asyncio.wait_for(_transport(stub).run_job(_job()), 10)

    assert outcome.artifacts["staging/lessons/same.md"] == "new\n"


async def test_a_subject_prefixed_staging_tree_is_recovered_to_the_artifact_root(
    remote: Path,
) -> None:
    """真机故障:Worker 把本地产物槽名重复套在远端 artifacts/ 下。

    本地 output_dir 已经由 Job 隔离，回收结果必须以它为根；否则契约校验只会看到
    ``sql-db-0816/staging``，误报 canonical ``staging/`` 不存在。
    """
    subject = "sql-db-0816"
    job = _job(subject=subject)
    with StubServer() as stub:
        transport = _transport(stub)
        worker = asyncio.create_task(
            _play_worker(
                remote,
                task_ref(job),
                "SUCCESS",
                "完成知识初始化。\n",
                artifacts={
                    f"{subject}/staging/project-map.yaml": "modules: []\n",
                    f"{subject}/staging/interfaces.yaml": "interfaces: []\ndatastores: []\n",
                },
            )
        )

        outcome = await asyncio.wait_for(transport.run_job(job), 10)
        await worker

    assert outcome.artifacts == {
        "result.json": '{"passed": true}',
        "staging/project-map.yaml": "modules: []\n",
        "staging/interfaces.yaml": "interfaces: []\ndatastores: []\n",
    }
    assert any(
        event.get("kind") == "note" and f"{subject}/staging" in event.get("text", "")
        for event in outcome.events
    ), "自动纠正不能静默发生，审计日志必须能解释 Worker 实际交回了什么"


async def test_the_worker_is_notified_via_its_matrix_room(remote: Path) -> None:
    with StubServer() as stub:
        transport = _transport(stub)
        worker = asyncio.create_task(
            _play_worker(remote, "ag-1-dev-employee-code-develop-r1", "SUCCESS", "")
        )
        await asyncio.wait_for(transport.run_job(_job()), 10)
        await worker

    sends = [r for r in stub.records if "/send/m.room.message/" in r["path"]]
    assert len(sends) == 1
    send = sends[0]
    from urllib.parse import unquote

    assert "!room:example.com" in unquote(send["path"])
    body = json.loads(send["body"])
    assert "alice" in body["body"], "要 @ 到 Worker"
    assert "ag-1-dev-employee-code-develop-r1" in body["body"], (
        "任务引用要在消息里,Worker 靠它找目录"
    )
    assert send["headers"].get("Authorization", "").startswith("Bearer ")
    assert "syt-matrix-1" not in send["path"], "令牌不进 URL——URL 会进访问日志"


async def test_a_terminal_remote_task_is_resumed_without_repush_or_renotify(
    remote: Path,
) -> None:
    """真机发现:进程崩溃重启后重派同一作业,重推会抹掉 Worker 已完成的活,
    重知会又会被 Matrix txn 去重吞掉。终态现场直接回收,一个字节不动。"""
    ref = "ag-1-dev-employee-code-develop-r1"
    task_dir = remote / "tasks" / ref
    (task_dir / "workspace").mkdir(parents=True)
    (task_dir / "workspace" / "src").mkdir()
    (task_dir / "workspace" / "src" / "app.py").write_text("print(99)\n", encoding="utf-8")
    (task_dir / "artifacts").mkdir()
    (task_dir / "artifacts" / "result.json").write_text('{"passed": true}', encoding="utf-8")
    (task_dir / "meta.json").write_text(json.dumps({"status": "SUCCESS"}), encoding="utf-8")

    with StubServer() as stub:
        outcome = await asyncio.wait_for(_transport(stub).run_job(_job()), 10)
        sends = [r for r in stub.records if "/send/" in r["path"]]

    assert outcome.ok is True
    assert outcome.changed_files == {"src/app.py": "print(99)\n"}
    assert sends == [], "终态续接不该再知会 Worker"
    assert (task_dir / "workspace" / "src" / "app.py").read_text() == "print(99)\n", (
        "已完成的现场不该被重推抹掉"
    )


async def test_an_in_flight_remote_task_is_renotified_but_not_wiped(remote: Path) -> None:
    """崩溃窗口也可能落在推送与知会之间——现场在但 Worker 没被叫醒。
    非终态续接要补一次知会(txn 每次唯一,不吃去重),但不重推目录。"""
    ref = "ag-1-dev-employee-code-develop-r1"
    task_dir = remote / "tasks" / ref
    (task_dir / "workspace").mkdir(parents=True)
    (task_dir / "workspace" / "Worker进行中的改动.py").write_text("wip\n", encoding="utf-8")
    (task_dir / "meta.json").write_text(json.dumps({"status": "PENDING"}), encoding="utf-8")

    async def complete_after_notify(stub: StubServer) -> None:
        """收到补发的知会才完工——时序与被测行为对齐,不抢跑。"""
        for _ in range(500):
            if any("/send/m.room.message/" in r["path"] for r in stub.records):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("补发的知会一直没来")
        (task_dir / "artifacts").mkdir(exist_ok=True)
        (task_dir / "artifacts" / "result.json").write_text('{"passed": true}', encoding="utf-8")
        (task_dir / "meta.json").write_text(json.dumps({"status": "SUCCESS"}), encoding="utf-8")

    with StubServer() as stub:
        transport = _transport(stub)
        worker = asyncio.create_task(complete_after_notify(stub))
        await asyncio.wait_for(transport.run_job(_job()), 10)
        await worker
        sends = [r for r in stub.records if "/send/m.room.message/" in r["path"]]

    assert len(sends) == 1, "非终态续接要补一次知会"
    assert (task_dir / "workspace" / "Worker进行中的改动.py").exists(), "进行中的现场不该被重推抹掉"


async def test_an_upstream_failure_state_keeps_its_reason(remote: Path) -> None:
    with StubServer() as stub:
        transport = _transport(stub)
        worker = asyncio.create_task(
            _play_worker(remote, "ag-1-dev-employee-code-develop-r1", "BLOCKED", "缺数据库凭证。\n")
        )

        outcome = await asyncio.wait_for(transport.run_job(_job()), 10)
        await worker

    assert outcome.ok is False
    assert "BLOCKED" in (outcome.detail or "")
    assert "缺数据库凭证" in (outcome.detail or "")


async def test_transport_recording_and_replay_cover_matrix_minio(
    remote: Path, tmp_path: Path
) -> None:
    """录制/回放对这个实现同样生效:录一次真往返,回放不需要 mc 也不需要平台。"""
    from agentgenome.agents.agentteams.recording import RecordingTransport, ReplayTransport

    library = tmp_path / "lib"
    with StubServer() as stub:
        recorder = RecordingTransport(_transport(stub), library)
        worker = asyncio.create_task(
            _play_worker(
                remote,
                "ag-1-dev-employee-code-develop-r1",
                "SUCCESS",
                "好了。\n",
                edit={"src/app.py": "print(9)\n"},
            )
        )
        recorded = await asyncio.wait_for(recorder.run_job(_job()), 10)
        await worker

    replayed = await ReplayTransport(library).run_job(_job())

    assert replayed == recorded
    assert replayed.changed_files == {"src/app.py": "print(9)\n"}


def test_preflight_checks_controller_and_matrix(remote: Path) -> None:
    with StubServer() as stub:
        _transport(stub).preflight()

        paths = [r["path"] for r in stub.records]

    assert any("/api/v1/status" in p for p in paths), "要打 controller"
    assert any("whoami" in p for p in paths), "要验 Matrix 令牌"


def test_preflight_surfaces_a_rejected_token(remote: Path) -> None:
    with StubServer(status=401) as stub, pytest.raises(PlatformUnavailable, match="401"):
        _transport(stub).preflight()
