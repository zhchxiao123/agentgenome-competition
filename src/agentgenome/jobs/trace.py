"""任务详情页的"执行轨迹":把每个 stage 落盘的归一化事件流,读成前端认识的块。

数据不是新造的。`job-attempt-N.jsonl` 本来就是每个 Job 逐行落盘的 `NormalizedEvent`
(见 `agents/subprocess_runtime.py` 的 `_consume`),对话工作台"查证过程"那个可展开的
工具调用块用的正是同一份 `blocks_from`——两处的数据源头不同(一个来自会话运行时的
实时流,一个来自任务 Job 落盘的文件),归一化之后的形状是同一个,没有理由再造一套转换,
前端也因此能直接复用同一套渲染组件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agentgenome.agents.events import EventKind, NormalizedEvent
from agentgenome.jobs.artifacts import ArtifactBus
from agentgenome.sessions.blocks import Block, blocks_from


@dataclass(frozen=True)
class StageTrace:
    """一个产物目录(一次 stage 派发)对应的执行轨迹。"""

    stage: str
    number: int
    blocks: tuple[Block, ...]


def _read_events(log_file: Path) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    for raw_line in log_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            events.append(
                NormalizedEvent(
                    kind=EventKind(payload["kind"]),
                    text=payload.get("text", ""),
                    message_id=payload.get("message_id"),
                    usage=payload.get("usage"),
                    detail=payload.get("detail", {}),
                )
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            # 一行解析不了不该让整段轨迹连带炸掉——这份日志是运行时逐行落盘的,
            # 半截的一行(比如中途被杀掉的 Job)本来就是可能出现的正常状态。
            continue
    return events


def read_trace(task_dir: Path) -> tuple[StageTrace, ...]:
    """按 stage 分组的执行轨迹,按分配顺序升序。

    **不是所有 stage 都有对话轨迹。** `unit-gate` 这类确定性执行(跑真的 pytest)不经过
    任何 Agent 运行时,自然没有 `job-attempt-*.jsonl`——这里返回空块列表,不是错误。
    同一个 slot 可能有多次尝试(契约失败重试),按文件名顺序拼接,序号统一续排。
    """
    bus = ArtifactBus(task_dir)
    stages = []
    for slot in bus.all():
        events: list[NormalizedEvent] = []
        for log_file in sorted(slot.path.glob("job-attempt-*.jsonl")):
            events.extend(_read_events(log_file))
        blocks = tuple(blocks_from(events, start_seq=0))
        stages.append(StageTrace(stage=slot.stage, number=slot.number, blocks=blocks))
    return tuple(stages)


def read_module_trace(task_dir: Path) -> tuple[StageTrace, ...]:
    """基因组作业的执行轨迹。

    基因组任务的产物目录**不走槽位编址**:深读按模块铺目录(`artifacts/<模块id>/`),
    一目录一模块;老的一趟式初始化直接写在 `artifacts/` 根上。`read_trace` 只认
    `NN-<stage>` 的槽位命名,对这些目录一个都看不见——于是"轨迹是空的"与"没跑过"
    分不开。这里按目录分组:有 `job-attempt-*.jsonl` 的才算,目录名就是分组名。
    """
    artifacts = Path(task_dir) / "artifacts"
    if not artifacts.is_dir():
        return ()
    groups: dict[str, list[Path]] = {}
    for log_file in sorted(artifacts.rglob("job-attempt-*.jsonl")):
        label = log_file.parent.relative_to(artifacts).as_posix()
        groups.setdefault("任务作业" if label == "." else label, []).append(log_file)
    stages = []
    for number, (label, files) in enumerate(sorted(groups.items()), start=1):
        events: list[NormalizedEvent] = []
        for log_file in files:
            events.extend(_read_events(log_file))
        stages.append(
            StageTrace(stage=label, number=number, blocks=tuple(blocks_from(events, start_seq=0)))
        )
    return tuple(stages)


__all__ = ["StageTrace", "read_module_trace", "read_trace"]
