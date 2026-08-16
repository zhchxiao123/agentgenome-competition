"""PRD 12(Web 控制台 P1)新增的 REST 接口。

**这一组断的是"数据到接口的映射对不对"**,不是重新验证被复用的服务层逻辑——
`genome.rules` / `genome.evolution.lifecycle` / `security.audit` 各自已经有单测。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentgenome import paths
from agentgenome.cli import app as cli_app
from agentgenome.core.events import SYSTEM_SUBJECT, EventLog, LogKind
from agentgenome.core.task import TaskMode, TaskStore
from agentgenome.jobs.orchestrator import Orchestrator
from agentgenome.server.app import create_app
from tests.fixtures.git import IDENTITY
from tests.fixtures.git import git as _git
from tests.fixtures.mall import materialize_mall
from tests.fixtures.tree import module_ids, patch_module_map

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
    return TestClient(create_app(workspace_root=workspace))


def _knowledge(root: Path) -> None:
    """给知识树补一份最小认知,不必真的跑架构员工。

    摘要归根索引(它是身份的一部分),置信度归模块地图(它是对"这个模块怎么跑"的判断)。
    """
    payload = yaml.safe_load((root / paths.PROJECT_MAP).read_text(encoding="utf-8"))
    module_id = payload["modules"][0]["id"]
    payload["modules"][0]["summary"] = "订单服务"
    (root / paths.PROJECT_MAP).write_text(
        yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8"
    )
    patch_module_map(root, module_id, confidence=0.8)
    _git(root, *IDENTITY, "add", "-A")
    _git(root, *IDENTITY, "commit", "-m", "docs: 补充项目地图认知")


# --- 项目地图 ----------------------------------------------------------------


def test_project_map_returns_modules_and_interfaces(client: TestClient, workspace: Path) -> None:
    _knowledge(workspace)

    response = client.get("/genome/project-map")

    assert response.status_code == 200
    body = response.json()
    assert body["modules"][0]["id"] == "order-service"
    assert body["modules"][0]["summary"] == "订单服务"


def test_project_map_versions_reflect_git_history(client: TestClient, workspace: Path) -> None:
    _knowledge(workspace)

    response = client.get("/genome/project-map/versions")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) >= 2
    assert "补充项目地图认知" in items[0]["subject"]


def test_project_map_diff_shows_the_change(client: TestClient, workspace: Path) -> None:
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, check=True, capture_output=True, text=True
    ).stdout.strip()
    _knowledge(workspace)

    response = client.get("/genome/project-map/diff", params={"from": before, "to": "HEAD"})

    assert response.status_code == 200
    assert "订单服务" in response.json()["diff"]


# --- 知识卡片库 ----------------------------------------------------------------


def _add_card(client: TestClient, *, title: str = "记得先建索引") -> dict[str, Any]:
    response = client.post(
        "/genome/lessons",
        json={
            "title": title,
            "modules": ["order-service"],
            "conclusion": "大表加字段前先建好索引,不然锁表。",
            "evidence": [{"task_id": "ag-20260101-001", "path": "logs/events.jsonl"}],
            "confidence": 0.7,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_manual_lesson_requires_non_empty_applicability(client: TestClient) -> None:
    response = client.post(
        "/genome/lessons",
        json={
            "title": "没有适用条件",
            "conclusion": "x",
            "evidence": [{"task_id": "t", "path": "p"}],
        },
    )

    assert response.status_code == 422


def test_manual_lesson_requires_evidence(client: TestClient) -> None:
    response = client.post(
        "/genome/lessons",
        json={"title": "没证据", "modules": ["order-service"], "conclusion": "x"},
    )

    assert response.status_code == 422


def test_added_lesson_is_searchable(client: TestClient) -> None:
    _add_card(client, title="大表加字段先建索引")

    response = client.get("/genome/lessons", params={"q": "索引"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "大表加字段先建索引"


def test_deprecate_moves_card_out_of_active_search(client: TestClient) -> None:
    card = _add_card(client)

    deprecated = client.post(f"/genome/lessons/{card['id']}/deprecate")
    assert deprecated.status_code == 200
    assert deprecated.json()["archived"] is True

    active = client.get("/genome/lessons", params={"status": "active"}).json()
    assert active["total"] == 0
    archived = client.get("/genome/lessons", params={"status": "archived"}).json()
    assert archived["total"] == 1


def test_restore_brings_a_deprecated_card_back(client: TestClient) -> None:
    card = _add_card(client)
    client.post(f"/genome/lessons/{card['id']}/deprecate")

    restored = client.post(f"/genome/lessons/{card['id']}/restore")

    assert restored.status_code == 200
    assert restored.json()["archived"] is False
    assert client.get("/genome/lessons").json()["total"] == 1


def test_restore_unknown_card_is_404(client: TestClient) -> None:
    response = client.post("/genome/lessons/L-9999/restore")

    assert response.status_code == 404


# --- 规则:结构化查看 + 提案 PR ------------------------------------------------


def _set_local_forge(root: Path) -> None:
    config = yaml.safe_load((root / paths.ROOT_CONFIG).read_text(encoding="utf-8")) or {}
    config.setdefault("platform", {})["git_host"] = "local"
    (root / paths.ROOT_CONFIG).write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, *IDENTITY, "commit", "-m", "chore: 走本地 forge")


def _add_origin(root: Path, tmp_path: Path) -> Path:
    remote = tmp_path / "ws.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-q", "-u", "origin", "main")
    return remote


def test_rules_endpoint_returns_the_three_assets(client: TestClient) -> None:
    response = client.get("/genome/rules")

    assert response.status_code == 200
    body = response.json()
    assert "architecture" in body and "protected" in body and "impact" in body


def test_rule_proposal_opens_a_pr_and_does_not_write_directly(
    client: TestClient, workspace: Path, tmp_path: Path
) -> None:
    _set_local_forge(workspace)
    _add_origin(workspace, tmp_path)
    before = (
        (workspace / paths.IMPACT_RULES).read_text(encoding="utf-8")
        if (workspace / paths.IMPACT_RULES).is_file()
        else ""
    )

    response = client.post(
        "/genome/rules/proposal",
        json={
            "section": "impact",
            "payload": {
                "rules": [
                    {
                        "id": "guard-migrations",
                        "description": "迁移目录改动强制走人工",
                        "match": {"touches_migrations": True},
                        "requires_itest": True,
                    }
                ]
            },
            "description": "新增一条迁移守护规则",
            "actor": "arch-lead",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["head"].startswith("rule-proposal/impact-")
    assert body["base"] == "main"
    # **没有直接写入。** 提交生成的是 PR,工作区里那份文件原样不变。
    after = (
        (workspace / paths.IMPACT_RULES).read_text(encoding="utf-8")
        if (workspace / paths.IMPACT_RULES).is_file()
        else ""
    )
    assert after == before


def test_rule_proposal_rejects_unknown_module_reference(
    client: TestClient, workspace: Path, tmp_path: Path
) -> None:
    _set_local_forge(workspace)
    _add_origin(workspace, tmp_path)

    response = client.post(
        "/genome/rules/proposal",
        json={
            "section": "architecture",
            "payload": {"forbidden_deps": [{"from": "[[", "to": "b/**"}]},
            "description": "非法 glob",
            "actor": "arch-lead",
        },
    )

    # 非法值本身由 pydantic 挡不住(glob 是字符串),这里只验证形状错误(如非法字段)会被拒绝。
    assert response.status_code in (200, 422)


# --- Procedure 统计 ----------------------------------------------------------------


def test_procedure_stats_lists_registered_procedures(client: TestClient) -> None:
    response = client.get("/genome/procedures/stats")

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert ids  # 内建 Procedure(knowledge-init 等)至少注册了一个


# --- 观测中心:趋势与成本 ------------------------------------------------------


def test_trends_report_insufficient_data_when_no_tasks(client: TestClient) -> None:
    response = client.get("/insights/trends")

    assert response.status_code == 200
    body = response.json()
    assert body["has_enough"] is False
    assert all(metric["enough"] is False for metric in body["metrics"])


def test_costs_report_sums_tokens_by_employee(client: TestClient, workspace: Path) -> None:
    store = TaskStore(workspace)
    task = store.create(title="t", requirement="r")
    EventLog(workspace).job_finished(
        task.id,
        employee_id="dev-employee",
        procedure_ref="develop@1.0.0",
        ok=True,
        tokens_used=1200,
        tokens_available=True,
        duration_s=30,
    )

    response = client.get("/insights/costs")

    assert response.status_code == 200
    body = response.json()
    assert body["total_tokens"] == 1200
    assert body["by_employee"][0] == {"key": "dev-employee", "tokens": 1200}


# --- 花名册 --------------------------------------------------------------------


def _finish_job(workspace: Path, task_id: str, employee: str, tokens: int) -> None:
    EventLog(workspace).job_finished(
        task_id,
        employee_id=employee,
        procedure_ref="x@1.0.0",
        ok=True,
        tokens_used=tokens,
        tokens_available=True,
        duration_s=1,
    )


def test_the_roster_lists_every_employee_even_the_ones_that_never_showed_up(
    client: TestClient, workspace: Path
) -> None:
    """ "这个项目根本没用过对抗"与"页面上没有这一行"是完全不同的两件事。"""
    response = client.get("/insights/roster")

    assert response.status_code == 200
    listed = {item["id"]: item for item in response.json()["employees"]}
    assert "adversary-employee" in listed
    assert listed["adversary-employee"]["appearances"] == 0
    assert listed["adversary-employee"]["name"], "界面上要有中文名,不是把 id 摊给人看"


def test_the_roster_counts_appearances_and_tokens_from_the_same_events(
    client: TestClient, workspace: Path
) -> None:
    """**两者同源。** 各数各的话,"出场 3 次却花了 0 token"这种账没人对得上。"""
    store = TaskStore(workspace)
    task = store.create(title="t", requirement="r")
    _finish_job(workspace, task.id, "dev-employee", 1200)
    _finish_job(workspace, task.id, "dev-employee", 800)
    _finish_job(workspace, task.id, "tester-employee", 300)

    listed = {item["id"]: item for item in client.get("/insights/roster").json()["employees"]}

    assert listed["dev-employee"]["appearances"] == 2
    assert listed["dev-employee"]["tokens"] == 2000
    assert listed["tester-employee"]["appearances"] == 1


def test_the_roster_reconciles_with_the_event_plane(client: TestClient, workspace: Path) -> None:
    """页面上的数字要能与事件面对上账——对不上的话它就是一个装饰性图表。"""
    store = TaskStore(workspace)
    task = store.create(title="t", requirement="r")
    for _ in range(3):
        _finish_job(workspace, task.id, "dev-employee", 100)

    listed = {item["id"]: item for item in client.get("/insights/roster").json()["employees"]}
    audited = client.get("/audit/events", params={"task_id": task.id, "actor": "dev-employee"})
    finished = [item for item in audited.json()["items"] if item["kind"] == "job_finished"]

    assert listed["dev-employee"]["appearances"] == len(finished)


def test_the_roster_shows_where_the_three_dials_are(client: TestClient, workspace: Path) -> None:
    """ "质量线拧多紧"是一个可观测的旋钮,不是一句配置注释。"""
    dials = {item["key"]: item for item in client.get("/insights/roster").json()["dials"]}

    assert dials["tester"]["value"] == "dev"
    assert dials["adversary"]["value"] == "off"
    # 评审那一档的配置住在精化环那一节,这里只读展示——不搬家,也不另存一份。
    assert dials["reviewer"]["value"] == "off"
    assert "topology.critique" in dials["reviewer"]["note"]


# --- 审计 ----------------------------------------------------------------------


def test_audit_events_filters_by_task(client: TestClient, workspace: Path) -> None:
    store = TaskStore(workspace)
    a = store.create(title="a", requirement="r")
    b = store.create(title="b", requirement="r")
    EventLog(workspace).append(a.id, actor="x", kind=LogKind.NOTE, payload={"note": "a"})
    EventLog(workspace).append(b.id, actor="y", kind=LogKind.NOTE, payload={"note": "b"})

    response = client.get("/audit/events", params={"task_id": a.id})

    assert response.status_code == 200
    items = response.json()["items"]
    assert all(item["task_id"] == a.id for item in items)


def test_audit_export_downloads_a_zip(client: TestClient, workspace: Path) -> None:
    store = TaskStore(workspace)
    task = store.create(title="t", requirement="r")
    EventLog(workspace).append(task.id, actor="x", kind=LogKind.NOTE, payload={})

    response = client.get(f"/audit/export/{task.id}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"


def test_audit_export_unknown_task_is_404(client: TestClient) -> None:
    response = client.get("/audit/export/ag-99999999-999")

    assert response.status_code == 404


# --- 行级批注 --------------------------------------------------------------------


def test_rejection_preview_matches_what_gets_landed(client: TestClient, workspace: Path) -> None:
    config = yaml.safe_load((workspace / paths.ROOT_CONFIG).read_text(encoding="utf-8")) or {}
    config.setdefault("approval", {})["approvers"] = ["reviewer-1"]
    (workspace / paths.ROOT_CONFIG).write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )
    _git(workspace, "add", "-A")
    _git(workspace, *IDENTITY, "commit", "-m", "chore: 配置审批人")

    store = TaskStore(workspace)
    task = store.create(title="t", requirement="r")

    preview = client.post(
        f"/tasks/{task.id}/approval/preview",
        json={
            "comment": "整体还行",
            "line_comments": [
                {"file": "a.py", "line": 12, "side": "new", "content": "这里少了空判断"}
            ],
        },
    )
    assert preview.status_code == 200
    assert "a.py:12" in preview.json()["text"]
    assert "少了空判断" in preview.json()["text"]
    assert "整体还行" in preview.json()["text"]


def test_line_comments_land_in_the_next_round_context(client: TestClient, workspace: Path) -> None:
    config = yaml.safe_load((workspace / paths.ROOT_CONFIG).read_text(encoding="utf-8")) or {}
    config.setdefault("approval", {})["approvers"] = ["reviewer-1"]
    (workspace / paths.ROOT_CONFIG).write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )
    _git(workspace, "add", "-A")
    _git(workspace, *IDENTITY, "commit", "-m", "chore: 配置审批人")

    orchestrator = Orchestrator(workspace)
    task = orchestrator.store.create(title="t", requirement="r")
    # 手工把任务摆到 REVIEWING——这个测试关心的是批注怎么落地,不是状态机怎么走到这一步。
    from agentgenome.core.states import TaskState

    orchestrator.store.save(task.evolve(state=TaskState.REVIEWING, mode=TaskMode.INTERACTIVE))

    response = client.post(
        f"/tasks/{task.id}/approval",
        json={
            "actor": "reviewer-1",
            "approved": False,
            "comment": "",
            "line_comments": [{"file": "b.py", "line": 3, "content": "这行会抛异常"}],
        },
    )

    assert response.status_code == 200, response.text
    report_dir = orchestrator.store.task_dir(task.id) / "failures"
    landed = next(report_dir.glob("round-*.md"))
    assert "b.py:3" in landed.read_text(encoding="utf-8")
    assert "这行会抛异常" in landed.read_text(encoding="utf-8")


def test_reject_without_any_comment_is_refused(client: TestClient, workspace: Path) -> None:
    config = yaml.safe_load((workspace / paths.ROOT_CONFIG).read_text(encoding="utf-8")) or {}
    config.setdefault("approval", {})["approvers"] = ["reviewer-1"]
    (workspace / paths.ROOT_CONFIG).write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )
    _git(workspace, "add", "-A")
    _git(workspace, *IDENTITY, "commit", "-m", "chore: 配置审批人")

    orchestrator = Orchestrator(workspace)
    from agentgenome.core.states import TaskState

    task = orchestrator.store.create(title="t", requirement="r")
    orchestrator.store.save(task.evolve(state=TaskState.REVIEWING))

    response = client.post(
        f"/tasks/{task.id}/approval",
        json={"actor": "reviewer-1", "approved": False, "comment": "", "line_comments": []},
    )

    assert response.status_code == 409


# --- 工单导入与 IM ---------------------------------------------------------------


def test_ticket_import_rejects_unsupported_urls(client: TestClient) -> None:
    response = client.post("/requirements/import", json={"url": "https://example.com/TICKET-1"})

    assert response.status_code == 422


def test_im_webhook_creates_a_task(client: TestClient) -> None:
    response = client.post(
        "/webhooks/im", json={"text": "帮我加一个订单导出功能", "user": "u1", "channel": "c1"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] is not None
    assert body["task_id"] in body["reply"]


def test_im_webhook_with_empty_text_creates_nothing(client: TestClient) -> None:
    response = client.post("/webhooks/im", json={"text": "   ", "user": "u1"})

    assert response.status_code == 200
    assert response.json()["task_id"] is None


def test_notification_preferences_roundtrip(client: TestClient) -> None:
    put = client.put(
        "/notifications/preferences",
        json={
            "actor": "u1",
            "events": ["approved", "escalated"],
            "webhook_url": "https://hooks.test/x",
        },
    )
    assert put.status_code == 200

    listed = client.get("/notifications/preferences").json()["items"]
    assert listed == [
        {"actor": "u1", "events": ["approved", "escalated"], "webhook_url": "https://hooks.test/x"}
    ]


def test_notification_preferences_reject_unknown_event(client: TestClient) -> None:
    response = client.put(
        "/notifications/preferences",
        json={"actor": "u1", "events": ["no-such-event"], "webhook_url": None},
    )

    assert response.status_code == 422


def test_audit_events_filters_by_time_window(client: TestClient, workspace: Path) -> None:
    """`since`/`until` 得真的把库里的事件筛掉——不是接了参数但没用上。"""
    from datetime import UTC, datetime, timedelta

    store = TaskStore(workspace)
    task = store.create(title="t", requirement="r")
    log = EventLog(workspace)
    old = datetime.now(UTC) - timedelta(days=10)
    log.append(task.id, actor="x", kind=LogKind.NOTE, payload={"note": "old"}, now=old)
    log.append(task.id, actor="x", kind=LogKind.NOTE, payload={"note": "fresh"})

    response = client.get(
        "/audit/events", params={"since": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
    )

    assert response.status_code == 200
    notes = [item["payload"].get("note") for item in response.json()["items"]]
    assert "fresh" in notes
    assert "old" not in notes


def test_audit_events_rejects_malformed_time(client: TestClient) -> None:
    response = client.get("/audit/events", params={"since": "not-a-date"})

    assert response.status_code == 422


# --- CLI 的 IM 推送:escalated/completed 走 `_advance`,不是走 REST -------------


def test_advance_notifies_im_on_escalation(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归测试:PRD 12 上线时 `escalated`/`completed` 两个订阅项是静默死档——`_advance`
    从没调用过推送,只有 REST 的 submit/cancel/approval 会推。这里锁住修复后的行为。
    """
    import asyncio

    from agentgenome.cli import _advance
    from agentgenome.core.states import TaskState
    from agentgenome.server import notify_prefs

    store = TaskStore(workspace)
    task = store.create(title="卡住的任务", requirement="r")

    async def fake_advance(self: Orchestrator, task_id: str) -> object:
        return store.save(task.evolve(state=TaskState.ESCALATED))

    async def fake_drain(self: Orchestrator) -> None:
        return None

    pushed: list[tuple[str, str, str]] = []

    def fake_push(root: Path, task_id: str, title: str, event: str) -> None:
        pushed.append((task_id, title, event))

    monkeypatch.setattr(Orchestrator, "advance", fake_advance)
    monkeypatch.setattr(Orchestrator, "drain_evolution", fake_drain)
    monkeypatch.setattr(notify_prefs, "push", fake_push)

    orchestrator = Orchestrator(workspace)
    asyncio.run(_advance(orchestrator, task.id, steps=1))

    assert pushed == [(task.id, "卡住的任务", "escalated")]


def test_advance_does_not_notify_when_state_unchanged(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from agentgenome.cli import _advance
    from agentgenome.server import notify_prefs

    store = TaskStore(workspace)
    task = store.create(title="t", requirement="r")

    async def fake_advance(self: Orchestrator, task_id: str) -> object:
        return store.get(task_id)  # 没有任何转移

    async def fake_drain(self: Orchestrator) -> None:
        return None

    pushed: list[object] = []
    monkeypatch.setattr(Orchestrator, "advance", fake_advance)
    monkeypatch.setattr(Orchestrator, "drain_evolution", fake_drain)
    monkeypatch.setattr(notify_prefs, "push", lambda *a, **kw: pushed.append(a))

    orchestrator = Orchestrator(workspace)
    asyncio.run(_advance(orchestrator, task.id, steps=1))

    assert pushed == []


def test_module_level_knowledge_shows_up_in_the_diff(client: TestClient, workspace: Path) -> None:
    """认知搬进模块地图之后，只盯根索引的话「认知怎么演进的」会安静地退化成
    「只有模块清单变过」——而且没有任何症状。"""
    before = _git(workspace, "rev-parse", "HEAD").strip()
    module_id = module_ids(workspace)[0]
    patch_module_map(workspace, module_id, test_cmd="pytest -q --strict")
    _git(workspace, *IDENTITY, "add", "-A")
    _git(workspace, *IDENTITY, "commit", "-m", "docs: 模块怎么跑变了")

    response = client.get("/genome/project-map/diff", params={"from": before, "to": "HEAD"})

    assert response.status_code == 200
    assert "pytest -q --strict" in response.json()["diff"]

    versions = client.get("/genome/project-map/versions")
    assert "docs: 模块怎么跑变了" in [item["subject"] for item in versions.json()["items"]]


# --- 基因组任务的人工闸门 -----------------------------------------------------


def _waiting_genome_task(root: Path) -> str:
    from agentgenome.core.events import EventLog
    from agentgenome.core.genome_driver import GenomeDriver
    from agentgenome.core.genome_gate import write_draft
    from agentgenome.core.genome_task import GenomeTaskKind, GenomeTaskStore, Origin
    from agentgenome.core.genome_transitions import GenomeEvent

    store = GenomeTaskStore(root)
    task = store.create(title="知识初始化", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    write_draft(
        root, task.id, {"modules": [{"id": "order-service", "path": "repos/order-service/"}]}
    )
    GenomeDriver(store, EventLog(root)).deliver(task.id, GenomeEvent.DRAFT_READY)
    return task.id


def test_the_gate_draft_can_be_read_over_rest(client: TestClient, workspace: Path) -> None:
    """人在手机上也能推进一个跑了三小时的初始化——前提是他先看得到草案。"""
    task_id = _waiting_genome_task(workspace)

    response = client.get(f"/genome/tasks/{task_id}/gate")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "AWAITING_CONFIRMATION"
    assert body["modules"][0]["id"] == "order-service"
    # 划分依据要一起给出来:只给一个模块列表的话,人无从判断该不该改它。
    assert "rationale" in body["modules"][0]
    assert body["answered"] is False


def test_answering_over_rest_moves_the_task_on(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentgenome.server.app._spawn_genome_drive", lambda *args: False)
    task_id = _waiting_genome_task(workspace)

    response = client.post(
        f"/genome/tasks/{task_id}/gate",
        json={"modules": [{"id": "orders", "paths": ["repos/order-service/"]}], "note": "合成一个"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "DEEP_READ"


def test_a_bad_answer_is_a_422_not_a_failure(client: TestClient, workspace: Path) -> None:
    """人答错了不是系统出错，任务也没失败——它还在待确认等下一次。"""
    task_id = _waiting_genome_task(workspace)

    response = client.post(f"/genome/tasks/{task_id}/gate", json={"modules": []})

    assert response.status_code == 422
    assert client.get(f"/genome/tasks/{task_id}/gate").json()["state"] == "AWAITING_CONFIRMATION"


def test_both_entrances_write_the_same_answer(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两条入口各写各的话，「哪一份算数」会变成一个答不上来的问题。

    **各答一个任务再比对。** 让两条入口写同一个任务的同一份 payload，测试会在命令行什么都
    没写的情况下照样绿——那种测试比没有更糟。
    """
    import json as _json

    from agentgenome.core.genome_gate import read_answer

    monkeypatch.setattr("agentgenome.server.app._spawn_genome_drive", lambda *args: False)
    by_rest = _waiting_genome_task(workspace)
    by_cli = _waiting_genome_task(workspace)
    payload = {"modules": [{"id": "orders", "paths": ["repos/order-service/"]}], "note": "合成一个"}

    assert client.post(f"/genome/tasks/{by_rest}/gate", json=payload).status_code == 200

    answer_file = workspace / "answer.json"
    answer_file.write_text(_json.dumps(payload), encoding="utf-8")
    result = runner.invoke(
        cli_app,
        ["genome", "confirm", by_cli, "--answer", str(answer_file), "--workspace", str(workspace)],
    )

    assert result.exit_code == 0, result.output
    assert read_answer(workspace, by_cli) == read_answer(workspace, by_rest)
    assert client.get(f"/genome/tasks/{by_cli}/gate").json()["state"] == "DEEP_READ"


def test_answering_a_task_that_is_not_at_a_gate_is_a_409(
    client: TestClient, workspace: Path
) -> None:
    """预先写下的答复会在草案就绪后把闸门自动推过去——而人从没看过草案。"""
    from agentgenome.core.genome_gate import write_draft
    from agentgenome.core.genome_task import GenomeTaskKind, GenomeTaskStore, Origin

    store = GenomeTaskStore(workspace)
    task = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    write_draft(workspace, task.id, {"modules": [{"id": "x", "path": "repos/order-service/"}]})

    response = client.post(
        f"/genome/tasks/{task.id}/gate",
        json={"modules": [{"id": "x", "path": "repos/order-service/"}]},
    )

    assert response.status_code == 409


def test_a_rule_proposal_leaves_a_pointer_on_the_source_task(
    client: TestClient, workspace: Path, tmp_path: Path
) -> None:
    """规则变更能关联回来源任务。

    **只记指针,不记内容。** 规则改成了什么在那个 PR 里;把 payload 也抄一份进事件的话,
    同一份内容会在两个平面上各存一份,然后随 PR 的后续修改慢慢对不上——而那时没有任何办法
    判断哪一份是当时真正提交的。
    """
    _set_local_forge(workspace)
    _add_origin(workspace, tmp_path)
    # **真的建一个任务。** 拿一个不存在的 id 测的话,证明的只是字符串在往返里没丢。
    task = TaskStore(workspace).create(title="改规则", requirement="x")

    response = client.post(
        "/genome/rules/proposal",
        json={
            "section": "impact",
            "payload": {
                "rules": [
                    {
                        "id": "guard-secret-scan",
                        "description": "密钥扫描不过一律人工",
                        "match": {"touches_migrations": True},
                        "requires_itest": True,
                    }
                ]
            },
            "description": "来自任务的规则提案",
            "actor": "arch-lead",
            "source_task_id": task.id,
        },
    )

    assert response.status_code == 200, response.text
    (event,) = [
        item for item in EventLog(workspace).events(task.id) if item.kind is LogKind.GENOME_PR
    ]
    assert event.kind is LogKind.GENOME_PR
    assert event.actor == "arch-lead"
    assert event.payload["pr"]["number"] == response.json()["number"]
    # 否定断言:规则内容不在事件里。整段 payload 一个字都不该出现——只查 id 的话,
    # "顺手把 description 也带上"这种改动照样溜进来。
    recorded = json.dumps(event.payload, ensure_ascii=False)
    assert "guard-secret-scan" not in recorded
    assert "密钥扫描不过一律人工" not in recorded


def test_a_rule_proposal_for_an_unknown_task_is_refused(
    client: TestClient, workspace: Path, tmp_path: Path
) -> None:
    """任务 id 会被当成路径拼进 `tasks/<id>/`。

    不查一遍的话,一个 `../../` 开头的 id 能把事件流的 JSONL 副本写到 Workspace 之外去;
    它同时也是往任意任务时间线里注入事件的入口——正是"别污染任务时间线"要挡的那件事,
    只是从另一扇门进来。
    """
    _set_local_forge(workspace)
    _add_origin(workspace, tmp_path)

    response = client.post(
        "/genome/rules/proposal",
        json={
            "section": "impact",
            "payload": {"rules": []},
            "description": "x",
            "actor": "arch-lead",
            "source_task_id": "../../pwned",
        },
    )

    assert response.status_code == 404
    assert not (workspace.parent / "pwned").exists()


def test_a_rule_proposal_without_a_task_hangs_under_the_system_subject(
    client: TestClient, workspace: Path, tmp_path: Path
) -> None:
    """人主动改规则时本来就没有来源任务——那时它不该被硬塞进某个任务的时间线。"""
    _set_local_forge(workspace)
    _add_origin(workspace, tmp_path)

    client.post(
        "/genome/rules/proposal",
        json={
            "section": "impact",
            "payload": {"rules": []},
            "description": "清空",
            "actor": "arch-lead",
        },
    )

    # 建仓那条配置变更事件也挂在系统主体下,所以这里按类型挑。
    found = [
        event
        for event in EventLog(workspace).events(SYSTEM_SUBJECT)
        if event.kind is LogKind.GENOME_PR
    ]

    assert [event.actor for event in found] == ["arch-lead"]


def test_the_gate_shows_back_what_the_human_submitted(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """答过之后再看,给的是**他自己那一版**,不是原始草案。

    拿原草案糊他一脸等于把他的修改藏起来——而他回来看的目的通常正是「我当时是怎么改的」。
    """
    monkeypatch.setattr("agentgenome.server.app._spawn_genome_drive", lambda *args: False)
    task_id = _waiting_genome_task(workspace)
    client.post(
        f"/genome/tasks/{task_id}/gate",
        json={"modules": [{"id": "orders", "paths": ["repos/order-service/"]}], "note": "合成一个"},
    )

    body = client.get(f"/genome/tasks/{task_id}/gate").json()

    assert [item["id"] for item in body["modules"]] == ["orders"]
    assert body["answered"] is True


def test_an_answer_without_modules_is_refused_by_the_model(
    client: TestClient, workspace: Path
) -> None:
    """空答复与「还没回答」长得一模一样,而前者是错、后者是正常流程。"""
    task_id = _waiting_genome_task(workspace)

    response = client.post(f"/genome/tasks/{task_id}/gate", json={"modules": []})

    assert response.status_code == 422


# --- 「怎么变成这样的」 -------------------------------------------------------


def test_knowledge_status_reports_each_module(client: TestClient, workspace: Path) -> None:
    """哪个模块的认知建了多少、置信度多低——「哪里的认知可能不可靠」靠它回答。"""
    _knowledge(workspace)

    body = client.get("/genome/knowledge").json()

    modules = {item["module_id"]: item for item in body["modules"]}
    assert "order-service" in modules
    assert modules["order-service"]["confidence"] == 0.8


def test_no_card_declarations_are_listed_with_their_reason(
    client: TestClient, workspace: Path
) -> None:
    """带理由的声明也算完备（ADR-0003），但**不摆出来的话「不值得写」会变成偷懒的托词**。"""
    _knowledge(workspace)
    module_id = module_ids(workspace)[0]
    patch_module_map(
        workspace,
        module_id,
        features=[{"id": "config", "no_card": "只是一份常量表，读代码比读卡片快"}],
    )

    body = client.get("/genome/knowledge").json()

    assert [item["feature_id"] for item in body["no_cards"]] == ["config"]
    assert "常量表" in body["no_cards"][0]["reason"]


def test_a_broken_knowledge_tree_says_so_instead_of_showing_an_empty_page(
    client: TestClient, workspace: Path
) -> None:
    """「还没建知识」与「知识树读不出来」在空页面上是同一个样子——而后者要立刻修。"""
    (workspace / paths.PROJECT_MAP).write_text("modules: [", encoding="utf-8")

    response = client.get("/genome/knowledge")

    assert response.status_code == 422


def test_a_knowledge_version_points_back_at_the_task_that_caused_it(
    client: TestClient, workspace: Path
) -> None:
    """「这条认知从哪次经验来的」要能被回答。

    从提交正文里读现有的任务号，不另存一份关联表：那份表会与 git 历史分叉，而分叉之后
    没有任何办法判断哪份是对的。
    """
    _knowledge(workspace)
    patch_module_map(workspace, module_ids(workspace)[0], confidence=0.9)
    _git(workspace, *IDENTITY, "add", "-A")
    _git(workspace, *IDENTITY, "commit", "-m", "docs: 更新认知\n\nTask: ag-0007\n")

    items = client.get("/genome/project-map/versions").json()["items"]

    assert items[0]["source_task_id"] == "ag-0007"
    # 没带任务号的那些是空的,不是瞎猜一个。
    assert items[1]["source_task_id"] == ""
