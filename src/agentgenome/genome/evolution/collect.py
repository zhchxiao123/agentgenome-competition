"""素材收集:把一个任务留下的东西变成可蒸馏的材料。

## 失败-修复对是这条管道的核心

每一轮的失败报告,配上**该轮之后**开发员工产生的 diff。这是"什么样的错误该怎么改"的直接
证据,也是这条管道最有价值的素材形态。**配错了后面全错**——一张把 A 错误配上 B 修复的卡片,
会教会后续每一个任务一件错事。

配对靠一条已有的不变式:某个 stage 的第 k 个产物目录就是它的第 k 轮。轮次已经编码在目录序号
里了,不要另起一套。

## 最后一轮的失败没有对应修复

那不是数据缺失,是"没修好"的证据——尤其在升级人工的任务里,它正好指向 AI 卡住的地方。所以
单独列出来,而不是因为配不上对就丢掉。

## 升级人工的任务价值最高

人最终怎么修的、AI 的尝试差在哪里,那是最好的教材。取不到人工 diff 时**明确标记**,不要
假装没有——"这个任务没有人工接管"与"我们没取到"是两件事。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.store import task_dir
from agentgenome.core.task import Task
from agentgenome.jobs.artifacts import ArtifactBus
from agentgenome.jobs.handlers import STAGE_DEVELOP
from agentgenome.jobs.reports import read_failure_reports


@dataclass(frozen=True)
class FailureFix:
    """一轮失败,以及下一轮针对它做了什么。"""

    round: int
    failure: str
    #: 下一轮开发员工报告的变更文件。空表示**没有下一轮**——那是"没修好"的证据。
    changed_files: tuple[str, ...] = ()
    fix_summary: str = ""

    @property
    def unfixed(self) -> bool:
        return not self.changed_files and not self.fix_summary

    def as_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "failure": self.failure,
            "changed_files": list(self.changed_files),
            "fix_summary": self.fix_summary,
            "unfixed": self.unfixed,
        }


@dataclass
class Material:
    """一次蒸馏的全部输入。"""

    task_id: str
    state: str
    requirement: str
    pairs: list[FailureFix] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    itest_reports: list[dict[str, Any]] = field(default_factory=list)
    #: 升级人工之后人做的改动。`None` 表示没取到,不是"没有"。
    human_diff: str | None = None
    escalate_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "requirement": self.requirement,
            "failure_fix_pairs": [pair.as_dict() for pair in self.pairs],
            "approvals": self.approvals,
            "itest_reports": self.itest_reports,
            "human_diff": self.human_diff,
            "human_diff_available": self.human_diff is not None,
            "escalate_reason": self.escalate_reason,
        }

    @property
    def is_empty(self) -> bool:
        """一点可蒸馏的东西都没有。直通且无审批意见的任务就是这种——没必要为它烧一次 Agent。"""
        return not (self.pairs or self.approvals or self.itest_reports or self.human_diff)


def collect(workspace_root: Path, task: Task) -> Material:
    """从任务目录与事件流里把素材捞出来。**纯读取**——不跑 Agent,不改任何东西。"""
    directory = task_dir(workspace_root, task.id)
    log = EventLog(workspace_root)
    material = Material(
        task_id=task.id,
        state=task.state.value,
        requirement=task.requirement,
        escalate_reason=task.escalate_reason or "",
    )
    material.pairs = _pairs(directory)
    material.approvals = [
        event.payload
        for event in log.events(task.id)
        if event.kind is LogKind.APPROVAL and not event.payload.get("approved")
    ]
    material.itest_reports = _itest_reports(directory)
    return material


def _pairs(directory: Path) -> list[FailureFix]:
    """失败-修复对。第 k 轮的失败配第 k+1 轮的开发产物。"""
    reports = read_failure_reports(directory)
    develop = ArtifactBus(directory).by_stage(STAGE_DEVELOP)
    found = []
    for record in reports:
        # 第 k 轮失败之后跑的是第 k+1 轮开发,而产物目录的第 k+1 个就是它。
        nxt = develop[record.round] if len(develop) > record.round else None
        payload = _read_json(nxt.path / "result.json") if nxt is not None else None
        found.append(
            FailureFix(
                round=record.round,
                failure=record.body,
                changed_files=tuple(
                    str(item) for item in (payload or {}).get("changed_files") or []
                ),
                fix_summary=str(((payload or {}).get("impact") or {}).get("rationale") or ""),
            )
        )
    return found


def _itest_reports(directory: Path) -> list[dict[str, Any]]:
    found = []
    for slot in ArtifactBus(directory).by_stage("itest"):
        payload = _read_json(slot.path / "itest-report.json")
        if payload is not None:
            found.append(payload)
    return found


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ["FailureFix", "Material", "collect"]
