"""把单步状态机驱动成一条连续的产品流程。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from agentgenome.core.task import Task
from agentgenome.jobs.handlers import can_advance


class StepOrchestrator(Protocol):
    def current(self, task_id: str) -> Task: ...

    async def advance(self, task_id: str) -> Task: ...

    async def drain_evolution(self) -> object: ...


class DriveStop(StrEnum):
    WAITING = "waiting"
    TERMINAL = "terminal"
    STALLED = "stalled"
    SAFETY_LIMIT = "safety_limit"


@dataclass(frozen=True)
class DriveResult:
    task: Task
    steps: int
    stop: DriveStop


class TaskDriver:
    """启动一次，连续推进到审批、人工待办、交互式开发或终态。"""

    def __init__(
        self,
        orchestrator: StepOrchestrator,
        *,
        max_steps: int = 64,
        on_step: Callable[[Task], None] | None = None,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps 必须大于 0")
        self.orchestrator = orchestrator
        self.max_steps = max_steps
        self.on_step = on_step

    async def drive(self, task_id: str) -> DriveResult:
        task = self.orchestrator.current(task_id)
        steps = 0
        stop = DriveStop.SAFETY_LIMIT
        try:
            while steps < self.max_steps:
                previous = task
                task = await self.orchestrator.advance(task_id)
                steps += 1
                if task == previous:
                    stop = DriveStop.STALLED
                    break
                if self.on_step is not None:
                    self.on_step(task)
                if task.is_terminal:
                    stop = DriveStop.TERMINAL
                    break
                if not can_advance(task):
                    stop = DriveStop.WAITING
                    break
        finally:
            await self.orchestrator.drain_evolution()

        return DriveResult(task=task, steps=steps, stop=stop)


__all__ = ["DriveResult", "DriveStop", "TaskDriver"]
