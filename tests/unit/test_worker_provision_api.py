"""从界面供应一个员工:真的去平台对齐,并且留痕。

**按员工一次一个。** 整份花名册由调用方逐个走完——那样进度是真的(第几个做完了),
而不是一个转到底的圈;一个失败也天然不拖垮其余。经假供应实现驱动,不起真实平台。
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentgenome.agents.agentteams.provision import (
    ProvisionError,
    ReconcileOutcome,
    WorkerRef,
    assemble_soul,
    is_ours,
    worker_name,
)
from agentgenome.agents.agentteams.transport import PlatformUnavailable
from agentgenome.core.events import EventLog, LogKind
from agentgenome.employees import EmployeeConfig, load_employees, workspace_employees_root
from agentgenome.server.app import create_app
from agentgenome.server.rbac import Principal, Role

ARCH = """\
id: arch-employee
runtime: agentteams
prompt: prompts/arch.md
procedures: []
"""

DEV_LOCAL = """\
id: dev-employee
runtime: claude-code
prompt: prompts/dev.md
procedures: []
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
    matrix_token_env: AGENTTEAMS_MATRIX_TOKEN
    storage_prefix: myminio/agentteams
platform: {git_host: local}
"""


#: 平台上别人建的那个 Worker。**名字不带我们的前缀**,所以"它是外人"这件事由真的前缀
#: 判断说了算,不是测试里另编的一个标记。
FOREIGN = "qa-engineer"


class FakeProvisioner:
    """平台替身。

    **整个平台只有一张 Worker 表**,我们的和别人的都在里面,按 Worker 名索引——与真平台
    一样。表分成两张的话,"没动到外人的"永远成立,那条断言就再也不会红。
    """

    def __init__(self, fail: Exception | None = None) -> None:
        self.workers: dict[str, str] = {FOREIGN: "人工建的 QA 工程师"}
        self.writes: list[str] = []
        self._fail = fail

    def _ref(self, employee_id: str) -> WorkerRef:
        name = worker_name(employee_id)
        return WorkerRef(
            name=name,
            room_id=f"!room-{name}:example.com",
            phase="Running",
            soul=self.workers.get(name, ""),
        )

    async def reconcile(self, employee: EmployeeConfig) -> ReconcileOutcome:
        if self._fail is not None:
            raise self._fail
        name = worker_name(employee.id)
        if not is_ours(name):  # pragma: no cover - 前缀是常量
            raise AssertionError("供应层放过了一个不属于我们的名字")
        wanted = assemble_soul(employee)
        if self.workers.get(name) == wanted:
            return ReconcileOutcome(ref=self._ref(employee.id), action="unchanged")
        action = "updated" if name in self.workers else "created"
        self.writes.append(f"{action}:{employee.id}")
        self.workers[name] = wanted
        return ReconcileOutcome(ref=self._ref(employee.id), action=action)

    async def resolve(self, employee_id: str) -> WorkerRef | None:
        return self._ref(employee_id) if worker_name(employee_id) in self.workers else None

    async def wake(self, employee_id: str) -> WorkerRef:
        return self._ref(employee_id)

    async def sleep(self, employee_id: str) -> None:  # pragma: no cover - 这一片不回收
        raise AssertionError("供应路径不该碰生命周期动作")

    async def delete(self, employee_id: str) -> None:  # pragma: no cover - 这一片不回收
        raise AssertionError("供应路径不该碰生命周期动作")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    root = tmp_path / "ws"
    prompts = root / "employees" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "arch.md").write_text("你负责项目认知。\n", encoding="utf-8")
    (prompts / "dev.md").write_text("你负责实现需求。\n", encoding="utf-8")
    for name, body in (("arch-employee", ARCH), ("dev-employee", DEV_LOCAL)):
        (root / "employees" / f"{name}.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    (root / "agentgenome.yaml").write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    return root


def _client(
    root: Path, provisioner: Any, principals: dict[str, Principal] | None = None
) -> TestClient:
    app = create_app(root, principals=principals)
    app.state.provisioner = provisioner
    return TestClient(app)


def _events(root: Path) -> list[Any]:
    return [
        event for event in EventLog(root).all_events() if event.kind == LogKind.WORKER_PROVISIONED
    ]


# --- 执行 -------------------------------------------------------------------


def test_provisioning_an_employee_creates_its_worker(workspace: Path) -> None:
    fake = FakeProvisioner()

    response = _client(workspace, fake).post("/employees/arch-employee/worker")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "created"
    assert body["worker"] == worker_name("arch-employee")
    assert body["room"].startswith("!room-")
    assert worker_name("arch-employee") in fake.workers


def test_provisioning_twice_writes_nothing_the_second_time(workspace: Path) -> None:
    """**幂等**:命令要能放进部署脚本反复执行,界面上的按钮也一样。"""
    fake = FakeProvisioner()
    client = _client(workspace, fake)

    client.post("/employees/arch-employee/worker")
    second = client.post("/employees/arch-employee/worker").json()

    assert second["action"] == "unchanged"
    assert fake.writes == ["created:arch-employee"], "第二次不该有任何写动作"


def test_a_drifted_employee_is_updated_not_recreated(workspace: Path) -> None:
    """重建会换掉房间,而在跑的任务还指着旧房间。"""
    fake = FakeProvisioner()
    fake.workers[worker_name("arch-employee")] = "# 一份过时的身份\n"

    body = _client(workspace, fake).post("/employees/arch-employee/worker").json()

    assert body["action"] == "updated"


def test_a_local_runtime_employee_is_refused_not_silently_skipped(workspace: Path) -> None:
    """点了名却什么都没发生,使用者会以为成功了——与命令行同一条规矩。"""
    fake = FakeProvisioner()

    response = _client(workspace, fake).post("/employees/dev-employee/worker")

    assert response.status_code == 422, response.text
    assert "claude-code" in response.json()["detail"]
    assert fake.workers == {FOREIGN: "人工建的 QA 工程师"}


def test_an_unknown_employee_is_a_404(workspace: Path) -> None:
    response = _client(workspace, FakeProvisioner()).post("/employees/nobody/worker")

    assert response.status_code == 404


def test_a_platform_failure_is_reported_with_the_reason(workspace: Path) -> None:
    fake = FakeProvisioner(fail=PlatformUnavailable("连接被拒绝"))

    response = _client(workspace, fake).post("/employees/arch-employee/worker")

    assert response.status_code == 503, response.text
    assert "连接被拒绝" in response.json()["detail"]


def test_one_failure_does_not_touch_the_others(workspace: Path) -> None:
    """一个员工失败不拖垮其余——**逐个调用**,所以这天然成立,但要有测试守住。"""
    fake = FakeProvisioner()
    client = _client(workspace, fake)

    client.post("/employees/dev-employee/worker")  # 422
    ok = client.post("/employees/arch-employee/worker")

    assert ok.status_code == 200
    assert worker_name("arch-employee") in fake.workers


# --- 所有权 -----------------------------------------------------------------


def test_a_worker_we_did_not_create_is_never_touched(workspace: Path) -> None:
    """**否定断言**:平台上人工建的容器必须原样存活。

    这里刻意让花名册里有一个 id 与平台上那个外人 Worker **同名**(`qa-engineer`),否则
    这条断言不可能红:两边名字不撞的话,"没动到外人的"是自动成立的,守不住任何东西。
    正确的行为是所有权前缀把它挪开——建出来的是 `agenome-qa-engineer`。
    """
    (workspace / "employees" / "prompts" / "qa.md").write_text("你负责质量。\n", encoding="utf-8")
    (workspace / "employees" / f"{FOREIGN}.yaml").write_text(
        f"id: {FOREIGN}\nruntime: agentteams\nprompt: prompts/qa.md\nprocedures: []\n",
        encoding="utf-8",
    )
    fake = FakeProvisioner()

    _client(workspace, fake).post(f"/employees/{FOREIGN}/worker")

    assert fake.workers[FOREIGN] == "人工建的 QA 工程师", "外人的身份文件被覆盖了"
    assert worker_name(FOREIGN) in fake.workers, "我们自己那个才是该被建出来的"


# --- 留痕 -------------------------------------------------------------------


def test_the_action_lands_on_the_event_plane(workspace: Path) -> None:
    fake = FakeProvisioner()

    _client(workspace, fake).post("/employees/arch-employee/worker")

    events = _events(workspace)
    assert len(events) == 1
    assert events[0].payload["employee_id"] == "arch-employee"
    assert events[0].payload["action"] == "created"
    assert events[0].payload["worker"] == worker_name("arch-employee")


def test_nothing_happening_leaves_no_record(workspace: Path) -> None:
    """**否定断言**:什么都没做就不该留下记录——与命令行入口同一条规矩。"""
    employee = load_employees(workspace_employees_root(workspace)).get("arch-employee")
    fake = FakeProvisioner()
    fake.workers[worker_name("arch-employee")] = assemble_soul(employee)

    _client(workspace, fake).post("/employees/arch-employee/worker")

    assert _events(workspace) == []


def test_the_record_carries_no_token(workspace: Path) -> None:
    """**否定断言**:事件面要被人和报告反复读,令牌进去了就再也收不回来。"""
    _client(workspace, FakeProvisioner()).post("/employees/arch-employee/worker")

    payload = _events(workspace)[0].payload
    assert set(payload) == {"employee_id", "worker", "room", "action", "entrance"}


def test_the_record_names_who_did_it(workspace: Path) -> None:
    client = _client(
        workspace,
        FakeProvisioner(),
        principals={"root": Principal(subject="root", roles=frozenset({Role.ADMIN}))},
    )

    client.post("/employees/arch-employee/worker", headers={"x-actor": "root"})

    assert _events(workspace)[0].actor == "root"


# --- 权限 -------------------------------------------------------------------


def test_provisioning_needs_the_settings_permission(workspace: Path) -> None:
    """**沿用改设置那一个动作,不新增权限项。** 前端藏起按钮不是安全边界。"""
    fake = FakeProvisioner()
    client = _client(
        workspace,
        fake,
        principals={"dev": Principal(subject="dev", roles=frozenset({Role.DEVELOPER}))},
    )

    response = client.post("/employees/arch-employee/worker", headers={"x-actor": "dev"})

    assert response.status_code == 403, response.text
    assert fake.workers == {FOREIGN: "人工建的 QA 工程师"}


def test_provisioning_when_the_platform_says_it_accepted_but_never_came_up(
    workspace: Path,
) -> None:
    fake = FakeProvisioner(fail=ProvisionError("Worker 在 180 秒内没有就绪"))

    response = _client(workspace, fake).post("/employees/arch-employee/worker")

    assert response.status_code == 503
    assert "没有就绪" in response.json()["detail"]
    assert _events(workspace) == []
