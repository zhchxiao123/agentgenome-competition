"""从界面回收容器:休眠与删除。

休眠可逆(下一次派发会自动唤醒),所以它是**纯粹的成本动作**;删除不可逆——房间会重建、
id 会变——所以它要一次显式确认。两个都进事件面。经假供应实现驱动,不起真实平台。
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
from agentgenome.employees import EmployeeConfig
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


#: 平台上别人建的那个 Worker。**名字不带我们的前缀**,所以它是"外人"的判据是真的
#: 前缀判断,不是测试里另编的一个标记。
FOREIGN = "qa-engineer"


class FakeProvisioner:
    """平台替身。

    **整个平台只有一张 Worker 表**,我们的和别人的都在里面——按 Worker 名索引,与真平台
    一样。这一点是这组测试的关键:表分成两张的话,"没动到外人的"永远成立,那条断言就
    再也不会红。所有权由前缀判据守(`is_ours`),与 `HttpWorkerProvisioner._own` 同一条。
    """

    def __init__(self, fail: Exception | None = None) -> None:
        self.workers: dict[str, str] = {FOREIGN: "人工建的 QA 工程师"}
        self.sleeping: set[str] = set()
        self._fail = fail

    def _ref(self, employee_id: str) -> WorkerRef:
        name = worker_name(employee_id)
        return WorkerRef(
            name=name,
            room_id=f"!room-{name}:example.com",
            phase="Sleeping" if name in self.sleeping else "Running",
            sleeping=name in self.sleeping,
            soul=self.workers.get(name, ""),
        )

    def _own(self, name: str) -> str:
        """要动一个 Worker 之前先问它是不是我们的。**不是的一律不碰。**"""
        if not is_ours(name):
            raise ProvisionError(f"拒绝操作一个不属于我们的 Worker: {name!r}")
        return name

    async def reconcile(self, employee: EmployeeConfig) -> ReconcileOutcome:
        name = self._own(worker_name(employee.id))
        self.workers[name] = assemble_soul(employee)
        return ReconcileOutcome(ref=self._ref(employee.id), action="created")

    async def resolve(self, employee_id: str) -> WorkerRef | None:
        return self._ref(employee_id) if worker_name(employee_id) in self.workers else None

    async def wake(self, employee_id: str) -> WorkerRef:
        self.sleeping.discard(worker_name(employee_id))
        return self._ref(employee_id)

    async def sleep(self, employee_id: str) -> None:
        if self._fail is not None:
            raise self._fail
        self.sleeping.add(self._own(worker_name(employee_id)))

    async def delete(self, employee_id: str) -> None:
        if self._fail is not None:
            raise self._fail
        name = self._own(worker_name(employee_id))
        self.workers.pop(name, None)
        self.sleeping.discard(name)


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


def _provisioned(root: Path, fake: FakeProvisioner) -> TestClient:
    client = _client(root, fake)
    client.post("/employees/arch-employee/worker")
    return client


def _lifecycle_events(root: Path) -> list[Any]:
    return [
        event
        for event in EventLog(root).all_events()
        if event.kind == LogKind.WORKER_PROVISIONED
        and event.payload.get("action") in {"slept", "deleted"}
    ]


# --- 休眠 -------------------------------------------------------------------


def test_sleeping_a_worker_needs_no_confirmation(workspace: Path) -> None:
    """休眠可逆:下一次派发会自动唤醒,所以它是纯粹的成本动作,不该逼人多点一次。"""
    fake = FakeProvisioner()
    client = _provisioned(workspace, fake)

    response = client.post("/employees/arch-employee/worker/sleep")

    assert response.status_code == 200, response.text
    assert worker_name("arch-employee") in fake.sleeping


def test_a_slept_worker_shows_as_sleeping_in_the_list(workspace: Path) -> None:
    fake = FakeProvisioner()
    client = _provisioned(workspace, fake)
    client.post("/employees/arch-employee/worker/sleep")

    rows = client.get("/employees/workers").json()["items"]

    assert rows[0]["status"] == "sleeping"


def test_sleeping_lands_on_the_event_plane(workspace: Path) -> None:
    fake = FakeProvisioner()

    _provisioned(workspace, fake).post("/employees/arch-employee/worker/sleep")

    events = _lifecycle_events(workspace)
    assert [event.payload["action"] for event in events] == ["slept"]
    assert events[0].payload["employee_id"] == "arch-employee"


# --- 删除 -------------------------------------------------------------------


def test_deleting_without_confirmation_does_nothing(workspace: Path) -> None:
    """**否定断言**:不可逆的动作不该被一次手滑的请求做掉。房间会重建、id 会变。"""
    fake = FakeProvisioner()
    client = _provisioned(workspace, fake)

    response = client.delete("/employees/arch-employee/worker")

    assert response.status_code == 400, response.text
    assert worker_name("arch-employee") in fake.workers, "没确认就不该删"


def test_deleting_with_confirmation_removes_the_worker(workspace: Path) -> None:
    fake = FakeProvisioner()
    client = _provisioned(workspace, fake)

    response = client.delete("/employees/arch-employee/worker?confirm=true")

    assert response.status_code == 200, response.text
    assert worker_name("arch-employee") not in fake.workers


def test_a_deleted_worker_shows_as_absent(workspace: Path) -> None:
    fake = FakeProvisioner()
    client = _provisioned(workspace, fake)
    client.delete("/employees/arch-employee/worker?confirm=true")

    rows = client.get("/employees/workers").json()["items"]

    assert rows[0]["status"] == "absent"


def test_deleting_lands_on_the_event_plane(workspace: Path) -> None:
    """删除尤其要记:它不可逆,而"谁删的"是事后唯一能问的问题。"""
    fake = FakeProvisioner()

    _provisioned(workspace, fake).delete("/employees/arch-employee/worker?confirm=true")

    events = _lifecycle_events(workspace)
    assert [event.payload["action"] for event in events] == ["deleted"]


def test_neither_action_touches_a_worker_we_did_not_create(workspace: Path) -> None:
    """**否定断言**:平台上人工建的容器必须原样存活。

    这里刻意让花名册里有一个 id 与平台上那个外人 Worker **同名**(`qa-engineer`):这条
    测试要能红,就得让"服务端把裸 id 当成 Worker 名传下去"变成一个真的会撞车的错误。
    正确的行为是所有权前缀把它挪开——动的是 `agenome-qa-engineer`,外人那个一个字节没变。
    """
    (workspace / "employees" / "prompts" / "qa.md").write_text("你负责质量。\n", encoding="utf-8")
    (workspace / "employees" / f"{FOREIGN}.yaml").write_text(
        f"id: {FOREIGN}\nruntime: agentteams\nprompt: prompts/qa.md\nprocedures: []\n",
        encoding="utf-8",
    )
    fake = FakeProvisioner()
    client = _client(workspace, fake)
    client.post(f"/employees/{FOREIGN}/worker")

    client.post(f"/employees/{FOREIGN}/worker/sleep")
    client.delete(f"/employees/{FOREIGN}/worker?confirm=true")

    assert fake.workers == {FOREIGN: "人工建的 QA 工程师"}, "外人的 Worker 被动了"
    assert worker_name(FOREIGN) not in fake.workers, "我们自己那个才是该被删掉的"


# --- 边界 -------------------------------------------------------------------


def test_a_local_runtime_employee_has_nothing_to_recycle(workspace: Path) -> None:
    response = _client(workspace, FakeProvisioner()).post("/employees/dev-employee/worker/sleep")

    assert response.status_code == 422, response.text


def test_a_platform_failure_is_reported_and_leaves_no_record(workspace: Path) -> None:
    """**没做成就不该留下记录**——事后按事件面复盘时,那条记录会指向一件没发生的事。"""
    fake = FakeProvisioner(fail=PlatformUnavailable("连接被拒绝"))

    response = _client(workspace, fake).post("/employees/arch-employee/worker/sleep")

    assert response.status_code == 503
    assert "连接被拒绝" in response.json()["detail"]
    assert _lifecycle_events(workspace) == []


def test_recycling_needs_the_settings_permission(workspace: Path) -> None:
    fake = FakeProvisioner()
    client = _client(
        workspace,
        fake,
        principals={"dev": Principal(subject="dev", roles=frozenset({Role.DEVELOPER}))},
    )

    slept = client.post("/employees/arch-employee/worker/sleep", headers={"x-actor": "dev"})
    removed = client.delete(
        "/employees/arch-employee/worker?confirm=true", headers={"x-actor": "dev"}
    )

    assert slept.status_code == 403
    assert removed.status_code == 403


def test_the_record_names_who_did_it(workspace: Path) -> None:
    principals = {"root": Principal(subject="root", roles=frozenset({Role.ADMIN}))}
    client = _client(workspace, FakeProvisioner(), principals=principals)
    client.post("/employees/arch-employee/worker", headers={"x-actor": "root"})

    client.post("/employees/arch-employee/worker/sleep", headers={"x-actor": "root"})

    assert _lifecycle_events(workspace)[0].actor == "root"
