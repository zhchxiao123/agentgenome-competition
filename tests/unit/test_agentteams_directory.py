"""按员工解析 Worker 与房间:让角色隔离在容器一侧也成立。

**不落盘**是这一层的核心主张——平台是房间的真相源,而 Worker 重建会换房间
(真机实测撞见过)。所以解析每个进程问一次平台,进程内缓存,不写任何本地文件。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from agentgenome.agents.agentteams.directory import (
    FixedDirectory,
    ProvisionedDirectory,
)
from agentgenome.agents.agentteams.provision import (
    HttpWorkerProvisioner,
    ReconcileOutcome,
    WorkerRef,
    worker_name,
)
from agentgenome.employees import EmployeeConfig
from tests.fixtures.fake_worker_platform import FakeWorkerPlatform


def _employee(employee_id: str = "arch-employee") -> EmployeeConfig:
    return EmployeeConfig(
        id=employee_id,
        runtime="agentteams",
        prompt="prompts/x.md",
        prompt_text=f"你是 {employee_id}。\n",
    )


class CountingProvisioner:
    """记账用的替身:解析被调了几次。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.woke: list[str] = []
        self.reconciled: list[str] = []

    async def reconcile(self, employee: EmployeeConfig) -> ReconcileOutcome:
        self.reconciled.append(employee.id)
        name = worker_name(employee.id)
        return ReconcileOutcome(
            ref=WorkerRef(name=name, room_id=f"!room-{name}:x", phase="Running"),
            action="created",
        )

    async def resolve(self, employee_id: str) -> WorkerRef | None:
        self.calls.append(employee_id)
        name = worker_name(employee_id)
        return WorkerRef(name=name, room_id=f"!room-{name}:x", phase="Running")

    async def wake(self, employee_id: str) -> WorkerRef:
        self.woke.append(employee_id)
        name = worker_name(employee_id)
        return WorkerRef(name=name, room_id=f"!room-{name}:x", phase="Running")


# --- 按员工解析 -------------------------------------------------------------


async def test_two_employees_resolve_to_their_own_rooms() -> None:
    """角色隔离在容器一侧成立的判据:两个员工不再共用一个房间。"""
    directory = ProvisionedDirectory(CountingProvisioner())

    arch = await directory.locate("arch-employee")
    dev = await directory.locate("dev-employee")

    assert arch.room_id != dev.room_id
    assert arch.name != dev.name


async def test_resolution_is_cached_within_a_process() -> None:
    """既不为每个 Job 多打一次平台……"""
    provisioner = CountingProvisioner()
    directory = ProvisionedDirectory(provisioner)

    await directory.locate("arch-employee")
    await directory.locate("arch-employee")

    assert provisioner.calls == ["arch-employee"]


async def test_a_fresh_directory_sees_a_rebuilt_workers_new_room() -> None:
    """……也不会跨进程陈旧:真机实测,Worker 重建后房间 id 会变。"""
    with FakeWorkerPlatform() as platform:
        provisioner = HttpWorkerProvisioner(
            endpoint=platform.url, token="t", poll_interval_s=0.01, ready_timeout_s=1.0
        )
        await provisioner.reconcile(_employee())
        first = await ProvisionedDirectory(provisioner).locate("arch-employee")

        # 平台侧重建:同名 Worker,新房间。
        name = worker_name("arch-employee")
        platform.workers[name]["roomID"] = "!room-rebuilt:x"

        second = await ProvisionedDirectory(provisioner).locate("arch-employee")

    assert first.room_id != second.room_id
    assert second.room_id == "!room-rebuilt:x"


async def test_an_employee_without_a_worker_is_provisioned_before_dispatch() -> None:
    """员工已声明容器运行时就是供应授权,不应再把任务打回给 CLI 用户。"""

    class Empty(CountingProvisioner):
        async def resolve(self, employee_id: str) -> WorkerRef | None:
            return None

    provisioner = Empty()
    recorded: list[tuple[str, str]] = []

    ref = await ProvisionedDirectory(
        provisioner,
        employee_provider=_employee,
        on_reconciled=lambda employee_id, outcome: recorded.append(
            (employee_id, outcome.action)
        ),
    ).locate("arch-employee")

    assert ref.phase == "Running"
    assert provisioner.reconciled == ["arch-employee"]
    assert recorded == [("arch-employee", "created")]


async def test_concurrent_first_jobs_only_provision_one_worker() -> None:
    """两个并发 Job 同时撞上空平台时只能创建一次,不能竞态出两个容器。"""

    class Empty(CountingProvisioner):
        async def resolve(self, employee_id: str) -> WorkerRef | None:
            return None

    provisioner = Empty()
    directory = ProvisionedDirectory(provisioner, employee_provider=_employee)

    first, second = await asyncio.gather(
        directory.locate("arch-employee"),
        directory.locate("arch-employee"),
    )

    assert first == second
    assert provisioner.reconciled == ["arch-employee"]


# --- 固定兜底(既有部署)-----------------------------------------------------


async def test_a_fixed_directory_always_answers_the_configured_pair() -> None:
    """只配了全局 worker/房间的既有部署行为逐字节不变。"""
    directory = FixedDirectory(worker="alice", room_id="!room:example.com")

    for employee_id in ("arch-employee", "dev-employee"):
        ref = await directory.locate(employee_id)
        assert ref.name == "alice"
        assert ref.room_id == "!room:example.com"


# --- 与传输的接线 -----------------------------------------------------------


async def test_the_transport_sends_each_employee_to_its_own_room(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端:两个员工的 Job 分别落到各自的房间。"""
    import asyncio
    import json

    from agentgenome.agents.agentteams.matrix_minio import MatrixMinioTransport
    from agentgenome.agents.agentteams.mirror import MinioMirror
    from agentgenome.agents.agentteams.transport import TransportJob
    from tests.fixtures import fake_mc
    from tests.fixtures.stub_http import StubServer

    prefix = "myminio/agentteams"
    remote = tmp_path / "remote"
    remote.mkdir()
    monkeypatch.setenv("FAKE_MC_ROOT", str(remote))
    monkeypatch.setenv("FAKE_MC_PREFIX", prefix)
    monkeypatch.delenv("FAKE_MC_FAIL", raising=False)

    async def play_worker(ref: str) -> None:
        task_dir = remote / "tasks" / ref
        for _ in range(500):
            if (task_dir / "meta.json").is_file():
                break
            await asyncio.sleep(0.01)
        (task_dir / "artifacts").mkdir(exist_ok=True)
        (task_dir / "artifacts" / "result.json").write_text("{}", encoding="utf-8")
        (task_dir / "meta.json").write_text(json.dumps({"status": "SUCCESS"}), encoding="utf-8")

    def _job(employee_id: str) -> TransportJob:
        return TransportJob(
            task_id="ag-1",
            employee_id=employee_id,
            procedure_ref="code-develop@1.0.0",
            round=1,
            attempt=1,
            subject="",
            context_text="# 上下文包\n",
            workspace={},
        )

    with StubServer() as stub:
        transport = MatrixMinioTransport(
            controller_endpoint=stub.url,
            controller_token="t",
            matrix_homeserver=stub.url,
            matrix_token="syt",
            directory=ProvisionedDirectory(CountingProvisioner()),
            mirror=MinioMirror(mc_argv=[sys.executable, fake_mc.__file__], prefix=prefix),
            poll_interval_s=0.02,
        )
        for employee_id in ("arch-employee", "dev-employee"):
            job = _job(employee_id)
            from agentgenome.agents.agentteams.taskdoc import task_ref

            worker = asyncio.create_task(play_worker(task_ref(job)))
            await asyncio.wait_for(transport.run_job(job), 10)
            await worker

        rooms = [
            r["path"] for r in stub.records if "/send/m.room.message/" in r["path"]
        ]

    assert len(rooms) == 2
    assert rooms[0] != rooms[1], "两个员工的通知必须发到各自的房间"


# --- 派发时自动唤醒(issue 05)------------------------------------------------


async def test_a_sleeping_worker_is_woken_before_dispatch() -> None:
    """休眠是纯成本优化。不自动唤醒的话,它会表现为"派下去没反应"——
    那是最难归因的一类症状。"""

    class Sleepy:
        def __init__(self) -> None:
            self.woke: list[str] = []
            self._asleep = True

        async def reconcile(self, employee: EmployeeConfig) -> Any:  # pragma: no cover
            raise AssertionError("解析路径不该触发对齐")

        async def resolve(self, employee_id: str) -> WorkerRef | None:
            name = worker_name(employee_id)
            return WorkerRef(
                name=name, room_id=f"!room-{name}:x",
                phase="Sleeping" if self._asleep else "Running",
                sleeping=self._asleep,
            )

        async def wake(self, employee_id: str) -> WorkerRef:
            self.woke.append(employee_id)
            self._asleep = False
            name = worker_name(employee_id)
            return WorkerRef(name=name, room_id=f"!room-{name}:x", phase="Running")

    provisioner = Sleepy()

    ref = await ProvisionedDirectory(provisioner).locate("arch-employee")

    assert provisioner.woke == ["arch-employee"]
    assert ref.sleeping is False


async def test_a_running_worker_is_not_disturbed_by_the_wake_call() -> None:
    """真实实现的 wake 先查状态,已在跑的直接返回、不发唤醒请求
    (见 test_waking_an_already_running_worker_does_not_disturb_it)。"""
    with FakeWorkerPlatform() as platform:
        provisioner = HttpWorkerProvisioner(
            endpoint=platform.url, token="t", poll_interval_s=0.01, ready_timeout_s=1.0
        )
        await provisioner.reconcile(_employee())
        before = len(platform.records)

        await ProvisionedDirectory(provisioner).locate("arch-employee")
        wakes = [r for r in platform.records[before:] if r["path"].endswith("/wake")]

    assert wakes == []


async def test_a_worker_that_falls_asleep_after_being_cached_is_still_woken() -> None:
    """缓存的是身份,不是"它醒着"这个事实。管理员随时可能 `--sleep`,
    而缓存命中直接返回的话,消息会发给一个停掉的容器且没有任何报错。"""

    class SleepsLater:
        def __init__(self) -> None:
            self.resolves = 0
            self.woke: list[str] = []
            self.asleep = False

        async def reconcile(self, employee: EmployeeConfig) -> Any:  # pragma: no cover
            raise AssertionError("解析路径不该触发对齐")

        async def resolve(self, employee_id: str) -> WorkerRef | None:
            self.resolves += 1
            name = worker_name(employee_id)
            return WorkerRef(name=name, room_id=f"!room-{name}:x", phase="Running")

        async def wake(self, employee_id: str) -> WorkerRef:
            if self.asleep:
                self.woke.append(employee_id)
                self.asleep = False
            name = worker_name(employee_id)
            return WorkerRef(name=name, room_id=f"!room-{name}:x", phase="Running")

    provisioner = SleepsLater()
    directory = ProvisionedDirectory(provisioner)
    await directory.locate("arch-employee")

    provisioner.asleep = True  # 管理员在两次派发之间把它睡了
    await directory.locate("arch-employee")

    assert provisioner.woke == ["arch-employee"], "第二次派发必须把它叫醒"
    assert provisioner.resolves == 1, "身份仍然只解析一次"
