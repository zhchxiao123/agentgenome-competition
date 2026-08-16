"""基因组任务的迁移表。

与研发任务那张表并列而不是共用。理由不是洁癖:两类任务的状态集**没有交集**,合成一张表之后,
读表的人会看到一堆对某一类永远不成立的边——而这张表存在的全部价值就是"迁移即文档,评审直读"。

## 待确认是表里唯一一条"停下来等人"的边,而它不是失败

它与研发任务的已升级人工正交:那条是计划外的停手(任务受伤,人做的是诊断);这条是计划内的
闸门(任务健康,人做的是确认或调整)。表现在这张表上就是——待确认**不带冻结、不带失败原因**,
而且提醒事件把它留在原地,不推它去任何别的状态。一个等人的健康任务不该因为人休假而失败。

## 失败要不要通知,看发起方

人敲了命令正在等结果的初始化失败了要通知;系统自己排的蒸馏失败了只留一条事件。**同样是失败,
响应完全不同**——所以"失败"本身不是判据,"有没有人在等"才是。这条判据在这里和
`GenomeTask.is_settled` 各出现一次,两处说的是同一件事的两面:通知与列表。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from agentgenome.core.genome_task import GenomeTask, GenomeTaskKind, GenomeTaskState, Origin


class GenomeEvent(StrEnum):
    """驱动基因组任务迁移的事件。"""

    #: 扫描完、边界草案已落盘。
    DRAFT_READY = "draft_ready"
    #: 人答复了闸门。
    CONFIRMED = "confirmed"
    #: 人确认了模块验证候选；它不需要再进入知识深读阶段。
    VERIFICATION_CONFIRMED = "verification_confirmed"
    #: 逐模块深读完成(蒸馏走同一条边:它的"读"是收集素材)。
    READ_DONE = "read_done"
    #: 汇总完成、知识变更已提交。
    SUBMITTED = "submitted"
    FAILED = "failed"
    CANCEL = "cancel"
    #: 闸门等太久了。**它不改变状态**,只让人再看见一次。
    REMINDED = "reminded"


class GenomeEffect(StrEnum):
    """迁移的副作用。这一层不执行任何东西,只说"该做什么"。"""

    #: 提醒该回答闸门的人。**进的是待办队列,不是异常队列。**
    NOTIFY_CONFIRMER = "notify_confirmer"
    #: 通知发起人任务失败了。只在有人在等的时候发。
    NOTIFY_OWNER = "notify_owner"
    ARCHIVE_REPORT = "archive_report"


@dataclass(frozen=True)
class GenomeFacts:
    """守卫需要的、任务模型上没有的事实。

    由调用方在迁移前算好再传进来——让守卫自己去读产物的话,这一层就不纯了,而它的全部价值
    就在于纯。
    """

    #: 边界草案落盘了吗。**没有草案的待确认是个死局**:两条入口都会拒绝回答,
    #: 而任务永远等不到答复——它看起来在等人,实际上谁也回答不了。
    draft_exists: bool = True
    #: 闸门答复合不合 schema。不合的话任务留在待确认,不往下走。
    answer_valid: bool = True
    #: **为什么停在这儿**,用「下一步该从哪查」的口径写。失败与取消都用它——
    #: 「这次没什么可蒸馏的」也是一个需要被记下来的停下理由,而它不是失败。
    #: **「失败」两个字不是原因**:「第三个模块深读超时」与「归档盘写不进去」指向完全
    #: 不同的人工动作。
    stop_reason: str = ""
    #: Token 预算是否作为迁移门禁。关闭时仍计量，但不因超额转 FAILED。
    enforce_budget: bool = True


@dataclass(frozen=True)
class GenomeDecision:
    allowed: bool
    to: GenomeTaskState | None = None
    effects: tuple[GenomeEffect, ...] = ()
    #: 被拒或停下时说清楚为什么。它进事件流,也进任务的停下原因。
    reason: str = ""


Guard = Callable[[GenomeTask, GenomeFacts], bool]

#: 表里表示"从任何状态出发"。
ANY_STATE = None


@dataclass(frozen=True)
class GenomeTransition:
    """表里的一行。**字段顺序与研发任务那张表刻意一致**——两张表会被并排读,
    第三列的含义不同的话,读的人会把它们看串。"""

    from_state: GenomeTaskState | None
    event: GenomeEvent
    guard: Guard | None
    to: GenomeTaskState
    effects: tuple[GenomeEffect, ...] = ()


def answer_is_valid(task: GenomeTask, facts: GenomeFacts) -> bool:
    return facts.answer_valid


def draft_is_on_disk(task: GenomeTask, facts: GenomeFacts) -> bool:
    return facts.draft_exists


def is_verification_task(task: GenomeTask, facts: GenomeFacts) -> bool:
    return task.kind is GenomeTaskKind.VERIFICATION and facts.answer_valid


#: 守卫没过时说给人听的那句话。**按守卫查,不是一句写死的文案**——第二个守卫加进来的时候,
#: 写死的那句会给出一个与实际原因无关的解释,而那比不解释更误导。
_GUARD_REASONS: dict[Guard, str] = {
    answer_is_valid: "闸门答复不合契约,任务留在待确认",
    draft_is_on_disk: "边界草案还没落盘,进了待确认也没人回答得了",
}


#: 完整迁移表。**顺序有意义**:同一个 (from, event) 的多行按先后试守卫,第一条过的生效。
TRANSITIONS: tuple[GenomeTransition, ...] = (
    GenomeTransition(
        GenomeTaskState.SCANNING,
        GenomeEvent.DRAFT_READY,
        draft_is_on_disk,
        GenomeTaskState.AWAITING_CONFIRMATION,
        effects=(GenomeEffect.NOTIFY_CONFIRMER,),
    ),
    GenomeTransition(
        GenomeTaskState.AWAITING_CONFIRMATION,
        GenomeEvent.VERIFICATION_CONFIRMED,
        is_verification_task,
        GenomeTaskState.SUBMITTED,
        effects=(GenomeEffect.ARCHIVE_REPORT,),
    ),
    GenomeTransition(
        GenomeTaskState.AWAITING_CONFIRMATION,
        GenomeEvent.CONFIRMED,
        answer_is_valid,
        GenomeTaskState.DEEP_READ,
    ),
    # 提醒把任务留在原地。**这一条是这张表最容易写错的地方**:让它迁到任何别的状态,
    # 一个等人的健康任务就会因为人休假而离开待确认。
    GenomeTransition(
        GenomeTaskState.AWAITING_CONFIRMATION,
        GenomeEvent.REMINDED,
        None,
        GenomeTaskState.AWAITING_CONFIRMATION,
        effects=(GenomeEffect.NOTIFY_CONFIRMER,),
    ),
    # 蒸馏与卡片补写不经过闸门:它们没有什么要问人的。
    GenomeTransition(
        GenomeTaskState.SCANNING, GenomeEvent.READ_DONE, None, GenomeTaskState.SUMMARISING
    ),
    GenomeTransition(
        GenomeTaskState.DEEP_READ, GenomeEvent.READ_DONE, None, GenomeTaskState.SUMMARISING
    ),
    GenomeTransition(
        GenomeTaskState.SUMMARISING,
        GenomeEvent.SUBMITTED,
        None,
        GenomeTaskState.SUBMITTED,
        effects=(GenomeEffect.ARCHIVE_REPORT,),
    ),
    GenomeTransition(ANY_STATE, GenomeEvent.CANCEL, None, GenomeTaskState.CANCELLED),
    # 失败进表而不是留在 `decide` 的特判里。**这张表的全部价值是"迁移即文档,评审直读"**,
    # 而一条只存在于代码分支里的边,在表上是看不见的——读表的人会以为任务不会失败。
    # 它的副作用按发起方算,所以是在 `decide` 里补的,不是写死在行上。
    GenomeTransition(ANY_STATE, GenomeEvent.FAILED, None, GenomeTaskState.FAILED),
)


def _over_budget(task: GenomeTask) -> bool:
    return task.budget_tokens is not None and task.tokens_used >= task.budget_tokens


def _failure_effects(task: GenomeTask) -> tuple[GenomeEffect, ...]:
    """失败要不要惊动人,看有没有人在等。"""
    if task.origin is Origin.HUMAN:
        return (GenomeEffect.NOTIFY_OWNER,)
    return ()


def decide(task: GenomeTask, event: GenomeEvent, facts: GenomeFacts) -> GenomeDecision:
    """给定任务、事件与外部事实,算出该去哪。纯函数。"""
    if task.is_terminal:
        return GenomeDecision(
            False, reason=f"{task.state.value} 是终态,不再迁移(收到 {event.value})"
        )

    matching = [
        row
        for row in TRANSITIONS
        if row.from_state in (task.state, ANY_STATE) and row.event is event
    ]
    if not matching:
        return GenomeDecision(
            False, reason=f"非法迁移: {task.state.value} 收到 {event.value},表里没有这条边"
        )

    for row in matching:
        if row.guard is not None and not row.guard(task, facts):
            continue
        # **预算是横切规则,但它不压过"停下来"与"原地不动"这两类边。**
        #
        # 压过取消:预算用光了人更想取消它,而不是被告知"已经失败所以取消不了"。
        # 压过失败:它本来就要停,再判一次只会把真正的失败原因换成"预算耗尽"。
        # 压过自环:**一条提醒不该杀掉一个正在等人的健康任务**——它没在烧 token,
        # 而"等人的任务不因人休假而失败"正是待确认这个状态存在的理由。
        if (
            facts.enforce_budget
            and _over_budget(task)
            and row.to is not task.state
            and not _is_stop(row.to)
        ):
            return GenomeDecision(
                True,
                to=GenomeTaskState.FAILED,
                effects=_failure_effects(task),
                reason=(
                    f"token 预算已耗尽({task.tokens_used} / {task.budget_tokens}),"
                    f"在 {task.state.value} 态停下"
                ),
            )
        effects = row.effects
        # 停下的理由**失败与取消都带**:「这次没什么可蒸馏的」也是一个需要被记下来的
        # 停下理由,而它不是失败。只给失败带的话,一次正常的跳过在记录上会一片空白。
        reason = facts.stop_reason if _is_stop(row.to) else ""
        if row.to is GenomeTaskState.FAILED:
            effects = (*effects, *_failure_effects(task))
        return GenomeDecision(True, to=row.to, effects=effects, reason=reason)

    guard = next(row.guard for row in matching if row.guard is not None)
    return GenomeDecision(
        False,
        reason=_GUARD_REASONS.get(guard, f"{task.state.value} 收到 {event.value},但守卫没过"),
    )


def _is_stop(state: GenomeTaskState) -> bool:
    """这条边本来就是"停下来"吗。"""
    return state in {GenomeTaskState.CANCELLED, GenomeTaskState.FAILED}


__all__ = [
    "ANY_STATE",
    "TRANSITIONS",
    "GenomeDecision",
    "GenomeEffect",
    "GenomeEvent",
    "GenomeFacts",
    "GenomeTransition",
    "decide",
]
