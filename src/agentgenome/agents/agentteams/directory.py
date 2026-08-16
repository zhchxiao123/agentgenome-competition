"""按员工找到该派给哪个 Worker、发到哪个房间。

## 为什么解析而不是配置

早先根配置里是一对全局的 `worker` / `matrix_room`,于是**所有员工的 Job 都挤在同一个
容器里**——角色隔离在容器这一侧根本不成立。更糟的是房间 id 会过期:Worker 一旦重建,
房间就换了(真机实测撞见过),而抄在配置里的那个还指着旧房间——派发石沉大海且没有报错。

所以房间**不落盘**:平台是它的真相源,每个进程问一次、进程内缓存。缓存不跨进程,
因此"重建换房间"这件事在下一次运行时自动跟上,不需要任何人去改配置。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from agentgenome.agents.agentteams.provision import (
    ReconcileOutcome,
    WorkerProvisioner,
    WorkerRef,
)
from agentgenome.employees import EmployeeConfig

EmployeeProvider = Callable[[str], EmployeeConfig]
ReconcileObserver = Callable[[str, ReconcileOutcome], None]


class WorkerNotProvisioned(RuntimeError):
    """这个员工在平台上还没有 Worker,且运行时没有 Workspace 上下文可供自动创建。

    正常的服务端与 CLI 派发都会传 Workspace 根,只在直接装配一个无根运行时的测试或
    嵌入调用中可能出现。
    """


class WorkerDirectory:
    """员工 → Worker 的解析。两个实现:按员工解析,或固定一对(既有部署的兜底)。"""

    async def locate(self, employee_id: str) -> WorkerRef:  # pragma: no cover - 接口
        raise NotImplementedError


class ProvisionedDirectory(WorkerDirectory):
    """向平台解析我们供应出的 Worker。"""

    def __init__(
        self,
        provisioner: WorkerProvisioner,
        employee_provider: EmployeeProvider | None = None,
        on_reconciled: ReconcileObserver | None = None,
    ) -> None:
        self._provisioner = provisioner
        self._employee_provider = employee_provider
        self._on_reconciled = on_reconciled
        #: 进程内缓存。**不落盘**——见模块文档。
        self._cache: dict[str, WorkerRef] = {}
        #: 首次派发可能并发撞上一个尚未供应的员工。平台对齐虽是幂等的,两个并发
        #: `POST` 仍会竞态;每员工一把锁把「查 → 建 → 缓存」收成一个动作。
        self._provision_locks: dict[str, asyncio.Lock] = {}

    async def locate(self, employee_id: str) -> WorkerRef:
        """解析并**确保它醒着**。

        缓存的是**身份**(名字与房间),不是"它醒着"这个易变事实——管理员随时可能
        休眠一批 Worker,而缓存命中就直接返回的话,消息会发给一个停掉的容器且没有
        任何报错。所以唤醒每次都确认一遍(实现里对已在跑的是空操作)。
        """
        if employee_id not in self._cache:
            lock = self._provision_locks.setdefault(employee_id, asyncio.Lock())
            async with lock:
                # 等锁期间另一个 Job 可能已经把 Worker 建好并写入缓存,进锁后必须再看一次。
                if employee_id not in self._cache:
                    ref = await self._provisioner.resolve(employee_id)
                    if ref is None:
                        ref = await self._provision(employee_id)
                    self._cache[employee_id] = ref
        # **休眠的要叫醒再派活。** 休眠是纯成本优化;不自动唤醒的话它会表现为
        # "派下去没反应",而那是最难归因的一类症状。
        awake = await self._provisioner.wake(employee_id)
        self._cache[employee_id] = awake
        return awake

    async def _provision(self, employee_id: str) -> WorkerRef:
        """首次派发时把员工声明收敛成 Worker；没有 Workspace 上下文才保留旧报错。"""
        if self._employee_provider is None:
            raise WorkerNotProvisioned(
                f"员工 {employee_id} 在平台上还没有 Worker。"
                "当前运行时没有 Workspace 员工上下文,无法自动供应。"
            )
        outcome = await self._provisioner.reconcile(self._employee_provider(employee_id))
        if self._on_reconciled is not None:
            self._on_reconciled(employee_id, outcome)
        return outcome.ref


class FixedDirectory(WorkerDirectory):
    """所有员工都指向同一个 Worker 与房间。

    只为**既有部署**的向后兼容存在:根配置里配了全局 worker/房间的,行为逐字节不变。
    新部署应当走供应,否则角色隔离在容器一侧不成立。
    """

    def __init__(self, worker: str, room_id: str) -> None:
        self._ref = WorkerRef(name=worker, room_id=room_id)

    async def locate(self, employee_id: str) -> WorkerRef:
        return self._ref


__all__ = [
    "FixedDirectory",
    "EmployeeProvider",
    "ProvisionedDirectory",
    "ReconcileObserver",
    "WorkerDirectory",
    "WorkerNotProvisioned",
]
