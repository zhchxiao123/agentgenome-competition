"""把基因组任务推进一步。

迁移表是纯的:它只说"该去哪"。这一层负责让那句话变成事实——落库、记事件、把副作用交出去。

## 被拒的迁移也要落地

`decide` 返回 `allowed=False` 本身不是记录。**不记的话"它怎么没动"查无可查**——而基因组
任务最容易卡住的地方恰恰是闸门:一份不合契约的答复会让任务原地不动,人看到的是"我明明回答
了,它却没往下走"。

## 顺序不能变

先落库再记事件的话,崩溃在两者之间会留下一个"状态变了但历史里没有"的任务,而事件流是审计
的唯一依据。与研发任务那条推进路径同一条理由。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from agentgenome.core.events import ORCHESTRATOR, EventLog
from agentgenome.core.genome_gate import (
    AnswerInvalid,
    Validator,
    any_mapping,
    module_boundary_answer,
    read_answer,
    write_answer,
)
from agentgenome.core.genome_task import (
    GenomeTask,
    GenomeTaskKind,
    GenomeTaskState,
    GenomeTaskStore,
)
from agentgenome.core.genome_transitions import (
    GenomeDecision,
    GenomeEffect,
    GenomeEvent,
    GenomeFacts,
    decide,
)

#: 「停下来」的两个终态。已提交不在里面:它是跑完了,不是停下了。
_STOPS = frozenset({GenomeTaskState.FAILED, GenomeTaskState.CANCELLED})


class NotWaiting(RuntimeError):
    """这个任务现在没有闸门要回答。"""


def _validator_for(task: GenomeTask) -> Validator:
    """这一种闸门该用哪个校验器。

    写死一个的话,每一种闸门都得说初始化的方言——而按模块重建、卡片补写要问的根本不是
    同一个问题。
    """
    if task.kind in (GenomeTaskKind.INIT, GenomeTaskKind.REINIT):
        return module_boundary_answer
    return any_mapping


@dataclass(frozen=True)
class Applied:
    """一次推进的结果。**被拒也是一种结果**,不是异常。"""

    task: GenomeTask
    decision: GenomeDecision

    @property
    def moved(self) -> bool:
        return self.decision.allowed


class GenomeDriver:
    """基因组任务的推进器。"""

    def __init__(
        self, store: GenomeTaskStore, log: EventLog, *, enforce_budget: bool = True
    ) -> None:
        self.store = store
        self.log = log
        self.enforce_budget = enforce_budget

    @property
    def root(self) -> Path:
        """这个推进器作用在哪个 Workspace 上。

        **从仓储上取,不另收一个参数。** 另收的话就有了两个真相来源,而它们能悄悄指向
        不同的目录——那种错在测试里几乎不会出现,在生产里一次就够。
        """
        return self.store.root

    def deliver(
        self,
        task_id: str,
        event: GenomeEvent,
        facts: GenomeFacts | None = None,
        actor: str = ORCHESTRATOR,
    ) -> Applied:
        """送一个事件进来,该动就动,不该动就留痕。

        `actor` 是**真人介入时的那个人**。缺省是编排器——大多数迁移确实是它推的;但一个人
        取消了跑飞的初始化、或者回答了闸门,记成编排器的话,"这个人干过什么"与"人对系统的
        每次介入都可见"这两个问题对这两种介入就答不上来了。
        """
        task = self.store.get(task_id)
        transition_facts = replace(facts or GenomeFacts(), enforce_budget=self.enforce_budget)
        decision = decide(task, event, transition_facts)
        if not decision.allowed or decision.to is None:
            self.log.genome_transition(task, event, decision, actor=actor)
            return Applied(task=task, decision=decision)

        updated = task.evolve(state=decision.to)
        if decision.reason and decision.to in _STOPS:
            # 停下的理由用「下一步该从哪查」的口径写,**失败与取消都记**——「这次没什么
            # 可蒸馏的」也是一个需要被记下来的停下理由,而它不是失败。
            updated = updated.evolve(failure_reason=decision.reason)
        saved = self.store.save(updated)
        self.log.genome_transition(task, event, decision, actor=actor)
        return Applied(task=saved, decision=decision)

    def confirm(self, task_id: str, answer: object, actor: str = ORCHESTRATOR) -> Applied:
        """人回答闸门。

        **只有正在等人的任务能被回答。** 不查的话,一份趁任务还在扫描时提前写下的答复会
        安静地躺在磁盘上,等草案一就绪,下一次恢复就把闸门自动推过去——人从没看过草案,而
        这个闸门存在的全部理由就是让人看一眼。旧周期遗留的答复同理。

        **答复不合契约时,先把这次拒绝记进事件流再抛。** 不记的话人看到的是"我明明回答了,
        它却没往下走",而查无可查——这正是推进器那条"被拒的迁移也要落地"点名要防的场景。
        """
        task = self.store.get(task_id)
        if task.state is not GenomeTaskState.AWAITING_CONFIRMATION:
            raise NotWaiting(f"{task_id} 不在待确认({task.state.value}),现在没有闸门要回答")
        try:
            write_answer(self.root, task_id, answer, validator=_validator_for(task))
        except AnswerInvalid:
            self.deliver(
                task_id, GenomeEvent.CONFIRMED, GenomeFacts(answer_valid=False), actor=actor
            )
            raise
        return self.deliver(task_id, GenomeEvent.CONFIRMED, actor=actor)

    def resume(self, task_id: str) -> Applied | None:
        """重启之后把这个任务捡起来。

        **恢复逻辑就是这一条**:看当前状态、读上一阶段的产物、继续。不需要额外的恢复分支
        ——这与"状态即事实"是同一条,交接物落盘就是为了让恢复无状态。

        **只捡闸门这一段。** 执行中的阶段(深读、汇总)要靠各自的阶段产物恢复,而那些产物
        由知识初始化流水线定义——这一层不知道它们长什么样,硬猜只会写出一条永远不对的分支。

        没什么可捡的返回 `None`:等人的任务还在等,那是正常状态,不是错误。
        """
        task = self.store.get(task_id)
        if task.state is not GenomeTaskState.AWAITING_CONFIRMATION:
            return None
        if read_answer(self.root, task_id) is None:
            return None
        return self.deliver(task_id, GenomeEvent.CONFIRMED)

    def pending_effects(self, applied: Applied) -> tuple[GenomeEffect, ...]:
        """这次迁移要求做的事。**执行在调用方**——这一层没有通知渠道,也不该有。"""
        return applied.decision.effects


__all__ = ["Applied", "GenomeDriver"]
