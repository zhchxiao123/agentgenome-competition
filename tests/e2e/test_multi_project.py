"""多项目地基(PRD 44):注册表持久化、零项目启动、默认拒绝与隔离。

隔离测试是**对抗性**的:断言拿不到,不是断言拿得到。遍历 OpenAPI 的全部 GET 端点,
手挑会漏,漏一个就是一次静默串数据。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from agentgenome.core.events import EventLog, LogKind
from agentgenome.server.app import create_app
from agentgenome.server.tenancy import WorkspaceRegistry, load_registry
from tests.fixtures.mall import materialize_mall

runner = CliRunner()

#: 不作用在某一个工作区上的端点:平台自身的信息,没有"该拒绝"的语义。
#: **每一项都要有理由**——这张表长一行,隔离就少护一个端点。
WORKSPACE_FREE_GETS = {
    "/health",  # 存活探针,编排器自身
    "/api/version",  # 版本号,进程级
    "/metrics",  # Prometheus 抓取,自带 workspace 标签
    "/workspaces",  # 它就是"有哪些工作区"的答案
    "/topologies",  # 内置模板目录,平台常量
    # SSE 通知通道:长连接,TestClient 一读就挂住。它只推「有新的了」不带数据,
    # 数据拉取全部工作区隔离;按项目订阅的收紧在 44/04(前端多项目化)一并做。
    "/events/stream",
}


def _dummy(schema: dict) -> str:
    """给一个 query 参数编一个能过类型校验的假值。"""
    kind = schema.get("type", "string")
    if kind in ("integer", "number"):
        return "1"
    if kind == "boolean":
        return "true"
    return "x"


@pytest.fixture
def two_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    roots = []
    for name in ("a", "b"):
        root = tmp_path / name
        result = runner.invoke(
            cli_app,
            [
                "init",
                "--local-only",
                str(root),
                "--name",
                name,
                "--repo",
                mall["order-service"].remote_url,
            ],
        )
        assert result.exit_code == 0, result.output
        roots.append(root)
    return roots[0], roots[1]


@pytest.fixture
def two_client(two_roots: tuple[Path, Path]) -> TestClient:
    registry = WorkspaceRegistry()
    registry.register("a", two_roots[0])
    registry.register("b", two_roots[1])
    return TestClient(create_app(workspaces=registry))


class TestZeroProjectStart:
    def test_empty_registry_still_serves(self) -> None:
        """空实例是合法状态:没有任何项目也能起,界面拿到空表而不是报错。"""
        client = TestClient(create_app(workspaces=WorkspaceRegistry()))
        assert client.get("/health").status_code == 200
        assert client.get("/api/version").status_code == 200
        assert client.get("/workspaces").json()["items"] == []


class TestIsolation:
    def test_each_project_sees_only_its_own(self, two_client: TestClient) -> None:
        created = two_client.post(
            "/tasks", json={"requirement": "只属于 a"}, headers={"x-workspace": "a"}
        )
        assert created.status_code == 201, created.text
        a_tasks = two_client.get("/tasks", headers={"x-workspace": "a"}).json()
        b_tasks = two_client.get("/tasks", headers={"x-workspace": "b"}).json()
        assert len(a_tasks) == 1
        assert b_tasks == []

    def test_no_workspace_param_is_refused(self, two_client: TestClient) -> None:
        assert two_client.get("/tasks").status_code == 400
        assert two_client.post("/tasks", json={"requirement": "x"}).status_code == 400

    def test_every_get_endpoint_refuses_without_workspace(self, two_client: TestClient) -> None:
        """遍历 OpenAPI 全部 GET 端点:多于一个项目时,不带参数一律被拒。

        豁免表是白名单,每项带理由(见 `WORKSPACE_FREE_GETS`)。新端点缺省被这条测试
        护住——想豁免必须来这里留一行,而这一行会被评审看见。
        """
        spec = two_client.get("/openapi.json").json()
        leaked = []
        for path, operations in spec["paths"].items():
            if "get" not in operations or path in WORKSPACE_FREE_GETS:
                continue
            concrete = re.sub(r"\{[^}]+\}", "x", path)
            # 把必填的 query 参数按类型填上假值:不填的话 FastAPI 在解析工作区之前就
            # 422 了,处理器根本没跑到——那不是"拒绝了",是"还没轮到拒绝"。
            params = {
                item["name"]: _dummy(item.get("schema", {}))
                for item in operations["get"].get("parameters", [])
                if item.get("required") and item.get("in") == "query"
            }
            response = two_client.get(concrete, params=params)
            if response.status_code != 400:
                leaked.append(f"{path} → {response.status_code}")
        assert not leaked, "这些 GET 端点在没说工作区时没有拒绝:\n" + "\n".join(leaked)


class TestStreamScoping:
    def test_notice_stays_inside_its_project(self) -> None:
        """通知按项目过滤:task_id 本身就是数据,不该跨项目可见。"""
        from agentgenome.server.bus import EventBus

        bus = EventBus()
        bus.publish("ag-1", "task_created", workspace="a")
        assert [item.task_id for item in bus.since(0, None, "a")] == ["ag-1"]
        assert bus.since(0, None, "b") == []
        # 没标项目的通知(单项目部署的老形态)对谁都可见——拦掉等于把推送整个关了。
        bus.publish("ag-2", "task_created")
        assert [item.task_id for item in bus.since(0, None, "b")] == ["ag-2"]

    def test_subscription_filters_by_workspace(self) -> None:
        from agentgenome.server.bus import EventBus, Notice

        bus = EventBus()
        mine = bus.subscribe(workspace="a")
        assert mine.wants(Notice(seq=1, task_id="x", kind="k", workspace="a"))
        assert not mine.wants(Notice(seq=2, task_id="x", kind="k", workspace="b"))
        assert mine.wants(Notice(seq=3, task_id="x", kind="k"))

    def test_stream_refuses_without_workspace_when_plural(self, two_client: TestClient) -> None:
        """多于一个项目时,不带参数的订阅当场 400——立即返回,不是开始流。"""
        assert two_client.get("/events/stream").status_code == 400


class TestRegistryFile:
    def test_register_list_roundtrip(self, two_roots: tuple[Path, Path], tmp_path: Path) -> None:
        registry_file = tmp_path / "registry.yaml"
        for name, root in zip(("a", "b"), two_roots, strict=True):
            result = runner.invoke(
                cli_app,
                ["workspace", "register", name, str(root), "--registry", str(registry_file)],
            )
            assert result.exit_code == 0, result.output
        loaded = load_registry(registry_file)
        assert loaded.names() == ["a", "b"]

        listed = runner.invoke(
            cli_app, ["workspace", "list", "--registry", str(registry_file), "--json"]
        )
        assert listed.exit_code == 0, listed.output
        assert '"a"' in listed.output

    def test_register_refuses_a_non_workspace(self, tmp_path: Path) -> None:
        """注册一个坏目录要在注册那一刻报错,不是第一个请求打过来的时候。"""
        registry_file = tmp_path / "registry.yaml"
        bare = tmp_path / "not-a-workspace"
        bare.mkdir()
        result = runner.invoke(
            cli_app, ["workspace", "register", "bad", str(bare), "--registry", str(registry_file)]
        )
        assert result.exit_code != 0
        assert not registry_file.exists()

    def test_unregister_keeps_the_files(self, two_roots: tuple[Path, Path], tmp_path: Path) -> None:
        """注销只摘注册表,不动磁盘——一次误操作不是一次数据丢失。"""
        registry_file = tmp_path / "registry.yaml"
        runner.invoke(
            cli_app,
            ["workspace", "register", "a", str(two_roots[0]), "--registry", str(registry_file)],
        )
        result = runner.invoke(
            cli_app, ["workspace", "unregister", "a", "--registry", str(registry_file)]
        )
        assert result.exit_code == 0, result.output
        assert load_registry(registry_file).names() == []
        assert (two_roots[0] / "agentgenome.yaml").is_file()
        events = EventLog(two_roots[0]).all_events(kind=LogKind.WORKSPACE_CHANGED)
        assert events and events[0].payload["action"] == "unregister"


class TestDeleteOverRest:
    def test_delete_unregisters_but_keeps_disk(
        self, two_roots: tuple[Path, Path], tmp_path: Path
    ) -> None:
        registry_file = tmp_path / "registry.yaml"
        registry = WorkspaceRegistry()
        registry.register("a", two_roots[0])
        registry.register("b", two_roots[1])
        client = TestClient(create_app(workspaces=registry, registry_path=registry_file))

        response = client.delete("/workspaces/a")
        assert response.status_code == 200, response.text
        assert client.get("/workspaces").json()["items"] == ["b"]
        # 持久化跟着变;磁盘目录原样留下;事件面记了一条。
        assert load_registry(registry_file).names() == ["b"]
        assert (two_roots[0] / "agentgenome.yaml").is_file()
        events = EventLog(two_roots[0]).all_events(kind=LogKind.WORKSPACE_CHANGED)
        assert events and events[0].payload["action"] == "unregister"

    def test_delete_unknown_is_404(self, two_client: TestClient) -> None:
        assert two_client.delete("/workspaces/nope").status_code == 404


class TestServeForms:
    def test_bare_serve_reads_the_registry(
        self, two_roots: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`agctl serve --registry <file>`(不带 -w)起在注册表模式上。"""
        registry_file = tmp_path / "registry.yaml"
        runner.invoke(
            cli_app,
            ["workspace", "register", "a", str(two_roots[0]), "--registry", str(registry_file)],
        )
        captured = {}
        monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.setdefault("app", app))
        result = runner.invoke(cli_app, ["serve", "--registry", str(registry_file)])
        assert result.exit_code == 0, result.output
        assert captured["app"].state.workspaces.names() == ["a"]

    def test_single_workspace_form_unchanged(
        self, two_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = {}
        monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.setdefault("app", app))
        result = runner.invoke(cli_app, ["serve", "-w", str(two_roots[0])])
        assert result.exit_code == 0, result.output
        assert captured["app"].state.workspaces.names() == ["default"]

    def test_explicit_non_workspace_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`-w` 指到一个不是 Workspace 的目录,报错一个字不变——不悄悄退回注册表模式。"""
        monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
        result = runner.invoke(cli_app, ["serve", "-w", str(tmp_path / "nowhere")])
        assert result.exit_code != 0
        assert "先跑 agctl init" in result.output
