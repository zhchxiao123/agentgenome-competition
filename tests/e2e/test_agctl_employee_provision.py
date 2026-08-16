"""`agctl employee provision`:把花名册里的员工对齐成平台上的 Worker。

经**假供应实现**驱动——真实的命令行、真实的花名册加载、真实的事件流,
只有"平台"被替换掉。真实 HTTP 实现的测试在 `test_agentteams_provision.py`。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome import cli
from agentgenome.agents.agentteams.provision import (
    ProvisionError,
    ReconcileOutcome,
    WorkerRef,
    assemble_soul,
    worker_name,
)
from agentgenome.agents.agentteams.transport import PlatformUnavailable
from agentgenome.cli import app
from agentgenome.core.events import EventLog, LogKind
from agentgenome.employees import EmployeeConfig

runner = CliRunner()

ARCH = """\
id: arch-employee
name: 架构员工
runtime: agentteams
prompt: prompts/arch.md
procedures: [requirement-analysis]
"""

DEV_LOCAL = """\
id: dev-employee
runtime: claude-code
prompt: prompts/dev.md
procedures: [code-develop]
"""

CONFIG = """\
runtime:
  default: claude-code
  claude-code: {cmd: claude}
  agentteams:
    transport: matrix-minio
    endpoint: http://controller.example.com
    consumer_token_env: AGENTTEAMS_CONSUMER_TOKEN
    matrix_homeserver: http://matrix.example.com
    matrix_room: "!room:example.com"
    matrix_token_env: AGENTTEAMS_MATRIX_TOKEN
    storage_prefix: myminio/agentteams
    worker: alice
platform: {git_host: local}
"""


class FakeProvisioner:
    """按剧本行事的平台替身。记录每次对齐,便于断言"平台上发生了什么"。"""

    def __init__(self, fail: Exception | None = None) -> None:
        self.reconciled: list[str] = []
        self.souls: dict[str, str] = {}
        self.slept: list[str] = []
        self.deleted: list[str] = []
        self._fail = fail

    async def reconcile(self, employee: EmployeeConfig) -> ReconcileOutcome:
        if self._fail is not None:
            raise self._fail
        action = "unchanged" if employee.id in self.reconciled else "created"
        if action == "created":
            self.reconciled.append(employee.id)
        # 记住建成什么样,不只是"建过"——否则预览算不出真实差异。
        self.souls[employee.id] = assemble_soul(employee)
        return ReconcileOutcome(ref=self._ref(employee.id), action=action)

    def _ref(self, employee_id: str) -> WorkerRef:
        name = worker_name(employee_id)
        return WorkerRef(
            name=name,
            room_id=f"!room-{name}:example.com",
            phase="Running",
            soul=self.souls.get(employee_id, ""),
        )

    async def resolve(self, employee_id: str) -> WorkerRef | None:
        if employee_id not in self.reconciled:
            return None
        return self._ref(employee_id)

    async def wake(self, employee_id: str) -> WorkerRef:
        ref = await self.resolve(employee_id)
        assert ref is not None
        return ref

    async def sleep(self, employee_id: str) -> None:
        self.slept.append(employee_id)

    async def delete(self, employee_id: str) -> None:
        self.deleted.append(employee_id)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTTEAMS_CONSUMER_TOKEN", "sk-consumer-1")
    monkeypatch.setenv("AGENTTEAMS_MATRIX_TOKEN", "syt-matrix-1")
    root = tmp_path / "ws"
    prompts = root / "employees" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "arch.md").write_text("你负责项目认知,读代码补全地图。\n", encoding="utf-8")
    (prompts / "dev.md").write_text("你负责实现需求。\n", encoding="utf-8")
    for name, body in (("arch-employee", ARCH), ("dev-employee", DEV_LOCAL)):
        (root / "employees" / f"{name}.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    (root / "agentgenome.yaml").write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    return root


def _install(monkeypatch: pytest.MonkeyPatch, provisioner: object) -> None:
    """把装配那一步换成替身。命令行、花名册、事件流全走真实路径。"""
    monkeypatch.setattr(cli, "build_provisioner", lambda config: provisioner)


def _run(workspace: Path, *args: str):
    return runner.invoke(app, ["employee", "provision", *args, "--workspace", str(workspace)])


# --- 正常路径 ---------------------------------------------------------------


def test_provisioning_an_employee_creates_a_worker_and_reports_its_room(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeProvisioner()
    _install(monkeypatch, fake)

    result = _run(workspace, "arch-employee")

    assert result.exit_code == 0, result.output
    assert fake.reconciled == ["arch-employee"]
    assert worker_name("arch-employee") in result.output
    assert "!room-" in result.output, "房间要打印出来,人才知道它去哪儿了"


def test_the_provisioning_action_lands_in_the_event_stream(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """记录平面对编制变化没有例外。"""
    _install(monkeypatch, FakeProvisioner())

    _run(workspace, "arch-employee")

    kinds = [e.kind for e in EventLog(workspace).events("system")]
    assert LogKind.WORKER_PROVISIONED in kinds


def test_the_event_payload_carries_no_token(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeProvisioner())

    _run(workspace, "arch-employee")

    for event in EventLog(workspace).events("system"):
        rendered = str(event.payload)
        assert "sk-consumer-1" not in rendered
        assert "syt-matrix-1" not in rendered


# --- 拒绝与失败 -------------------------------------------------------------


def test_a_local_runtime_employee_is_refused_with_a_reason(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """混合部署下行为要可预期:跑在本地的员工没有 Worker 可言。"""
    fake = FakeProvisioner()
    _install(monkeypatch, fake)

    result = _run(workspace, "dev-employee")

    assert result.exit_code != 0
    assert "claude-code" in result.output or "本地" in result.output
    assert fake.reconciled == [], "不该把本地员工推上平台"


def test_an_unknown_employee_fails_clearly(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeProvisioner())

    result = _run(workspace, "查无此人")

    assert result.exit_code != 0
    assert "查无此人" in result.output


def test_platform_unavailability_points_at_the_platform(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeProvisioner(fail=PlatformUnavailable("连接被拒绝")))

    result = _run(workspace, "arch-employee")

    assert result.exit_code != 0
    assert "连接被拒绝" in result.output


def test_a_worker_that_never_starts_is_reported_as_such(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeProvisioner(fail=ProvisionError("Worker 在 180 秒内没有就绪")))

    result = _run(workspace, "arch-employee")

    assert result.exit_code != 0
    assert "就绪" in result.output


def test_a_missing_consumer_token_fails_at_assembly(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """凭证问题在动手之前暴露,并指出是哪个环境变量。"""
    monkeypatch.delenv("AGENTTEAMS_CONSUMER_TOKEN", raising=False)

    result = _run(workspace, "arch-employee")

    assert result.exit_code != 0
    assert "AGENTTEAMS_CONSUMER_TOKEN" in result.output


def test_a_workspace_without_the_agentteams_runtime_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    prompts = root / "employees" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "dev.md").write_text("你负责实现需求。\n", encoding="utf-8")
    (root / "employees" / "dev-employee.yaml").write_text(
        textwrap.dedent(DEV_LOCAL), encoding="utf-8"
    )
    (root / "agentgenome.yaml").write_text("platform: {git_host: local}\n", encoding="utf-8")

    result = _run(root, "dev-employee")

    assert result.exit_code != 0
    assert "agentteams" in result.output


# --- 整份花名册与预览(issue 02)----------------------------------------------


def test_no_argument_provisions_the_whole_roster(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeProvisioner()
    _install(monkeypatch, fake)

    result = _run(workspace)

    assert result.exit_code == 0, result.output
    assert fake.reconciled == ["arch-employee"], "只对齐容器员工"
    assert "dev-employee" in result.output, "跳过了谁也要说出来"


def test_running_twice_reports_nothing_changed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """幂等要看得见:管理员要能一眼确认"这次什么都没动"。"""
    fake = FakeProvisioner()
    _install(monkeypatch, fake)
    _run(workspace)

    result = _run(workspace)

    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output or "无变化" in result.output


def test_dry_run_plans_without_touching_the_platform(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeProvisioner()
    _install(monkeypatch, fake)

    result = _run(workspace, "--dry-run")

    assert result.exit_code == 0, result.output
    assert fake.reconciled == [], "预览不该产生任何平台动作"
    assert "arch-employee" in result.output


def test_dry_run_leaves_no_event_behind(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没发生的事不该进记录平面。"""
    _install(monkeypatch, FakeProvisioner())

    _run(workspace, "--dry-run")

    assert list(EventLog(workspace).events("system")) == []


def test_one_failure_does_not_sink_the_rest(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个员工失败,其余照常处理,末尾汇总——否则一次小故障会让整份花名册停在半路。"""
    (workspace / "employees" / "prompts" / "qa.md").write_text("你负责质量。\n", encoding="utf-8")
    (workspace / "employees" / "qa-employee.yaml").write_text(
        "id: qa-employee\nruntime: agentteams\nprompt: prompts/qa.md\nprocedures: []\n",
        encoding="utf-8",
    )

    class Picky(FakeProvisioner):
        async def reconcile(self, employee: EmployeeConfig):
            if employee.id == "arch-employee":
                raise ProvisionError("这一个起不来")
            return await super().reconcile(employee)

    fake = Picky()
    _install(monkeypatch, fake)

    result = _run(workspace)

    assert result.exit_code != 0, "有失败就该以非零退出"
    assert fake.reconciled == ["qa-employee"], "另一个照常处理"
    assert "arch-employee" in result.output
    assert "这一个起不来" in result.output


# --- 生命周期(issue 05)------------------------------------------------------


def test_sleep_stops_the_workers_of_the_named_employees(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeProvisioner()
    _install(monkeypatch, fake)

    result = _run(workspace, "arch-employee", "--sleep")

    assert result.exit_code == 0, result.output
    assert fake.slept == ["arch-employee"]
    assert fake.reconciled == [], "休眠不该顺带创建"


def test_delete_removes_the_workers_of_the_named_employees(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeProvisioner()
    _install(monkeypatch, fake)

    result = _run(workspace, "arch-employee", "--delete")

    assert result.exit_code == 0, result.output
    assert fake.deleted == ["arch-employee"]


def test_sleep_and_delete_together_are_a_contradiction(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删掉的东西没法休眠——两个都给说明使用者没想清楚要哪个。"""
    fake = FakeProvisioner()
    _install(monkeypatch, fake)

    result = _run(workspace, "arch-employee", "--sleep", "--delete")

    assert result.exit_code != 0
    assert fake.slept == [] and fake.deleted == []


# --- 评审修正 ---------------------------------------------------------------


def test_an_unknown_model_tier_fails_before_touching_the_platform(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未知档位是配置错误,该在动手之前拦下——而不是让一半员工已经推上去了才炸。"""
    (workspace / "employees" / "arch-employee.yaml").write_text(
        textwrap.dedent(ARCH) + "model: chaep\n", encoding="utf-8"
    )
    config = (workspace / "agentgenome.yaml").read_text(encoding="utf-8")
    (workspace / "agentgenome.yaml").write_text(
        config.replace(
            "    worker: alice\n",
            "    worker: alice\n    model_tiers:\n      cheap: {model: qwen-turbo}\n",
        ),
        encoding="utf-8",
    )
    fake = FakeProvisioner()
    _install(monkeypatch, fake)

    result = _run(workspace, "arch-employee")

    assert result.exit_code != 0
    assert "chaep" in result.output
    assert fake.reconciled == [], "配置错误不该已经推上去一半"


def test_dry_run_says_which_are_new_and_which_are_updates(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"将对齐"对管理员没有信息量——他要知道的是这次会新建还是更新。"""
    fake = FakeProvisioner()
    _install(monkeypatch, fake)
    _run(workspace)  # 先建起来

    result = _run(workspace, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output or "无变化" in result.output


def test_sleep_lands_in_the_event_stream(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """休眠也是编制变化。记录平面对它没有例外。"""
    _install(monkeypatch, FakeProvisioner())

    _run(workspace, "arch-employee", "--sleep")

    payloads = [
        e.payload
        for e in EventLog(workspace).events("system")
        if e.kind is LogKind.WORKER_PROVISIONED
    ]
    assert any(p.get("action") == "slept" for p in payloads)
