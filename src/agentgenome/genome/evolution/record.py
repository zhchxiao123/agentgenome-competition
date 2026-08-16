"""蒸馏的身份:一条基因组任务,加上源任务时间线上的一条指针。

蒸馏此前是挂在源研发任务上的一段内联管道,只在结束时记一条结果事件。这在"没人在等它"这个
场景下是对的,但它**没有身份**:失败与耗时没有地方安放,进行中时人完全看不见。

## 指针不是内容

源研发任务上留一条指向这条基因组任务的记录,这样"这个任务的经验变成了什么"仍然读得连贯。
它只是个 id,不违反「同一件事只由一个平面记内容」。

## 执行仍然内联

这一步只做**建模与可见性**:蒸馏真的建一条记录、真的产生生命周期事件。Job 本身仍然挂在源
任务上跑,产物也仍落在源任务目录——把派发也拆过去要动 `dispatch` 的签名与产物总线,那是
下一步。

**但第一步不能只做界面**:那会产生一个只在界面上存在的假任务——没有真的 id、查不到事件、
点开是空的。

## 这里的每一条路径都不抛

蒸馏管道的第一原则是"失败绝不影响已经完成的任务"。建档比管道更靠前,它要是能抛,那条原则
就从第一行起就不成立了——一个终态任务后面的收尾流程(通知、报告)会被建档的一次磁盘错误
整条掐断。

## 三种结局,不是两种

跑出了卡片、什么都没跑成、以及**这次压根没蒸馏**(素材为空、预算不够),是三件不同的事。
把后两者合并成"成功"的话,看板上一条"已提交"的记录背后可能一张卡片都没有;合并成"失败"
的话,一次正常的"没什么可学的"会变成需要人看的异常。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agentgenome.core.events import ORCHESTRATOR, EventLog, LogKind
from agentgenome.core.genome_driver import GenomeDriver
from agentgenome.core.genome_task import (
    GenomeTask,
    GenomeTaskKind,
    GenomeTaskStore,
    ModuleBusy,
    Origin,
)
from agentgenome.core.genome_transitions import GenomeEvent, GenomeFacts
from agentgenome.genome.evolution.pipeline import DistillResult

#: 记录标题里的那几个字。人在看板上一眼要认出这是什么。
DISTILL_TITLE = "经验蒸馏"

#: 源任务时间线上那条指针的 note 键。**按键查,不按文案查**——文案会改。
POINTER_NOTE = "distillation"


def open_distillation(
    workspace_root: Path, source_task_id: str, budget_tokens: int | None = None
) -> GenomeTask | None:
    """给一次蒸馏建档,并在源任务上留一条指针。

    **建不出来返回 `None`,不抛。** 一个终态任务后面的收尾流程(通知、报告)不该被建档的
    一次磁盘错误整条掐断——这条管道的第一原则是"失败绝不影响已经完成的任务",而建档比管道
    更靠前。
    """
    root = Path(workspace_root)
    try:
        task = GenomeTaskStore(root).create(
            title=f"{DISTILL_TITLE}:{source_task_id}",
            kind=GenomeTaskKind.DISTILL,
            origin=Origin.SYSTEM,
            source_task_id=source_task_id,
            budget_tokens=budget_tokens,
        )
    except (OSError, sqlite3.Error, ModuleBusy) as error:
        _note(root, source_task_id, f"蒸馏建档失败(不影响本任务): {error}")
        return None
    _note_pointer(root, source_task_id, task.id)
    return task


def _note_pointer(root: Path, source_task_id: str, genome_task_id: str) -> None:
    try:
        EventLog(root).append(
            source_task_id,
            actor=ORCHESTRATOR,
            kind=LogKind.NOTE,
            payload={"note": POINTER_NOTE, "genome_task_id": genome_task_id},
        )
    except (OSError, sqlite3.Error):
        return


def finish_distillation(workspace_root: Path, task_id: str, result: DistillResult) -> None:
    """按蒸馏的结局把这条记录推到终态。

    **收整个结果而不是 `(ok, reason)`。** 拆成两个参数的话,"跳过算成功还是失败"这个判断
    就落到调用方头上,而它在那里只会被写成一句 `result.error or result.skipped`——跳过
    于是安静地变成了成功。

    失败与跳过都走**系统自发**那条:算已了结,只留一条事件,不进任何人的队列。但它们仍然
    查得到——「这次蒸馏挂了」必须能被看到,只是不该变成待办。
    """
    root = Path(workspace_root)
    driver = GenomeDriver(GenomeTaskStore(root), EventLog(root))

    def _push(event: GenomeEvent, reason: str = "") -> bool:
        applied = driver.deliver(task_id, event, GenomeFacts(stop_reason=reason))
        if not applied.moved:
            # **推不动也要留痕。** 崩在建档与收尾之间的话,记录会停在扫描中,而不留痕的话
            # 那条僵尸记录没有任何线索说明它为什么不动。
            _note(root, task_id, f"蒸馏记录推不动:{applied.decision.reason}")
        return applied.moved

    try:
        if not result.ok:
            _push(GenomeEvent.FAILED, result.error or "蒸馏失败")
        elif result.skipped:
            # 这次没蒸馏。**不是失败**——素材为空、预算不够都是正常结果,只是没有产出。
            _push(GenomeEvent.CANCEL, result.skipped)
        elif _push(GenomeEvent.READ_DONE):
            _push(GenomeEvent.SUBMITTED)
    except (OSError, sqlite3.Error) as error:
        _note(root, task_id, f"蒸馏记录收尾失败(不影响源任务): {error}")


def _note(root: Path, task_id: str, note: str) -> None:
    """留一条痕。**它自己也不许抛**——记录失败的那条路径要是能失败,就等于没有兜底。"""
    try:
        EventLog(root).append(
            task_id, actor=ORCHESTRATOR, kind=LogKind.NOTE, payload={"note": note}
        )
    except (OSError, sqlite3.Error):
        return


__all__ = [
    "DISTILL_TITLE",
    "POINTER_NOTE",
    "finish_distillation",
    "open_distillation",
]
