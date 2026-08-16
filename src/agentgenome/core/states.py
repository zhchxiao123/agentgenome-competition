"""任务状态机的状态与事件词汇。

**词汇只定在这一处。** Procedure 的 `failure.on_fail` 声明的是"语义失败时状态机该收到什么事件",
如果 Procedure 那边自己定一套再做映射,两套词汇迟早会漂移——而漂移的表现是某个失败悄悄走错了边。

状态机本身(迁移表、守卫、副作用)在 PRD 05。这里先把词汇定下来,让 PRD 03 能引用它。
"""

from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    """任务生命周期。后三个是终态。"""

    CREATED = "CREATED"
    DEVELOPING = "DEVELOPING"
    UNIT_TESTING = "UNIT_TESTING"
    INTEGRATION_TESTING = "INTEGRATION_TESTING"
    READY_TO_COMMIT = "READY_TO_COMMIT"
    REVIEWING = "REVIEWING"
    MERGING = "MERGING"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"

    @classmethod
    def terminal(cls) -> frozenset[TaskState]:
        """状态机不再自动迁移的状态。**这是给机器看的。**"""
        return frozenset({cls.COMPLETED, cls.ESCALATED, cls.CANCELLED})

    @classmethod
    def settled(cls) -> frozenset[TaskState]:
        """人不用再看的状态。**这是给人看的**,比 `terminal` 少一个 `ESCALATED`。

        两个概念长得像,但差的那一个恰恰是最需要注意力的:升级人工意味着机器停手了,
        不意味着这件事了结了。合成一个词的代价是**升级人工的任务从所有列表里消失**——
        人只能靠"我记得提过这个任务"才知道它存在,而这正是最不该靠记性的时候。

        `terminal` 管迁移、画图、写报告、触发蒸馏;`settled` 只管"列表里还要不要出现"。
        """
        return frozenset({cls.COMPLETED, cls.CANCELLED})


class TaskEvent(StrEnum):
    """驱动状态迁移的事件。"""

    PLAN_DONE = "plan_done"
    PLAN_FAILED = "plan_failed"
    DEV_DONE = "dev_done"
    #: 员工申请扩权到计划外的模块,校验通过。回到开发态重跑一轮,新一轮里那个模块可写。
    SCOPE_WIDENED = "scope_widened"
    GATE_PASS = "gate_pass"
    GATE_FAIL = "gate_fail"
    ITEST_PASS = "itest_pass"
    ITEST_FAIL = "itest_fail"
    RISK_HIGH = "risk_high"
    RISK_LOW = "risk_low"
    PRECHECK_FAIL = "precheck_fail"
    APPROVE = "approve"
    REJECT = "reject"
    MERGED = "merged"
    MERGE_CONFLICT = "merge_conflict"
    CANCEL = "cancel"


__all__ = ["TaskEvent", "TaskState"]
