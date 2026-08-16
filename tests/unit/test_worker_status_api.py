"""容器状态与供应计划:界面上看清"谁准备好了"与"这次点下去会发生什么"。

**这一片只读。** 执行在 issue 04。经假供应实现驱动,不起真实平台。
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
    worker_name,
)
from agentgenome.agents.agentteams.transport import PlatformUnavailable
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


class FakeProvisioner:
    """按剧本行事的平台替身,记住建成什么样——否则计划算不出真实差异。"""

    def __init__(self, fail: Exception | None = None) -> None:
        self.souls: dict[str, str] = {}
        self.sleeping: set[str] = set()
        self._fail = fail

    def _ref(self, employee_id: str) -> WorkerRef:
        name = worker_name(employee_id)
        return WorkerRef(
            name=name,
            room_id=f"!room-{name}:example.com",
            phase="Sleeping" if employee_id in self.sleeping else "Running",
            sleeping=employee_id in self.sleeping,
            soul=self.souls.get(employee_id, ""),
        )

    async def reconcile(self, employee: EmployeeConfig) -> ReconcileOutcome:
        if self._fail is not None:
            raise self._fail
        action = "unchanged" if employee.id in self.souls else "created"
        self.souls[employee.id] = assemble_soul(employee)
        return ReconcileOutcome(ref=self._ref(employee.id), action=action)

    async def resolve(self, employee_id: str) -> WorkerRef | None:
        if self._fail is not None:
            raise self._fail
        return self._ref(employee_id) if employee_id in self.souls else None

    async def wake(self, employee_id: str) -> WorkerRef:
        self.sleeping.discard(employee_id)
        return self._ref(employee_id)

    async def sleep(self, employee_id: str) -> None:
        self.sleeping.add(employee_id)

    async def delete(self, employee_id: str) -> None:
        self.souls.pop(employee_id, None)


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


# --- 状态 -------------------------------------------------------------------


def test_an_unprovisioned_employee_reports_as_such(workspace: Path) -> None:
    response = _client(workspace, FakeProvisioner()).get("/employees/workers")

    assert response.status_code == 200, response.text
    rows = {row["employee_id"]: row for row in response.json()["items"]}
    assert rows["arch-employee"]["status"] == "absent"
    assert rows["arch-employee"]["worker"] == ""


def test_a_provisioned_employee_reports_its_worker_and_room(workspace: Path) -> None:
    fake = FakeProvisioner()
    fake.souls["arch-employee"] = "# 已经建过\n"

    rows = _client(workspace, fake).get("/employees/workers").json()["items"]

    row = next(item for item in rows if item["employee_id"] == "arch-employee")
    assert row["status"] == "running"
    assert row["worker"] == worker_name("arch-employee")
    assert row["room"].startswith("!room-")


def test_a_sleeping_worker_is_distinguishable_from_a_running_one(workspace: Path) -> None:
    """休眠与运行中要分得开:一个是省钱,一个是在岗。"""
    fake = FakeProvisioner()
    fake.souls["arch-employee"] = "x"
    fake.sleeping.add("arch-employee")

    rows = _client(workspace, fake).get("/employees/workers").json()["items"]

    row = next(item for item in rows if item["employee_id"] == "arch-employee")
    assert row["status"] == "sleeping"


def test_local_runtime_employees_have_no_container_status(workspace: Path) -> None:
    """跑在本地的员工这一格是空的,不是"未供应"——那会让人以为该去供应它。"""
    rows = _client(workspace, FakeProvisioner()).get("/employees/workers").json()["items"]

    assert [row["employee_id"] for row in rows] == ["arch-employee"]


def test_the_platform_being_down_is_reported_not_swallowed(workspace: Path) -> None:
    fake = FakeProvisioner(fail=PlatformUnavailable("连接被拒绝"))

    rows = _client(workspace, fake).get("/employees/workers").json()["items"]

    row = next(item for item in rows if item["employee_id"] == "arch-employee")
    assert row["status"] == "unknown"
    assert "连接被拒绝" in row["detail"]


def test_the_status_is_asked_of_the_platform_every_time(workspace: Path) -> None:
    """真机实测:Worker 重建会换房间 id。缓存的那个会静默过期。"""
    fake = FakeProvisioner()
    fake.souls["arch-employee"] = "x"
    client = _client(workspace, fake)
    first = client.get("/employees/workers").json()["items"][0]["room"]

    fake._ref = lambda employee_id: WorkerRef(  # type: ignore[method-assign]
        name=worker_name(employee_id), room_id="!room-rebuilt:x", phase="Running"
    )
    second = client.get("/employees/workers").json()["items"][0]["room"]

    assert first != second
    assert second == "!room-rebuilt:x"


# --- 计划 -------------------------------------------------------------------


def test_the_plan_says_what_would_happen_for_each_employee(workspace: Path) -> None:
    response = _client(workspace, FakeProvisioner()).get("/employees/workers/plan")

    assert response.status_code == 200, response.text
    rows = {row["employee_id"]: row["action"] for row in response.json()["items"]}
    assert rows["arch-employee"] == "created"


def test_the_plan_recognises_an_already_aligned_employee(workspace: Path) -> None:
    """平台上已经是这个样子时报"无变化"——而这个样子由供应层同一条判据算。"""
    employee = load_employees(workspace_employees_root(workspace)).get("arch-employee")
    fake = FakeProvisioner()
    fake.souls["arch-employee"] = assemble_soul(employee)

    rows = _client(workspace, fake).get("/employees/workers/plan").json()["items"]

    assert rows[0]["action"] == "unchanged"


def test_a_drifted_employee_is_planned_as_an_update(workspace: Path) -> None:
    """身份文件漂了要报"更新",不是"无变化"——否则人以为不必点,而它一直跑着旧的。"""
    fake = FakeProvisioner()
    fake.souls["arch-employee"] = "# 一份过时的身份\n"

    rows = _client(workspace, fake).get("/employees/workers/plan").json()["items"]

    assert rows[0]["action"] == "updated"


def test_the_plan_writes_nothing(workspace: Path) -> None:
    """**否定断言**:计划是只读的,平台侧零写动作。"""
    fake = FakeProvisioner()

    _client(workspace, fake).get("/employees/workers/plan")

    assert fake.souls == {}, "计划不该创建任何 Worker"


def test_a_platform_failure_during_planning_is_surfaced(workspace: Path) -> None:
    fake = FakeProvisioner(fail=ProvisionError("平台受理了但没起来"))

    rows = _client(workspace, fake).get("/employees/workers/plan").json()["items"]

    assert rows[0]["action"] == "unknown"
    assert "没起来" in rows[0]["detail"]


# --- 可用性 -----------------------------------------------------------------


def test_someone_without_the_permission_still_sees_the_state(workspace: Path) -> None:
    """看得到、点不动。**可用性由服务端算**——前端复刻一遍权限矩阵,复刻错的那份要到
    点下去才说话。"""
    fake = FakeProvisioner()
    fake.souls["arch-employee"] = "x"
    client = _client(
        workspace,
        fake,
        principals={"dev": Principal(subject="dev", roles=frozenset({Role.DEVELOPER}))},
    )

    body = client.get("/employees/workers", headers={"x-actor": "dev"}).json()

    assert body["items"][0]["status"] == "running"
    assert body["can_provision"] is False


def test_an_admin_may_provision(workspace: Path) -> None:
    client = _client(
        workspace,
        FakeProvisioner(),
        principals={"root": Principal(subject="root", roles=frozenset({Role.ADMIN}))},
    )

    body = client.get("/employees/workers", headers={"x-actor": "root"}).json()

    assert body["can_provision"] is True
