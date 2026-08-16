"""原始日志留多久。

三个记录平面的寿命差了三个数量级,而它们在设计文档里是并排列着的,读起来像同样可靠:

| 平面 | 载体 | 寿命 |
|---|---|---|
| 版本面 | git | 永久 |
| 事件面 | 数据库(真相) + JSONL(流式副本) | 永久;副本可清理 |
| 日志面 | `tasks/*/logs/` | **有限** |

日志面是唯一会消失的那个。没有保留期不等于永久保留,只等于**磁盘满的那天由运维随手删掉**
——那是最不可控的清理策略。

## 判定是纯函数

收任务集与截止时刻,给出该清理谁。当前时刻由调用方算。这样测试直接传截止时刻,不必注入
时钟、也不必等三十天——而注入时钟会为了一个日期比较在系统里开一道缝。

## 没有审计包就不清理

哪怕远超保留期。反过来的设计(超期就删、不管有没有包)会在归档盘满这种故障下**静默地把
证据全删光**,而且删得越干净越像一切正常。这条约束的代价是磁盘会在故障时持续增长——那是
有意的:宁可磁盘告警,不可证据无声消失。所以清理报告里的跳过原因必须醒目,它是这个故障
唯一的暴露渠道。
"""

from __future__ import annotations

import shutil
import zipfile
from collections.abc import Container, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from agentgenome.core.store import task_dir
from agentgenome.core.task import Task
from agentgenome.security.audit import bundle_path

#: 日志目录在任务目录下的位置。事件的 JSONL 副本也住在这里。
LOGS = Path("logs")


class SkipReason(StrEnum):
    """为什么这个任务的日志没被清。

    **值是机器可读的槽,人话在 `message` 里。** 直接把整句中文当枚举值的话,它会顺着
    `--json` 泄进对外契约,改一个措辞就成了一次接口变更。这与 `core.scope` 的处理一致:
    枚举给槽,渲染给人。
    """

    NOT_TERMINAL = "not_terminal"
    WITHIN_RETENTION = "within_retention"
    NO_BUNDLE = "no_bundle"
    NO_LOGS = "no_logs"

    @property
    def message(self) -> str:
        """给人读的那一句。**跳过原因是归档故障唯一的暴露渠道**,所以它必须说清后果,
        不能只说"跳过"。"""
        return {
            SkipReason.NOT_TERMINAL: "还在跑",
            SkipReason.WITHIN_RETENTION: "还在保留期内",
            SkipReason.NO_BUNDLE: "没有审计包(不清理:宁可占磁盘,不可丢证据)",
            SkipReason.NO_LOGS: "没有日志目录",
        }[self]


@dataclass(frozen=True)
class Skipped:
    task_id: str
    reason: SkipReason


@dataclass(frozen=True)
class Prunable:
    task_id: str
    path: Path


@dataclass(frozen=True)
class PrunePlan:
    """打算删谁、跳过谁。**预演与实删用的是同一份计划**,所以预演报告不会与实际执行分叉。"""

    prune: tuple[Prunable, ...] = ()
    skipped: tuple[Skipped, ...] = ()


@dataclass
class PruneReport:
    dry_run: bool
    pruned: list[str] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    reclaimed_bytes: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


def plan_prune(
    tasks: Iterable[Task],
    *,
    workspace_root: Path,
    cutoff: datetime,
    sealed: Container[str],
) -> PrunePlan:
    """哪些任务的原始日志可以删。

    三个条件缺一不可:已进终态、终态时刻早于 `cutoff`、审计包已在 `sealed` 里。

    **用 `updated_at` 当终态时刻是一处有意的近似。** 模型里没有单独的"进终态那一刻",而
    任何一次终态之后的写入都会把这个时刻推后——也就是说误差只会让日志**留得更久**。这个
    方向的误差是安全的,为它加一列的收益抵不过成本。
    """
    prune: list[Prunable] = []
    skipped: list[Skipped] = []
    root = Path(workspace_root)
    for task in tasks:
        if not task.is_terminal:
            skipped.append(Skipped(task.id, SkipReason.NOT_TERMINAL))
            continue
        if task.updated_at > cutoff:
            skipped.append(Skipped(task.id, SkipReason.WITHIN_RETENTION))
            continue
        if task.id not in sealed:
            skipped.append(Skipped(task.id, SkipReason.NO_BUNDLE))
            continue
        prune.append(Prunable(task.id, task_dir(root, task.id) / LOGS))
    return PrunePlan(prune=tuple(prune), skipped=tuple(skipped))


def plan_workspace_prune(
    workspace_root: Path,
    tasks: Iterable[Task],
    *,
    archive: Path,
    retention_days: int,
    now: datetime,
) -> PrunePlan:
    """一个 Workspace 当下该清哪些日志。

    "谁已封存"与"截止时刻怎么算"是**领域规则,不是命令行的事**。放在命令里的话,第二个
    调用方(REST 端点、运维定时任务)必须把这两条重新推导一遍——而推错任何一条的后果都是
    删掉不该删的证据。

    **封存判定看的是"包能不能打开"而不是"文件在不在"。** 归档盘写到一半断掉会留下一个
    零字节或截断的 zip;按存在性判定的话它算已封存,于是日志被删——正是这条规则要防的那种
    静默证据丢失。
    """
    sealed = {
        task.id
        for task in tasks
        if zipfile.is_zipfile(bundle_path(workspace_root, task.id, archive))
    }
    return plan_prune(
        tasks,
        workspace_root=workspace_root,
        cutoff=now - timedelta(days=retention_days),
        sealed=sealed,
    )


def _size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def apply_prune(plan: PrunePlan, *, dry_run: bool) -> PruneReport:
    """执行一份计划。删失败只记账,不中断——一个目录删不掉不该让其余的也留着。"""
    report = PruneReport(dry_run=dry_run, skipped=list(plan.skipped))
    for item in plan.prune:
        if not item.path.is_dir():
            report.skipped.append(Skipped(item.task_id, SkipReason.NO_LOGS))
            continue
        size = _size(item.path)
        if not dry_run:
            try:
                shutil.rmtree(item.path)
            except OSError as error:
                report.failed.append((item.task_id, str(error)))
                continue
        report.pruned.append(item.task_id)
        report.reclaimed_bytes += size
    return report


__all__ = [
    "LOGS",
    "Prunable",
    "PrunePlan",
    "PruneReport",
    "SkipReason",
    "Skipped",
    "apply_prune",
    "plan_prune",
    "plan_workspace_prune",
]
