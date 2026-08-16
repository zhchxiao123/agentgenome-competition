"""任务详情页的执行轨迹:把每个 stage 落盘的归一化事件流,读成前端认识的块。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome.agents.artifacts import log_filename
from agentgenome.agents.events import EventKind, NormalizedEvent
from agentgenome.jobs.artifacts import ArtifactBus
from agentgenome.jobs.trace import read_trace


@pytest.fixture
def bus(tmp_path: Path) -> ArtifactBus:
    return ArtifactBus(tmp_path / "tasks" / "ag-1")


def _write_log(slot_path: Path, attempt: int, events: list[NormalizedEvent]) -> None:
    lines = "\n".join(event.to_json() for event in events)
    (slot_path / log_filename(attempt)).write_text(lines + "\n", encoding="utf-8")


def test_a_stage_with_no_log_file_has_empty_blocks(bus: ArtifactBus) -> None:
    """`unit-gate` 这类确定性执行不经过任何 Agent 运行时——没有日志文件不是错误。"""
    bus.allocate("unit-gate")

    stages = read_trace(bus.root.parent)

    assert len(stages) == 1
    assert stages[0].stage == "unit-gate"
    assert stages[0].blocks == ()


def test_a_stage_with_a_real_log_produces_ordered_blocks(bus: ArtifactBus) -> None:
    slot = bus.allocate("plan")
    _write_log(
        slot.path,
        attempt=1,
        events=[
            NormalizedEvent(kind=EventKind.TEXT, text="我来先看看当前项目的结构。"),
            NormalizedEvent(
                kind=EventKind.TOOL_USE,
                text="Read genome/rules/architecture.md",
                detail={"name": "Read"},
            ),
            NormalizedEvent(kind=EventKind.USAGE, usage={"input_tokens": 100, "output_tokens": 50}),
        ],
    )

    stages = read_trace(bus.root.parent)

    assert len(stages) == 1
    assert stages[0].stage == "plan"
    assert stages[0].number == 1
    kinds = [block.kind.value for block in stages[0].blocks]
    # USAGE 不产块——用量是记账,不是给用户看的内容,与 `blocks_from` 的约定一致。
    assert kinds == ["text", "tool-step"]


def test_stages_are_ordered_by_allocation_and_attempts_are_concatenated(bus: ArtifactBus) -> None:
    """修复循环会多次进同一个 stage;一个 Job 内部因契约失败也可能重跑出多个 attempt。"""
    first = bus.allocate("plan")
    _write_log(first.path, attempt=1, events=[NormalizedEvent(kind=EventKind.TEXT, text="第一次")])
    second = bus.allocate("develop")
    _write_log(second.path, attempt=1, events=[NormalizedEvent(kind=EventKind.TEXT, text="尝试一")])
    _write_log(second.path, attempt=2, events=[NormalizedEvent(kind=EventKind.TEXT, text="尝试二")])

    stages = read_trace(bus.root.parent)

    assert [(stage.stage, stage.number) for stage in stages] == [("plan", 1), ("develop", 2)]
    assert [block.text for block in stages[1].blocks] == ["尝试一", "尝试二"]


def test_a_malformed_line_is_skipped_not_fatal(bus: ArtifactBus) -> None:
    """日志是运行时逐行落盘的,半截的一行(比如中途被杀掉的 Job)是正常状态,不该整段炸掉。"""
    slot = bus.allocate("develop")
    good = NormalizedEvent(kind=EventKind.TEXT, text="正常这一行")
    (slot.path / log_filename(1)).write_text(
        good.to_json() + "\n" + "这不是 JSON\n" + '{"kind": "not-a-real-kind"}\n',
        encoding="utf-8",
    )

    stages = read_trace(bus.root.parent)

    assert [block.text for block in stages[0].blocks] == ["正常这一行"]


def test_module_trace_reads_flat_module_directories(tmp_path: Path) -> None:
    """基因组作业按模块铺目录(`artifacts/<模块id>/`),不走槽位编址。

    槽位读取器(`read_trace`)对它们一个都看不见——"轨迹是空的"与"没跑过"因此分不开,
    这正是 `read_module_trace` 存在的理由。老的一趟式作业直接写在 `artifacts/` 根上,
    也要读得回来。
    """
    from agentgenome.jobs.trace import read_module_trace

    task = tmp_path / "tasks" / "gn-1"
    module_dir = task / "artifacts" / "order-service"
    module_dir.mkdir(parents=True)
    _write_log(module_dir, 1, [NormalizedEvent(kind=EventKind.TEXT, text="读 order 的代码")])
    _write_log(task / "artifacts", 1, [NormalizedEvent(kind=EventKind.TEXT, text="一趟式作业")])

    assert read_trace(task) == ()
    stages = read_module_trace(task)
    assert [stage.stage for stage in stages] == ["order-service", "任务作业"]
    assert stages[0].blocks and stages[1].blocks


def test_module_trace_ignores_directories_without_logs(tmp_path: Path) -> None:
    """staging 树、result.json 这些产物目录没有对话日志——不该以空轨迹的形态出现。"""
    from agentgenome.jobs.trace import read_module_trace

    task = tmp_path / "tasks" / "gn-1"
    (task / "artifacts" / "order-service" / "staging").mkdir(parents=True)
    (task / "artifacts" / "order-service" / "result.json").write_text("{}", encoding="utf-8")

    assert read_module_trace(task) == ()
