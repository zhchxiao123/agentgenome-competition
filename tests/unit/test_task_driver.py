from __future__ import annotations

from agentgenome.core.states import TaskState
from agentgenome.core.task import Task
from agentgenome.jobs.driver import DriveStop, TaskDriver


def _task(state: TaskState, *, plan_retries: int = 0) -> Task:
    return Task(
        id="ag-1",
        title="测试任务",
        requirement="把流程跑通",
        state=state,
        plan_retries=plan_retries,
    )


class SequenceOrchestrator:
    def __init__(self, initial: Task, tasks: list[Task]) -> None:
        self.initial = initial
        self.tasks = tasks
        self.calls = 0
        self.drained = False

    def current(self, task_id: str) -> Task:
        assert task_id == "ag-1"
        return self.initial

    async def advance(self, task_id: str) -> Task:
        assert task_id == "ag-1"
        task = self.tasks[self.calls]
        self.calls += 1
        return task

    async def drain_evolution(self) -> None:
        self.drained = True


async def test_one_drive_consumes_a_created_self_loop_and_keeps_going() -> None:
    orchestrator = SequenceOrchestrator(
        _task(TaskState.CREATED),
        [
            _task(TaskState.CREATED, plan_retries=1),
            _task(TaskState.DEVELOPING, plan_retries=1),
            _task(TaskState.REVIEWING, plan_retries=1),
        ]
    )

    result = await TaskDriver(orchestrator).drive("ag-1")

    assert result.task.state is TaskState.REVIEWING
    assert result.steps == 3
    assert result.stop is DriveStop.WAITING
    assert orchestrator.drained is True


async def test_driver_stops_at_a_terminal_state() -> None:
    orchestrator = SequenceOrchestrator(
        _task(TaskState.REVIEWING), [_task(TaskState.COMPLETED)]
    )

    result = await TaskDriver(orchestrator).drive("ag-1")

    assert result.stop is DriveStop.TERMINAL
    assert result.steps == 1


async def test_driver_reports_each_visible_step_as_it_lands() -> None:
    landed: list[TaskState] = []
    orchestrator = SequenceOrchestrator(
        _task(TaskState.CREATED),
        [_task(TaskState.DEVELOPING), _task(TaskState.REVIEWING)]
    )

    await TaskDriver(orchestrator, on_step=lambda task: landed.append(task.state)).drive("ag-1")

    assert landed == [TaskState.DEVELOPING, TaskState.REVIEWING]


async def test_driver_stops_when_an_advance_makes_no_progress() -> None:
    """同一份失败结果的幂等回放不能被循环包装成多次执行尝试。"""
    unchanged = _task(TaskState.DEVELOPING)
    orchestrator = SequenceOrchestrator(unchanged, [unchanged])

    result = await TaskDriver(orchestrator).drive("ag-1")

    assert result.task is unchanged
    assert result.steps == 1
    assert result.stop is DriveStop.STALLED
    assert orchestrator.drained is True
