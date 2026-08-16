"""agentteams 运行时的装配路径:配置 → 注册表 → 能力矩阵 → 会话拒绝 → 泳道派发。

装配错误必须在配置装配层暴露且指向配置段——不是任务跑一半才炸。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agentgenome.agents.agentteams import AgentTeamsRuntime
from agentgenome.agents.capabilities import capabilities_of
from agentgenome.agents.factory import (
    RuntimeAssemblyError,
    build_runtimes,
    build_session_runtimes,
    why_no_session,
)
from agentgenome.agents.pool import AgentPool
from agentgenome.agents.runtime import JobSpec
from agentgenome.config import Config, RuntimeConfig, RuntimeEntry, load_config
from agentgenome.core.events import EventLog, LogKind
from agentgenome.genome.errors import GenomeValidationError
from tests.fixtures.fake_agentteams import FakeTransport
from tests.fixtures.fake_worker_platform import FakeWorkerPlatform
from tests.unit.test_agentteams_runtime import _ok_outcome

TOKEN_ENV = "AGENTTEAMS_CONSUMER_TOKEN"


def _write(tmp_path: Path, config: str) -> Path:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    (root / "agentgenome.yaml").write_text(textwrap.dedent(config))
    return root


def _agentteams_config() -> Config:
    return Config(
        runtime=RuntimeConfig(
            default="claude-code",
            runtimes={
                "claude-code": RuntimeEntry(cmd="claude"),
                "agentteams": RuntimeEntry(
                    endpoint="https://teams.example.com", consumer_token_env=TOKEN_ENV
                ),
            },
        )
    )


# --- 配置解析 ---------------------------------------------------------------


def test_config_parses_a_flat_agentteams_entry(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        """\
        runtime:
          default: claude-code
          claude-code: {cmd: claude}
          agentteams:
            endpoint: https://teams.example.com
            consumer_token_env: AGENTTEAMS_CONSUMER_TOKEN
        """,
    )

    config = load_config(root)

    entry = config.runtime.runtimes["agentteams"]
    assert entry.endpoint == "https://teams.example.com"
    assert entry.consumer_token_env == "AGENTTEAMS_CONSUMER_TOKEN"


def test_an_agentteams_entry_without_an_endpoint_is_rejected_at_parse(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        """\
        runtime:
          default: claude-code
          claude-code: {cmd: claude}
          agentteams: {consumer_token_env: AGENTTEAMS_CONSUMER_TOKEN}
        """,
    )

    with pytest.raises(GenomeValidationError, match="endpoint"):
        load_config(root)


def test_an_agentteams_entry_with_a_cmd_is_a_contradiction(tmp_path: Path) -> None:
    """agentteams 不是本地 CLI——写了 cmd 说明使用者理解错了模型,必须当场纠正。"""
    root = _write(
        tmp_path,
        """\
        runtime:
          default: claude-code
          claude-code: {cmd: claude}
          agentteams:
            cmd: agentteams
            endpoint: https://teams.example.com
            consumer_token_env: AGENTTEAMS_CONSUMER_TOKEN
        """,
    )

    with pytest.raises(GenomeValidationError, match="cmd"):
        load_config(root)


def test_a_cli_entry_still_requires_a_cmd(tmp_path: Path) -> None:
    """cmd 变为可选不能放松既有 CLI 条目的校验——漏配 cmd 仍要在装配层报错。"""
    root = _write(
        tmp_path,
        """\
        runtime:
          default: claude-code
          claude-code: {max_turns: 40}
        """,
    )

    with pytest.raises(GenomeValidationError, match="cmd"):
        load_config(root)


# --- 工厂装配 ---------------------------------------------------------------


def test_factory_builds_an_agentteams_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    monkeypatch.setenv(TOKEN_ENV, "sk-consumer-1")

    runtimes = build_runtimes(_agentteams_config())

    assert isinstance(runtimes["agentteams"], AgentTeamsRuntime)


def test_a_missing_consumer_token_env_fails_assembly_and_points_at_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)

    with pytest.raises(RuntimeAssemblyError, match=TOKEN_ENV):
        build_runtimes(_agentteams_config())


# --- 能力矩阵与会话 ---------------------------------------------------------


def test_agentteams_declares_it_cannot_open_sessions() -> None:
    profile = capabilities_of("agentteams")

    assert profile is not None
    assert profile.sessions is False


def test_session_registry_excludes_agentteams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    monkeypatch.setenv(TOKEN_ENV, "sk-consumer-1")

    sessions = build_session_runtimes(_agentteams_config())

    assert "agentteams" not in sessions
    assert "claude-code" in sessions


def test_why_no_session_tells_the_user_to_pick_another_employee() -> None:
    """指向"换员工",不是"改配置"——按提示去改配置当然没用,而错误信息一个字都不变。"""
    reason = why_no_session(_agentteams_config(), "agentteams")

    assert "换一个" in reason


# --- matrix-minio 传输的装配 -------------------------------------------------

MATRIX_ENV = "AGENTTEAMS_MATRIX_TOKEN"

MATRIX_MINIO_YAML = """\
runtime:
  default: claude-code
  claude-code: {cmd: claude}
  agentteams:
    transport: matrix-minio
    endpoint: https://controller.example.com
    consumer_token_env: AGENTTEAMS_CONSUMER_TOKEN
    matrix_homeserver: https://matrix.example.com
    matrix_room: "!room:example.com"
    matrix_token_env: AGENTTEAMS_MATRIX_TOKEN
    storage_prefix: myminio/agentteams
    worker: alice
    mc_cmd: /nonexistent/mc
"""


def test_config_parses_a_matrix_minio_entry(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, MATRIX_MINIO_YAML))

    entry = config.runtime.runtimes["agentteams"]
    assert entry.transport == "matrix-minio"
    assert entry.storage_prefix == "myminio/agentteams"
    assert entry.worker == "alice"


def test_matrix_minio_without_a_storage_prefix_is_rejected(tmp_path: Path) -> None:
    yaml = MATRIX_MINIO_YAML.replace("    storage_prefix: myminio/agentteams\n", "")

    with pytest.raises(GenomeValidationError, match="storage_prefix"):
        load_config(_write(tmp_path, yaml))


def test_matrix_fields_on_the_http_transport_are_a_contradiction(tmp_path: Path) -> None:
    """写了 matrix 字段却留在 http 传输,说明使用者只改了一半——当场纠正。"""
    yaml = MATRIX_MINIO_YAML.replace("transport: matrix-minio", "transport: http")

    with pytest.raises(GenomeValidationError, match="transport"):
        load_config(_write(tmp_path, yaml))


def test_an_unknown_transport_name_is_rejected(tmp_path: Path) -> None:
    yaml = MATRIX_MINIO_YAML.replace("matrix-minio", "matrix-mini0")

    with pytest.raises(GenomeValidationError, match="transport"):
        load_config(_write(tmp_path, yaml))


def test_factory_builds_the_matrix_minio_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """行为性证明选到了 matrix-minio:预检先查 mc,配置里给了不存在的路径就该报它。"""
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    monkeypatch.setenv(TOKEN_ENV, "sk-consumer-1")
    monkeypatch.setenv(MATRIX_ENV, "syt-matrix-1")
    config = load_config(_write(tmp_path, MATRIX_MINIO_YAML))

    runtime = build_runtimes(config)["agentteams"]

    from agentgenome.agents.agentteams import PlatformUnavailable

    assert isinstance(runtime, AgentTeamsRuntime)
    with pytest.raises(PlatformUnavailable, match="mc"):
        runtime.preflight()


def test_a_missing_matrix_token_env_fails_assembly_and_points_at_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    monkeypatch.setenv(TOKEN_ENV, "sk-consumer-1")
    monkeypatch.delenv(MATRIX_ENV, raising=False)
    config = load_config(_write(tmp_path, MATRIX_MINIO_YAML))

    with pytest.raises(RuntimeAssemblyError, match=MATRIX_ENV):
        build_runtimes(config)


# --- 泳道派发 ---------------------------------------------------------------


def _pool_spec(tmp_path: Path, task_id: str) -> JobSpec:
    workdir = tmp_path / task_id / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    context = tmp_path / task_id / "context.md"
    context.write_text("# 上下文包\n", encoding="utf-8")
    return JobSpec(
        task_id=task_id,
        employee_id="dev-employee",
        procedure_id="code-develop",
        procedure_version="1.0.0",
        round=1,
        workdir=workdir,
        context_file=context,
        output_dir=tmp_path / task_id / "out",
        timeout_s=5,
    )


async def test_both_lanes_dispatch_through_the_same_pool(tmp_path: Path) -> None:
    """研发任务与基因组任务共用执行池——进化链路不因运行时不同而分叉。"""
    pool = AgentPool({"agentteams": AgentTeamsRuntime(FakeTransport([_ok_outcome()]))})

    dev = await pool.submit(_pool_spec(tmp_path, "ag-7"), "agentteams")
    genome = await pool.submit(_pool_spec(tmp_path, "gn-3"), "agentteams")

    assert dev.ok is True
    assert genome.ok is True


# --- 全局 worker/房间变为可选兜底(issue 03)---------------------------------


def test_matrix_minio_without_worker_and_room_resolves_per_employee(tmp_path: Path) -> None:
    """不配全局 worker/房间 = 按员工解析。角色隔离在容器一侧因此才成立。"""
    yaml = MATRIX_MINIO_YAML.replace('    matrix_room: "!room:example.com"\n', "").replace(
        "    worker: alice\n", ""
    )

    config = load_config(_write(tmp_path, yaml))

    entry = config.runtime.runtimes["agentteams"]
    assert entry.worker is None
    assert entry.matrix_room is None


def test_half_configured_fixed_routing_is_refused(tmp_path: Path) -> None:
    """只给一半是配了一半:静默按另一半的缺省走,会让"为什么没发到那个房间"
    要查到三层之外的默认值。"""
    only_worker = MATRIX_MINIO_YAML.replace('    matrix_room: "!room:example.com"\n', "")

    with pytest.raises(GenomeValidationError, match="matrix_room|worker"):
        load_config(_write(tmp_path, only_worker))

    only_room = MATRIX_MINIO_YAML.replace("    worker: alice\n", "")

    with pytest.raises(GenomeValidationError, match="matrix_room|worker"):
        load_config(_write(tmp_path, only_room))


def test_the_factory_picks_per_employee_resolution_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentgenome.agents.agentteams.directory import FixedDirectory, ProvisionedDirectory

    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    monkeypatch.setenv(TOKEN_ENV, "sk-consumer-1")
    monkeypatch.setenv(MATRIX_ENV, "syt-matrix-1")
    yaml = MATRIX_MINIO_YAML.replace('    matrix_room: "!room:example.com"\n', "").replace(
        "    worker: alice\n", ""
    )
    config = load_config(_write(tmp_path, yaml))

    runtime = build_runtimes(config)["agentteams"]
    directory = runtime._transport._directory  # type: ignore[attr-defined]

    assert isinstance(directory, ProvisionedDirectory)
    assert not isinstance(directory, FixedDirectory)


async def test_the_workspace_factory_auto_provisions_and_records_a_missing_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生产装配必须把员工真相源与事件面接进目录,不能只有目录单测能自动供应。"""
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    monkeypatch.setenv(TOKEN_ENV, "sk-consumer-1")
    monkeypatch.setenv(MATRIX_ENV, "syt-matrix-1")
    with FakeWorkerPlatform() as platform:
        yaml = (
            MATRIX_MINIO_YAML.replace("https://controller.example.com", platform.url)
            .replace('    matrix_room: "!room:example.com"\n', "")
            .replace("    worker: alice\n", "")
        )
        root = _write(tmp_path, yaml)
        employees = root / "employees"
        (employees / "prompts").mkdir(parents=True)
        (employees / "prompts" / "dev.md").write_text("你是开发员工。\n", encoding="utf-8")
        (employees / "dev-employee.yaml").write_text(
            "id: dev-employee\nruntime: agentteams\nprompt: prompts/dev.md\n",
            encoding="utf-8",
        )
        runtime = build_runtimes(load_config(root), root)["agentteams"]
        directory = runtime._transport._directory  # type: ignore[attr-defined]

        ref = await directory.locate("dev-employee")

    assert ref.name == "agenome-dev-employee"
    events = list(EventLog(root).all_events(kind=LogKind.WORKER_PROVISIONED))
    assert len(events) == 1
    assert events[0].payload["employee_id"] == "dev-employee"
    assert events[0].payload["action"] == "created"
    assert events[0].payload["entrance"] == "auto"


def test_the_factory_keeps_fixed_routing_for_existing_deployments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既有部署行为逐字节不变。"""
    from agentgenome.agents.agentteams.directory import FixedDirectory

    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    monkeypatch.setenv(TOKEN_ENV, "sk-consumer-1")
    monkeypatch.setenv(MATRIX_ENV, "syt-matrix-1")
    config = load_config(_write(tmp_path, MATRIX_MINIO_YAML))

    runtime = build_runtimes(config)["agentteams"]

    assert isinstance(runtime._transport._directory, FixedDirectory)  # type: ignore[attr-defined]


# --- 模型档位映射(issue 04)--------------------------------------------------

TIERED_YAML = MATRIX_MINIO_YAML + """\
    model_tiers:
      cheap: {model: qwen-turbo, provider: dashscope}
      strong: {model: qwen-max}
"""


def test_config_parses_the_model_tier_table(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, TIERED_YAML))

    tiers = config.runtime.runtimes["agentteams"].model_tiers
    assert tiers["cheap"].model == "qwen-turbo"
    assert tiers["cheap"].provider == "dashscope"
    assert tiers["strong"].provider == ""


def test_model_tiers_on_a_local_cli_entry_are_refused(tmp_path: Path) -> None:
    """档位映射是平台字段——写在本地 CLI 条目上说明使用者放错了地方。"""
    yaml = """\
    runtime:
      default: claude-code
      claude-code:
        cmd: claude
        model_tiers:
          cheap: {model: qwen-turbo}
    """

    with pytest.raises(GenomeValidationError, match="model_tiers|平台字段"):
        load_config(_write(tmp_path, yaml))
