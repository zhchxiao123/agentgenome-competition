"""到期扫描:提醒 → 改派 → 升级人工。

## 为什么是三段,不是一段

人的待办超时是**常态,不是事故**。三天没动多半是这个人在休假,而不是任务出了问题。

而"已升级人工"是**终态**:没有任何事件能把它推向下一步。到期直接升级的话,"等另一个人接管"
这句话接不回来——那个任务只能被新建一个来替代,而它已经花掉的钱与已经产出的东西都跟着废掉。

所以中间要有一段"换个人"的路。**改派仍然是待确认**:机器停手等人,任务健康。待确认与已升级
人工的区别是词汇表里第二容易被合并掉的一条,而合并的代价是异常队列里混进一堆健康任务,
真正出事的那个被淹没。

## 为什么是扫描,不是等待

Job 已经挂起返回了,没有协程在那儿等。到期这件事因此只能由**外面**问:形状与调度那边的
到期检查同源(`Schedule.expire`)。

扫描必须**幂等**:同一分钟连跑三次,提醒只发一次——靠待办上那个"提醒发过没有"的标记,
不靠"上次扫描是什么时候"这种会随部署丢失的记忆。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentgenome.approval.notify import send_payload
from agentgenome.config import Config, HumanConfig
from agentgenome.core.events import ORCHESTRATOR, EventLog, LogKind
from agentgenome.core.task import TaskStore
from agentgenome.todo.store import ESCALATED, Todo, TodoStore

#: 事件里的三段动作名。
REMINDED = "reminded"
REASSIGNED = "reassigned"
EXPIRED = "escalated"


@dataclass(frozen=True)
class SweepReport:
    """这一遍扫描做了什么。空的三个列表就是"没到期的",不是"没扫"。"""

    reminded: list[str] = field(default_factory=list)
    reassigned: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)
    #: 只看不动那一遍里,"本来会怎样"。**它与真做过的分开列**——混在一起的话,一份 dry-run
    #: 报告读起来像是已经发生过了。
    would_remind: list[str] = field(default_factory=list)
    would_escalate: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "reminded": self.reminded,
            "reassigned": self.reassigned,
            "escalated": self.escalated,
            "would_remind": self.would_remind,
            "would_escalate": self.would_escalate,
        }


def sweep(
    root: Path,
    config: Config | None = None,
    now: datetime | None = None,
    escalate: bool = True,
) -> SweepReport:
    """扫一遍所有还等着人干的待办。

    `escalate=False` 只看不动第三段——给"我先看看有哪些要升级"这种问法用,它不该顺手把
    任务推进终态。
    """
    settings = (config or Config()).human
    # 通知渠道与人工审批共用同一个:待办提醒与"等你审批"是同一类打扰,分两个渠道配
    # 只会让其中一个长期没人配。
    webhook = (config or Config()).approval.notify.webhook
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    store = TodoStore(root)
    log = EventLog(root)
    report = SweepReport()

    for todo in store.open_todos():
        age = stamp - todo.created_at
        if _due(age, settings.reassign_after_days):
            # **过了改派窗口的待办不再"提醒"。** 只看不动时直接跳过它:掉进提醒分支的话,
            # 一次"我先看看有哪些要升级"会把提醒标记烧掉,而真正那一遍扫描从此再也不提醒了
            # ——一个只看的动作改掉了将来的行为,这是 dry-run 最坏的失败形态。
            if escalate:
                _move_on(root, webhook, store, log, todo, stamp, settings, report)
            else:
                report.would_escalate.append(todo.id)
        elif _due(age, settings.reminder_after_days) and not todo.reminded:
            if escalate:
                _remind(webhook, store, log, todo, stamp, report)
            else:
                report.would_remind.append(todo.id)
    return report


def _due(age: timedelta, days: float) -> bool:
    return days > 0 and age >= timedelta(days=days)


def _notify(webhook: str | None, todo: Todo, action: str, to: str = "") -> None:
    """通知走既有的传输层。**永远不抛**——通知渠道没配就是"这个部署不需要",
    而一条发不出去的提醒不该让整遍扫描停下来。
    """
    send_payload(
        webhook,
        {
            "kind": f"todo_{action}",
            "task_id": todo.task_id,
            "todo": todo.id,
            "to": to or todo.assignee,
            "stage": todo.stage,
        },
    )


def _remind(
    webhook: str | None,
    store: TodoStore,
    log: EventLog,
    todo: Todo,
    stamp: datetime,
    report: SweepReport,
) -> None:
    store.save(replace(todo, reminded=True), now=stamp)
    log.append(
        todo.task_id,
        actor=ORCHESTRATOR,
        kind=LogKind.TODO,
        payload={"todo": todo.id, "action": REMINDED, "assignee": todo.assignee},
    )
    _notify(webhook, todo, REMINDED)
    report.reminded.append(todo.id)


def _move_on(
    root: Path,
    webhook: str | None,
    store: TodoStore,
    log: EventLog,
    todo: Todo,
    stamp: datetime,
    settings: HumanConfig,
    report: SweepReport,
) -> None:
    """往下走一段:先改派,改派额度用完才升级人工。

    名字不叫 `_escalate`:它**两件事都干**,而叫升级的话读的人会以为这里只有终态那一条路。
    """
    backup = _next_assignee(todo, settings)
    if todo.reassignments < settings.max_reassignments and backup:
        moved = replace(
            todo,
            assignee=backup,
            reassignments=todo.reassignments + 1,
            reminded=False,
            # **轮次 +1**:换了人就是新的一段等待,提醒与到期都从头算。
            created_at=stamp,
            history=(*todo.history, backup),
        )
        store.save(moved, now=stamp)
        log.append(
            todo.task_id,
            actor=ORCHESTRATOR,
            kind=LogKind.TODO,
            payload={
                "todo": todo.id,
                "action": REASSIGNED,
                "from": todo.assignee,
                "to": backup,
                "round": moved.reassignments,
            },
        )
        _notify(webhook, todo, REASSIGNED, to=backup)
        report.reassigned.append(todo.id)
        return

    store.save(replace(todo, state=ESCALATED), now=stamp)
    log.append(
        todo.task_id,
        actor=ORCHESTRATOR,
        kind=LogKind.TODO,
        payload={"todo": todo.id, "action": EXPIRED, "assignee": todo.assignee},
    )
    # **升级也要发通知。** 前两段发了、这一段不发的话,最该被人知道的那一次反而最安静。
    _notify(webhook, todo, EXPIRED)
    tasks = TaskStore(root)
    # 清掉"在等谁":待办已经走完三段,任务不再等这张待办了。
    tasks.save(tasks.get(todo.task_id).evolve(pending_todo_id=""))
    # **走既有的升级路径,不自己写一条状态迁移。** 状态机是治理模型:多开一条入口,
    # "任务怎么进的终态"就有了第二个答案。
    from agentgenome.jobs.orchestrator import Orchestrator

    Orchestrator(root).escalate(todo.task_id, reason=_reason(todo, settings))
    report.escalated.append(todo.id)


def _reason(todo: Todo, settings: HumanConfig) -> str:
    """升级原因按"下一步该从哪查"的口径写。**"超时"不是原因。**"""
    return (
        f"派给人的活没人接:待办 {todo.id} 在 {todo.assignee} 手里"
        f"(已改派 {todo.reassignments} 次)超过 {settings.reassign_after_days} 天。"
        "找一个能接手的人,或者把这个员工改回自动执行。"
    )


def _next_assignee(todo: Todo, settings: HumanConfig) -> str:
    """下一个该接手的人。

    **不猜**:候选名单是配出来的。猜的话(比如"随便找个管理员"),一张待办会转到一个根本
    不知道这件事的人手上,而他看到的只是一条没有上下文的通知。
    """
    for candidate in settings.backups:
        if candidate not in todo.history:
            return candidate
    return ""


__all__ = ["EXPIRED", "REASSIGNED", "REMINDED", "SweepReport", "sweep"]
