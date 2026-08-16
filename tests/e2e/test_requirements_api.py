"""需求实体的 REST 面(PRD 43)。

从今天起每个新任务都属于一个需求;使用者什么都不用改。这里断言的是外部可观察物:
提交任务之后需求列表长什么样、事件面记了什么、存量任务不炸。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.requirement import RequirementStore
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskRunStatus, TaskStore
from agentgenome.server.app import create_app
from tests.fixtures.mall import materialize_mall

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        cli_app,
        [
            "init",
            "--local-only",
            str(root),
            "--name",
            "mall",
            "--repo",
            mall["order-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    return root


@pytest.fixture
def client(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace))


def _requirements(client: TestClient) -> list[dict]:
    response = client.get("/requirements")
    assert response.status_code == 200, response.text
    return response.json()


class TestRequirementBornWithTask:
    def test_submit_creates_requirement_with_first_attempt(self, client: TestClient) -> None:
        """`POST /tasks` 形状不变;服务端在里面建需求 + 第一次尝试并挂链。"""
        response = client.post(
            "/tasks", json={"requirement": "支持部分退款", "title": "退款", "priority": 7}
        )
        assert response.status_code == 201, response.text
        task = response.json()
        assert task["requirement_id"].startswith("req-")

        found = _requirements(client)
        assert len(found) == 1
        entry = found[0]
        assert entry["id"] == task["requirement_id"]
        assert entry["title"] == "退款"
        assert entry["text"] == "支持部分退款"
        assert entry["priority"] == 7
        assert entry["state"] == "in_progress"
        assert entry["attempts"] == 1

    def test_task_detail_carries_requirement_id(self, client: TestClient) -> None:
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        detail = client.get(f"/tasks/{task['id']}").json()
        assert detail["requirement_id"] == task["requirement_id"]

    def test_events_on_both_subjects(self, client: TestClient, workspace: Path) -> None:
        """需求创建记在需求 id 名下;任务事件只带指针(PRD 43 D6)。"""
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        log = EventLog(workspace)
        requirement_events = log.events(task["requirement_id"])
        assert [event.kind for event in requirement_events] == [LogKind.REQUIREMENT_CREATED]
        task_created = [
            event for event in log.events(task["id"]) if event.kind is LogKind.TASK_CREATED
        ]
        assert task_created[0].payload["requirement_id"] == task["requirement_id"]

    def test_im_webhook_creates_requirement(self, client: TestClient) -> None:
        response = client.post("/webhooks/im", json={"text": "对账单导出", "user": "王五"})
        assert response.status_code == 200, response.text
        assert response.json()["task_id"]
        found = _requirements(client)
        assert len(found) == 1
        assert found[0]["text"] == "对账单导出"
        assert found[0]["attempts"] == 1

    def test_legacy_task_without_requirement_does_not_break(
        self, client: TestClient, workspace: Path
    ) -> None:
        """存量任务 `requirement_id` 为 NULL:列表与详情照常,不假装它有需求。"""
        legacy = TaskStore(workspace).create(title="旧任务", requirement="旧需求")
        assert _requirements(client) == []
        detail = client.get(f"/tasks/{legacy.id}").json()
        assert detail["requirement_id"] is None
        assert client.get("/tasks").status_code == 200


class TestRetryUnderSameRequirement:
    def test_retry_rejects_a_second_active_attempt(
        self, client: TestClient, workspace: Path
    ) -> None:
        first = client.post("/tasks", json={"requirement": "仍在执行的需求"}).json()

        second = client.post(
            "/tasks",
            json={"requirement": "重复点击", "requirement_id": first["requirement_id"]},
        )

        assert second.status_code == 409
        cli_result = runner.invoke(
            cli_app,
            [
                "task",
                "submit",
                "--requirement",
                "CLI 重复提交",
                "--requirement-id",
                first["requirement_id"],
                "-w",
                str(workspace),
            ],
        )
        assert cli_result.exit_code != 0
        assert "已有进行中的尝试" in cli_result.output

    def test_retry_rejects_a_parked_requirement(self, client: TestClient) -> None:
        first = client.post("/tasks", json={"requirement": "已经搁置的需求"}).json()
        client.post(f"/tasks/{first['id']}/cancel")
        client.patch(f"/requirements/{first['requirement_id']}", json={"park": "暂不处理"})

        second = client.post(
            "/tasks",
            json={"requirement": "不应创建", "requirement_id": first["requirement_id"]},
        )

        assert second.status_code == 409

    def test_failed_retry_creation_does_not_rewrite_requirement_text(
        self, client: TestClient, workspace: Path
    ) -> None:
        first = client.post("/tasks", json={"requirement": "原始需求"}).json()
        TaskStore(workspace).save(
            TaskStore(workspace).get(first["id"]).evolve(state=TaskState.ESCALATED)
        )
        database = RequirementStore(workspace).database
        with sqlite3.connect(database) as connection:
            connection.execute(
                "create trigger reject_retry before insert on tasks "
                "begin select raise(abort, 'simulated task insert failure'); end"
            )

        with pytest.raises(sqlite3.IntegrityError, match="simulated task insert failure"):
            client.post(
                "/tasks",
                json={
                    "requirement": "修改后的需求",
                    "requirement_id": first["requirement_id"],
                },
            )

        assert RequirementStore(workspace).get(first["requirement_id"]).text == "原始需求"

    def test_retry_links_second_attempt(self, client: TestClient, workspace: Path) -> None:
        """带 `requirement_id` 的提交在同一需求下建第二次尝试,链按时间排。"""
        first = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        store = TaskStore(workspace)
        store.save(store.get(first["id"]).evolve(state=TaskState.ESCALATED))

        second = client.post(
            "/tasks",
            json={"requirement": "支持部分退款(按 SKU)", "requirement_id": first["requirement_id"]},
        )
        assert second.status_code == 201, second.text
        assert second.json()["requirement_id"] == first["requirement_id"]

        detail = client.get(f"/requirements/{first['requirement_id']}").json()
        assert [item["id"] for item in detail["chain"]] == [first["id"], second.json()["id"]]
        assert detail["attempts"] == 2
        assert detail["chain"][0]["state"] == "ESCALATED"

    def test_retry_updates_current_text_but_not_snapshots(
        self, client: TestClient, workspace: Path
    ) -> None:
        """再试时改的措辞就是需求的最新表述;第一次尝试的快照原样未动(PRD 43 D5)。"""
        first = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        store = TaskStore(workspace)
        store.save(store.get(first["id"]).evolve(state=TaskState.ESCALATED))
        client.post(
            "/tasks",
            json={"requirement": "支持部分退款(按 SKU)", "requirement_id": first["requirement_id"]},
        )
        detail = client.get(f"/requirements/{first['requirement_id']}").json()
        assert detail["text"] == "支持部分退款(按 SKU)"
        assert store.get(first["id"]).requirement == "支持部分退款"

    def test_unknown_requirement_rejected_identically_on_both_entrances(
        self, client: TestClient, workspace: Path
    ) -> None:
        """两条入口对同一个不存在的需求 id 给同一句报错——一套校验,不是两份。"""
        response = client.post(
            "/tasks", json={"requirement": "x", "requirement_id": "req-20990101-001"}
        )
        assert response.status_code == 422
        message = response.json()["detail"]

        result = runner.invoke(
            cli_app,
            [
                "task",
                "submit",
                "--requirement",
                "x",
                "--requirement-id",
                "req-20990101-001",
                "-w",
                str(workspace),
            ],
        )
        assert result.exit_code != 0
        assert message in result.output

    def test_cli_submit_also_born_a_requirement(self, client: TestClient, workspace: Path) -> None:
        """CLI 提交与 REST 走同一条路:没给 id 也先落成需求(入口不该有一个漏网的)。"""
        result = runner.invoke(
            cli_app, ["task", "submit", "--requirement", "对账单导出", "-w", str(workspace)]
        )
        assert result.exit_code == 0, result.output
        found = _requirements(client)
        assert len(found) == 1
        assert found[0]["attempts"] == 1

    def test_total_tokens_sums_attempts(self, client: TestClient, workspace: Path) -> None:
        first = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        store = TaskStore(workspace)
        store.save(store.get(first["id"]).evolve(state=TaskState.ESCALATED, tokens_used=1200))
        second = client.post(
            "/tasks",
            json={"requirement": "支持部分退款", "requirement_id": first["requirement_id"]},
        ).json()
        store.save(store.get(second["id"]).evolve(tokens_used=800))
        detail = client.get(f"/requirements/{first['requirement_id']}").json()
        assert detail["total_tokens"] == 2000

    def test_detail_of_unknown_requirement_is_404(self, client: TestClient) -> None:
        assert client.get("/requirements/req-20990101-001").status_code == 404


class TestEditAndPark:
    def test_patch_text_updates_entity_not_snapshots(
        self, client: TestClient, workspace: Path
    ) -> None:
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        response = client.patch(
            f"/requirements/{task['requirement_id']}", json={"text": "支持部分退款(按 SKU)"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["text"] == "支持部分退款(按 SKU)"
        assert TaskStore(workspace).get(task["id"]).requirement == "支持部分退款"

    def test_park_and_resume_cycle(self, client: TestClient) -> None:
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        parked = client.patch(f"/requirements/{task['requirement_id']}", json={"park": "不做了"})
        assert parked.status_code == 200
        assert parked.json()["state"] == "parked"
        assert parked.json()["parked"] == "不做了"

        resumed = client.patch(f"/requirements/{task['requirement_id']}", json={"resume": True})
        assert resumed.status_code == 200
        # 恢复后状态回到推导值:唯一那次尝试还开着,所以是进行中。
        assert resumed.json()["state"] == "in_progress"
        assert resumed.json()["parked"] == ""

    def test_park_with_running_attempt_is_allowed(
        self, client: TestClient, workspace: Path
    ) -> None:
        """搁置是对需求的判断,不是取消任务——尝试不受影响。"""
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        assert (
            client.patch(
                f"/requirements/{task['requirement_id']}", json={"park": "先停"}
            ).status_code
            == 200
        )
        assert TaskStore(workspace).get(task["id"]).state is TaskState.CREATED

    def test_empty_park_reason_rejected(self, client: TestClient) -> None:
        """空原因的搁置被拒——它会与"恢复"分不开,而两者是相反的动作。"""
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        assert (
            client.patch(f"/requirements/{task['requirement_id']}", json={"park": ""}).status_code
            == 422
        )

    def test_park_and_resume_together_rejected(self, client: TestClient) -> None:
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        response = client.patch(
            f"/requirements/{task['requirement_id']}", json={"park": "x", "resume": True}
        )
        assert response.status_code == 422

    def test_priority_patch(self, client: TestClient) -> None:
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        response = client.patch(f"/requirements/{task['requirement_id']}", json={"priority": 9})
        assert response.status_code == 200
        assert response.json()["priority"] == 9

    def test_each_action_recorded_on_requirement_subject(
        self, client: TestClient, workspace: Path
    ) -> None:
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        rid = task["requirement_id"]
        client.patch(f"/requirements/{rid}", json={"text": "改一版"})
        client.patch(f"/requirements/{rid}", json={"park": "先停"})
        client.patch(f"/requirements/{rid}", json={"resume": True})
        actions = [
            event.payload.get("action")
            for event in EventLog(workspace).events(rid)
            if event.kind is LogKind.REQUIREMENT_CHANGED
        ]
        assert actions == ["text", "park", "resume"]

    def test_patch_unknown_requirement_is_404(self, client: TestClient) -> None:
        assert client.patch("/requirements/req-20990101-001", json={"park": "x"}).status_code == 404

    def test_cli_park_and_list(self, client: TestClient, workspace: Path) -> None:
        """CLI 与 PATCH 走同一套校验;列表视角一致。"""
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        rid = task["requirement_id"]
        result = runner.invoke(
            cli_app, ["requirement", "park", rid, "--reason", "不做了", "-w", str(workspace)]
        )
        assert result.exit_code == 0, result.output
        assert _requirements(client)[0]["state"] == "parked"

        listed = runner.invoke(cli_app, ["requirement", "list", "--json", "-w", str(workspace)])
        assert listed.exit_code == 0, listed.output
        import json as json_module

        rows = json_module.loads(listed.output)["requirements"]
        assert rows[0]["id"] == rid
        assert rows[0]["state"] == "parked"

        shown = runner.invoke(cli_app, ["requirement", "show", rid, "--json", "-w", str(workspace)])
        assert shown.exit_code == 0, shown.output
        assert json_module.loads(shown.output)["chain"][0]["id"] == task["id"]

    def test_cli_empty_reason_rejected_identically(
        self, client: TestClient, workspace: Path
    ) -> None:
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        rid = task["requirement_id"]
        rest_message = client.patch(f"/requirements/{rid}", json={"park": ""}).json()["detail"]
        result = runner.invoke(
            cli_app, ["requirement", "park", rid, "--reason", "", "-w", str(workspace)]
        )
        assert result.exit_code != 0
        assert rest_message in result.output


class TestDerivedStateOverRest:
    def test_attempt_chain_exposes_execution_projection(
        self, client: TestClient, workspace: Path
    ) -> None:
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        store = TaskStore(workspace)
        store.save(store.get(task["id"]).evolve(run_status=TaskRunStatus.RUNNING))

        detail = client.get(f"/requirements/{task['requirement_id']}").json()

        assert detail["chain"][0]["execution_status"] == "interrupted"

    def test_escalated_attempt_puts_requirement_back_in_queue(
        self, client: TestClient, workspace: Path
    ) -> None:
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        store = TaskStore(workspace)
        store.save(store.get(task["id"]).evolve(state=TaskState.ESCALATED))
        assert _requirements(client)[0]["state"] == "queued"

    def test_completed_attempt_delivers_even_with_new_open_attempt(
        self, client: TestClient, workspace: Path
    ) -> None:
        task = client.post("/tasks", json={"requirement": "支持部分退款"}).json()
        store = TaskStore(workspace)
        store.save(store.get(task["id"]).evolve(state=TaskState.COMPLETED))
        # 交付后又发起跟进尝试(店级挂链;REST 的「再试一次」入口是 43/02 的事)。
        store.create(
            title="退款", requirement="支持部分退款", requirement_id=task["requirement_id"]
        )
        entry = _requirements(client)[0]
        assert entry["state"] == "delivered"
        assert entry["attempts"] == 2
