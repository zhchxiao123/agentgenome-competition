"""任务状态机的迁移表。

流程如果是一段嵌套的 `if`,"测试失败之后到底是重新开发、还是升级人工、还是重跑一遍"这个问题
就只能靠通读三百行代码来回答。这里把它变成**一张能被评审的表**:什么状态、什么事件、什么条件、
去哪个状态、有什么副作用。

## 迁移即文档

用一个显式列表而不是状态机框架——框架带来的表达力对这个规模没有收益,代价却是评审时要先学
一层 DSL。守卫与副作用是表里引用的**具名函数与具名常量**,不是内联 lambda,这样表本身保持可读。

## 这一层是纯的

输入是(任务快照、事件、外部事实),输出是(目标状态、要执行的副作用名单、计数器增量)。不碰
数据库、不碰文件、不拉 Job——副作用由 `jobs.handlers` 执行。这么切是因为迁移表是整个编排器里
最该被密集测试的东西,而它一旦沾上 I/O,19 条边的全覆盖就会变成 19 个慢测试。

## 三条不在表里的规则

- **表里没有的 `(state, event)` 组合是非法迁移**:拒绝、记事件、不改变状态。悄悄进入不可能的
  状态比报错难查得多。
- **启用预算门禁时,预算耗尽是横切规则**:任何状态下
  `tokens_used ≥ budget_tokens` 立即转 `ESCALATED`。写成
  表里的边的话每个状态都要加一行,而漏掉的那一行不会有任何症状。唯一的例外是 `cancel`——
  预算用光了人更想取消它,而不是被告知"已经升级人工所以取消不了"。
- **所有回到 `DEVELOPING` 的边统一 `fix_rounds+1`**,包括 `precheck_fail`、`reject`、
  `merge_conflict`。轮次的语义是"这个任务重来了几次",不区分为什么重来;不统一的话
  `precheck_fail` 那条边可以无限循环,它是三重上限里唯一漏掉的一条路。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.task import ItestNeed, Task

#: 需求解析最多重试几次。1 而不是 3:解析失败通常是需求本身说不清,多试两次只是多烧 token。
MAX_PLAN_RETRIES = 1


class Effect(StrEnum):
    """迁移的副作用。

    **具名常量而非函数引用**:这一层不执行任何东西,它只说"该做什么"。执行在
    `jobs.handlers`,那里才有数据库、文件系统与 AgentPool。
    """

    CREATE_WORKSPACE = "create_workspace"
    CLEANUP_WORKSPACE = "cleanup_workspace"
    DISPATCH_PLAN = "dispatch_plan"
    DISPATCH_DEV = "dispatch_dev"
    DISPATCH_GATE = "dispatch_gate"
    DISPATCH_ITEST = "dispatch_itest"
    ENTER_COMMIT_PIPELINE = "enter_commit_pipeline"
    OPEN_PR = "open_pr"
    NOTIFY = "notify"
    NOTIFY_APPROVER = "notify_approver"
    RECORD_APPROVAL = "record_approval"
    ARCHIVE_REPORT = "archive_report"
    #: 把上一轮的失败报告写进任务目录,供下一轮的上下文包注入。
    INJECT_FAILURE_REPORT = "inject_failure_report"
    #: 冻结现场:分支与工作区都保留,等人接管。
    FREEZE = "freeze"
    #: 把本任务首次发现并通过的验证规格提升为项目控制面的版本化事实。
    PROMOTE_VERIFICATION = "promote_verification"
    TRIGGER_EVOLUTION = "trigger_evolution"


class PlanFailureCause(StrEnum):
    """计划阶段失败后，人下一步应该检查哪一层。"""

    NONE = "none"
    DELIVERY = "delivery"
    CONTRACT = "contract"
    RUNTIME = "runtime"
    LIMIT = "limit"
    SCOPE = "scope"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Facts:
    """守卫需要的、任务模型上没有的事实。

    由调用方在迁移前算好再传进来——让守卫自己去读产物的话,这一层就不纯了,而它的
    全部价值就在于纯。
    """

    max_fix_rounds: int
    #: 计划产物是否有效(涉及模块真实存在、验收标准非空)。
    plan_valid: bool = True
    #: 这次计划失败是否能靠重新生成修复。图结构错误可以；计划明确要求补充需求不可以。
    plan_retryable: bool = False
    #: 计划没产出时的失败归因；NONE 表示计划自己的语义结论。
    plan_failure_cause: PlanFailureCause = PlanFailureCause.NONE
    #: `result.json` 是否合契约。
    result_valid: bool = True
    #: 提交前检查(越权/泄密)是否全过。
    prechecks_passed: bool = True
    #: 审批人是否在审批人名单里。
    approver_ok: bool = True
    #: 这次失败改代码能不能解决。
    #:
    #: 缺 gitleaks、门禁配置被篡改这类失败**改代码解决不了**——每一轮都会以完全相同的
    #: 方式失败,直到轮次耗尽。让 AI 反复尝试去修它是最典型的预算浪费方式。
    recoverable: bool = True
    #: Token 预算是否参与状态裁决。关闭时仍计量，但不能把任务送进 ESCALATED。
    enforce_budget: bool = True


@dataclass(frozen=True)
class Decision:
    """一次迁移的判定结果。

    计数器用**增量**而不是终值表达:终值要求这一层知道任务当前的计数,而那正是调用方
    手里的东西——两边各算一遍就会有一边算错。
    """

    allowed: bool
    to: TaskState | None = None
    effects: tuple[Effect, ...] = ()
    fix_rounds_delta: int = 0
    plan_retries_delta: int = 0
    #: 被拒或被升级时说清楚为什么。它进事件流,也进 `escalate_reason`。
    reason: str = ""


Guard = Callable[[Task, Facts], bool]


# --- 守卫(具名,表里只出现名字)----------------------------------------------


def plan_is_valid(task: Task, facts: Facts) -> bool:
    return facts.plan_valid


def plan_can_retry(task: Task, facts: Facts) -> bool:
    return facts.plan_retryable and task.plan_retries < MAX_PLAN_RETRIES


def plan_retries_exhausted(task: Task, facts: Facts) -> bool:
    return not plan_can_retry(task, facts)


def result_is_valid(task: Task, facts: Facts) -> bool:
    return facts.result_valid


def needs_itest(task: Task, facts: Facts) -> bool:
    return task.needs_itest is ItestNeed.YES


def skips_itest(task: Task, facts: Facts) -> bool:
    """**只有显式判定为否才跳过。**

    把 `UNDECIDED` 当成"不用跑"的话,判定环节压根没跑的那些改动会一路绿灯合进去——
    而那正是最需要集成测试的情形。
    """
    return task.needs_itest is ItestNeed.NO


def has_rounds_left(task: Task, facts: Facts) -> bool:
    return task.fix_rounds < facts.max_fix_rounds


def rounds_exhausted(task: Task, facts: Facts) -> bool:
    return not has_rounds_left(task, facts)


def cannot_retry(task: Task, facts: Facts) -> bool:
    """`can_retry` 的补集。**两条边必须互补**——不互补就会留下"两行都不匹配"的静默卡死。"""
    return not can_retry(task, facts)


def can_retry(task: Task, facts: Facts) -> bool:
    """还能不能再修一轮。

    **两个条件:轮次没用尽,而且这次失败改代码能解决。** 第二个条件不加的话,一个
    "环境里没装 gitleaks"的失败会把三轮修复全部烧掉,每一轮都以完全相同的方式失败。
    """
    return facts.recoverable and has_rounds_left(task, facts)


def must_escalate(task: Task, facts: Facts) -> bool:
    return not can_retry(task, facts)


def prechecks_passed(task: Task, facts: Facts) -> bool:
    return facts.prechecks_passed


def approver_is_legitimate(task: Task, facts: Facts) -> bool:
    return facts.approver_ok


#: 守卫不过时给人看的话。守卫本身返回布尔,理由写在这里——把两者揉进一个返回值会让
#: 表里的守卫变成一堆返回元组的函数,可读性立刻塌掉。
_GUARD_REASONS: dict[Guard, str] = {
    plan_is_valid: "计划产物无效",
    plan_can_retry: "需求解析重试次数已用尽",
    plan_retries_exhausted: "需求解析还能重试",
    result_is_valid: "result.json 不合契约",
    needs_itest: "needs_itest 不为 yes",
    skips_itest: "needs_itest 不为 no(未判定不等于不用跑)",
    has_rounds_left: "修复轮次已达上限",
    rounds_exhausted: "修复轮次还没到上限",
    can_retry: "轮次已达上限,或这次失败改代码解决不了",
    must_escalate: "还能再修一轮",
    prechecks_passed: "提交前检查未通过",
    approver_is_legitimate: "审批人不在名单里",
}


# --- 迁移表(附录 A)----------------------------------------------------------

#: 第 19 行的 `*`:任何非终态都能取消。
ANY_STATE = None


@dataclass(frozen=True)
class Transition:
    """表里的一行。"""

    from_state: TaskState | None
    event: TaskEvent
    guard: Guard | None
    to: TaskState
    effects: tuple[Effect, ...]


TRANSITIONS: tuple[Transition, ...] = (
    # 1
    Transition(
        TaskState.CREATED,
        TaskEvent.PLAN_DONE,
        plan_is_valid,
        TaskState.DEVELOPING,
        (Effect.CREATE_WORKSPACE, Effect.DISPATCH_DEV),
    ),
    # 2
    Transition(
        TaskState.CREATED,
        TaskEvent.PLAN_FAILED,
        plan_can_retry,
        TaskState.CREATED,
        (Effect.DISPATCH_PLAN,),
    ),
    # 3
    Transition(
        TaskState.CREATED,
        TaskEvent.PLAN_FAILED,
        plan_retries_exhausted,
        TaskState.ESCALATED,
        (Effect.NOTIFY, Effect.TRIGGER_EVOLUTION),
    ),
    # 4
    Transition(
        TaskState.DEVELOPING,
        TaskEvent.DEV_DONE,
        result_is_valid,
        TaskState.UNIT_TESTING,
        (Effect.DISPATCH_GATE,),
    ),
    # 4b:扩权之后重跑一轮开发。
    #
    # **自环,不是新状态。** 新状态要动迁移表、控制面、看板、报告,而它换来的东西(早几十
    # 分钟问人)比不上这个代价。守卫沿用修复轮次的上限:扩权也要烧一轮 Job,没有理由让它
    # 绕过那条闸。
    Transition(
        TaskState.DEVELOPING,
        TaskEvent.SCOPE_WIDENED,
        can_retry,
        TaskState.DEVELOPING,
        (Effect.DISPATCH_DEV,),
    ),
    # 4c:再修一轮的条件不成立却还要扩权 → 转人工。
    #
    # 少了这一行的表现不是"拒绝扩权",是**任务卡死**:事件没有任何一行接得住,于是状态不动、
    # 也没有人被通知——一个没有任何错误信息的静默停摆,正是这张表最该防的东西。
    #
    # **守卫用 `can_retry` 的补集,不用 `rounds_exhausted`。** 后者只问"轮次还有没有",而
    # `can_retry` 要的是"轮次还有 **且** 这次失败改代码能解决";两者不互补,于是"不可恢复
    # 但还有轮次"落在两行之间——恰恰是这一行存在的理由本身没被堵住。
    #
    # 带 `FREEZE`:从开发态升级人工时分支与工作区是活的,而人正要接手的就是它们。其余每一条
    # 从有活分支的状态升级的边都带它,少这一条没有任何理由。
    Transition(
        TaskState.DEVELOPING,
        TaskEvent.SCOPE_WIDENED,
        cannot_retry,
        TaskState.ESCALATED,
        (Effect.FREEZE, Effect.NOTIFY, Effect.TRIGGER_EVOLUTION),
    ),
    # 5
    Transition(
        TaskState.UNIT_TESTING,
        TaskEvent.GATE_PASS,
        needs_itest,
        TaskState.INTEGRATION_TESTING,
        (Effect.DISPATCH_ITEST,),
    ),
    # 6
    Transition(
        TaskState.UNIT_TESTING,
        TaskEvent.GATE_PASS,
        skips_itest,
        TaskState.READY_TO_COMMIT,
        (Effect.ENTER_COMMIT_PIPELINE,),
    ),
    # 7
    Transition(
        TaskState.UNIT_TESTING,
        TaskEvent.GATE_FAIL,
        can_retry,
        TaskState.DEVELOPING,
        (Effect.INJECT_FAILURE_REPORT, Effect.DISPATCH_DEV),
    ),
    # 8
    Transition(
        TaskState.UNIT_TESTING,
        TaskEvent.GATE_FAIL,
        must_escalate,
        TaskState.ESCALATED,
        (Effect.FREEZE, Effect.NOTIFY, Effect.TRIGGER_EVOLUTION),
    ),
    # 9
    Transition(
        TaskState.INTEGRATION_TESTING,
        TaskEvent.ITEST_PASS,
        None,
        TaskState.READY_TO_COMMIT,
        (Effect.ARCHIVE_REPORT, Effect.ENTER_COMMIT_PIPELINE),
    ),
    # 10
    Transition(
        TaskState.INTEGRATION_TESTING,
        TaskEvent.ITEST_FAIL,
        can_retry,
        TaskState.DEVELOPING,
        (Effect.INJECT_FAILURE_REPORT, Effect.DISPATCH_DEV),
    ),
    # 11
    Transition(
        TaskState.INTEGRATION_TESTING,
        TaskEvent.ITEST_FAIL,
        must_escalate,
        TaskState.ESCALATED,
        (Effect.FREEZE, Effect.NOTIFY, Effect.TRIGGER_EVOLUTION),
    ),
    # 12
    Transition(
        TaskState.READY_TO_COMMIT,
        TaskEvent.RISK_HIGH,
        None,
        TaskState.REVIEWING,
        (Effect.NOTIFY_APPROVER,),
    ),
    # 13
    Transition(
        TaskState.READY_TO_COMMIT,
        TaskEvent.RISK_LOW,
        prechecks_passed,
        TaskState.MERGING,
        (Effect.OPEN_PR,),
    ),
    # 14
    Transition(
        TaskState.READY_TO_COMMIT,
        TaskEvent.PRECHECK_FAIL,
        can_retry,
        TaskState.DEVELOPING,
        (Effect.INJECT_FAILURE_REPORT, Effect.DISPATCH_DEV),
    ),
    # 14b:与 7/8、10/11 同一套处置。**扫描工具没装是改代码解决不了的**——不分这一条的话,
    # 一个缺 gitleaks 的环境会把三轮修复全部烧掉,每一轮都以完全相同的方式失败。
    Transition(
        TaskState.READY_TO_COMMIT,
        TaskEvent.PRECHECK_FAIL,
        must_escalate,
        TaskState.ESCALATED,
        (Effect.FREEZE, Effect.NOTIFY, Effect.TRIGGER_EVOLUTION),
    ),
    # 15
    Transition(
        TaskState.REVIEWING,
        TaskEvent.APPROVE,
        approver_is_legitimate,
        TaskState.MERGING,
        (Effect.RECORD_APPROVAL, Effect.OPEN_PR),
    ),
    # 16
    Transition(
        TaskState.REVIEWING,
        TaskEvent.REJECT,
        None,
        TaskState.DEVELOPING,
        (Effect.INJECT_FAILURE_REPORT, Effect.DISPATCH_DEV),
    ),
    # 17
    Transition(
        TaskState.MERGING,
        TaskEvent.MERGED,
        None,
        TaskState.COMPLETED,
        (
            Effect.PROMOTE_VERIFICATION,
            Effect.TRIGGER_EVOLUTION,
            Effect.CLEANUP_WORKSPACE,
        ),
    ),
    # 18
    Transition(
        TaskState.MERGING,
        TaskEvent.MERGE_CONFLICT,
        None,
        TaskState.DEVELOPING,
        (Effect.INJECT_FAILURE_REPORT, Effect.DISPATCH_DEV),
    ),
    # 19
    Transition(
        ANY_STATE,
        TaskEvent.CANCEL,
        None,
        TaskState.CANCELLED,
        (Effect.CLEANUP_WORKSPACE,),
    ),
)


def decide(task: Task, event: TaskEvent, facts: Facts) -> Decision:
    """给定任务、事件与外部事实,算出该去哪。纯函数。"""
    if task.is_terminal:
        return Decision(False, reason=f"{task.state.value} 是终态,不再迁移(收到 {event.value})")

    # 取消优先于一切:预算用光了人更想取消它,而不是被告知"已经升级人工所以取消不了"。
    if event is not TaskEvent.CANCEL and facts.enforce_budget and _over_budget(task):
        return Decision(
            True,
            to=TaskState.ESCALATED,
            effects=(Effect.FREEZE, Effect.NOTIFY, Effect.TRIGGER_EVOLUTION),
            reason=(
                f"任务 token 预算已耗尽({task.tokens_used} / {task.budget_tokens}),"
                f"在 {task.state.value} 态升级人工"
            ),
        )

    candidates = [row for row in TRANSITIONS if row.from_state in (task.state, ANY_STATE)]
    matching = [row for row in candidates if row.event is event]
    if not matching:
        return Decision(
            False, reason=f"非法迁移: {task.state.value} 收到 {event.value},表里没有这条边"
        )

    for row in matching:
        if row.guard is None or row.guard(task, facts):
            return Decision(
                True,
                to=row.to,
                effects=row.effects,
                fix_rounds_delta=_round_delta(task.state, row.to),
                plan_retries_delta=1 if row.event is TaskEvent.PLAN_FAILED else 0,
                reason=_escalation_reason(row.to, task, event, facts),
            )

    blocked = "; ".join(
        _GUARD_REASONS.get(row.guard, "守卫未通过") for row in matching if row.guard
    )
    return Decision(False, reason=f"{task.state.value} 收到 {event.value} 但守卫未通过: {blocked}")


def _escalation_reason(
    to: TaskState, task: Task, event: TaskEvent, facts: Facts
) -> str:
    """升级人工时说清楚为什么。

    "失败"不是原因。我接手时第一个问题永远是"从哪开始查",而"环境问题"与"改了三轮
    还是不对"指向完全不同的下一步。
    """
    if to is not TaskState.ESCALATED:
        return ""
    if event is TaskEvent.PLAN_FAILED:
        if facts.plan_failure_cause is PlanFailureCause.DELIVERY:
            return "需求解析的结构化产物交付失败；请检查运行时、输出 schema 或终态解析日志"
        if facts.plan_failure_cause is PlanFailureCause.CONTRACT:
            return "需求解析产物不符合契约；请检查 result.json 与输出 schema，不需要修改需求"
        if facts.plan_failure_cause is PlanFailureCause.RUNTIME:
            return "需求解析运行时失败；请检查子进程、超时设置和 Job 日志，不需要修改需求"
        if facts.plan_failure_cause is PlanFailureCause.LIMIT:
            return "需求解析被运行额度中止；请检查 Job/任务预算与执行上限，不需要修改需求"
        if facts.plan_failure_cause is PlanFailureCause.SCOPE:
            return "需求解析员工越出授权范围；请检查员工权限与工序写集，不需要修改需求"
        if facts.plan_failure_cause is PlanFailureCause.UNKNOWN:
            return "需求解析留下了未知失败类型；请检查产物 manifest 与 Job 日志，不需要修改需求"
        if not facts.plan_retryable:
            return "需求信息不足，需要人工修改或澄清；自动重试不会产生缺失的信息"
        return f"需求解析重试次数已达上限 {MAX_PLAN_RETRIES},需要人工修改或澄清需求"
    if not facts.recoverable:
        return f"在 {task.state.value} 态遇到改代码解决不了的失败(环境或配置问题)"
    return f"在 {task.state.value} 态修复轮次已达上限 {facts.max_fix_rounds}"


def visible_escalation_reason(task: Task) -> str | None:
    """当前应展示的升级原因，兼容升级前已经写入错误通用文案的任务。"""
    reason = task.escalate_reason
    if (
        task.state is TaskState.ESCALATED
        and task.plan_retries > 0
        and reason
        and "CREATED 态修复轮次已达上限" in reason
    ):
        return f"需求解析重试次数已达上限 {MAX_PLAN_RETRIES},需要人工修改或澄清需求"
    return reason


def _over_budget(task: Task) -> bool:
    return task.budget_tokens is not None and task.tokens_used >= task.budget_tokens


def _round_delta(from_state: TaskState, to: TaskState) -> int:
    """回到开发态即算重来一轮。

    第 1 行(CREATED → DEVELOPING)是任务第一次开工,不是重来——把它也加一的话每个任务
    都平白少一轮可用。
    """
    if to is TaskState.DEVELOPING and from_state is not TaskState.CREATED:
        return 1
    return 0


__all__ = [
    "ANY_STATE",
    "MAX_PLAN_RETRIES",
    "visible_escalation_reason",
    "TRANSITIONS",
    "Decision",
    "Effect",
    "Facts",
    "PlanFailureCause",
    "Transition",
    "decide",
]
