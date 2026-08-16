"""容器运行时的界面化:配置段可改 + 就绪检查分项报。

配置写入**沿用既有的设置写入路径**(落盘 → 严格校验 → 提交 → 事件),这里只测
"这一段现在可改了"以及"就绪检查怎么报"。就绪检查经"真实的假平台"与假 mc 驱动,
不起任何真实平台。
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from agentgenome.agents.agentteams.readiness import ReadinessItem, check_readiness
from agentgenome.config import RuntimeEntry, load_config
from agentgenome.core.events import EventLog, LogKind
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.server.rbac import Principal, Role
from agentgenome.server.settings import EDITABLE, Entrance
from agentgenome.server.settings import update as update_settings
from agentgenome.space.gitcmd import ORCHESTRATOR_IDENTITY, git
from tests.fixtures import fake_mc
from tests.fixtures.fake_worker_platform import FakeWorkerPlatform

CONSUMER_ENV = "AGENTTEAMS_CONSUMER_TOKEN"
MATRIX_ENV = "AGENTTEAMS_MATRIX_TOKEN"


# --- 白名单 -----------------------------------------------------------------


def test_the_runtime_section_is_editable_from_the_console() -> None:
    """容器运行时要能在界面上配完——这是 PRD 33 的全部前提。"""
    assert "runtime" in EDITABLE


def test_the_editable_whitelist_still_refuses_unknown_sections() -> None:
    """白名单仍然是白名单:放开一段不等于放开全部。"""
    assert "platform" not in EDITABLE
    assert "evidence" not in EDITABLE


# --- 就绪检查 ---------------------------------------------------------------


def _entry(platform: FakeWorkerPlatform, **overrides: Any) -> RuntimeEntry:
    fields: dict[str, Any] = {
        "transport": "matrix-minio",
        "endpoint": platform.url,
        "consumer_token_env": CONSUMER_ENV,
        "matrix_homeserver": platform.url,
        "matrix_token_env": MATRIX_ENV,
        "storage_prefix": "myminio/agentteams",
        "mc_cmd": f"{sys.executable} {fake_mc.__file__}",
    }
    fields.update(overrides)
    return RuntimeEntry(**fields)


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "remote"
    root.mkdir()
    monkeypatch.setenv("FAKE_MC_ROOT", str(root))
    monkeypatch.setenv("FAKE_MC_PREFIX", "myminio/agentteams")
    monkeypatch.delenv("FAKE_MC_FAIL", raising=False)
    monkeypatch.setenv(CONSUMER_ENV, "sk-consumer-1")
    monkeypatch.setenv(MATRIX_ENV, "syt-matrix-1")
    return root


def _by_name(items: tuple[ReadinessItem, ...]) -> dict[str, ReadinessItem]:
    return {item.name: item for item in items}


async def test_a_healthy_deployment_reports_every_item_ok(storage: Path) -> None:
    with FakeWorkerPlatform() as platform:
        report = await check_readiness(_entry(platform))

    assert report.ok is True
    assert {item.name for item in report.items} >= {"platform", "storage", "matrix", "credentials"}
    assert all(item.ok for item in report.items)


async def test_each_item_is_reported_separately(storage: Path) -> None:
    """合成一个布尔的话,"哪一项挂了"就答不出来——而这几项指向不同的运维动作。"""
    with FakeWorkerPlatform(fail_status=500) as platform:
        report = await check_readiness(_entry(platform))

    items = _by_name(report.items)
    assert items["platform"].ok is False
    assert items["storage"].ok is True, "平台挂了不影响存储的结论"
    assert report.ok is False


async def test_a_broken_storage_does_not_hide_a_healthy_platform(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MC_FAIL", "AccessDenied: 凭证过期")

    with FakeWorkerPlatform() as platform:
        report = await check_readiness(_entry(platform))

    items = _by_name(report.items)
    assert items["storage"].ok is False
    assert "凭证过期" in items["storage"].detail
    assert items["platform"].ok is True


async def test_a_missing_credential_names_the_environment_variable(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """要说清是**哪个变量**,而不是笼统的"鉴权失败"——人得知道去哪台机器上补。"""
    monkeypatch.delenv(MATRIX_ENV, raising=False)

    with FakeWorkerPlatform() as platform:
        report = await check_readiness(_entry(platform))

    credentials = _by_name(report.items)["credentials"]
    assert credentials.ok is False
    assert MATRIX_ENV in credentials.detail


async def test_no_report_item_leaks_a_token_value(storage: Path) -> None:
    with FakeWorkerPlatform() as platform:
        report = await check_readiness(_entry(platform))

    rendered = " ".join(f"{item.name}{item.detail}" for item in report.items)
    assert "sk-consumer-1" not in rendered
    assert "syt-matrix-1" not in rendered


async def test_credentials_are_checked_without_calling_anything(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """凭证在不在是本地就能答的问题。平台整个挂掉时它照样要有结论。"""
    monkeypatch.delenv(CONSUMER_ENV, raising=False)

    with FakeWorkerPlatform(fail_status=503) as platform:
        report = await check_readiness(_entry(platform))

    credentials = _by_name(report.items)["credentials"]
    assert credentials.ok is False
    assert CONSUMER_ENV in credentials.detail


# --- 配置往返(经既有写入路径)-----------------------------------------------


SECTION = {
    "default": "claude-code",
    "claude-code": {"cmd": "claude"},
    "agentteams": {
        "transport": "matrix-minio",
        "endpoint": "http://controller.example.com",
        "consumer_token_env": CONSUMER_ENV,
        "matrix_homeserver": "http://matrix.example.com",
        "matrix_token_env": MATRIX_ENV,
        "storage_prefix": "agentteams/agentteams-storage/shared",
    },
}

ROOT = Principal(subject="root", roles=frozenset({Role.ADMIN}))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个真的 git 仓库。**版本面是这条路径的一半**,拿非仓库测等于只测了另一半。"""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "agentgenome.yaml").write_text(
        textwrap.dedent("runtime:\n  default: claude-code\n  claude-code: {cmd: claude}\n"),
        encoding="utf-8",
    )
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, *ORCHESTRATOR_IDENTITY, "commit", "-q", "-m", "init")
    return root


def test_a_runtime_section_written_from_the_console_loads_back(repo: Path) -> None:
    """界面写下去的那一段必须是能加载的——否则下一次启动整个工作区起不来。

    **经真实写入路径**(`settings.update`),不是手写一份 YAML 再读回来:后者测的是
    yaml 与 pydantic,而这一条要守的是"界面那条路写出来的东西能加载"。
    """
    update_settings(repo, ROOT, "runtime", SECTION, entrance=Entrance.WEB)

    config = load_config(repo)

    assert config.runtime.runtimes["agentteams"].transport == "matrix-minio"
    assert config.runtime.runtimes["agentteams"].consumer_token_env == CONSUMER_ENV


def test_the_saved_section_is_committed(repo: Path) -> None:
    """内容进 git:**改成了什么去这个提交里看**,事件面只记动作。"""
    change = update_settings(repo, ROOT, "runtime", SECTION, entrance=Entrance.WEB)

    assert change.rev, "没拿到提交 sha"
    dirty = git(repo, "status", "--porcelain", "--", "agentgenome.yaml").stdout.strip()
    assert dirty == "", "配置改了但没被提交进去"
    committed = git(repo, "show", f"{change.rev}:agentgenome.yaml").stdout
    assert "controller.example.com" in committed, "提交里没有这次改的内容"


def test_the_save_lands_on_the_event_plane(repo: Path) -> None:
    update_settings(repo, ROOT, "runtime", SECTION, entrance=Entrance.WEB)

    events = list(EventLog(repo).all_events(kind=LogKind.CONFIG_CHANGED))
    assert [event.payload["section"] for event in events] == ["runtime"]
    assert events[0].payload["entrance"] == "web"


def test_a_contradictory_section_is_refused_and_the_file_rolled_back(repo: Path) -> None:
    """**否定断言**:校验不过时盘上那份必须一个字节没变。

    提交一份加载不了的配置,等于把下一次启动押在没人会去看的 git 历史上。
    """
    before = (repo / "agentgenome.yaml").read_text(encoding="utf-8")
    broken = {"default": "claude-code", "claude-code": {"cmd": ["不是一个字符串"]}}

    with pytest.raises(GenomeValidationError):
        update_settings(repo, ROOT, "runtime", broken, entrance=Entrance.WEB)

    assert (repo / "agentgenome.yaml").read_text(encoding="utf-8") == before
    assert list(EventLog(repo).all_events(kind=LogKind.CONFIG_CHANGED)) == []


# --- REST:就绪检查端点 ------------------------------------------------------


def _app(root: Path, checker: Any):
    """把就绪检查换成替身。路由、鉴权、序列化全走真实路径。"""
    from agentgenome.server.app import create_app

    app = create_app(root)
    app.state.readiness_checker = checker
    return app


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "agentgenome.yaml").write_text(
        textwrap.dedent(
            """\
            runtime:
              default: claude-code
              claude-code: {cmd: claude}
              agentteams:
                transport: matrix-minio
                endpoint: http://controller.example.com
                consumer_token_env: AGENTTEAMS_CONSUMER_TOKEN
                matrix_homeserver: http://matrix.example.com
                matrix_token_env: AGENTTEAMS_MATRIX_TOKEN
                storage_prefix: agentteams/agentteams-storage/shared
            platform: {git_host: local}
            """
        ),
        encoding="utf-8",
    )
    return root


def test_the_readiness_endpoint_reports_each_item(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from agentgenome.agents.agentteams.readiness import ReadinessReport

    async def checker(entry: Any) -> ReadinessReport:
        return ReadinessReport(
            items=(
                ReadinessItem("platform", True, "可达"),
                ReadinessItem("storage", False, "桶列不出来"),
                ReadinessItem("matrix", True, "令牌有效"),
                ReadinessItem("credentials", True, "都有值"),
            )
        )

    client = TestClient(_app(_workspace(tmp_path), checker))

    response = client.post("/settings/container-runtime/readiness")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is False
    by_name = {item["name"]: item for item in payload["items"]}
    assert by_name["platform"]["ok"] is True
    assert by_name["storage"]["ok"] is False
    assert "桶列不出来" in by_name["storage"]["detail"]


def test_the_readiness_endpoint_says_so_when_the_runtime_is_not_configured(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    root = tmp_path / "plain"
    root.mkdir()
    (root / "agentgenome.yaml").write_text("platform: {git_host: local}\n", encoding="utf-8")

    async def checker(entry: Any) -> Any:  # pragma: no cover - 不该被调
        raise AssertionError("没配容器运行时就不该去探测")

    client = TestClient(_app(root, checker))

    response = client.post("/settings/container-runtime/readiness")

    assert response.status_code == 400
    assert "agentteams" in response.text


def test_the_settings_view_exposes_the_runtime_section(tmp_path: Path) -> None:
    """界面要"改一个已知状态",不是"重填一份配置"——重填会把没打算动的字段写成默认值。"""
    from agentgenome.server.models import SettingsView

    view = SettingsView.of(load_config(_workspace(tmp_path)), can_edit=True)

    assert view.runtime.runtimes["agentteams"].endpoint == "http://controller.example.com"


def test_the_settings_view_carries_env_var_names_not_token_values(tmp_path: Path) -> None:
    """运行时段存的是**环境变量名**,值只在服务端进程里——所以端出这一段不等于端出密钥。"""
    from agentgenome.server.models import SettingsView

    view = SettingsView.of(load_config(_workspace(tmp_path)), can_edit=True)

    entry = view.runtime.runtimes["agentteams"]
    assert entry.consumer_token_env == "AGENTTEAMS_CONSUMER_TOKEN"
    assert "sk-" not in view.model_dump_json()
