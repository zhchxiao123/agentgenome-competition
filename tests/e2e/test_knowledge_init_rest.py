"""知识初始化的 REST 入口(PRD 44)。

一条管道,两个起点:`POST /genome/tasks/init` 与 `agctl knowledge plan` 走同一层
(扫描 → 建任务 → 草案落盘 → 停在闸门)。这里断言的是"两条路建出来的东西同形",
以及重复发起在提交那一刻被拒。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from agentgenome.core.genome_gate import read_draft
from agentgenome.core.genome_task import GenomeTaskKind, GenomeTaskState, GenomeTaskStore, Origin
from agentgenome.server.app import create_app
from tests.fixtures.mall import materialize_mall

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        cli_app,
        [
            "init", "--local-only",
            str(root),
            "--name",
            "mall",
            "--repo",
            mall["order-service"].remote_url,
            "--repo",
            mall["inventory-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    return root


@pytest.fixture
def client(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace))


class TestKnowledgeInitOverRest:
    def test_creates_the_same_task_as_the_cli_plan(
        self, client: TestClient, workspace: Path
    ) -> None:
        """REST 建出的任务与 CLI `knowledge plan` 同形:kind、origin、停在闸门、草案在。"""
        response = client.post("/genome/tasks/init")
        assert response.status_code == 201, response.text
        payload = response.json()

        record = GenomeTaskStore(workspace).get(payload["id"])
        assert record.kind is GenomeTaskKind.INIT
        assert record.origin is Origin.HUMAN
        assert record.state is GenomeTaskState.AWAITING_CONFIRMATION
        # 草案已落盘,闸门端点当场可看可答。
        assert read_draft(workspace, record.id).get("modules")
        gate = client.get(f"/genome/tasks/{record.id}/gate")
        assert gate.status_code == 200
        assert gate.json()["modules"]

    def test_shows_up_on_the_genome_board(self, client: TestClient) -> None:
        created = client.post("/genome/tasks/init").json()
        listed = client.get("/genome/tasks").json()["items"]
        assert any(item["id"] == created["id"] for item in listed)
        progress = client.get(f"/genome/tasks/{created['id']}/progress")
        assert progress.status_code == 200

    def test_second_init_is_refused_while_one_is_open(
        self, client: TestClient, workspace: Path
    ) -> None:
        """重复发起在提交那一刻被拒,报错带着那个在跑的任务 id——重试同样的请求不会成功。"""
        first = client.post("/genome/tasks/init").json()
        second = client.post("/genome/tasks/init")
        assert second.status_code == 409
        assert first["id"] in second.json()["detail"]

    def test_cli_plan_refuses_identically(self, client: TestClient, workspace: Path) -> None:
        """CLI 与 REST 共用同一个查重:两条路对"已有一个在跑"给同一句报错。"""
        first = client.post("/genome/tasks/init").json()
        result = runner.invoke(cli_app, ["knowledge", "plan", "-w", str(workspace)])
        assert result.exit_code != 0
        assert first["id"] in result.output

    def test_events_and_logs_render_for_a_genome_task(self, client: TestClient) -> None:
        """事件日志标签打得开:两个读端点服务的是主体 id,不是某一类任务。

        此前存在性检查只问研发任务那张表,`gn-` id 一律 404——日志明明在盘上
        (`tasks/<id>/logs/events.jsonl`),界面却说"没有这个任务"。
        """
        created = client.post("/genome/tasks/init").json()
        task_id = created["id"]

        events = client.get(f"/tasks/{task_id}/events")
        assert events.status_code == 200, events.text
        # 建任务时至少落了一条迁移事件(SCANNING → AWAITING_CONFIRMATION)。
        kinds = [item["kind"] for item in events.json()["items"]]
        assert "transition" in kinds

        logs = client.get(f"/tasks/{task_id}/logs")
        assert logs.status_code == 200, logs.text
        assert logs.json()["total"] >= 1

        # 不存在的 id 仍然是 404——存在性检查区分的是"任务不存在"与"还没有日志"。
        assert client.get("/tasks/gn-20990101-001/logs").status_code == 404

    def test_a_human_failure_can_be_removed_from_the_attention_queue(
        self, client: TestClient, workspace: Path
    ) -> None:
        store = GenomeTaskStore(workspace)
        task = store.create(title="知识初始化", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
        store.save(task.evolve(state=GenomeTaskState.FAILED, failure_reason="深读超时"))

        response = client.post(
            f"/genome/tasks/{task.id}/intervention/resolve", json={"note": "暂不重试"}
        )

        assert response.status_code == 200, response.text
        assert response.json()["state"] == "FAILED"
        listed = client.get("/genome/tasks").json()["items"]
        assert task.id not in [item["id"] for item in listed]

    def test_cli_task_passes_the_rest_duplicate_check(
        self, client: TestClient, workspace: Path
    ) -> None:
        """反方向同理:CLI 先 plan,REST 再发起 → 409 指向 CLI 建的那个任务。"""
        planned = runner.invoke(
            cli_app, ["knowledge", "plan", "-w", str(workspace), "--json"]
        )
        assert planned.exit_code == 0, planned.output
        task_id = json.loads(planned.output)["task_id"]
        response = client.post("/genome/tasks/init")
        assert response.status_code == 409
        assert task_id in response.json()["detail"]
