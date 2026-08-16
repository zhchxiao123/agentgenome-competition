"""迁移表:附录 A 的 19 条边逐行覆盖,加上非法组合与横切规则。

**这一层是纯的**——没有 tmp_path、没有数据库、没有子进程。整个仓库最密集也最快的
测试区应该在这里:流程的正确性靠这张表,而表一旦沾上 I/O,19 条边的全覆盖就会变成
19 个慢测试。
"""

from __future__ import annotations

import pytest

from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.task import ItestNeed, Task
from agentgenome.core.transitions import (
    TRANSITIONS,
    Decision,
    Effect,
    Facts,
    PlanFailureCause,
    decide,
)


def _task(state: TaskState = TaskState.CREATED, **kwargs: object) -> Task:
    return Task(id="ag-1", title="t", requirement="r", state=state, **kwargs)  # type: ignore[arg-type]


def _decide(
    state: TaskState, event: TaskEvent, facts: Facts | None = None, **task_kwargs: object
) -> Decision:
    return decide(_task(state, **task_kwargs), event, facts or Facts(max_fix_rounds=3))


# --- 附录 A 逐行 -------------------------------------------------------------
#
# 每一行至少一个用例。参数化的形式让"表里有的边都测了"这件事本身可读。


@pytest.mark.parametrize(
    ("row", "state", "event", "facts", "task_kwargs", "expected"),
    [
        (1, TaskState.CREATED, TaskEvent.PLAN_DONE, Facts(3), {}, TaskState.DEVELOPING),
        (
            2,
            TaskState.CREATED,
            TaskEvent.PLAN_FAILED,
            Facts(3, plan_retryable=True),
            {"plan_retries": 0},
            TaskState.CREATED,
        ),
        (
            3,
            TaskState.CREATED,
            TaskEvent.PLAN_FAILED,
            Facts(3),
            {"plan_retries": 1},
            TaskState.ESCALATED,
        ),
        (4, TaskState.DEVELOPING, TaskEvent.DEV_DONE, Facts(3), {}, TaskState.UNIT_TESTING),
        (
            5,
            TaskState.UNIT_TESTING,
            TaskEvent.GATE_PASS,
            Facts(3),
            {"needs_itest": ItestNeed.YES},
            TaskState.INTEGRATION_TESTING,
        ),
        (
            6,
            TaskState.UNIT_TESTING,
            TaskEvent.GATE_PASS,
            Facts(3),
            {"needs_itest": ItestNeed.NO},
            TaskState.READY_TO_COMMIT,
        ),
        (
            7,
            TaskState.UNIT_TESTING,
            TaskEvent.GATE_FAIL,
            Facts(3),
            {"fix_rounds": 1},
            TaskState.DEVELOPING,
        ),
        (
            8,
            TaskState.UNIT_TESTING,
            TaskEvent.GATE_FAIL,
            Facts(3),
            {"fix_rounds": 3},
            TaskState.ESCALATED,
        ),
        (
            9,
            TaskState.INTEGRATION_TESTING,
            TaskEvent.ITEST_PASS,
            Facts(3),
            {},
            TaskState.READY_TO_COMMIT,
        ),
        (
            10,
            TaskState.INTEGRATION_TESTING,
            TaskEvent.ITEST_FAIL,
            Facts(3),
            {"fix_rounds": 0},
            TaskState.DEVELOPING,
        ),
        (
            11,
            TaskState.INTEGRATION_TESTING,
            TaskEvent.ITEST_FAIL,
            Facts(3),
            {"fix_rounds": 3},
            TaskState.ESCALATED,
        ),
        (12, TaskState.READY_TO_COMMIT, TaskEvent.RISK_HIGH, Facts(3), {}, TaskState.REVIEWING),
        (13, TaskState.READY_TO_COMMIT, TaskEvent.RISK_LOW, Facts(3), {}, TaskState.MERGING),
        (
            14,
            TaskState.READY_TO_COMMIT,
            TaskEvent.PRECHECK_FAIL,
            Facts(3),
            {},
            TaskState.DEVELOPING,
        ),
        (15, TaskState.REVIEWING, TaskEvent.APPROVE, Facts(3), {}, TaskState.MERGING),
        (16, TaskState.REVIEWING, TaskEvent.REJECT, Facts(3), {}, TaskState.DEVELOPING),
        (17, TaskState.MERGING, TaskEvent.MERGED, Facts(3), {}, TaskState.COMPLETED),
        (18, TaskState.MERGING, TaskEvent.MERGE_CONFLICT, Facts(3), {}, TaskState.DEVELOPING),
        (19, TaskState.DEVELOPING, TaskEvent.CANCEL, Facts(3), {}, TaskState.CANCELLED),
    ],
)
def test_every_row_of_the_table(
    row: int,
    state: TaskState,
    event: TaskEvent,
    facts: Facts,
    task_kwargs: dict,
    expected: TaskState,
) -> None:
    decision = _decide(state, event, facts, **task_kwargs)

    assert decision.allowed is True, f"第 {row} 行被拒了: {decision.reason}"
    assert decision.to is expected, f"第 {row} 行落错了状态"


def test_the_table_has_exactly_the_rows_of_appendix_a() -> None:
    """表长度变了就是有人加了或删了一条边——那该是一次显式的评审,不是顺手改。

    第 21、22 行是扩权自环与它的轮次耗尽分支。**扩权刻意是一条边而不是一个新状态**:
    新状态要动迁移表、控制面、看板、报告,而它换来的东西比不上那个代价。
    """
    assert len(TRANSITIONS) == 22


# --- 副作用 -----------------------------------------------------------------


def test_entering_development_creates_the_workspace_and_dispatches() -> None:
    decision = _decide(TaskState.CREATED, TaskEvent.PLAN_DONE)

    assert Effect.CREATE_WORKSPACE in decision.effects
    assert Effect.DISPATCH_DEV in decision.effects


def test_a_failing_gate_injects_the_report() -> None:
    decision = _decide(TaskState.UNIT_TESTING, TaskEvent.GATE_FAIL, fix_rounds=0)

    assert Effect.INJECT_FAILURE_REPORT in decision.effects


def test_escalating_freezes_the_branch() -> None:
    """我接手时要能看到完整的失败历史,不是一个被清理干净的空目录。"""
    decision = _decide(TaskState.UNIT_TESTING, TaskEvent.GATE_FAIL, fix_rounds=3)

    assert Effect.FREEZE in decision.effects
    assert Effect.NOTIFY in decision.effects


def test_completing_cleans_up_and_triggers_evolution() -> None:
    decision = _decide(TaskState.MERGING, TaskEvent.MERGED)

    assert Effect.CLEANUP_WORKSPACE in decision.effects
    assert Effect.PROMOTE_VERIFICATION in decision.effects
    assert Effect.TRIGGER_EVOLUTION in decision.effects


def test_cancelling_cleans_up() -> None:
    decision = _decide(TaskState.DEVELOPING, TaskEvent.CANCEL)

    assert Effect.CLEANUP_WORKSPACE in decision.effects


@pytest.mark.parametrize(
    ("state", "event", "task_kwargs"),
    [
        (TaskState.CREATED, TaskEvent.PLAN_FAILED, {"plan_retries": 1}),
        (TaskState.UNIT_TESTING, TaskEvent.GATE_FAIL, {"fix_rounds": 3}),
        (TaskState.INTEGRATION_TESTING, TaskEvent.ITEST_FAIL, {"fix_rounds": 3}),
        (TaskState.READY_TO_COMMIT, TaskEvent.PRECHECK_FAIL, {"fix_rounds": 3}),
    ],
)
def test_every_escalating_edge_also_triggers_evolution(
    state: TaskState, event: TaskEvent, task_kwargs: dict
) -> None:
    """ESCALATED 任务是最富矿的蒸馏素材(design doc §10.1)——回归测试:此前只有第 17 行
    (MERGED→COMPLETED)带 `TRIGGER_EVOLUTION`,四条 ESCALATED 出边都漏了,任务升级人工
    之后从来没有排队蒸馏过,而且没有任何症状。
    """
    decision = _decide(state, event, Facts(3), **task_kwargs)

    assert decision.to is TaskState.ESCALATED, f"这个用例本该走到 ESCALATED: {decision.reason}"
    assert Effect.TRIGGER_EVOLUTION in decision.effects


def test_running_out_of_budget_also_triggers_evolution() -> None:
    """预算耗尽走的是横切规则,不经过表——同样不该漏掉蒸馏排队。"""
    decision = _decide(
        TaskState.DEVELOPING, TaskEvent.DEV_DONE, budget_tokens=1000, tokens_used=1000
    )

    assert decision.to is TaskState.ESCALATED
    assert Effect.TRIGGER_EVOLUTION in decision.effects


# --- 轮次统一递增 -----------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "event"),
    [
        (TaskState.UNIT_TESTING, TaskEvent.GATE_FAIL),
        (TaskState.INTEGRATION_TESTING, TaskEvent.ITEST_FAIL),
        (TaskState.READY_TO_COMMIT, TaskEvent.PRECHECK_FAIL),
        (TaskState.REVIEWING, TaskEvent.REJECT),
        (TaskState.MERGING, TaskEvent.MERGE_CONFLICT),
    ],
)
def test_every_edge_back_to_developing_increments_the_round(
    state: TaskState, event: TaskEvent
) -> None:
    """轮次的语义是"这个任务重来了几次",不区分为什么重来。

    不统一的话第 14 条边(precheck_fail)可以无限循环——它是三重上限里唯一漏掉的一条路。
    """
    decision = _decide(state, event, fix_rounds=0)

    assert decision.to is TaskState.DEVELOPING
    assert decision.fix_rounds_delta == 1, f"{state.value} --{event.value}--> 没有加轮次"


def test_the_first_entry_into_development_is_not_a_redo() -> None:
    """第 1 行是任务第一次开工,不是重来。把它也加一的话每个任务都平白少一轮。"""
    decision = _decide(TaskState.CREATED, TaskEvent.PLAN_DONE)

    assert decision.fix_rounds_delta == 0


def test_a_plan_retry_counts_separately_from_fix_rounds() -> None:
    """需求解析重试与代码修复轮次是两件事:前者上限 1,后者上限 3。"""
    decision = _decide(
        TaskState.CREATED,
        TaskEvent.PLAN_FAILED,
        Facts(max_fix_rounds=3, plan_retryable=True),
        plan_retries=0,
    )

    assert decision.plan_retries_delta == 1
    assert decision.fix_rounds_delta == 0


def test_a_plan_that_requests_clarification_does_not_guess_again() -> None:
    decision = _decide(TaskState.CREATED, TaskEvent.PLAN_FAILED, plan_retries=0)

    assert decision.to is TaskState.ESCALATED
    assert "澄清" in decision.reason
    assert "自动重试不会" in decision.reason


def test_a_plan_protocol_failure_does_not_blame_the_requirement() -> None:
    decision = _decide(
        TaskState.CREATED,
        TaskEvent.PLAN_FAILED,
        Facts(max_fix_rounds=3, plan_failure_cause=PlanFailureCause.DELIVERY),
        plan_retries=0,
    )

    assert decision.to is TaskState.ESCALATED
    assert "结构化产物交付失败" in decision.reason
    assert "需求信息不足" not in decision.reason


@pytest.mark.parametrize(
    ("cause", "next_check"),
    [
        (PlanFailureCause.CONTRACT, "result.json"),
        (PlanFailureCause.RUNTIME, "Job 日志"),
        (PlanFailureCause.LIMIT, "预算"),
        (PlanFailureCause.SCOPE, "权限"),
        (PlanFailureCause.UNKNOWN, "manifest"),
    ],
)
def test_every_nonsemantic_plan_failure_points_to_the_right_layer(
    cause: PlanFailureCause, next_check: str
) -> None:
    decision = _decide(
        TaskState.CREATED,
        TaskEvent.PLAN_FAILED,
        Facts(max_fix_rounds=3, plan_failure_cause=cause),
    )

    assert next_check in decision.reason
    assert "需求信息不足" not in decision.reason


def test_disabled_budget_enforcement_does_not_change_the_flow() -> None:
    decision = _decide(
        TaskState.DEVELOPING,
        TaskEvent.DEV_DONE,
        Facts(max_fix_rounds=3, enforce_budget=False),
        budget_tokens=100,
        tokens_used=1_000,
    )

    assert decision.to is TaskState.UNIT_TESTING


def test_plan_retry_exhaustion_names_the_stage_that_needs_human_help() -> None:
    """需求解析失败不能误报成「代码修复三轮」。那会把人带去完全错误的处理入口。"""
    decision = _decide(
        TaskState.CREATED,
        TaskEvent.PLAN_FAILED,
        Facts(max_fix_rounds=3, plan_retryable=True),
        plan_retries=1,
    )

    assert decision.to is TaskState.ESCALATED
    assert "需求解析" in decision.reason
    assert "1" in decision.reason
    assert "修复轮次" not in decision.reason


# --- 非法迁移 ---------------------------------------------------------------


def test_an_event_the_table_does_not_know_is_refused() -> None:
    """悄悄进入不可能的状态比报错难查得多。"""
    decision = _decide(TaskState.CREATED, TaskEvent.MERGED)

    assert decision.allowed is False
    assert decision.to is None
    assert "CREATED" in decision.reason
    assert "merged" in decision.reason


def test_a_refused_transition_has_no_effects() -> None:
    decision = _decide(TaskState.CREATED, TaskEvent.MERGED)

    assert decision.effects == ()
    assert decision.fix_rounds_delta == 0


@pytest.mark.parametrize("state", sorted(TaskState.terminal()))
@pytest.mark.parametrize("event", sorted(TaskEvent))
def test_terminal_states_never_move(state: TaskState, event: TaskEvent) -> None:
    """终态就是终点。任何事件都不该把一个已完成的任务拉回流程里。"""
    decision = _decide(state, event)

    assert decision.allowed is False


def test_settled_is_terminal_minus_escalated() -> None:
    """两个词必须差且只差一个 `ESCALATED`。

    把它们合并回一个概念,是这个仓库真实犯过的错:升级人工的任务在所有列表里消失,
    而 `GET /tasks/{id}` 照常返回——于是"我提的任务哪去了"没有任何地方能回答。
    反过来,如果 `settled` 悄悄多含了别的状态,那些任务就再也不会出现在待办里。
    """
    assert TaskState.settled() == TaskState.terminal() - {TaskState.ESCALATED}


@pytest.mark.parametrize("state", sorted(TaskState.settled()))
@pytest.mark.parametrize("event", sorted(TaskEvent))
def test_settled_states_never_move_either(state: TaskState, event: TaskEvent) -> None:
    """`settled` 只放宽"要不要显示",不放宽"能不能迁移"。"""
    assert _decide(state, event).allowed is False


@pytest.mark.parametrize("state", [s for s in TaskState if s not in TaskState.terminal()])
def test_cancel_works_from_every_non_terminal_state(state: TaskState) -> None:
    """第 19 行的 `*` 是真的全覆盖,不是列举了几个常见状态。"""
    decision = _decide(state, TaskEvent.CANCEL)

    assert decision.allowed is True
    assert decision.to is TaskState.CANCELLED


# --- 守卫 -------------------------------------------------------------------


def test_an_invalid_plan_blocks_the_first_edge() -> None:
    decision = _decide(TaskState.CREATED, TaskEvent.PLAN_DONE, Facts(3, plan_valid=False))

    assert decision.allowed is False
    assert "计划" in decision.reason


def test_an_invalid_result_blocks_the_hand_off_to_the_gate() -> None:
    decision = _decide(TaskState.DEVELOPING, TaskEvent.DEV_DONE, Facts(3, result_valid=False))

    assert decision.allowed is False


def test_an_undecided_itest_need_is_not_treated_as_no() -> None:
    """ "还没判断"被当成"不用跑"的话,该跑集成测试的改动会一路绿灯合进去。"""
    decision = _decide(TaskState.UNIT_TESTING, TaskEvent.GATE_PASS, needs_itest=ItestNeed.UNDECIDED)

    assert decision.allowed is False
    assert "needs_itest" in decision.reason


def test_an_illegitimate_approver_blocks_the_merge() -> None:
    decision = _decide(TaskState.REVIEWING, TaskEvent.APPROVE, Facts(3, approver_ok=False))

    assert decision.allowed is False


def test_failing_prechecks_block_the_auto_merge() -> None:
    decision = _decide(
        TaskState.READY_TO_COMMIT, TaskEvent.RISK_LOW, Facts(3, prechecks_passed=False)
    )

    assert decision.allowed is False


def test_the_round_limit_comes_from_the_facts_not_a_constant() -> None:
    """上限来自规则文件,项目可以自己定。写死的话规则层就白做了。"""
    below = _decide(TaskState.UNIT_TESTING, TaskEvent.GATE_FAIL, Facts(5), fix_rounds=4)
    at = _decide(TaskState.UNIT_TESTING, TaskEvent.GATE_FAIL, Facts(5), fix_rounds=5)

    assert below.to is TaskState.DEVELOPING
    assert at.to is TaskState.ESCALATED


# --- 预算横切规则 -----------------------------------------------------------


def test_running_out_of_budget_escalates_from_any_state() -> None:
    """写成表里的边的话,每个状态都要加一行,而漏掉的那一行不会有任何症状。"""
    for state in (TaskState.DEVELOPING, TaskState.UNIT_TESTING, TaskState.READY_TO_COMMIT):
        decision = _decide(state, TaskEvent.DEV_DONE, budget_tokens=1000, tokens_used=1000)

        assert decision.to is TaskState.ESCALATED, state.value
        assert "预算" in decision.reason


def test_the_budget_rule_wins_over_the_table() -> None:
    """否则一个已经超预算的任务还能顺着表继续走下去,继续烧钱。"""
    decision = _decide(
        TaskState.UNIT_TESTING,
        TaskEvent.GATE_PASS,
        needs_itest=ItestNeed.NO,
        budget_tokens=1000,
        tokens_used=1200,
    )

    assert decision.to is TaskState.ESCALATED


def test_cancel_still_works_when_the_budget_is_blown() -> None:
    """预算用光了我更想取消它,而不是被告知"已经升级人工所以取消不了"。"""
    decision = _decide(TaskState.DEVELOPING, TaskEvent.CANCEL, budget_tokens=1000, tokens_used=9999)

    assert decision.to is TaskState.CANCELLED


def test_a_task_without_a_budget_is_not_escalated() -> None:
    decision = _decide(TaskState.DEVELOPING, TaskEvent.DEV_DONE, tokens_used=10**9)

    assert decision.to is TaskState.UNIT_TESTING


# --- 状态图 -----------------------------------------------------------------


def test_the_diagram_has_one_edge_per_row() -> None:
    """手画的图三个月后必然与代码对不上,而对不上的图比没有图更误导。"""
    from agentgenome.core.diagram import edge_count, render_mermaid

    assert render_mermaid().startswith("stateDiagram-v2")
    assert edge_count() == len(TRANSITIONS)


def test_the_diagram_names_the_guards() -> None:
    """ "什么时候会走这条边"是读图的人第一个要问的。"""
    from agentgenome.core.diagram import render_mermaid

    assert "can_retry" in render_mermaid()


# --- 改代码解决不了的失败 ---------------------------------------------------


def test_an_unrecoverable_gate_failure_escalates_even_with_rounds_left() -> None:
    """让 AI 反复尝试去修"环境里没装 gitleaks"是最典型的预算浪费方式。"""
    decision = _decide(
        TaskState.UNIT_TESTING, TaskEvent.GATE_FAIL, Facts(3, recoverable=False), fix_rounds=0
    )

    assert decision.to is TaskState.ESCALATED
    assert decision.fix_rounds_delta == 0, "环境问题不该吃掉一轮修复额度"


def test_an_unrecoverable_itest_failure_escalates_too() -> None:
    decision = _decide(
        TaskState.INTEGRATION_TESTING,
        TaskEvent.ITEST_FAIL,
        Facts(3, recoverable=False),
        fix_rounds=0,
    )

    assert decision.to is TaskState.ESCALATED


def test_a_recoverable_failure_still_goes_back_to_development() -> None:
    """只测拦得住不测放得过,等于没测。"""
    decision = _decide(
        TaskState.UNIT_TESTING, TaskEvent.GATE_FAIL, Facts(3, recoverable=True), fix_rounds=0
    )

    assert decision.to is TaskState.DEVELOPING
    assert decision.fix_rounds_delta == 1


def test_the_escalation_reason_distinguishes_the_two_causes() -> None:
    """ "环境问题"与"改了三轮还是不对"指向完全不同的下一步。"""
    environment = _decide(
        TaskState.UNIT_TESTING, TaskEvent.GATE_FAIL, Facts(3, recoverable=False), fix_rounds=0
    )
    exhausted = _decide(TaskState.UNIT_TESTING, TaskEvent.GATE_FAIL, Facts(3), fix_rounds=3)

    assert "环境" in environment.reason
    assert "轮次" in exhausted.reason


def test_the_table_is_still_twenty_two_rows() -> None:
    """守卫分叉不该让表长出新的一行——`failure_kind` 是产物里的字段,不是事件。

    第 20 行是 PRD 08 显式加的:`precheck_fail` 分成"还能修"与"改代码解决不了"两条,
    与 7/8、10/11 同一套处置。不分的话,一个缺 gitleaks 的环境会把三轮修复全部烧掉。

    第 21、22 行是 PRD 32 显式加的扩权自环与它的轮次耗尽分支——
    少了后者的表现不是拒绝扩权,是任务静默卡死。
    """
    assert len(TRANSITIONS) == 22


# --- 扩权自环 ---------------------------------------------------------------


def test_widening_the_scope_goes_back_to_development_and_costs_a_round() -> None:
    """扩权也要烧一轮 Job,没有理由让它绕过修复轮次那条闸。"""
    decision = _decide(TaskState.DEVELOPING, TaskEvent.SCOPE_WIDENED, fix_rounds=0)

    assert decision.to is TaskState.DEVELOPING
    assert decision.fix_rounds_delta == 1
    assert Effect.DISPATCH_DEV in decision.effects


def test_widening_cannot_outlive_the_fix_round_budget() -> None:
    """轮次用完了就转人工——否则"一路申请一路扩"会变成绕过上限的常规手段。"""
    decision = _decide(TaskState.DEVELOPING, TaskEvent.SCOPE_WIDENED, Facts(3), fix_rounds=3)

    assert decision.to is TaskState.ESCALATED


def test_the_two_widening_rows_are_exhaustive() -> None:
    """**不可恢复但还有轮次**此前落在两行之间:两个守卫都不匹配,事件没人接,任务静默卡死。

    而 4c 存在的全部理由就是防这个。守卫改成互补之后,任何一种事实组合都必然命中一行。
    """
    decision = _decide(
        TaskState.DEVELOPING, TaskEvent.SCOPE_WIDENED, Facts(3, recoverable=False), fix_rounds=0
    )

    assert decision.to is TaskState.ESCALATED


def test_escalating_from_development_freezes_the_branch() -> None:
    """人正要接手的就是那个分支与工作区。其余每条从有活分支的状态升级的边都冻结。"""
    decision = _decide(
        TaskState.DEVELOPING, TaskEvent.SCOPE_WIDENED, Facts(3, recoverable=False), fix_rounds=0
    )

    assert Effect.FREEZE in decision.effects
