"""接线验证:那些模块真的被系统调用了吗。

**这一组存在的理由**:单元测试全绿不代表模块在跑。一个建好、测好、但没有任何调用方的模块,
把它从代码库里删掉,系统行为一点都不会变——而它会随周围代码演进慢慢腐烂,半年后再接的成本
就跟重写差不多了。

所以这里断言的不是"逻辑对不对"(那由单元测试管),是**"它有没有被接上"**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from agentgenome.server.app import create_app
from agentgenome.server.rbac import Principal, Role
from agentgenome.server.tenancy import WorkspaceRegistry
from agentgenome.umodel.graph import Entity, EntityKind, InMemoryGraph, Relation, RelationKind
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
    # `agctl init` 自己已经提交过了,这里再 commit 一次会因为没有改动而失败。
    return root


# --- 全局基因库接上了 CLI ---------------------------------------------------


def _registry(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    tpl = root / "templates" / "python-service" / "genome" / "rules"
    tpl.mkdir(parents=True)
    (root / "templates" / "python-service" / "template.yaml").write_text(
        "summary: Python 服务骨架\nshape: python-service\n", encoding="utf-8"
    )
    (tpl / "extra.md").write_text("# 从模板来的\n", encoding="utf-8")
    return root


def test_the_registry_is_reachable_from_the_cli(workspace: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        cli_app, ["registry", "templates", "--registry", str(_registry(tmp_path))]
    )

    assert result.exit_code == 0, result.output
    assert "python-service" in result.output


def test_applying_a_template_actually_writes_files(workspace: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        cli_app,
        [
            "registry",
            "apply",
            "python-service",
            "--registry",
            str(_registry(tmp_path)),
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (workspace / "genome" / "rules" / "extra.md").is_file()


def test_no_registry_configured_says_so_instead_of_guessing(workspace: Path) -> None:
    """**不给默认路径。** 猜一个出来的话,拼错的配置会表现为「基因库是空的」。"""
    result = runner.invoke(cli_app, ["registry", "templates"], env={"AGENTGENOME_REGISTRY": ""})

    assert result.exit_code != 0
    assert "没有配置" in result.output


# --- 进化接上了 CLI ---------------------------------------------------------


def test_lesson_cards_are_visible_from_the_cli(workspace: Path) -> None:
    """命中数是这套机制唯一对外可见的「这条经验有没有用」。藏起来的话自然选择没人能验证。"""
    lessons = workspace / "genome" / "knowledge" / "lessons"
    lessons.mkdir(parents=True, exist_ok=True)
    (lessons / "L-0001.md").write_text(
        "---\napplies_to:\n  modules: [order-service]\n  path_globs: []\n  scenario: ''\n"
        "archived: false\nconfidence: 0.8\ncreated_from: ag-1\nevidence:\n"
        "- {note: '', path: artifacts/x.json, task_id: ag-1}\nhits: 12\nid: L-0001\n"
        "level: L1\ntitle: 测试要先起 Redis\n---\n\n先起 Redis。\n",
        encoding="utf-8",
    )

    result = runner.invoke(cli_app, ["evolve", "cards", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "L-0001" in result.output
    assert "12" in result.output


def test_the_weekly_report_refuses_to_conclude_without_data(workspace: Path) -> None:
    """三个任务画出来的「下降趋势」是噪声。"""
    result = runner.invoke(cli_app, ["evolve", "report", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "数据不足" in result.output


def test_rule_proposals_are_reachable(workspace: Path) -> None:
    result = runner.invoke(cli_app, ["evolve", "propose", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output


# --- 告警反查接上了 REST ----------------------------------------------------


def _graph_with_service() -> InMemoryGraph:
    graph = InMemoryGraph()
    graph.upsert(
        [Entity(EntityKind.MODULE, "order-service"), Entity(EntityKind.SERVICE, "order-api")],
        [Relation(RelationKind.DEPLOYED_FROM, "service:order-api", "module:order-service")],
    )
    return graph


def test_an_alert_creates_a_fix_task(workspace: Path) -> None:
    client = TestClient(create_app(workspace, graph=_graph_with_service()))

    body = client.post(
        "/alerts", json={"id": "AL-1", "service": "order-api", "summary": "5xx 飙升"}
    ).json()

    assert body["located"] is True
    assert body["task_id"]
    detail = client.get(f"/tasks/{body['task_id']}").json()
    assert "AL-1" in detail["requirement"]
    assert "order-service" in detail["requirement"]


def test_an_unlocatable_alert_creates_nothing(workspace: Path) -> None:
    """**瞎猜一个模块比不建任务更糟。**

    它会让数字员工去改一段与故障无关的代码,而人还以为系统在处理这次告警。
    """
    client = TestClient(create_app(workspace, graph=_graph_with_service()))

    body = client.post("/alerts", json={"id": "AL-9", "service": "没在图上", "summary": "x"}).json()

    assert body["located"] is False
    assert body["task_id"] is None
    assert client.get("/tasks").json() == []


def test_an_alert_storm_only_creates_one_task(workspace: Path) -> None:
    """一次告警风暴能创建几百个任务,把预算烧光。"""
    client = TestClient(create_app(workspace, graph=_graph_with_service()))

    for index in range(5):
        client.post("/alerts", json={"id": f"AL-{index}", "service": "order-api", "summary": "x"})

    assert len(client.get("/tasks").json()) == 1


def test_without_a_graph_alerts_are_only_recorded(workspace: Path) -> None:
    """图谱是增值,不是流程的一部分。没配就跳过,不报错。"""
    client = TestClient(create_app(workspace))

    body = client.post("/alerts", json={"id": "AL-1", "service": "x", "summary": "y"}).json()

    assert body["task_id"] is None
    assert "没有配置语义图谱" in body["reason"]


# --- 多工作区接上了 REST ----------------------------------------------------


def test_two_workspaces_refuse_an_unqualified_request(workspace: Path, tmp_path: Path) -> None:
    """**默认工作区是跨租户数据泄漏最常见的来源。**"""
    registry = WorkspaceRegistry()
    registry.register("mall", workspace)
    registry.register("shop", workspace)
    client = TestClient(create_app(workspaces=registry))

    assert client.get("/tasks").status_code == 400


def test_naming_the_workspace_works(workspace: Path) -> None:
    registry = WorkspaceRegistry()
    registry.register("mall", workspace)
    registry.register("shop", workspace)
    client = TestClient(create_app(workspaces=registry))

    assert client.get("/tasks?workspace=mall").status_code == 200


def test_an_unknown_workspace_is_a_404(workspace: Path) -> None:
    registry = WorkspaceRegistry()
    registry.register("mall", workspace)
    client = TestClient(create_app(workspaces=registry))

    assert client.get("/tasks?workspace=nope").status_code == 404


# --- RBAC 接上了写接口 ------------------------------------------------------


def _guarded(workspace: Path) -> TestClient:
    return TestClient(
        create_app(
            workspace,
            principals={
                "alice": Principal("alice", frozenset({Role.APPROVER})),
                "bob": Principal("bob", frozenset({Role.DEVELOPER})),
            },
        )
    )


def test_an_anonymous_write_is_refused_once_identities_exist(workspace: Path) -> None:
    """**前端藏起按钮不是安全边界。** 用 curl 一样能调,所以校验在服务端。"""
    response = _guarded(workspace).post("/tasks", json={"requirement": "x"})

    assert response.status_code == 403


def test_an_approver_cannot_submit(workspace: Path) -> None:
    """审自己写的东西不叫审批。"""
    response = _guarded(workspace).post(
        "/tasks", json={"requirement": "x"}, headers={"x-actor": "alice"}
    )

    assert response.status_code == 403


def test_a_developer_can_submit(workspace: Path) -> None:
    response = _guarded(workspace).post(
        "/tasks", json={"requirement": "x"}, headers={"x-actor": "bob"}
    )

    assert response.status_code == 201


def test_single_machine_development_is_not_forced_to_configure_accounts(workspace: Path) -> None:
    """身份表为空时放行。注册了任何身份之后立刻开始强制。"""
    response = TestClient(create_app(workspace)).post("/tasks", json={"requirement": "x"})

    assert response.status_code == 201


# --- 设置审计接上了 REST ----------------------------------------------------


def test_a_settings_change_is_audited_over_rest(workspace: Path) -> None:
    client = TestClient(
        create_app(workspace, principals={"root": Principal("root", frozenset({Role.ADMIN}))})
    )

    changed = client.put(
        "/settings",
        json={"section": "concurrency", "value": {"global_jobs": 8}},
        headers={"x-actor": "root"},
    )

    assert changed.status_code == 200, changed.text
    history = client.get("/settings/history", headers={"x-actor": "root"}).json()
    assert history[-1]["actor"] == "root"


def test_versioned_assets_cannot_be_edited_over_rest(workspace: Path) -> None:
    """白名单之外的段是版本化资产,改它们要走 git 评审。

    拿 `platform` 举例而不是 `runtime`:后者已经被有意放进白名单(容器运行时那一段要能
    从界面配,见 PRD 33)。这条守的是**白名单本身还在**,不是某一段的去留。
    """
    client = TestClient(
        create_app(workspace, principals={"root": Principal("root", frozenset({Role.ADMIN}))})
    )

    response = client.put(
        "/settings", json={"section": "platform", "value": {}}, headers={"x-actor": "root"}
    )

    assert response.status_code == 400


# --- 并行调度接上了编排器 ---------------------------------------------------


# 子任务调度器那两条测试随 `core.scheduler` 一起删了:那一层被执行拓扑取代,
# 而"计划怎么变成可并行的工作"现在由 `tests/e2e/test_dag_execution.py` 端到端验。


# --- 审批身份绑定到认证身份,不再信任 body.actor ------------------------------


def test_approval_actor_is_bound_to_the_authenticated_identity(
    workspace: Path, tmp_path: Path
) -> None:
    """回归测试:此前 `decide()` 用服务端角色校验(`x-actor`)挡住"连审批入口都不该碰"
    的角色,但真正记进审批记录、拿去核对审批人名单的是 `body.actor`——一个独立的、客户端
    随便填的 JSON 字段。一个有审批权的人(mallory)能在 body 里填另一个审批人的名字
    (alice),系统会把这次决定记成 alice 做的。
    """
    import yaml

    from agentgenome.core.states import TaskState
    from agentgenome.core.task import TaskStore
    from agentgenome.jobs.orchestrator import Orchestrator

    config_path = workspace / "agentgenome.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config.setdefault("approval", {})["approvers"] = ["alice", "mallory"]
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    task = TaskStore(workspace).create(title="t", requirement="r")
    TaskStore(workspace).save(task.evolve(state=TaskState.REVIEWING))

    client = TestClient(
        create_app(
            workspace,
            principals={"mallory": Principal("mallory", frozenset({Role.APPROVER}))},
        )
    )

    response = client.post(
        f"/tasks/{task.id}/approval",
        json={"actor": "alice", "approved": True, "comment": ""},
        headers={"x-actor": "mallory"},
    )

    assert response.status_code == 200, response.text
    records = [
        event
        for event in Orchestrator(workspace).log.events(task.id)
        if event.kind.value == "approval"
    ]
    assert records[-1].actor == "mallory", (
        "记录的审批人必须是认证身份(mallory),不是 body 里声明的名字(alice)"
    )


# --- L2 候选卡片能被规则提案看到 --------------------------------------------


def test_l2_candidate_cards_reach_the_rule_proposal_command(workspace: Path) -> None:
    """回归测试:`land_cards` 把非 L1 卡片存进 `genome/knowledge/lessons/candidates/`,
    而 `agctl evolve propose` 此前只读 `lessons/`(非递归 glob,看不到子目录)——L2 候选卡片
    因此永远到不了规则提案这一步,`evolve propose` 从第一天起就是个空壳。
    """
    candidates = workspace / "genome" / "knowledge" / "lessons" / "candidates"
    candidates.mkdir(parents=True, exist_ok=True)
    card = (
        "---\napplies_to:\n  modules: []\n  path_globs: ['repos/order-service/migrations/**']\n"
        "  scenario: ''\narchived: false\nconfidence: 0.8\ncreated_from: ag-{n}\n"
        "evidence:\n- {{note: '', path: artifacts/x.json, task_id: ag-{n}}}\nhits: 0\n"
        "id: L-000{n}\nlevel: L2\ntitle: 迁移目录改动反复漏审\n---\n\n"
        "迁移目录的改动应该强制走人工审批。\n"
    )
    for n in (1, 2, 3):
        (candidates / f"L-000{n}.md").write_text(card.format(n=n), encoding="utf-8")

    result = runner.invoke(cli_app, ["evolve", "propose", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    # 提案 id 由路径 glob 派生,`/` 会被折成 `-`——它是一个标识符,不是一条路径。
    assert "distilled-repos-order-service-migrations" in result.output, result.output
    assert "L-0001" in result.output and "L-0003" in result.output
