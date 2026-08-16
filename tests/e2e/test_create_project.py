"""界面建项目(PRD 44):骨架同步、挂载异步、失败进异常队列。

clone 用本地 bare 仓夹具,不碰网络——测的是编排与失败处置,不是 git 本身。
挂载任务是 `origin=human` 的基因组任务:人在等结果,失败要进异常队列。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentgenome.core.genome_task import (
    GenomeTaskKind,
    GenomeTaskState,
    GenomeTaskStore,
    Origin,
)
from agentgenome.genome.scaffold import WORKSPACE_PUSH_PENDING
from agentgenome.server.app import create_app
from agentgenome.server.tenancy import WorkspaceRegistry, load_registry
from agentgenome.space.gitcmd import git
from tests.fixtures.mall import materialize_mall

TERMINAL = {GenomeTaskState.SUBMITTED, GenomeTaskState.FAILED, GenomeTaskState.CANCELLED}


@pytest.fixture
def mall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    return materialize_mall(tmp_path / "upstream")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "workspaces-home"


@pytest.fixture
def registry_file(tmp_path: Path) -> Path:
    return tmp_path / "registry.yaml"


@pytest.fixture
def workspace_repo(tmp_path: Path) -> str:
    remote = tmp_path / "workspace.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    return str(remote)


@pytest.fixture
def client(home: Path, registry_file: Path) -> TestClient:
    return TestClient(
        create_app(
            workspaces=WorkspaceRegistry(),
            registry_path=registry_file,
            workspaces_home=home,
        )
    )


def _wait_terminal(root: Path, task_id: str, timeout_s: float = 30) -> GenomeTaskState:
    store = GenomeTaskStore(root)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = store.get(task_id).state
        if state in TERMINAL:
            return state
        time.sleep(0.1)
    raise AssertionError(f"挂载任务 {task_id} 没在 {timeout_s}s 内到终态")


class TestCreateProject:
    def test_unreachable_workspace_repo_is_rejected_without_a_half_workspace(
        self, client: TestClient, mall, home: Path, tmp_path: Path
    ) -> None:
        missing = tmp_path / "missing-workspace.git"

        response = client.post(
            "/workspaces",
            json={
                "name": "shop",
                "workspace_repo": str(missing),
                "repos": [mall["order-service"].remote_url],
            },
        )

        assert response.status_code == 422
        assert "顶层项目仓库推送失败" in response.json()["detail"]
        assert not (home / "shop").exists()

    def test_scaffold_now_mount_async(
        self,
        client: TestClient,
        mall,
        home: Path,
        registry_file: Path,
        workspace_repo: str,
    ) -> None:
        """响应即含新项目;挂载走 MOUNT 基因组任务;挂完能提研发任务。"""
        response = client.post(
            "/workspaces",
            json={
                "name": "shop",
                "workspace_repo": workspace_repo,
                "repos": [mall["order-service"].remote_url],
            },
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["name"] == "shop"

        # 骨架同步就位:注册表(内存 + 文件)都有它,项目立刻在切换器里。
        assert "shop" in client.get("/workspaces").json()["items"]
        assert load_registry(registry_file).names() == ["shop"]
        root = home / "shop"
        assert (root / "agentgenome.yaml").is_file()

        record = GenomeTaskStore(root).get(payload["mount_task_id"])
        assert record.kind is GenomeTaskKind.MOUNT
        assert record.origin is Origin.HUMAN

        assert _wait_terminal(root, record.id) is GenomeTaskState.SUBMITTED
        assert (root / "repos" / "order-service" / ".git").exists()
        assert git(Path(workspace_repo), "show", "main:.gitmodules")
        # initializing 消失;研发任务可提。
        entries = {
            item["name"]: item for item in client.get("/workspaces").json()["entries"]
        }
        assert entries["shop"]["initializing"] is False
        submitted = client.post(
            "/tasks", json={"requirement": "第一个需求"}, headers={"x-workspace": "shop"}
        )
        assert submitted.status_code == 201, submitted.text

    def test_an_empty_remote_stays_initializing_and_refuses_dev_tasks(
        self, client: TestClient, home: Path, tmp_path: Path, workspace_repo: str
    ) -> None:
        """失败 clone 留下 `.git` 也不能把项目伪装成已经挂载完成。"""
        remote = tmp_path / "empty.git"
        git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))

        created = client.post(
            "/workspaces",
            json={"name": "empty", "workspace_repo": workspace_repo, "repos": [str(remote)]},
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["mount_task_id"]
        assert _wait_terminal(home / "empty", task_id) is GenomeTaskState.FAILED

        entries = {
            item["name"]: item for item in client.get("/workspaces").json()["entries"]
        }
        assert entries["empty"]["initializing"] is True
        refused = client.post(
            "/tasks", json={"requirement": "开始开发"}, headers={"x-workspace": "empty"}
        )
        assert refused.status_code == 409
        assert "初始化" in refused.json()["detail"]

    def test_bad_repo_fails_into_attention_queue_and_can_retry(
        self,
        client: TestClient,
        mall,
        home: Path,
        tmp_path: Path,
        workspace_repo: str,
    ) -> None:
        """单仓失败不放弃其余仓;人发起的失败进异常队列;修好后可重试。"""
        missing = tmp_path / "not-there.git"
        response = client.post(
            "/workspaces",
            json={
                "name": "shop",
                "workspace_repo": workspace_repo,
                "repos": [mall["order-service"].remote_url, str(missing)],
            },
        )
        assert response.status_code == 201, response.text
        root = home / "shop"
        task_id = response.json()["mount_task_id"]
        assert _wait_terminal(root, task_id) is GenomeTaskState.FAILED

        # 好的那个仓照常挂上;失败的没挂;任务在异常队列里。
        assert (root / "repos" / "order-service" / ".git").exists()
        assert not (root / "repos" / "not-there" / ".git").exists()
        attention = GenomeTaskStore(root).needs_attention()
        assert any(item.id == task_id for item in attention)

        # 初始化未完成:提交研发任务被拒且报错说明原因。
        refused = client.post(
            "/tasks", json={"requirement": "x"}, headers={"x-workspace": "shop"}
        )
        assert refused.status_code == 409
        assert "初始化" in refused.json()["detail"]

        # 修好远端:建出那个 bare 仓并给它一个提交——空仓挂不上(没有分支可 checkout),
        # 而"修好"的含义本来就是远端真的可用了。
        import subprocess

        def _git(*args: str, cwd: Path | None = None) -> None:
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                check=True,
                capture_output=True,
                cwd=cwd,
            )

        # `-b main`:bare 仓的 HEAD 要指向真的会被推的分支,否则 clone 下来没有分支可
        # checkout,与"远端还不存在"是两种不同的失败。
        _git("init", "-q", "--bare", "-b", "main", str(missing))
        seed = tmp_path / "seed"
        _git("init", "-q", "-b", "main", str(seed))
        (seed / "README.md").write_text("x", encoding="utf-8")
        _git("add", "-A", cwd=seed)
        _git("commit", "-q", "-m", "seed", cwd=seed)
        _git("push", "-q", str(missing), "main", cwd=seed)
        retried = client.post("/workspaces/shop/mount")
        assert retried.status_code == 202, retried.text
        assert _wait_terminal(root, retried.json()["id"]) is GenomeTaskState.SUBMITTED
        assert (
            client.post(
                "/tasks", json={"requirement": "x"}, headers={"x-workspace": "shop"}
            ).status_code
            == 201
        )

    def test_adopts_an_orphaned_workspace_directory(
        self,
        client: TestClient,
        mall,
        home: Path,
        tmp_path: Path,
        workspace_repo: str,
    ) -> None:
        """内存注册表 + 重启留下的孤儿:目录在、注册没了。再点一次"创建"要认领它,
        不是拿"目标位置已存在"把恢复的路堵死。"""
        created = client.post(
            "/workspaces",
            json={
                "name": "shop",
                "workspace_repo": workspace_repo,
                "repos": [mall["order-service"].remote_url],
            },
        ).json()
        _wait_terminal(home / "shop", created["mount_task_id"])

        # 模拟重启后注册丢失:全新的空注册表、同一个受管目录。
        reborn_registry = tmp_path / "reborn-registry.yaml"
        reborn = TestClient(
            create_app(
                workspaces=WorkspaceRegistry(),
                registry_path=reborn_registry,
                workspaces_home=home,
            )
        )
        assert reborn.get("/workspaces").json()["items"] == []

        response = reborn.post(
            "/workspaces",
            json={
                "name": "shop",
                "workspace_repo": workspace_repo,
                "repos": [mall["order-service"].remote_url],
            },
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["adopted"] is True
        # 业务仓早就挂齐了:不再建挂载任务,目录里的东西一样没动。
        assert payload["mount_task_id"] is None
        assert reborn.get("/workspaces").json()["items"] == ["shop"]
        assert load_registry(reborn_registry).names() == ["shop"]
        assert (home / "shop" / "repos" / "order-service" / ".git").exists()

    def test_refuses_to_adopt_an_unloadable_directory(
        self, client: TestClient, home: Path
    ) -> None:
        """加载不了的目录不认领:静默接进来的是一个第一个请求就 500 的项目。"""
        broken = home / "shop"
        broken.mkdir(parents=True)
        (broken / "agentgenome.yaml").write_text(
            "budgets: {per_task_tokens: -1}\n", encoding="utf-8"
        )
        response = client.post(
            "/workspaces",
            json={"name": "shop", "workspace_repo": "x.git", "repos": ["x.git"]},
        )
        assert response.status_code == 409
        assert "不能认领" in response.json()["detail"]

    def test_in_memory_mode_still_persists_to_the_default_registry(
        self,
        mall,
        home: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        workspace_repo: str,
    ) -> None:
        """没配注册表文件(单项目模式)建的项目也要留痕——孤儿就是这么来的。"""
        fallback = tmp_path / "default-registry.yaml"
        monkeypatch.setattr("agentgenome.server.app.DEFAULT_REGISTRY", fallback)
        client = TestClient(create_app(workspaces=WorkspaceRegistry(), workspaces_home=home))

        response = client.post(
            "/workspaces",
            json={
                "name": "shop",
                "workspace_repo": workspace_repo,
                "repos": [mall["order-service"].remote_url],
            },
        )
        assert response.status_code == 201, response.text
        assert load_registry(fallback).names() == ["shop"]
        _wait_terminal(home / "shop", response.json()["mount_task_id"])

    def test_duplicate_name_is_refused_without_half_a_workspace(
        self, client: TestClient, mall, home: Path, workspace_repo: str
    ) -> None:
        first = client.post(
            "/workspaces",
            json={
                "name": "shop",
                "workspace_repo": workspace_repo,
                "repos": [mall["order-service"].remote_url],
            },
        )
        assert first.status_code == 201
        again = client.post(
            "/workspaces",
            json={
                "name": "shop",
                "workspace_repo": workspace_repo,
                "repos": [mall["order-service"].remote_url],
            },
        )
        assert again.status_code == 409
        _wait_terminal(home / "shop", first.json()["mount_task_id"])

    def test_illegal_name_is_422(self, client: TestClient, mall, workspace_repo: str) -> None:
        response = client.post(
            "/workspaces",
            json={
                "name": "../etc",
                "workspace_repo": workspace_repo,
                "repos": [mall["order-service"].remote_url],
            },
        )
        assert response.status_code == 422

    def test_repos_are_required(self, client: TestClient) -> None:
        response = client.post(
            "/workspaces",
            json={"name": "shop", "workspace_repo": "x.git", "repos": []},
        )
        assert response.status_code == 422

    def test_workspace_repo_is_required(self, client: TestClient) -> None:
        response = client.post("/workspaces", json={"name": "shop", "repos": ["x.git"]})
        assert response.status_code == 422

    def test_retry_with_nothing_pending_is_409(
        self, client: TestClient, mall, home: Path, workspace_repo: str
    ) -> None:
        created = client.post(
            "/workspaces",
            json={
                "name": "shop",
                "workspace_repo": workspace_repo,
                "repos": [mall["order-service"].remote_url],
            },
        ).json()
        _wait_terminal(home / "shop", created["mount_task_id"])
        response = client.post("/workspaces/shop/mount")
        assert response.status_code == 409

    def test_retry_can_finish_only_the_workspace_push(
        self, client: TestClient, mall, home: Path, workspace_repo: str
    ) -> None:
        created = client.post(
            "/workspaces",
            json={
                "name": "shop",
                "workspace_repo": workspace_repo,
                "repos": [mall["order-service"].remote_url],
            },
        ).json()
        root = home / "shop"
        assert _wait_terminal(root, created["mount_task_id"]) is GenomeTaskState.SUBMITTED
        marker = root / WORKSPACE_PUSH_PENDING
        marker.write_text("simulate interrupted push", encoding="utf-8")

        retried = client.post("/workspaces/shop/mount")

        assert retried.status_code == 202, retried.text
        assert _wait_terminal(root, retried.json()["id"]) is GenomeTaskState.SUBMITTED
        assert not marker.exists()
