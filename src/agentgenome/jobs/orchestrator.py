"""编排器:把处理器、迁移表、事件流与存储接成一台机器。

## 一次 advance 做的事

查处理器 → 先看产物在不在(幂等)→ 不在就跑 → 拿到事件 → 过迁移表 → 落库 → 记事件 →
执行副作用。**顺序不能变**:先落库再记事件的话,崩溃在两者之间会留下一个"状态变了但历史
里没有"的任务,而事件流是审计的唯一依据。

## 待办队列从数据库派生

"状态即事实"这条原则最容易被违反的地方就在这里:如果队列是内存里的一个 list,崩溃后它就
没了,而任务还在库里等着——表现为"重启之后有些任务再也不动了"。内存里只允许有短暂的
执行中集合。

## 表留下的一个洞:守卫挡住之后怎么办

迁移表里 `DEVELOPING` 只有 `dev_done` 一条出边,守卫是"result.json 有效"。产物不合契约时
这条边走不通,任务原地不动——而原地不动的任务会被下一次调度再推一遍,理论上无限循环。
表本身没有为这种情况准备边。

这里的兜底是:**连续被守卫挡住的次数达到修复轮次上限即升级人工**。次数从事件流里数出来
(不是内存计数),所以重启后仍然有效。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from agentgenome import paths
from agentgenome.agents.capabilities import capabilities_of
from agentgenome.agents.human import HUMAN
from agentgenome.agents.pool import AgentPool
from agentgenome.agents.runtime import FailureKind
from agentgenome.approval.notify import Notification, send
from agentgenome.commit.message import (
    CommitInputs,
    PullRequestInputs,
    commit_message,
    pull_request_body,
)
from agentgenome.commit.pipeline import STAGE_PRECHECK
from agentgenome.commit.topology import (
    DualLayerResult,
    SubmoduleMergeFailed,
    merge_submodules,
    merge_workspace,
    publish_task_branches,
)
from agentgenome.config import Config, GitHost, load_config
from agentgenome.context import FailureReport
from agentgenome.core.events import (
    GATE_ACTOR,
    ORCHESTRATOR,
    ActorKind,
    EventLog,
    LogKind,
)
from agentgenome.core.requirement import (
    Requirement,
    RequirementNotFound,
    RequirementScene,
    RequirementState,
    RequirementStore,
)
from agentgenome.core.scope import ScopePolicy, is_under
from agentgenome.core.scope_grants import (
    GRANTS_FILE,
    ScopeGrant,
    append_grants,
    effective_modules,
    effective_mount_paths,
    granted_modules,
)
from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.task import ItestNeed, Task, TaskMode, TaskStore
from agentgenome.core.topology import (
    ASSISTED,
    ATTEMPT,
    BEST_OF_N,
    CRITIQUE_LOOP,
    JUDGE,
    PROBE,
    ExecutionLimits,
    Executor,
    TemplateChoice,
    TopologyRefused,
    TopologyRun,
    TopologyTemplate,
    UnknownTopology,
    Variant,
    assisted_template,
    best_of_n_template,
    critique_loop_template,
    dag_template,
    probe_after_gate_template,
    single_template,
    test_first_template,
)
from agentgenome.core.topology_validate import globs_overlap, validate_topology
from agentgenome.core.transitions import Decision, Effect, Facts, PlanFailureCause, decide
from agentgenome.core.verdict import VerdictKind, is_recoverable
from agentgenome.employees import (
    TASK_MODULES_PLACEHOLDER,
    EmployeeConfig,
    EmployeeNotFound,
    EmployeeRegistry,
    ProcedureNotAllowed,
    load_employees,
    workspace_employees_root,
)
from agentgenome.gates.task_gates import (
    changed_paths,
    run_modules,
    targets_for,
)
from agentgenome.genome.dispatch import DispatchResult, dispatch_procedure
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.evolution.lifecycle import credit_hits
from agentgenome.genome.evolution.pipeline import DistillResult, distill_with_agent
from agentgenome.genome.evolution.promotion import promotions_for, write_promotions
from agentgenome.genome.evolution.record import finish_distillation, open_distillation
from agentgenome.genome.features import features_covering, invariants_of
from agentgenome.genome.hits import LedgerUnreadable, record_credits, take_exposures
from agentgenome.genome.loader import load_project_map, load_tree
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.procedures import (
    Outputs,
    ProcedureKind,
    ProcedureRegistry,
    ProcedureSpec,
    Trigger,
    load_workspace_registry,
)
from agentgenome.genome.roster import (
    ADVERSARIAL_PROBE,
    ADVERSARY_EMPLOYEE,
    CODE_CRITIQUE,
    DECISION_EMPLOYEE,
    DEVELOPER_EMPLOYEE,
    PLAN_PROCEDURES,
    REVIEWER_EMPLOYEE,
    TESTER_EMPLOYEE,
    WRITE_TESTS_PROCEDURE,
)
from agentgenome.genome.routing import RouteInputs
from agentgenome.genome.rules import effective_max_fix_rounds, load_rules
from agentgenome.genome.suspects import (
    Suspect,
    SuspectKind,
    record_suspects,
    stale_suspects,
)
from agentgenome.itest.decide import (
    ItestDecision,
    agent_decision,
    evaluate_rules,
    manual_decision,
    rule_decision,
)
from agentgenome.jobs.artifacts import ArtifactBus, Slot
from agentgenome.jobs.catalog import BUILDABLE
from agentgenome.jobs.commit_handlers import CommitPipelineHandler, MergeHandler
from agentgenome.jobs.handlers import (
    HANDLERS,
    STAGE_DEVELOP,
    STAGE_PLAN,
    STAGE_UNIT_GATE,
    Handler,
    Outcome,
    ProcedureHandler,
    can_advance,
    read_result,
    register,
)
from agentgenome.jobs.plan_io import PLAN_FILE, load_plan_modules, read_plan
from agentgenome.jobs.reports import (
    failure_paths,
    read_failure_reports,
    write_failure_report,
    write_task_report,
)
from agentgenome.jobs.split import (
    SPLIT_KEY,
    closing_snapshot,
    effective_children,
    proposal_of,
    read_verdict,
    resplit_snapshot,
    split_issues,
    split_todo_id,
)
from agentgenome.quality_line import adversary_is_on, extra_forbid, tester_is_dedicated
from agentgenome.security.audit import seal_terminal_evidence
from agentgenome.security.precheck import participant_policies
from agentgenome.space.forge import Forge, ForgeError, MergeConflict
from agentgenome.space.forge import select as select_forge
from agentgenome.space.git_ws import GitWorkspace, branch_name, submodule_paths
from agentgenome.todo.service import record_delivery
from agentgenome.todo.store import PENDING as TODO_PENDING
from agentgenome.todo.store import SPLIT as SPLIT_TODO
from agentgenome.todo.store import Todo, TodoStore
from agentgenome.umodel.graph import InMemoryGraph
from agentgenome.umodel.sync import sync_project_map, sync_task
from agentgenome.verification.bootstrap import (
    load_bootstrap_specs,
    promote_bootstrap_specs,
    record_bootstrap_spec,
)
from agentgenome.verification.discovery import resolve_verification
from agentgenome.verification.evidence import seal_agent_proposal
from agentgenome.verification.models import NeedsConfirmation, PendingVerification
from agentgenome.verification.proposal import EMPLOYEE_ID as VERIFICATION_ANALYST
from agentgenome.verification.proposal import PROCEDURE_ID as VERIFICATION_PROPOSE
from agentgenome.verification.proposal import PROCEDURE_VERSION as VERIFICATION_PROPOSE_VERSION
from agentgenome.verification.proposal import RECEIPT_SCHEMA as VERIFICATION_PROPOSAL_SCHEMA
from agentgenome.verification.proposal import build_prompt as verification_analysis_prompt
from agentgenome.verification.proposal import load_proposal as load_verification_proposal
from agentgenome.verification.proposal import proposal_output_check
from agentgenome.verification.report import load_gate_receipt

#: 下一轮要重跑哪些节点。**没有这份清单就只能整张图重跑**——而一次跑通的东西不该因为
#: 别人挂了而重花一遍钱(与增量修复同一个经济学)。
REDO_FILE = "redo-nodes.json"

#: 一次 best-of-n 的胜负对比。**蒸馏的输入**:胜者与落选者的差异 + 裁决理由。
CONTRAST_FILE = "contrast.json"

#: 适应度用的那道工序。与单元门禁那一步跑的是同一个。
GATE_PROCEDURE = "unit-gate"

#: 拓扑事件的两种:选了哪张图 / 这张图跑成了什么。
PHASE_CHOSEN = "chosen"
PHASE_RAN = "ran"

#: 确定性发现说不准时的只读架构调查。独立 stage 让 token、产物与崩溃恢复都有据可查。
STAGE_VERIFICATION_ANALYSIS = "verification-analysis"
VERIFICATION_ANALYSIS_RECEIPT = "dispatch-receipt.json"
MAX_VERIFICATION_ANALYSIS_ATTEMPTS = 3


def _latest_passing_gate(slots: Sequence[Slot]) -> Slot | None:
    """取最后一张通过报告；它是否带冷启动规格由调用者另行判断。"""
    return next(
        (slot for slot in reversed(slots) if load_gate_receipt(slot.path).passed),
        None,
    )


#: 计划产物在任务目录里的名字。YAML 而不是 JSON:需求方会读它。

#: 终态到 IM 通知事件名的映射。**只覆盖单步推进能产出的终态**——`CANCELLED` 走取消那条
#: 独立的路径,不经过 `advance`,那条命令/接口自己发通知。
#:
#: 推 `advance` 的调用方(`cli.py::_advance`、`server/app.py` 的 `POST /tasks/{id}/run`)
#: 都用这一份:这两个终态通常是单步推进跑出来的,不是某次请求同步触发的——各判一次的话,
#: 一处忘了接,订阅这两个事件的人就会在那条路径上永远收不到推送,而且没有任何测试会红。
TERMINAL_NOTIFY_EVENT = {TaskState.ESCALATED: "escalated", TaskState.COMPLETED: "completed"}

#: 转正提案里建议的落点子目录。**只是给人看的建议**,见 `_regression_dir`。
SUGGESTED_REGRESSION_SUBDIR = "tests"

#: 兜底判定的产物 stage 与执行者。
STAGE_ITEST_DECIDE = "itest-decide"
ITEST_DECIDE_PROCEDURE = "itest-decide"
ITEST_DECIDE_EMPLOYEE = DECISION_EMPLOYEE

#: 双层合并的幂等键落在这里。**平台上重复开 PR 会得到两个**,所以这份记录不是优化,
#: 是正确性的一部分。
MERGE_STATE_FILE = "merge-state.json"

#: 子任务调度状态。与「状态即事实」同一条:内存里的队列崩溃后就没了,而子任务还在等着。

# 提交阶段的两个处理器在这里挂上去。写进 `jobs.handlers` 那张表的话,那个模块要 import
# 提交流水线,而提交流水线又要 import 它的 `Outcome`——绕成一个环。
register(TaskState.READY_TO_COMMIT, CommitPipelineHandler())
register(TaskState.MERGING, MergeHandler())


@dataclass
class JobContext:
    """一次处理器执行需要的全部东西。"""

    task: Task
    bus: ArtifactBus
    orchestrator: Orchestrator

    async def dispatch(
        self,
        procedure_id: str,
        employee_id: str,
        slot: Slot,
        state: TaskState,
        inputs: dict[str, Any] | None = None,
        subject: str = "",
        assignee: str = "",
        runtime: str = "",
        worktree: str = "",
        write_scope: tuple[str, ...] | None = None,
    ) -> DispatchResult:
        return await self.orchestrator.dispatch(
            self.task,
            procedure_id,
            employee_id,
            slot,
            state,
            inputs=inputs,
            subject=subject,
            assignee=assignee,
            runtime=runtime,
            worktree=worktree,
            write_scope=write_scope,
        )

    def task_inputs(self) -> dict[str, Any]:
        """这个任务的标准入参。节点在它之上叠自己需要的额外材料。"""
        return self.orchestrator.task_inputs(self.task)

    def template(self, state: TaskState, employee: str, procedure: str) -> TemplateChoice:
        return self.orchestrator.template_for(
            self.task, state, employee=employee, procedure=procedure
        )

    def probe_inputs(self) -> dict[str, Any]:
        return self.orchestrator.probe_inputs(self.task)

    def record_topology(self, choice: TemplateChoice, stage: str) -> None:
        self.orchestrator.record_topology(self.task, choice, stage=stage)

    def record_run(self, template: TopologyTemplate, run: TopologyRun, stage: str) -> None:
        self.orchestrator.record_run(self.task, template, run, stage=stage)

    def merge_nodes(self, nodes: list[str]) -> str:
        return self.orchestrator.merge_nodes(self.task, nodes)

    def nodes_to_redo(self) -> set[str]:
        return self.orchestrator.nodes_to_redo(self.task)

    def reset_node_worktree(self, node: str) -> None:
        self.orchestrator.reset_node_worktree(self.task, node)

    def record_contrast(self, run: TopologyRun) -> None:
        self.orchestrator.record_contrast(self.task, run)

    def limits(self) -> ExecutionLimits:
        return ExecutionLimits(
            budget_tokens=self.task.budget_tokens
            if self.orchestrator.config.budgets.enforce
            else None
        )


class Orchestrator:
    """推动任务往前走的那台机器。"""

    def __init__(
        self,
        root: Path,
        pool: AgentPool | None = None,
        runtime_name: str | None = None,
        config: Config | None = None,
        on_change: Callable[[str, str], None] | None = None,
    ) -> None:
        self.root = Path(root)
        # 只送事件(取消、审批)时不派发任何东西,不必逼调用方造一个空池。
        self.pool = pool if pool is not None else AgentPool({})
        self.runtime_name = runtime_name
        self.config = config or load_config(self.root)
        self.on_change = on_change
        self.store = TaskStore(self.root)
        self.log = EventLog(self.root)
        self.rules = load_rules(self.root)
        self.max_fix_rounds = effective_max_fix_rounds(self.rules, self.config)
        #: 最近一次派发的失败原因。任务预算不足与单 Job 超限要分开处置。
        self._last_failure = FailureKind.NONE
        self._last_failure_detail = ""
        #: 走到终态、等着被蒸馏的任务。**不在副作用里直接跑**:副作用是同步的,而蒸馏
        #: 要派 Job。调用方跑完一轮 `advance` 之后调 `drain_evolution()`。
        self._pending_distill: str | None = None
        #: 语义图谱。**没配就是 `None`**,整段同步跳过——图谱是增值,不是流程的一部分。
        self.graph: InMemoryGraph | None = None

    # --- 依赖(懒加载:每次都重读,因为基因组会被任务自己改)-------------------

    @property
    def employees(self) -> EmployeeRegistry:
        """这个工作区的花名册。**带上排他清单**——归属冲突要在真正派活的这条路上被看见。

        非严格:一份写坏的定义不该让整个编排器起不来。冲突不在这里抛,它落在
        `registry.conflicts` 里,由 `advance` 在派第一个 Job 之前变成一次可操作的升级人工。
        """
        return load_employees(
            workspace_employees_root(self.root), strict=False, exclusive=PLAN_PROCEDURES
        )

    @property
    def procedures(self) -> ProcedureRegistry:
        return load_workspace_registry(self.root)

    def bus(self, task: Task) -> ArtifactBus:
        return ArtifactBus(self.store.task_dir(task.id))

    # --- 推进 ----------------------------------------------------------------

    def current(self, task_id: str) -> Task:
        """返回驱动前的任务快照，用于辨认一次 `advance` 是否真的产生了进展。"""
        return self.store.get(task_id)

    async def advance(self, task_id: str) -> Task:
        """把一个任务推进一步。

        没有处理器的状态原样返回——终态与等外部事件的状态都是这种,它们不该被"推"。
        """
        task = self.store.get(task_id)
        # 终态、等外部事件的状态、交互式任务正在结对的开发态——三种"不该被推"的情况
        # 收在 `can_advance` 一处判断。服务端的 `POST /tasks/{id}/run` 复用同一个函数,
        # 两处才不会出现"接口放行了,这里却照旧 no-op"的落差。会话结束时走 `finish_pair`
        # 送 DEV_DONE,后续照原路走——那不是这里要管的分支。
        if not can_advance(task):
            return task
        stale = self._stale_roster()
        if stale is not None:
            # **归属没收敛就不派活。** 照派的话,事件面上"这个决定该质询谁"会记下一个
            # 说不清的答案,而事件是不可改的——错了就永远错在那儿。
            return self._escalate(task_id, stale)
        handler = HANDLERS[task.state]

        bus = self.bus(task)
        outcome = handler.existing_outcome(bus, handler.attempt(task))
        if outcome is None:
            refusal = self._empty_scope_refusal(task, handler)
            if refusal is not None:
                return self._escalate(task_id, refusal)
            try:
                outcome = await handler.run(JobContext(task=task, bus=bus, orchestrator=self))
            except TopologyRefused as refused:
                # 图跑不动(比如 N 路变体装不下预算):**这是一个配置层面的死结**,
                # 重试只会再撞一次同一堵墙——升级人工,并把"下一步该怎么办"写进原因里。
                return self._escalate(task_id, str(refused))
            # **战果在这一轮就要收。** 下一轮的产物会覆盖不了它(编址带轮次),但任务可能
            # 在下一轮就终结,而终结之后没有人会回头去翻一份没人提过案的攻击用例。
            self._harvest_attack_cases(task, outcome)
            if outcome.fatal_reason:
                # 越权不是“代码质量没过”，重复派发同一个员工只会扩大风险与费用。
                return self._escalate(task_id, outcome.fatal_reason)
            if outcome.redo:
                # 人看过之后说"不行",或者图里有节点没成:这一轮的产出不算数,重来一轮。
                # scope guard 失败也是节点失败，但下一轮仍要先知道当前授权与申请通道。
                # 合进同一份报告：同轮另写一份会被 `_redo` 的报告覆盖。
                guidance = self._scope_channel_guidance(task, handler)
                if guidance:
                    notes = (outcome.note, *guidance)
                    outcome = replace(outcome, note="。".join(item for item in notes if item))
                return self._redo(task, outcome)
            if outcome.suspended:
                # **挂起不是失败也不是成功:状态不动。** 人一交活,提交那一侧会把产物写进
                # 这次派发的产物槽再推一步,而那一步走的是崩溃恢复那条重放路——整条"继续"
                # 因此不需要任何内存态。
                return self._suspend(task, outcome)
            self._teach_the_scope_channel(task, handler)
            settled = self._grant_scope(task, handler, outcome)
            if settled is not None:
                return settled
            if self._last_failure is FailureKind.TASK_BUDGET:
                # 任务预算装不下下一个 Job:重试只会再撞一次同一堵墙。
                return self._escalate(
                    task_id, f"任务预算不足以再跑一个 Job,在 {task.state.value} 态升级人工"
                )
        elif outcome.replayed:
            if outcome.fatal_reason:
                return self._escalate(task_id, outcome.fatal_reason)
            if outcome.redo:
                return self._redo(task, outcome)
            if outcome.suspended:
                return self._suspend(task, outcome)
            self.log.append(
                task.id,
                actor=ORCHESTRATOR,
                kind=LogKind.NOTE,
                payload={
                    "note": f"{task.state.value} 的产物已存在,跳过执行",
                    "stage": handler.stage,
                },
            )
        outcome = self._refuse_a_broken_graph(task, outcome)
        checkpoint = self._split_checkpoint(task, outcome)
        if isinstance(checkpoint, Task):
            # 提案在等人裁决:任务停住,不进迁移表——待确认不是失败也不是成功。
            return checkpoint
        outcome = checkpoint
        deepened = await self._deepen_ambiguous_verification(task, outcome)
        if isinstance(deepened, Task):
            return deepened
        if deepened is None:
            # 深入分析是门禁内部的自动子步骤。分析运行时/合约暂时失败时留在
            # UNIT_TESTING 重试，不把“调查没做成”误算成代码修复轮次。
            return self.store.get(task.id)
        outcome = deepened
        self._log_gate_result(task, outcome)
        if outcome.event is TaskEvent.GATE_PASS:
            # 判定必须在事件进状态机**之前**落库:两条守卫读的是 `task.needs_itest`,
            # 判定晚一步的话它们看到的是 `undecided`,两条都不认,任务原地打转到升级人工。
            await self._decide_itest(task_id)
        return self.deliver(task_id, outcome.event, outcome=outcome)

    async def _deepen_ambiguous_verification(
        self, task: Task, outcome: Outcome
    ) -> Outcome | Task | None:
        """门禁发现歧义时自动让架构员工深挖，并用平台验真的候选重跑同一关。"""
        if outcome.event is not TaskEvent.GATE_FAIL or outcome.payload is None:
            return outcome
        try:
            requests = tuple(
                PendingVerification.model_validate(item)
                for item in outcome.payload.get("verification_requests", [])
            )
        except ValueError as error:
            self._note(task, f"验证规格深入分析请求损坏，留在门禁态自动重试: {error}")
            return None
        if not requests:
            return outcome

        workdir = self._workdir(task)
        gate_slot = self.bus(task).latest(STAGE_UNIT_GATE)
        if gate_slot is None:
            return None

        for pending in requests:
            module_id = pending.module
            module = load_project_map(workdir).module(module_id)
            resolution = resolve_verification(module_id, workdir / module.path)
            if not isinstance(resolution, NeedsConfirmation):
                continue
            current = PendingVerification(
                module=module_id,
                issues=resolution.issues,
                candidates=resolution.candidates,
            )
            analysis_slot = self._completed_verification_analysis(
                task, module_id, workdir / module.path
            )
            if analysis_slot is None:
                analysis_slot = self.bus(task).allocate(STAGE_VERIFICATION_ANALYSIS)
                try:
                    dispatched = await self._run_verification_analysis(
                        task, module_id, module.path, current, analysis_slot
                    )
                except (LookupError, RuntimeError, GenomeValidationError) as error:
                    analysis_slot.write_manifest(
                        producer=VERIFICATION_ANALYST,
                        inputs=[module_id],
                        outputs=[],
                        summary="自动深入分析模块验证规格",
                        task_attempt=task.fix_rounds + 1,
                        result_ok=False,
                        failure_kind=FailureKind.PROCESS.value,
                        failure_detail=str(error),
                    )
                    return self._retry_or_block_verification_analysis(task, module_id, str(error))
                analysis_slot.write_manifest(
                    producer=VERIFICATION_ANALYST,
                    inputs=[module_id],
                    outputs=["result.json"] if dispatched.result_path is not None else [],
                    summary="自动深入分析模块验证规格",
                    task_attempt=task.fix_rounds + 1,
                    result_ok=dispatched.ok,
                    failure_kind=dispatched.failure_kind.value,
                    failure_detail=dispatched.failure_detail or "",
                )
                if not dispatched.ok:
                    return self._retry_or_block_verification_analysis(
                        task,
                        module_id,
                        dispatched.failure_detail or dispatched.failure_kind.value,
                    )

            issue = proposal_output_check(
                task.id,
                module_id,
                workdir / module.path,
                pending=current,
            )(analysis_slot.path)
            if issue is not None:
                analysis_slot.write_manifest(
                    producer=VERIFICATION_ANALYST,
                    inputs=[module_id],
                    outputs=["result.json"],
                    summary="自动深入分析模块验证规格",
                    task_attempt=task.fix_rounds + 1,
                    result_ok=False,
                    failure_kind=FailureKind.CONTRACT.value,
                    failure_detail=issue,
                )
                return self._retry_or_block_verification_analysis(task, module_id, issue)
            proposal = load_verification_proposal(analysis_slot.path / "result.json")
            candidate = seal_agent_proposal(proposal.spec, workdir / module.path)
            record_bootstrap_spec(gate_slot.path, candidate)

        changed = changed_paths(self.root, task.id)
        report = run_modules(
            workdir=workdir,
            task_id=task.id,
            output_dir=gate_slot.path,
            changed=changed,
            targets=targets_for(workdir, changed),
            control_root=self.root,
        )
        report.write(gate_slot.path)
        (gate_slot.path / "result.json").write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if report.verification_requests:
            self._note(task, "仍有模块需要深入分析，留在门禁态继续自动调查")
            return None
        return Outcome(
            event=TaskEvent.GATE_PASS if report.passed else TaskEvent.GATE_FAIL,
            valid=True,
            payload=report.as_dict(),
        )

    def _retry_or_block_verification_analysis(
        self, task: Task, module_id: str, detail: str
    ) -> Task | None:
        """有限次自动重试；耗尽后明确终止，不创建 human Job 或确认待办。"""
        attempt = task.fix_rounds + 1
        failures = sum(
            1
            for slot in self.bus(task).by_stage(STAGE_VERIFICATION_ANALYSIS)
            if (manifest := slot.manifest()) is not None
            and manifest.get("task_attempt") == attempt
            and manifest.get("inputs") == [module_id]
            and manifest.get("result_ok") is False
        )
        if failures >= MAX_VERIFICATION_ANALYSIS_ATTEMPTS:
            return self._escalate(
                task.id,
                f"自动验证规格分析已阻塞: 模块 {module_id} 连续失败 {failures} 次。"
                f"系统未创建人工分析待办。最后错误: {detail}",
            )
        self._note(
            task,
            f"验证规格深入分析未完成({failures}/{MAX_VERIFICATION_ANALYSIS_ATTEMPTS})，"
            f"留在门禁态自动重试: {detail}",
        )
        return None

    async def _run_verification_analysis(
        self,
        task: Task,
        module_id: str,
        module_path: str,
        pending: PendingVerification,
        slot: Slot,
    ) -> DispatchResult:
        """用平台内建的只读调查契约派架构员工；旧 Workspace 无需先升级 Procedure。"""
        employee = self.employees.get(VERIFICATION_ANALYST)
        runtime = self._verification_analysis_runtime(employee)
        employee = employee.model_copy(
            update={
                "procedures": list(dict.fromkeys((*employee.procedures, VERIFICATION_PROPOSE))),
                "tools": employee.tools.model_copy(
                    update={
                        "allow": ["read_file", "search", "list_files"],
                        "deny": [
                            "write_file",
                            "edit_file",
                            "run_command",
                            "fetch_url",
                            "web_search",
                        ],
                    }
                ),
            }
        )
        spec = ProcedureSpec(
            id=VERIFICATION_PROPOSE,
            version=VERIFICATION_PROPOSE_VERSION,
            summary="自动调查歧义模块的验证入口",
            kind=ProcedureKind.AGENTIC,
            trigger=Trigger(states=[TaskState.UNIT_TESTING]),
            outputs=Outputs(artifacts=["result.json"]),
            path=slot.path,
            prompt=verification_analysis_prompt(task.id, module_id, module_path, pending),
            output_schema=VERIFICATION_PROPOSAL_SCHEMA,
        )
        self.pool.set_task_budget(
            task.id,
            task.budget_tokens if self.config.budgets.enforce else None,
            task.tokens_used,
        )
        self.log.append(
            task.id,
            actor=VERIFICATION_ANALYST,
            actor_kind=ActorKind.EMPLOYEE,
            kind=LogKind.JOB_STARTED,
            payload={
                "procedure_ref": spec.ref,
                "stage": STAGE_VERIFICATION_ANALYSIS,
                "round": task.fix_rounds + 1,
            },
        )
        if self.on_change is not None:
            self.on_change(task.id, "job_started")
        dispatched = await dispatch_procedure(
            spec,
            pool=self.pool,
            employee=employee,
            task_id=task.id,
            state=TaskState.UNIT_TESTING,
            inputs={},
            workdir=self._workdir(task),
            output_dir=slot.path,
            config=self.config,
            runtime_name=runtime,
            round_=task.fix_rounds + 1,
            subject=module_id,
            requirement=task.requirement,
            modules=self.effective_modules(task),
            failures=self._failures(task),
            write_scope=(),
        )
        # 先记账和事件、最后才写可恢复小票：看到小票就意味着本次成本与结束事实已经
        # 持久化。外部 runtime 返回到本地记账之间无法跨系统原子提交，采用 at-least-once；
        # 若在那一窄窗崩溃会重派，但绝不凭一份未记账小票复用结果。
        self._charge(task, dispatched)
        self.log.job_finished(
            task.id,
            VERIFICATION_ANALYST,
            dispatched.procedure_ref,
            dispatched.ok,
            dispatched.tokens_used,
            dispatched.tokens_available,
            dispatched.duration_s,
            dispatched.failure_kind.value,
            dispatched.failure_detail,
        )
        if self.on_change is not None:
            self.on_change(task.id, "job_finished")
        self._write_verification_analysis_receipt(slot, dispatched)
        self._last_failure = dispatched.failure_kind
        self._last_failure_detail = dispatched.failure_detail or ""
        return dispatched

    @staticmethod
    def _write_verification_analysis_receipt(slot: Slot, result: DispatchResult) -> None:
        """在平台复核 manifest 前持久化池的最终裁决，恢复时不能只信 result.json。"""
        target = slot.path / VERIFICATION_ANALYSIS_RECEIPT
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "ok": result.ok,
                    "failure_kind": result.failure_kind.value,
                    "failure_detail": result.failure_detail or "",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(target)

    def _verification_analysis_runtime(self, employee: EmployeeConfig) -> str:
        """歧义调查必须由硅基运行时执行，绝不隐式生成一张人的待办。"""
        candidates = (
            self.runtime_name,
            employee.runtime,
            self.config.runtime.default,
            *self.config.runtime.runtimes,
        )
        registered = set(self.pool.runtime_names())
        runtime = next(
            (
                item
                for item in candidates
                if item
                and item != HUMAN
                and item in registered
                and (profile := capabilities_of(item)) is not None
                and profile.enforces_read_only
            ),
            None,
        )
        if runtime is None:
            raise RuntimeError("验证规格深入分析没有可强制只读的硅基运行时")
        return runtime

    def _completed_verification_analysis(
        self, task: Task, module_id: str, module_root: Path
    ) -> Slot | None:
        """取本任务轮次已完成的分析，崩溃恢复时不重复烧 token。"""
        attempt = task.fix_rounds + 1
        for slot in reversed(self.bus(task).by_stage(STAGE_VERIFICATION_ANALYSIS)):
            manifest = slot.manifest() or {}
            recorded = (
                manifest.get("task_attempt") == attempt
                and manifest.get("inputs") == [module_id]
                and manifest.get("result_ok") is True
            )
            issue = proposal_output_check(task.id, module_id, module_root)(slot.path)
            if recorded and issue is None:
                return slot
            # Agent 已交付、进程却在 manifest 落盘前崩溃：result.json 本身带 task/module，
            # 平台复核通过即可补票复用，避免为同一调查重复烧 token。
            receipt = self._verification_analysis_receipt(slot)
            if not manifest and receipt is not None and receipt.get("ok") is True and issue is None:
                slot.write_manifest(
                    producer=VERIFICATION_ANALYST,
                    inputs=[module_id],
                    outputs=["result.json"],
                    summary="恢复已完成的自动验证规格分析",
                    task_attempt=attempt,
                    result_ok=True,
                    failure_kind=FailureKind.NONE.value,
                    failure_detail="",
                )
                return slot
        return None

    @staticmethod
    def _verification_analysis_receipt(slot: Slot) -> dict[str, Any] | None:
        try:
            value = json.loads((slot.path / VERIFICATION_ANALYSIS_RECEIPT).read_text())
        except (OSError, UnicodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def finish_pair(self, task_id: str) -> Task:
        """结对会话结束,等价于开发做完了。

        **人参与不等于少走流程**:这里送的就是自主执行那条路上同一个 `DEV_DONE` 事件,
        于是任务照常进 UNIT_TESTING,照走门禁 → 集测判定 → 安全提交 → 审批。

        越权检查同样不放过——工作区里那些文件是人和员工一起改的,而"人在场"不放宽边界。
        """
        task = self.store.get(task_id)
        if task.mode is not TaskMode.INTERACTIVE:
            raise ValueError(f"{task_id} 不是交互式任务,没有结对会话可结束")
        if task.state is not TaskState.DEVELOPING:
            raise ValueError(f"{task_id} 当前在 {task.state.value},不在结对开发中")

        # **越权检查照跑。** 人在场不放宽边界——工作区里那些文件是人和员工一起改的,
        # 而"结对的改动不用查"正好是这条线上最容易被开的口子。
        violated = self._enforce_pair_scope(task)
        if violated is not None:
            return self._escalate(task_id, f"结对会话的改动越权: {violated}")

        # 下游要知道这一轮改了什么(集测判定、提交流水线都读它),所以结对也得留一份
        # develop 产物。**改动清单从 git 读,不是让谁自报**——这比自主模式的自评更可信:
        # 自评会漏、会瞒,而 git 不会。
        self._write_pair_result(task)
        return self.deliver(task_id, TaskEvent.DEV_DONE)

    def _enforce_pair_scope(self, task: Task) -> str | None:
        """结对结束后的越权检查。越权返回原因,合规返回 `None`。"""
        dev_handler = HANDLERS[TaskState.DEVELOPING]
        assert isinstance(dev_handler, ProcedureHandler), "DEVELOPING 固定派发 Procedure"
        return self.enforce_human_scope(task, dev_handler.employee_id)

    def enforce_human_scope(self, task: Task, employee_id: str) -> str | None:
        """**人在工作树里改完之后**的越权检查。越权返回原因,合规返回 `None`。

        用那个员工原有的 `ScopePolicy`——**人在场不放宽边界**。结对与派给人的开发待办共用
        这一个函数:各写一遍的话,"人改的不用查"这个最容易被开的口子会以另一种形式再开一次,
        而它一旦被开,人就成了系统里唯一不受最小权限约束的执行者。
        """
        workdir = self._workdir(task)
        employee = self.employees.get(employee_id)
        policy = employee.scope(
            task.id,
            self.rules.protected.paths_for(employee.id),
            module_paths=self.plan_mount_paths(task),
            extra_forbid=self.extra_forbid(task, employee_id),
        )
        touched = self._pair_changed_paths(workdir)
        offending = [path for path in touched if not policy.allows(path)]
        if not offending:
            return None
        return f"{len(offending)} 个路径超出授权: {', '.join(offending[:5])}"

    def human_changed_paths(self, task: Task) -> list[str]:
        """人在这个任务的工作树里碰过哪些路径。**从 git 读,不是让谁自报。**

        自评会漏、会瞒,而 git 不会——这条对人和对员工是同一句话。
        """
        return self._pair_changed_paths(self._workdir(task))

    @staticmethod
    def _pair_changed_paths(workdir: Path) -> list[str]:
        from agentgenome.space.git_ws import working_tree_touched_paths
        from agentgenome.space.gitcmd import GitError

        try:
            return sorted(working_tree_touched_paths(workdir))
        except GitError:
            return []

    def _write_pair_result(self, task: Task) -> None:
        changed = self._pair_changed_paths(self._workdir(task))

        slot = self.bus(task).allocate(STAGE_DEVELOP)
        (slot.path / "result.json").write_text(
            json.dumps(
                {
                    "task_id": task.id,
                    "producer": "pair-session",
                    "created_at": datetime.now(UTC).isoformat(),
                    "passed": True,
                    "changed_files": changed,
                    "summary": "结对会话产出",
                    "impact": {"modules": self.plan_modules(task), "rationale": "结对开发"},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def deliver(
        self,
        task_id: str,
        event: TaskEvent,
        outcome: Outcome | None = None,
        facts: Facts | None = None,
    ) -> Task:
        """把一个事件送进状态机。外部事件(取消、审批)也走这里。

        `facts` 显式传值是给外部事件用的:审批那条边的守卫问"这个人在不在名单里",
        而编排器自己算不出那件事——身份是调用方带进来的,校验在审批服务里做完。
        """
        task = self.store.get(task_id)
        facts = facts or self._facts(outcome)
        decision = decide(task, event, facts)
        self.log.transition(task, event, decision)

        if not decision.allowed:
            return self._handle_refusal(task, decision)
        return self._apply(task, decision, outcome)

    async def recover(self) -> tuple[Task, ...]:
        """重启后把所有非终态任务各推一步。

        中断的 Job 视为失败——进程已经死了,那个子进程也死了。乐观地假设它可能还活着
        会让任务永远卡在"执行中"。而"重新跑一遍"是安全的:处理器先查产物,已经产出的
        那一步不会重复执行。
        """
        recovered = []
        for task in self.store.open_tasks():
            recovered.append(await self.advance(task.id))
        # 需求树巡检:交付钩子可能崩在"子需求完成了但兄弟没解锁/收口没落"的缝里,
        # 而那条钩子不会再响——恢复是唯一能把树接上的地方。幂等,白跑无害。
        parents = {
            requirement.parent_id
            for requirement in RequirementStore(self.root).all()
            if requirement.parent_id
        }
        for parent_id in sorted(parents):
            self.sweep_requirement_tree(parent_id)
        return tuple(recovered)

    async def drain_evolution(self) -> DistillResult | None:
        """把上一轮终结的任务送去蒸馏。没有就返回 `None`。

        **与 `advance` 分开**:副作用是同步执行的,而蒸馏要派一个 Job。硬塞进副作用里的话,
        要么把整条副作用链改成 async,要么在里面起一个事件循环——后者在已经跑着的循环里会
        死锁。

        分开还有一个好处:调用方可以选择不蒸馏(比如批量回放历史任务时)。
        """
        task_id, self._pending_distill = self._pending_distill, None
        if task_id is None:
            return None
        # **先建档再跑。** 建在后面的话,一次跑到一半的崩溃会留下"蒸馏发生过但查不到"的
        # 空档——而这条记录存在的全部理由,就是让失败与耗时有地方安放。
        budget = self.config.genome_tasks.per_task_tokens
        record = open_distillation(self.root, task_id, budget_tokens=budget)
        # **预算真的传给管道。** 只写进记录不传下去的话,那个字段就是装饰:管道里那条
        # 预算分支永远走不到,而蒸馏烧的仍然是源任务的额度——正好是「独立预算」的反面。
        #
        # 注意 Job 层的池预算此刻仍挂在源任务上(派发还没拆出去,见 record.py 的模块说明),
        # 所以这一层是"独立预算"目前唯一真正生效的地方。
        result = await distill_with_agent(self, self.store.get(task_id), budget_tokens=budget)
        if record is not None:
            finish_distillation(self.root, record.id, result)
        self._record_evaporation(task_id, result)
        return result

    def next_tasks(self) -> tuple[Task, ...]:
        """接下来该推哪些任务。**从数据库派生,内存里不留队列。**"""
        return tuple(task for task in self.store.open_tasks() if task.state in HANDLERS)

    # --- 派发 ----------------------------------------------------------------

    async def dispatch(
        self,
        task: Task,
        procedure_id: str,
        employee_id: str,
        slot: Slot,
        state: TaskState,
        inputs: dict[str, Any] | None = None,
        subject: str = "",
        assignee: str = "",
        runtime: str = "",
        worktree: str = "",
        write_scope: tuple[str, ...] | None = None,
    ) -> DispatchResult:
        """以某个员工的身份派发一个 Procedure,并把结果记进事件流。

        `inputs` 缺省是这个任务的标准入参。显式传值给的是需要额外材料的 Procedure——
        比如兜底判定要看开发员工的自评影响面,那不是每个 Procedure 都该拿到的东西。
        """
        employee = self.employees.get(employee_id)
        spec = self.procedures.get(procedure_id)
        workdir = self._workdir(task, slot=worktree)
        round_ = task.plan_retries + 1 if state is TaskState.CREATED else task.fix_rounds + 1
        # 池是进程内的,重启之后它不知道这个任务已经烧了多少。真相在任务表里。
        self.pool.set_task_budget(
            task.id,
            task.budget_tokens if self.config.budgets.enforce else None,
            task.tokens_used,
        )
        self.log.append(
            task.id,
            actor=employee_id,
            actor_kind=ActorKind.EMPLOYEE,
            kind=LogKind.JOB_STARTED,
            payload={"procedure_ref": spec.ref, "stage": slot.stage, "round": round_},
        )
        if self.on_change is not None:
            self.on_change(task.id, "job_started")
        result = await dispatch_procedure(
            spec,
            pool=self.pool,
            employee=employee,
            task_id=task.id,
            state=state,
            inputs=self.task_inputs(task) if inputs is None else inputs,
            workdir=workdir,
            output_dir=slot.path,
            config=self.config,
            # **节点上的执行者优先。** `executor: manual` 说的就是"这一个节点交给人",
            # 而员工声明的运行时说的是"这个角色平时跑在哪儿"——不让前者覆盖后者的话,
            # 那个字段就只是一句没人读的注释。
            runtime_name=runtime or self.runtime_name,
            round_=round_,
            # **回放键的第四维。** 环里的批判、图里的并行节点都是"同一轮里同 employee 同
            # procedure 派多次";不带它的话回放给每一次返回同一份产出,而测试照样是绿的。
            subject=subject,
            # 派给谁是**派发那一刻就定了的事**,不是待办系统自己猜的。
            assignee=assignee,
            requirement=task.requirement,
            # **生效模块,不是计划模块。** 用后者的话扩权批下来却进不了 Job 的授权,
            # 整条申请通道是死的——而它死得很安静:批准留了痕、事件也记了,只有员工下一轮
            # 照样撞墙。
            modules=self.effective_modules(task),
            failures=self._failures(task),
            route_inputs=self._route_inputs(task),
            # 任务级禁令与生效模块同源:两者都是"这一次"的事实,不是这个角色的事实。
            extra_forbid=self.extra_forbid(task, employee_id),
            write_scope=write_scope,
        )
        self.log.job_finished(
            task.id,
            employee_id=employee_id,
            procedure_ref=result.procedure_ref,
            ok=result.ok,
            tokens_used=result.tokens_used,
            tokens_available=result.tokens_available,
            duration_s=round(result.duration_s, 3),
            failure_kind=result.failure_kind.value,
            failure_detail=result.failure_detail,
        )
        self._charge(task, result)
        if self.on_change is not None:
            self.on_change(task.id, "job_finished")
        self._last_failure = result.failure_kind
        self._last_failure_detail = result.failure_detail or ""
        return result

    def task_inputs(self, task: Task) -> dict[str, Any]:
        """派给 Procedure 的入参。需求原文永远在里面——它是任务的全部起点。"""
        return {
            "requirement": task.requirement,
            "task_id": task.id,
            "modules": self.plan_modules(task),
        }

    def plan_modules(self, task: Task) -> list[str]:
        """这次涉及哪些模块。基因组切片按它过滤。

        来源是计划产物,不是猜的:猜的话切出来的认知与员工实际要改的代码对不上,
        而对不上的认知比没有认知更误导。
        """
        return load_plan_modules(self._plan(task))

    def _teach_the_scope_channel(self, task: Task, handler: Handler) -> None:
        """越权被拦之后,告诉员工**有一条正当的出路**。

        不告诉的话,它只知道"我被拦了",不知道能申请——于是下一轮多半还是同样的写法,修复循环
        变成反复撞同一堵墙,而每一轮都在烧 token。收窄权限的收益会被这个空转吃掉一大半。

        **区分两类越界。** 越到别的业务模块可以申请;越到基因组、CI 配置、子模块指针不能——
        建议申请只会把它引向一次注定被拒的尝试,又白烧一轮。
        """
        lines = self._scope_channel_guidance(task, handler)
        if not lines:
            return
        write_failure_report(
            self.store.task_dir(task.id),
            round_=task.fix_rounds + 1,
            title=f"第 {task.fix_rounds + 1} 轮越权被拦",
            payload={"failures": [{"message": line} for line in lines]},
        )

    def _scope_channel_guidance(self, task: Task, handler: Handler) -> tuple[str, ...]:
        """越权失败要回注的授权事实与正当申请通道；没有适用建议时返回空。"""
        if self._last_failure is not FailureKind.SCOPE:
            return ()
        if not isinstance(handler, ProcedureHandler) or handler.stage != STAGE_DEVELOP:
            return ()
        employee = self.employees.get(handler.employee_id)
        if not any(TASK_MODULES_PLACEHOLDER in glob for glob in employee.permissions.write_paths):
            return ()

        detail = self._last_failure_detail
        authorized = self.effective_modules(task)
        project_map = self.project_map()
        mounts = project_map.module_paths() if project_map else {}
        authorized_set = set(authorized)
        widenable = any(
            module not in authorized_set
            and is_under(mount, paths.REPOS.as_posix())
            and f"{mount.rstrip('/')}/" in detail
            for module, mount in mounts.items()
        )
        lines = [
            f"越界的路径:{detail}",
            f"本任务当前授权的模块:{', '.join(authorized) or '(空)'}",
        ]
        lines.append(
            "确实需要动别的业务模块的话,**不要直接写**——在 result.json 的 scope_request 里"
            "声明 {module, reason},reason 要说清为什么这个需求绕不开它。批准之后下一轮那个"
            "模块就可写了。"
            if widenable
            else "这条路径不能通过扩权申请(可能已属于授权模块的受保护路径,或位于基因组、"
            "CI 配置、子模块指针等不可申请区域),改回来即可——申请它只会被拒,又白烧一轮。"
        )
        return tuple(lines)

    def _grant_scope(self, task: Task, handler: Handler, outcome: Outcome) -> Task | None:
        """开发员工申请扩权到计划外的模块。

        返回 `None` 表示**这一步还没有结论**,调用方照常往下走;返回一个任务表示这一步已经
        settled(批准并重派了一轮,或者撞上上限转了人工),调用方直接返回它。

        **一个返回值只背一种含义。** 上一版用 `TaskEvent | None` 表达三件事(没申请 / 批了 /
        已经转人工),于是超上限那一支先 `_escalate` 再返回 `None`,调用方把它当成"没批任何
        东西"继续走,最终对一个已经是终态的任务再投递一次事件——多一条毫无意义的"迁移被拒",
        而控制流在撒谎。

        **批准是自动的,对价是这个任务必经人工审批**(风险评级里的 `scope-widened`)。
        把人卡在这里会让每个合法的跨模块任务停摆几小时;而「这个任务该不该碰库存域」这个问题,
        人拿着 diff 在评审环节回答比在飞行途中凭一句理由回答准得多。

        不批的申请**不当失败处理**:员工没有越权,它只是要了个要不到的东西。让这一轮照常
        按它自己的产物走下去(多半是门禁),失败报告那条路会把理由带给下一轮。
        """
        if not isinstance(handler, ProcedureHandler) or handler.stage != STAGE_DEVELOP:
            return None
        requested = _scope_requests(outcome.payload)
        if not requested:
            return None

        project_map = self.project_map()
        mounts = project_map.module_paths() if project_map else {}
        already = set(self.effective_modules(task))
        cap = self.config.limits.max_scope_grants
        used = len(granted_modules(self.root, task.id))

        approved: list[ScopeGrant] = []
        refused: list[str] = []
        for module, reason in requested:
            if module in already:
                continue
            if module not in mounts:
                refused.append(f"{module}:根索引里没有这个模块")
                continue
            if not is_under(mounts[module], paths.REPOS.as_posix()):
                # **上限之外的目标一律不批。** 根索引里的 `path` 是人写的,一条挂到
                # `genome/` 的模块条目会让扩权变成一条绕过禁写规则的路——而那正是这套
                # 收窄要挡的东西。查"模块存不存在"挡不住它:它确实存在。
                refused.append(f"{module}:挂载点 {mounts[module]} 不在业务代码里,不可申请")
                continue
            if used + len(approved) >= cap:
                # 超上限不是"少批一个",是**这个任务该转人工**了:一路申请一路扩说明真正的
                # 问题在计划阶段,而不在权限。已经批下去的那几个照样留痕,否则审批人看到的
                # diff 会比记录宽。
                if approved:
                    self._record_grants(task, approved)
                return self._escalate(
                    task.id,
                    f"扩权次数已达上限 {cap},还在申请 {module}——"
                    "先看计划阶段为什么没把这些模块列全,那多半才是真正的问题。",
                )
            approved.append(ScopeGrant(module=module, reason=reason, round_=task.fix_rounds + 1))

        if refused:
            # **不批要留下理由。** 静默丢掉的话员工下一轮不知道自己申请错在哪,多半原样再申请
            # 一次——又一轮 token 烧在同一个误解上。
            write_failure_report(
                self.store.task_dir(task.id),
                round_=task.fix_rounds + 1,
                title=f"第 {task.fix_rounds + 1} 轮:扩权申请未获批准",
                payload={"failures": [{"message": item} for item in refused]},
            )
        if not approved:
            return None
        self._record_grants(task, approved)
        return self.deliver(task.id, TaskEvent.SCOPE_WIDENED)

    def _record_grants(self, task: Task, approved: list[ScopeGrant]) -> None:
        """批准落到两个平面:内容进产物,事件面只记指针。

        **内容只由一个平面记录。** 两个平面各存一份必然发散,而发散之后没有办法判断哪一份
        是对的——所以事件里只有模块名(那是身份,不是内容)与产物路径,理由留在产物里。
        """
        append_grants(self.root, task.id, approved)
        for grant in approved:
            self.log.append(
                task.id,
                actor=ORCHESTRATOR,
                kind=LogKind.SCOPE_WIDENED,
                payload={"module": grant.module, "round": grant.round_, "detail": GRANTS_FILE},
            )

    def _empty_scope_refusal(self, task: Task, handler: Handler) -> str | None:
        """按任务收窄的员工,在算不出模块时不该被派发。返回升级原因,能派就返回 `None`。

        **在派发前判,不在 Job 结束后判。** 后者要烧掉一整个 Job 才发现员工一行都写不进去,
        而症状是一份看不懂的空 diff——既不指向根因,也不指向动作。

        **只管声明了按任务收窄的员工。** 判据是它的可写路径里有没有那个占位符,不是"这个状态
        是不是开发态"——写死状态的话,哪天别的角色也用上这个占位符,这道保护会静默地漏掉它。
        """
        if not isinstance(handler, ProcedureHandler):
            return None
        with contextlib.suppress(EmployeeNotFound):
            employee = self.employees.get(handler.employee_id)
            if not any(
                TASK_MODULES_PLACEHOLDER in glob for glob in employee.permissions.write_paths
            ):
                return None
            if not self.plan_mount_paths(task):
                return (
                    f"{employee.id} 的可写范围来自计划命中的模块,而这个任务算不出任何模块——"
                    "先看计划产物(tasks/<id>/plan.yaml)是不是缺了、坏了,或者根本没写模块。"
                )
        return None

    def plan_mount_paths(self, task: Task) -> list[str]:
        """这次涉及的模块的**挂载点**。收窄授权用它。

        权限层说的是路径,而计划产物说的是模块 id。翻译只在这里做一次,三个授权构造点
        (Job 派发、结对结束后的检查、提交前的复查)拿到的是同一份结果——各自翻译的话
        迟早有一处更宽。
        """
        return effective_mount_paths(self.root, task.id)

    def effective_modules(self, task: Task) -> list[str]:
        """这个任务**当前**授权的模块:计划命中的,加上中途批准扩到的。

        扩权写在运行态而不是回写计划——计划是架构员工的产物,编排器回写它会让"这份计划是谁
        写的"变成一个答不出的问题。
        """
        return effective_modules(self.root, task.id)

    def _plan(self, task: Task) -> dict[str, Any] | None:
        return read_plan(self.root, task.id)

    def _route_inputs(self, task: Task) -> RouteInputs:
        """这次任务碰到了哪些地方——知识路由的三个输入来源。

        **失败报告那一条是这里最大的收益。** 门禁告诉你哪个文件炸了,路由就把覆盖那个文件
        的知识捞到员工面前;而在此之前,那份信息只被渲成给人读的散文,机器完全没用起来。

        算不出来就给空:一个还没开工的任务本来就没有路径可路由,而空输入与"匹配不上"在
        路由记录里是分开的两件事。
        """
        changed: tuple[str, ...] = ()
        with contextlib.suppress(Exception):
            changed = tuple(changed_paths(self.root, task.id))
        return RouteInputs(
            paths=changed,
            failure_paths=failure_paths(
                self.store.task_dir(task.id), before_round=task.fix_rounds + 1
            ),
            keywords=tuple(self.plan_modules(task)),
        )

    def _failures(self, task: Task) -> tuple[FailureReport, ...]:
        """此前各轮的失败报告。**全量,不只是最近一轮。**"""
        records = read_failure_reports(
            self.store.task_dir(task.id), before_round=task.fix_rounds + 1
        )
        return tuple(
            FailureReport(round=record.round, title=record.title, body=record.body)
            for record in records
        )

    # --- needs_itest 判定 -----------------------------------------------------

    async def _decide_itest(self, task_id: str) -> None:
        """定下这个任务要不要跑集成测试,并把结论与理由记进事件流。

        **每次门禁通过都重新求值规则**——规则求值是纯函数,零成本,而 diff 是累积的:
        第二轮才碰到契约文件的改动,第一轮的判定看不见它。兜底那一级不重跑,它烧 token。
        """
        task = self.store.get(task_id)
        decision = await self._itest_decision(task)
        if decision is None:
            return
        self.store.save(self.store.get(task_id).evolve(needs_itest=decision.need))
        self.log.append(
            task_id, actor=ORCHESTRATOR, kind=LogKind.ITEST_DECISION, payload=decision.as_dict()
        )

    async def _itest_decision(self, task: Task) -> ItestDecision | None:
        """三级判定。返回 `None` 表示"沿用上一轮的结论,什么都不改"。"""
        manual = manual_decision(task.itest_override)
        if manual is not None:
            # 人发过话就不必算了——规则求值虽然免费,但算出一个不会被采纳的结论只会让
            # 事件流里多一条误导人的记录。
            return manual

        changed = changed_paths(self.root, task.id)
        decided = self._rule_itest_decision(changed)
        if decided is not None:
            return decided
        if task.needs_itest is not ItestNeed.UNDECIDED:
            # 规则没命中,但上一轮已经判过了。再问一次 AI 只是重复烧同一笔 token。
            return None
        return await self._agent_itest_decision(task, changed)

    def _rule_itest_decision(self, changed: list[str]) -> ItestDecision | None:
        try:
            project_map = load_project_map(self.root)
        except GenomeValidationError:
            # 地图读不出来时不拿它否决——`touches_interface_schema` 这类谓词求不出值,
            # 硬判成"没命中"会把一次本该跑的集成测试悄悄跳掉。交给兜底那一级。
            return None
        return rule_decision(evaluate_rules(changed, project_map, self.rules.impact))

    async def _agent_itest_decision(self, task: Task, changed: list[str]) -> ItestDecision:
        """规则说不知道时的那一次廉价判断。**永远返回一个判定。**

        判定悬空的话任务会卡在 `undecided`,而两条守卫都不认这个值。所以派发失败、Procedure
        没装、员工没被授权——一律降级为"跑",由 `agent_decision` 统一处理。
        """
        bus = self.bus(task)
        slot = bus.allocate(STAGE_ITEST_DECIDE)
        try:
            result = await self.dispatch(
                task,
                ITEST_DECIDE_PROCEDURE,
                ITEST_DECIDE_EMPLOYEE,
                slot,
                TaskState.UNIT_TESTING,
                inputs=self._itest_decide_inputs(task, changed),
            )
        except (KeyError, EmployeeNotFound, ProcedureNotAllowed, GenomeValidationError) as error:
            slot.write_manifest(
                producer=ITEST_DECIDE_EMPLOYEE,
                summary=f"兜底判定派发失败: {type(error).__name__}",
            )
            self._note(task, f"兜底判定派发失败({type(error).__name__}),按安全侧处理")
            return agent_decision(None)
        payload = read_result(slot) if result.ok else None
        slot.write_manifest(
            producer=ITEST_DECIDE_EMPLOYEE,
            outputs=["result.json"] if payload is not None else [],
            summary=f"{ITEST_DECIDE_PROCEDURE} {'成功' if result.ok else '失败'}",
        )
        return agent_decision(payload)

    def _itest_decide_inputs(self, task: Task, changed: list[str]) -> dict[str, Any]:
        """兜底判定看的材料:开发员工的自评影响面、计划里的风险、这一轮的 diff。

        写代码的人对影响面的认知不该被浪费——但它也不是仲裁者,只是这次判断的输入之一。
        """
        develop = self.bus(task).latest(STAGE_DEVELOP)
        result = read_result(develop) if develop is not None else None
        plan = self._plan(task) or {}
        return {
            "task_id": task.id,
            "requirement": task.requirement,
            "impact": (result or {}).get("impact") or {},
            "risks": plan.get("risks") or [],
            "changed_files": changed,
            "modules": self.plan_modules(task),
        }

    def _refuse_a_broken_graph(self, task: Task, outcome: Outcome) -> Outcome:
        """计划拆出一张不合法的图 → 这次解析不算成功。

        **它消耗需求解析重试,不消耗修复轮次。** 图不合法是"没把需求读明白",不是实现没写对
        ——两个计数器的上限不同、理由不同(需求说不清多试无益,所以它的上限低得多),
        混用会让其中一条静默失效。

        判在计划这一刻而不是派发那一刻:放到派发时,任务已经进了开发态,而那里没有回到解析
        的路。报错逐条进事件面——它的第一读者是下一次拆图的那次推理,"节点 B 声明消费
        spec.md,但无上游产出它"能直接改,"图不合法"不能。
        """
        if outcome.event is not TaskEvent.PLAN_DONE:
            return outcome
        issues = self.planned_graph_issues(outcome.payload)
        if not issues:
            return outcome
        self.log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.TOPOLOGY,
            payload={
                "phase": PHASE_CHOSEN,
                "stage": STAGE_PLAN,
                "why": "refused",
                "issues": issues,
            },
        )
        return replace(outcome, event=TaskEvent.PLAN_FAILED)

    def _split_checkpoint(self, task: Task, outcome: Outcome) -> Task | Outcome:
        """需求解析的第三种结局:提案不是计划(PRD 48)。

        产物带 `split` 时**不发 PLAN_DONE**——那会让任务带着一份没有 modules 的"计划"进
        DEVELOPING。三条出路,全部落在既有的边上,状态机零改动:

        - 提案不合法(批外引用、环)→ 按解析失败处理,消耗解析重试,**不投待办**——
          机器判得了对错的东西不该拿去打扰人;
        - 还没有裁决 → 挂一张 `split` 待办停在 CREATED。**这是待确认,不是升级人工**:
          任务健康,人一裁决机器自己往下走;
        - 人打回了 → 反馈以失败报告回注下一轮(与 `_redo` 同一条路),走解析重试。
        """
        if task.state is not TaskState.CREATED or outcome.event is not TaskEvent.PLAN_DONE:
            return outcome
        proposal = proposal_of(outcome.payload)
        if proposal is None:
            return outcome
        issues = split_issues(
            proposal, self.config.limits.split_max_children
        ) + self._split_tree_issues(task, proposal)
        if issues:
            self.log.append(
                task.id,
                actor=ORCHESTRATOR,
                kind=LogKind.NOTE,
                payload={"note": "拆分提案不合法,按解析失败处理", "issues": issues},
            )
            return replace(outcome, event=TaskEvent.PLAN_FAILED)
        todo = self._deliver_split_todo(task)
        if todo.state == TODO_PENDING:
            return self._suspend(task, replace(outcome, suspended=True, todo_id=todo.id))
        verdict = read_verdict(self.root, todo.output_dir) or {}
        if not verdict.get("approved", False):
            feedback = (
                str(verdict.get("feedback") or "").strip() or "确认人打回了拆分提案(没有留反馈)"
            )
            # 反馈写在第 0 轮:计划阶段的重试不动 fix_rounds,而失败报告按
            # `before_round=fix_rounds+1` 读——写在第 1 轮的话,下一轮解析根本看不见它。
            write_failure_report(
                self.store.task_dir(task.id),
                round_=0,
                title="拆分提案被打回",
                payload={"failures": [{"message": feedback}]},
            )
            return replace(outcome, event=TaskEvent.PLAN_FAILED, note=feedback)
        return self._land_split(task, proposal, verdict)

    def _split_tree_issues(self, task: Task, proposal: dict[str, Any]) -> list[str]:
        """提案放进需求树里会不会不合法:容器缺失、超配置上限、树深触顶。

        与 `split_issues` 的分工:那边管提案自身(schema 说不了的批外引用与环),这边管
        "这棵树装不装得下它"——后者要读库与配置,前者是纯函数。两类都在**提案产出那一刻**
        拒,不等人来发现。
        """
        issues: list[str] = []
        if not task.requirement_id:
            issues.append("任务没有需求容器(存量任务),拆分无处安放子需求")
            return issues
        depth = RequirementStore(self.root).depth_of(task.requirement_id)
        if depth + 1 > self.config.limits.split_max_depth:
            issues.append(
                f"需求树深度已达上限 {self.config.limits.split_max_depth}"
                f"(这个需求在第 {depth} 层),不能再拆"
            )
        return issues

    def _land_split(self, task: Task, proposal: dict[str, Any], verdict: dict[str, Any]) -> Task:
        """人确认了:落树(PRD 48 D3)。

        顺序是崩溃安全的全部:**先建子需求(一笔事务),再派首批,最后取消提案任务**。
        任何一个中间态在下一次 advance 重放时都能接上——子需求存在就不再建(树是一笔
        事务落的,存在即完整),尝试存在就不再派,最后一步照常取消。
        """
        requirement_id = task.requirement_id
        assert requirement_id, "_split_tree_issues 已经拦掉了没有容器的任务"
        batch = effective_children(proposal, verdict)
        issues = split_issues({"children": batch}, self.config.limits.split_max_children)
        if issues:
            # 提交面已验过编辑稿;走到这里说明有人绕过提交面手写了裁决文件——按解析失败
            # 处理,不能把一个环落成静默死锁。
            self.log.append(
                task.id,
                actor=ORCHESTRATOR,
                kind=LogKind.NOTE,
                payload={"note": "确认的批次不合法,按解析失败处理", "issues": issues},
            )
            return self.deliver(task.id, TaskEvent.PLAN_FAILED)
        store = RequirementStore(self.root)
        # 幂等键是子需求上的 `origin_task`,**与创建同一笔事务**。拿事件当键的话,
        # "建了但事件还没落"那道缝里的崩溃会让重放再落一批重复的子需求;拿"有没有
        # children"当键的话,重拆分(树上本来就有子需求)的批次永远落不下去。
        children = tuple(
            child for child in store.children_of(requirement_id) if child.origin_task == task.id
        )
        if not children:
            is_resplit = self._created_via(task.id) == "resplit-proposal"
            replaced = [
                child
                for child in store.children_of(requirement_id)
                if not self.store.attempts_of(child.id) and not child.parked
            ]
            if is_resplit:
                stale = self._resplit_went_stale(task, replaced)
                if stale:
                    return stale
            children = store.create_children(
                requirement_id,
                [
                    (
                        str(child["title"]),
                        str(child["text"]),
                        [int(index) for index in (child.get("blocked_by") or [])],
                    )
                    for child in batch
                ],
                priority=task.priority,
                origin_task=task.id,
            )
            new_ids = [child.id for child in children]
            if is_resplit:
                for outdated in replaced:
                    # 不删——记录不被后来的人修饰;搁置原因指向新批次,复盘能顺着走。
                    store.save(
                        outdated.evolve(parked=f"被重拆分替代(新批次: {', '.join(new_ids)})")
                    )
            self.log.append(
                requirement_id,
                actor=ORCHESTRATOR,
                kind=LogKind.REQUIREMENT_SPLIT,
                payload={
                    "children": new_ids,
                    "task": task.id,
                    "replaced": [outdated.id for outdated in replaced] if is_resplit else [],
                },
            )
        self.dispatch_split_children(requirement_id)
        self.log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.NOTE,
            payload={
                "note": f"已拆分为 {len(children)} 个子需求",
                "children": [child.id for child in children],
            },
        )
        # 提案任务以 CANCELLED 了结:这次尝试不会交付,诚实地说出来;连续性由子需求
        # 承载,正如重试的连续性由需求承载。
        return self.deliver(task.id, TaskEvent.CANCEL)

    def _resplit_went_stale(self, task: Task, remainder: list[Requirement]) -> Task | None:
        """重拆分提案挂待确认期间,替代范围里有子需求开工了 → 提案过期,不能落。

        照落的话,新批次覆盖的范围与刚开工的那个子需求重叠——同一块活两份在跑,合并时
        必然相撞。按解析失败退回:重试会带着新的现场重新提案,重试耗尽照常升级人工。
        """
        intended = {
            str(item)
            for item in (self._task_created_payload(task.id).get("replaces") or [])
        }
        still_pending = {child.id for child in remainder}
        started_meanwhile = sorted(intended - still_pending)
        if not started_meanwhile:
            return None
        self.log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.NOTE,
            payload={
                "note": "重拆分提案过期:待替代的子需求在确认期间开工了,按解析失败退回",
                "started": started_meanwhile,
            },
        )
        return self.deliver(task.id, TaskEvent.PLAN_FAILED)

    def start_resplit(self, requirement_id: str, actor: str) -> Task:
        """人对一棵树发起「重新拆分剩余」(PRD 48 D7):新的拆分提案,范围 = 没开工的。

        只由人触发——自动重规划是一个没有终止判据的环(PRD Out of Scope)。报错正文
        REST 与 CLI 逐字共用:两条入口各写一遍的话,第一次改措辞就分叉。
        """
        store = RequirementStore(self.root)
        parent = store.get(requirement_id)  # RequirementNotFound 由调用方翻译
        children = store.children_of(requirement_id)
        if not children:
            raise ValueError(f"这不是一棵树:{requirement_id} 没有子需求,没有'剩余'可重拆")
        started: list[Requirement] = []
        remainder: list[Requirement] = []
        for child in children:
            if child.parked:
                continue
            if self.store.attempts_of(child.id):
                started.append(child)
            else:
                remainder.append(child)
        if not remainder:
            raise ValueError(
                f"{requirement_id} 没有可重拆的子需求:未搁置的子需求全部已开工——"
                "已开工的不动是重拆分的边界,要调整它们,去各自的需求下操作"
            )
        proposal_task = self.store.create(
            title=f"重拆分:{parent.title}",
            requirement=resplit_snapshot(
                parent.text,
                kept=[(child.title, child.id) for child in started],
                remainder=[(child.title, child.id, child.text) for child in remainder],
            ),
            priority=parent.priority,
            requirement_id=requirement_id,
        )
        self.log.append(
            proposal_task.id,
            actor=actor or ORCHESTRATOR,
            kind=LogKind.TASK_CREATED,
            payload={
                "title": proposal_task.title,
                "priority": proposal_task.priority,
                "mode": proposal_task.mode.value,
                "requirement_id": requirement_id,
                "via": "resplit-proposal",
                # 发起那一刻圈定的替代范围。落树时拿它对现场:期间有谁开工了,提案就过期。
                "replaces": [child.id for child in remainder],
            },
        )
        return proposal_task

    def _on_requirement_delivered(self, task: Task) -> None:
        """一次交付在需求树上的涟漪:解锁兄弟,或者让收口出场。

        交付的需求不在树上(平面需求、树根自己的收口)时,顺着 `parent_id` 链什么都
        不会发生——除了收口交付会把"母需求已交付"这颗石子丢进**它**的父层,于是嵌套
        的树也自己往前走。
        """
        try:
            requirement = RequirementStore(self.root).get(task.requirement_id or "")
        except RequirementNotFound:
            return
        if not requirement.parent_id:
            return
        self.sweep_requirement_tree(requirement.parent_id)

    def sweep_requirement_tree(self, requirement_id: str) -> tuple[Task, ...]:
        """把一棵树推到它该在的样子:派发条件齐备的子需求,全交付了就落收口。

        幂等——交付钩子、崩溃恢复(`recover`)、搁置恢复三个入口共用这一条:各写一遍的话,
        某个入口漏掉收口检查不会有任何症状,树就安静地停在"都交付了但没人收口"上。
        """
        created = list(self.dispatch_split_children(requirement_id))
        closing = self._maybe_close(requirement_id)
        if closing is not None:
            created.append(closing)
        return tuple(created)

    def _maybe_close(self, requirement_id: str) -> Task | None:
        """全部子需求已交付、母需求自己还没了局 → 自动发起收口尝试(PRD 48 D6)。

        **升级人工过的收口不自动再来**:机器认输了,下一步是人的(再试一次),机器换个
        马甲重来只会把同一堵墙再撞一遍。人取消过的收口同理——取消是人的显式判断。
        """
        store = RequirementStore(self.root)
        try:
            parent = store.get(requirement_id)
        except RequirementNotFound:
            return None
        if parent.parked:
            return None
        scene = RequirementScene.load(self.root)
        children = scene.children(requirement_id)
        if not children:
            return None
        if any(
            scene.states[child.id] is not RequirementState.DELIVERED for child in children
        ):
            return None
        own = scene.attempts(requirement_id)
        if any(item.state not in TaskState.terminal() for item in own):
            return None  # 收口(或别的什么)正在跑
        if any(item.state in (TaskState.COMPLETED, TaskState.ESCALATED) for item in own):
            return None  # 已交付,或机器已认输等人接管
        if any(
            item.state is TaskState.CANCELLED and self._created_via(item.id) == "split-closing"
            for item in own
        ):
            return None  # 人取消过收口,不自动再来

        entries = []
        for child in children:
            delivered = next(
                (
                    item
                    for item in scene.attempts(child.id)
                    if item.state is TaskState.COMPLETED
                ),
                None,
            )
            entries.append(
                (
                    child.title,
                    child.id,
                    delivered.id if delivered else "",
                    (delivered.branch or "") if delivered else "",
                )
            )
        closing = self.store.create(
            title=f"收口:{parent.title}",
            requirement=closing_snapshot(parent.text, entries),
            priority=parent.priority,
            requirement_id=requirement_id,
        )
        self.log.append(
            closing.id,
            actor=ORCHESTRATOR,
            kind=LogKind.TASK_CREATED,
            payload={
                "title": closing.title,
                "priority": closing.priority,
                "mode": closing.mode.value,
                "requirement_id": requirement_id,
                "via": "split-closing",
            },
        )
        return closing

    def _created_via(self, task_id: str) -> str:
        """这个任务是谁发起的:人提交的没有 via,树派发的是 split-dispatch / split-closing。"""
        return str(self._task_created_payload(task_id).get("via") or "")

    def _task_created_payload(self, task_id: str) -> dict[str, Any]:
        for event in self.log.events(task_id):
            if event.kind is LogKind.TASK_CREATED:
                return dict(event.payload)
        return {}

    def dispatch_split_children(self, requirement_id: str) -> tuple[Task, ...]:
        """给条件齐备的子需求建首次尝试:没有任何尝试,且 `blocked_by` 全部已交付。

        **这是需求层唯一的派发规则**,拆分确认与(PRD 48 issue 03 的)子需求交付都调它。
        自动发起是系统行为,沿用 Origin 纪律:**发起本身**失败只记事件,子需求留在排队中
        人看得见、可手动发起;发起成功后的尝试走正常研发任务生命周期。

        只建不推:驱动是各表面自己的事(serve 下由后台驱动接手,CLI 单步语义只建)。
        """
        scene = RequirementScene.load(self.root)
        created: list[Task] = []
        for child in scene.ready_to_start(requirement_id):
            try:
                attempt = self.store.create(
                    title=child.title,
                    requirement=child.text,
                    priority=child.priority,
                    requirement_id=child.id,
                )
            except Exception as error:  # noqa: BLE001 —— 发起失败只记事件,不断链
                self.log.append(
                    requirement_id,
                    actor=ORCHESTRATOR,
                    kind=LogKind.NOTE,
                    payload={"note": f"子需求 {child.id} 自动发起失败: {error}"},
                )
                continue
            self.log.append(
                attempt.id,
                actor=ORCHESTRATOR,
                kind=LogKind.TASK_CREATED,
                payload={
                    "title": attempt.title,
                    "priority": attempt.priority,
                    "mode": attempt.mode.value,
                    "requirement_id": child.id,
                    # 谁解锁的它:交付驱动派发,不是人提交的。
                    "via": "split-dispatch",
                },
            )
            created.append(attempt)
        return tuple(created)

    def _deliver_split_todo(self, task: Task) -> Todo:
        """给这份提案投(或找回)它的裁决待办。同键幂等——崩溃恢复会重新走到这里。"""
        attempt = task.plan_retries + 1
        slots = self.bus(task).by_node(STAGE_PLAN)
        assert len(slots) >= attempt, "提案已经产出,它的槽必然存在"
        slot = slots[attempt - 1]
        output_dir = slot.path.resolve()
        relative = (
            str(output_dir.relative_to(self.root))
            if output_dir.is_relative_to(self.root)
            else str(output_dir)
        )
        assisted = self.config.topology.assisted
        # 与 assisted 确认同一条链:配了确认人用确认人,否则用决策员工背后的人;
        # 都没有时落在员工 id 上——与 human 运行时"assignee or employee_id"同一个兜底。
        assignee = assisted.confirmer or self._assignee_of(DECISION_EMPLOYEE) or DECISION_EMPLOYEE
        return TodoStore(self.root).deliver(
            Todo(
                id=split_todo_id(task.id, attempt),
                task_id=task.id,
                stage=STAGE_PLAN,
                node=SPLIT_KEY,
                attempt=attempt,
                assignee=assignee,
                employee_id=DECISION_EMPLOYEE,
                procedure_id="requirement-analysis",
                output_dir=relative,
                context_file="",
                kind=SPLIT_TODO,
            )
        )

    def _redo(self, task: Task, outcome: Outcome) -> Task:
        """这一轮不算,回开发态重来一轮。两个来源:人拒了,或者图里有节点失败了。

        **不抛状态机事件,也不新增状态。** 任务本来就在开发态里——"回开发态"要表达的是
        "这一轮不算,再来一轮",而那正是修复轮次的含义。多造一个事件等于给状态机开第二条
        入口,而两条入口迟早对"这算第几轮"给出两个答案。

        意见按**失败报告**回注:下一轮的上下文包本来就装历史失败报告,人的意见走同一条路
        ——只记一条事件的话,下一轮的员工看不到它,于是原样再做一遍。
        """
        # dispatch 会在本次推进途中把每个 Job 的用量即时记到账上。这里收到的 `task`
        # 是推进开始时的快照；直接 evolve 它会把刚写入的 tokens_used 覆盖回旧值。
        current = self.store.get(task.id)
        rounds = current.fix_rounds + 1
        if outcome.redo_nodes:
            # **下一轮只重跑这些。** 记在盘上而不是内存里:下一轮是一次全新的推进,
            # 中间可能隔着一次重启;而"上一轮谁成了"正是那时候唯一还能省下的钱。
            target = self.store.task_dir(task.id) / REDO_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(sorted(outcome.redo_nodes), ensure_ascii=False), encoding="utf-8"
            )
        write_failure_report(
            self.store.task_dir(task.id),
            round_=current.fix_rounds + 1,
            title="这一轮没通过",
            payload={"failures": [{"message": outcome.note or "确认人没有通过这一轮的产出"}]},
        )
        self.log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.TODO,
            payload={"action": "rejected", "note": outcome.note, "round": rounds},
        )
        if rounds > self.max_fix_rounds:
            return self._escalate(
                task.id,
                f"这一步连续没通过,修复轮次已达上限 {self.max_fix_rounds}。"
                f"最后一轮的现场:{outcome.note or '(没有说明)'}。"
                "要么需求本身要改,要么这一步的拆法不对。",
            )
        return self.store.save(current.evolve(fix_rounds=rounds, pending_todo_id=""))

    def _suspend(self, task: Task, outcome: Outcome) -> Task:
        """把任务标记成"在等这张待办",并把投递记进事件面。

        标记存在任务上而不是内存里:一张待办的寿命以工作日计,而进程活不了那么久。
        **投递要进事件面**:三平面对 human Job 没有例外——"这个 Job 是谁干的、何时投递何时
        完成"要能一条时间线讲清楚,人机混合的任务尤其如此。
        """
        todo = TodoStore(self.root).get(outcome.todo_id)
        record_delivery(self.root, todo)
        return self.store.save(task.evolve(pending_todo_id=outcome.todo_id))

    def resume(self, task_id: str) -> Task:
        """人交完活,把"在等谁"这条标记清掉。

        **只清标记,不抛事件**:接下来照常推一步,由处理器的幂等约定认出产物、走重放路径
        抛出它本来就该抛的那个事件。多抛一个"人交活了"的迁移事件,等于给状态机开第二条入口。
        """
        task = self.store.get(task_id)
        return self.store.save(task.evolve(pending_todo_id=""))

    # --- 执行拓扑 -------------------------------------------------------------

    def template_for(
        self, task: Task, state: TaskState, employee: str, procedure: str
    ) -> TemplateChoice:
        """这个任务这一步跑哪张图,以及**为什么是这张**。

        **选择是策略,不是处理器的事**:任务级覆盖优先于根配置,两者都没说就是 `single`。
        选不出来时报错而不是回退——回退的话,一个配成 `dag` 的部署会安安静静地按单节点跑,
        表现为"并行怎么没生效",而原因在三层之外的一张注册表里。

        "为什么是这张"跟着模板一起回来:它要进事件面。事后判断"这个策略值不值"的第一个
        问题就是"这次为什么进了环",而那个答案只在这里存在过一次。
        """
        template_id = task.topology or self.config.topology.default
        if template_id not in BUILDABLE:
            raise UnknownTopology(
                f"模板 {template_id} 还没有可派发的图(能派的: {', '.join(sorted(BUILDABLE))})。"
                "改 topology.default 或任务级覆盖。"
            )

        assisted = self.config.topology.assisted
        # **只长在开发态上。** 别的状态上"拒绝"没有回退边可走:任务会留在原地反复重来,
        # 而那不是拒绝该有的语义。要给门禁或集测加确认,得先想清楚它们的回退边是什么。
        wants_confirmation = employee in assisted.employees or template_id == ASSISTED
        if wants_confirmation and state is TaskState.DEVELOPING:
            # **assisted 优先于 critique。** 两者都命中时不硬拼成一张四节点的图:那是另一个
            # 模板,值得单独定它的终止判据与预算,而不是在这里顺手拼一个没人验过的形状。
            return self._checked(
                assisted_template(
                    employee=employee,
                    procedure=procedure,
                    assignee=assisted.confirmer or self._assignee_of(employee),
                ),
                why="assisted",
            )

        if template_id == BEST_OF_N and state is TaskState.DEVELOPING:
            return self._checked(self._best_of_n(task, employee, procedure), why="task-override")

        wants_test_first = self._test_first_hit(task, state)
        planned = self._planned_graph(task, state)
        if planned is not None:
            # **计划产的图赢。** 两者都要占开发态那一个位置,而"每个计划节点前置一个测试
            # 节点"是另一张图,值得单独定它的写集分离与合并语义,不该在这里顺手拼一个
            # 没人验过的形状。降级**记进事件面**:静默降级会让人以为专职测试生效了。
            why = "plan-graph-wins" if wants_test_first else "plan"
            return self._checked(
                dag_template(employee=employee, procedure=procedure, nodes=planned), why=why
            )

        if wants_test_first:
            return self._checked(
                test_first_template(
                    tester=TESTER_EMPLOYEE,
                    tester_procedure=WRITE_TESTS_PROCEDURE,
                    developer=employee,
                    developer_procedure=procedure,
                ),
                why=wants_test_first,
            )

        wants_probe = self._adversary_hit(task, state)
        if wants_probe:
            # **长在门禁态上,与开发态那条选择链互不干扰。** 两条链各管各的状态,所以
            # "专职测试 + 对抗"可以同时生效——它们不抢同一个位置。
            return self._checked(
                probe_after_gate_template(
                    gate_employee=employee,
                    gate_procedure=procedure,
                    adversary=ADVERSARY_EMPLOYEE,
                    probe_procedure=ADVERSARIAL_PROBE,
                ),
                why=wants_probe,
            )

        why = self._critique_hit(task, state, forced=template_id == CRITIQUE_LOOP)
        critique = self.config.topology.critique
        if why:
            template = critique_loop_template(
                employee=employee,
                procedure=procedure,
                reviewer=REVIEWER_EMPLOYEE,
                critique_procedure=CODE_CRITIQUE,
                max_rounds=critique.max_rounds,
                budget_share=critique.budget_share,
            )
        else:
            template = single_template(
                employee=employee,
                procedure=procedure,
                # **员工是人的话,这个节点就是 manual。** 不标的话图上写着"自动",而实际上
                # 它会变成一张待办——自动化率这个指标读的正是这张图,于是它会把人干的活
                # 算成机器干的。
                executor=self._executor_of(employee),
            )

        return self._checked(template, why=why)

    def _checked(self, template: TopologyTemplate, why: str) -> TemplateChoice:
        """派发前跑一遍图校验。**无例外路径。**

        single 平凡通过,所以这条在今天多半只是把规则立在那儿——但它必须从第一天就没有旁路,
        否则第一张真图会带着一条已有的例外上线。
        """
        issues = validate_topology(template)
        if issues:
            raise TopologyRefused(tuple(issue.render() for issue in issues))
        return TemplateChoice(template=template, why=why)

    def _executor_of(self, employee_id: str) -> Executor:
        """这个员工是人还是机器。**图上要如实写。**"""
        try:
            runtime = self.employees.get(employee_id).runtime
        except EmployeeNotFound:
            return Executor.AUTO
        return Executor.MANUAL if runtime == HUMAN else Executor.AUTO

    def _assignee_of(self, employee_id: str) -> str:
        """这个员工背后的人。**没配确认人时的兜底**:让那个角色自己确认自己的产出没有意义,
        所以它只在员工本来就是人的时候才是一个有效答案。
        """
        try:
            return self.employees.get(employee_id).assignee
        except EmployeeNotFound:
            return ""

    def _best_of_n(self, task: Task, employee: str, procedure: str) -> TopologyTemplate:
        """N 路变体的那张图,**派发前先算钱**。

        预算 = N × 单路上限,一次性校验:跑到一半发现钱不够的话,前面几路的钱已经花掉了,
        而那正是这套模式最贵的失败形态。
        """
        settings = self.config.topology.best_of_n
        attempts = settings.attempts[: settings.max_attempts]
        needed = len(attempts) * self.config.budgets.per_job_tokens
        remaining = (task.budget_tokens or 0) - task.tokens_used
        if self.config.budgets.enforce and task.budget_tokens is not None and needed > remaining:
            raise TopologyRefused(
                (
                    f"{len(attempts)} 路变体要 {needed} token,而这个任务只剩 {remaining}。"
                    "降低路数,或者把它改回单路。",
                )
            )
        return best_of_n_template(
            employee=employee,
            procedure=procedure,
            # **适应度就是门禁那道工序本身**,不是它的一个仿制品:每一路跑的与主路径跑的
            # 是同一份确定性检查,否则"过闸"这两个字在两条路上意思不一样。
            gate_procedure=GATE_PROCEDURE,
            judge_employee=settings.judge_employee,
            judge_procedure=settings.judge_procedure,
            variants=[Variant(key=item.key, hint=item.hint) for item in attempts],
        )

    def _planned_graph(self, task: Task, state: TaskState) -> list[dict[str, Any]] | None:
        """计划有没有拆出一张图。没拆就是 `None`——一条线也是合法的计划。

        **只在开发态用**:图是"这次的活怎么分",而需求解析与门禁各自只有一件事要干。
        """
        if state is not TaskState.DEVELOPING:
            return None
        nodes = (self._plan(task) or {}).get("nodes")
        if not isinstance(nodes, list) or len(nodes) < 2:
            # 单节点图与一条线没有区别,而 `single` 那条路是逐字节验过的。
            return None
        return [item for item in nodes if isinstance(item, dict)]

    def _test_first_hit(self, task: Task, state: TaskState) -> str:
        """这次要不要让测试员工先出题,以及命中的是哪一档。不出题返回空串。

        **只长在开发态上。** 别的状态上"先写测试再实现"没有意义:门禁与集测各自只有一件
        事要干,而需求解析压根没有可测的产出。
        """
        return self._dial_hit(
            task, state, TaskState.DEVELOPING, tester_is_dedicated, self.config.quality_line.tester
        )

    def _harvest_attack_cases(self, task: Task, outcome: Outcome) -> list[Path]:
        """把这一轮抓到的攻击用例提案转正。没抓到东西时什么都不做。

        **只提案,不落地。** 落地走正常提交路径——由测试员工或开发员工在一次普通任务里把
        用例加进业务仓,过门禁、过评审。这里直接写进业务仓的话,回归集就有了一条绕过门禁的
        入口,而它迟早会被用来绕过门禁。
        """
        payload = outcome.payload or {}
        if "findings" not in payload:
            return []
        slots = self.bus(task).by_node(STAGE_UNIT_GATE, PROBE)
        promotions = promotions_for(
            task.id,
            payload,
            slot=slots[-1].path.name if slots else "",
            target_dir=self._regression_dir(task),
        )
        if not promotions:
            return []
        written = write_promotions(self.root, promotions)
        self.log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.PROMOTION,
            payload={
                "action": "attack-case-promotion",
                "proposals": [item.as_dict() for item in promotions],
            },
        )
        return written

    def _regression_dir(self, task: Task) -> str:
        """**建议**转正落到业务仓的哪个目录。它是提案里的一句话,不是一条会被执行的路径。

        故意不去从出题人的写集反推一个"正确"落点:那几条是 glob,glob 反推目录要么给出一个
        带通配符的东西,要么在多条之间挑一条——两者都是在假装这里有一个机器算得出的答案。
        真正决定落点的是接手转正的那个人(或员工),而他有整份提案可读。

        计划一个模块都认不出时退回任务目录:**方向是不落进业务仓**,而那是可恢复的。
        """
        mounts = self.plan_mount_paths(task)
        if not mounts:
            return f"tasks/{task.id}"
        return f"{mounts[0].rstrip('/')}/{SUGGESTED_REGRESSION_SUBDIR}"

    def probe_inputs(self, task: Task) -> dict[str, Any]:
        """红队这一趟带着什么去攻击:这一轮改了什么、验收标准、以及**攻击清单**。

        攻击清单 = 被改动路径命中的功能卡片里逐条列出的不变量。**知识分类学在这里兑现**:
        不变量卡本来就是"哪条线碰不得"的清单,攻击就是逐条问一句"真的碰不得吗"。

        知识树读不出来时给空清单而不是抛错:卡片坏了不该让这一步整个失效——红队照样能按
        四类攻击方法试,只是少了清单那一半。
        """
        changed = self._changed_files(task)
        return {
            "changed_files": changed,
            "acceptance": list((self._plan(task) or {}).get("acceptance") or []),
            "invariants": self._invariants_covering(changed),
        }

    def _invariants_covering(self, changed: Sequence[str]) -> list[str]:
        try:
            tree = load_tree(self.root)
        except GenomeValidationError:
            return []
        found: list[str] = []
        for path in changed:
            for ref in features_covering(tree, path):
                card = tree.card(ref.module_id, ref.feature.id)
                if card is not None:
                    found.extend(invariants_of(card))
        # 去重但保序:同一张卡片会被多条改动路径命中,而重复的清单会让红队重复攻击同一条。
        return list(dict.fromkeys(found))

    def _changed_files(self, task: Task) -> list[str]:
        """上一轮开发碰过哪些文件。产物里读不出来就退回 git 的事实。"""
        listed = self._changed_from_develop(task)
        return listed if listed is not None else self.changed_paths(task)

    def _changed_from_develop(self, task: Task) -> list[str] | None:
        """开发自述的变更清单。**从后往前找第一份带 `changed_files` 的产物。**

        取最后一个槽是错的:上一轮如果进过环,最后一个 develop 槽是批判的小票,它没有这个
        字段——于是"上一轮改了什么"永远算成空,而消费它的两条策略都是给修复轮准备的。
        """
        for slot in reversed(self.bus(task).by_stage(STAGE_DEVELOP)):
            changed = (read_result(slot) or {}).get("changed_files")
            if isinstance(changed, list):
                return [str(item) for item in changed]
        return None

    def _adversary_hit(self, task: Task, state: TaskState) -> str:
        """这次要不要在门禁之后追加一次对抗,以及命中的是哪一档。不追加返回空串。

        **只长在门禁态上。** 开发态上"先攻击再实现"没有对象;集测态上再攻击就晚了——
        那时改动已经准备进提交流水线,而回退一步的代价高得多。
        """
        return self._dial_hit(
            task,
            state,
            TaskState.UNIT_TESTING,
            adversary_is_on,
            self.config.quality_line.adversary,
        )

    def _dial_hit(
        self,
        task: Task,
        state: TaskState,
        lives_on: TaskState,
        decide: Callable[[Config, bool], bool],
        dial: StrEnum,
    ) -> str:
        """一个质量线旋钮这次命不命中,以及命中的是哪一档。不命中返回空串。

        两个旋钮共用这一段:**"只长在某个状态上"与"受保护路径命中"两条判据完全相同**,
        各写一遍的话,哪天改了受保护路径的算法会只改到其中一个,而漏掉的那个不会有症状。
        """
        if state is not lives_on:
            return ""
        protected_hit = self._plan_touches_protected(self.plan_modules(task))
        return dial.value if decide(self.config, protected_hit) else ""

    def _critique_hit(self, task: Task, state: TaskState, forced: bool = False) -> str:
        """这次该不该进精化环,以及命中的是哪条策略。不进环返回空串。

        **判据只用已有的事实。** 改了哪些路径、命中哪些模块,系统里已经有唯一来源;再算
        一遍的话,迟早出现"按 A 该进环、按 B 不该"这种自相矛盾。

        **规模判据用的是代理指标。** 真正想问的是"这次改动大不大",但 diff 在决定进不进环
        的那一刻**还不存在**——它由环里的第一个节点产出。所以首轮用计划命中的模块数,
        修复轮用上一轮开发碰过的文件数;两者都拿不到就不进环,而不是猜一个。
        """
        if state is not TaskState.DEVELOPING:
            # 环只长在开发态上。别的状态硬套的话,"需求解析要不要批判"会变成一个没人回答过
            # 的问题,而它的答案默认成了"要"。
            return ""
        critique = self.config.topology.critique
        if forced:
            # 任务级把模板直接点成了环:那是一个人做出的决定,不再问策略。
            return "task-override"
        if not critique.enabled:
            return ""

        modules = self.plan_modules(task)
        if critique.on_protected and self._plan_touches_protected(modules):
            return "protected-paths"
        if critique.min_modules and len(modules) >= critique.min_modules:
            return "modules"
        changed = self._last_changed_files(task)
        if critique.min_changed_files and changed >= critique.min_changed_files:
            return "changed-files"
        return ""

    def _plan_touches_protected(self, modules: list[str]) -> bool:
        """计划命中的模块的子树,与哪条受保护路径相交。

        受保护路径是项目自己声明"这里出事代价大"的地方——它与"值得多花一轮批判"是同一个
        判断,所以直接复用,不另立一张风险清单。

        **判的是"两个 glob 有没有交集",不是"谁在谁下面"。** 受保护路径既可能在模块之上
        (整个仓受保护),也可能在模块之内(`repos/order/src/payment/**`),而后者才是常态
        ——只判前者的话这条策略在真实项目里永远不命中,而它的默认值是开着的。
        """
        protected = list(self.rules.protected.all_paths)
        if not protected:
            return False
        subtrees = [f"{path.rstrip('/')}/**" for path in map(self._mount_path, modules) if path]
        return any(globs_overlap(subtree, glob) for subtree in subtrees for glob in protected)

    def _mount_path(self, module_id: str) -> str:
        project_map = self.project_map()
        if project_map is None:
            return ""
        for module in project_map.modules:
            if module.id == module_id:
                return str(module.path)
        return ""

    def _last_changed_files(self, task: Task) -> int:
        """上一轮开发碰过几个文件。没有上一轮就是 0(首轮走别的判据)。"""
        return len(self._changed_from_develop(task) or [])

    def record_run(
        self, task: Task, template: TopologyTemplate, run: TopologyRun, stage: str
    ) -> None:
        """这次拓扑跑成了什么。**单节点的图不记**——它没有"为什么停"可言,记一条只会把
        事件面灌满与 `single` 等价的噪音。
        """
        if len(template.nodes) <= 1:
            return
        self.log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.TOPOLOGY,
            payload={
                # **两条拓扑事件靠这个字段分开**:一条是"选了哪张图",一条是"这张图跑成了
                # 什么"。让读的人按有没有某个键去猜,加第三种事件时那些猜法会一起错。
                "phase": PHASE_RAN,
                "stage": stage,
                "template_id": template.id,
                "stopped_because": run.stopped_because,
                "rounds": run.rounds,
                "tokens_used": run.tokens_used if run.tokens_available else None,
                "tokens_available": run.tokens_available,
                "nodes": [
                    {
                        "id": outcome.node.id,
                        "kind": outcome.node.kind.value,
                        "ok": outcome.ok,
                        "passed": bool((outcome.payload or {}).get("passed", False)),
                        # 判定的**结论与条数**是指标,进事件面;意见**原文**是产物,只记指针。
                        "approved": bool((outcome.payload or {}).get("approved", False)),
                        "findings": len((outcome.payload or {}).get("findings", []) or []),
                        "slot": outcome.slot,
                    }
                    for outcome in run.outcomes
                ],
            },
        )

    def merge_nodes(self, task: Task, nodes: list[str]) -> str:
        """把并行节点的改动按拓扑序合并回任务分支。冲突返回说明,顺利返回空串。

        **顶层仍然一个任务分支、一个 PR。** 拆成 N 个 PR 的话,审批人面对的是一堆互相依赖的
        碎片,而"这个任务到底改了什么"要靠他自己拼——双层提交拓扑不变。

        **按拓扑序合**:下游读的是上游产出的东西,反着合会让中间态在任务分支上出现一次。
        """
        return GitWorkspace(self.root).merge_slots_into_task(task.id, nodes)

    def nodes_to_redo(self, task: Task) -> set[str]:
        """这一轮里哪些节点必须重跑。空集合 = 没有上一轮,或者上一轮整张图都得重来。"""
        target = self.store.task_dir(task.id) / REDO_FILE
        if not target.is_file():
            return set()
        try:
            return {str(item) for item in json.loads(target.read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError):
            return set()

    def reset_node_worktree(self, task: Task, node: str) -> None:
        """重试 DAG 节点前从当前任务分支重建其版本基线。"""
        GitWorkspace(self.root).reset_slot(task.id, node)

    def record_contrast(self, task: Task, run: TopologyRun) -> None:
        """胜负对比落盘,然后清理落选的工作树。

        **N 倍成本的辩护理由有一半在这里**:买的不只是更好的方案,还有"为什么别的路不行"的
        可遗传知识——失败方案通常连记录都不留,而它恰恰是经验卡片最稀缺的素材。

        记的是**指针**(各路的产物目录),不是产物内容:同一件事只由一个平面记录内容。
        """
        attempts = [
            {
                "variant": outcome.node.variants[0].key if outcome.node.variants else "",
                "hint": outcome.node.variants[0].hint if outcome.node.variants else "",
                "node": outcome.node.id,
                "slot": outcome.slot,
                "passed": bool((outcome.payload or {}).get("passed", False)),
            }
            for outcome in run.outcomes
            if outcome.node.variants
        ]
        contrast: dict[str, Any] = {
            "task_id": task.id,
            "winner": run.winner,
            "survivors": list(run.survivors),
            "attempts": attempts,
            "judge": next(
                (
                    (outcome.payload or {}).get("notes", "")
                    for outcome in run.outcomes
                    if outcome.node.id == JUDGE
                ),
                "",
            ),
        }
        target = self.store.task_dir(task.id) / CONTRAST_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(contrast, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.TOPOLOGY,
            payload={"phase": "contrast", "winner": run.winner, "survivors": list(run.survivors)},
        )
        # **蒸馏取完材才清理。** 落选分支上的东西是证据,删早了就只剩一句"另外两路没赢"。
        workspace = GitWorkspace(self.root)
        for variant in {str(item["variant"]) for item in attempts}:
            if variant and variant != run.winner:
                workspace.cleanup_slot(task.id, f"{ATTEMPT}-{variant}")

    def record_topology(self, task: Task, choice: TemplateChoice, stage: str) -> None:
        """把这次跑的拓扑实例整份写进事件面。

        **图是数据不是黑箱。** 前端画图的数据源就是它,不另造一份;并行出问题时,
        "当时跑的到底是哪张图"是第一个要问的问题。
        """
        self.log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.TOPOLOGY,
            payload={
                "phase": PHASE_CHOSEN,
                "stage": stage,
                "template": choice.template.to_payload(),
                "why": choice.why,
            },
        )

    # --- 提交阶段 -------------------------------------------------------------

    def workdir(self, task: Task) -> Path:
        """员工干活的地方。提交流水线也在这上面查。"""
        return self._workdir(task)

    def changed_paths(self, task: Task) -> list[str]:
        """这个任务到目前为止改了哪些路径。

        与门禁、与影响判定共用同一个来源(`gates.task_gates.changed_paths`)。三处各自推导
        的话,迟早出现"按 A 算越权了、按 B 算没有"这种自相矛盾。
        """
        return changed_paths(self.root, task.id)

    def project_map(self) -> ProjectMap | None:
        """项目地图。读不出来时返回 `None` 而不是抛错。

        风险评级会因此少掉从地图推导的那两条规则,但内置那三条仍然生效——地图坏了不该让
        整道关卡失效,那个方向是把高风险改动放过去。
        """
        try:
            return load_project_map(self.root)
        except GenomeValidationError:
            return None

    def participants(self, task: Task) -> list[EmployeeConfig]:
        """这个任务里**真的干过活**的员工。

        从事件流数,不从配置读:配置说的是"谁可以参与",而授权范围的并集只该包含真的参与过
        的那些。按配置算的话,一个从未被派发的员工也会把它的授权范围贡献进来。
        """
        registry = self.employees
        found: list[EmployeeConfig] = []
        for event in self.log.events(task.id):
            if event.kind is not LogKind.JOB_STARTED:
                continue
            try:
                employee = registry.get(event.actor)
            except EmployeeNotFound:
                continue
            if employee not in found:
                found.append(employee)
        return found

    def _stale_roster(self) -> str | None:
        """花名册的归属收敛了没有。没收敛返回一句可操作的原因,收敛了返回 `None`。"""
        conflicts = self.employees.conflicts
        return "花名册的工序归属没有收敛:" + ";".join(conflicts) if conflicts else None

    def extra_forbid(self, task: Task, employee_id: str) -> tuple[str, ...]:
        """这个任务额外禁止这个员工写哪些路径。**唯一来源。**

        角色级的禁令写在员工定义里;这里算的是**任务级**的那一类——它随策略与这一次的生效
        模块变化,写不进定义。三处授权构造(Job 派发、结对会话结束后的检查、提交前的越权
        复查)都从这里取,各算一遍的话迟早出现"派发时允许、复查时越权"这种自相矛盾,
        而矛盾的两边都会声称自己是对的。

        缺省是空的:没有任何策略要求额外收窄时,授权范围与不存在这个机制时逐字节相同。
        """
        return extra_forbid(
            employee_id,
            DEVELOPER_EMPLOYEE,
            self._tester_write_paths(),
            self._dedicated_testing(task),
        )

    def _tester_write_paths(self) -> tuple[str, ...]:
        """出题人声明的写集——**"哪些路径算测试"的唯一答案**。

        测试员工不在花名册里时给空:那样的工作区根本派不出出题节点,禁令自然也不该存在。
        """
        try:
            return tuple(self.employees.get(TESTER_EMPLOYEE).permissions.write_paths)
        except EmployeeNotFound:
            return ()

    def _dedicated_testing(self, task: Task) -> bool:
        """这个任务的测试是不是专职分工。

        **与状态无关。** 写集分离在提交前复查那一刻同样要成立,而那时任务早就离开开发态了
        ——按状态算的话,一个越权改动能在 Job 那一关被抓住却在最后一关被放行。
        """
        protected_hit = self._plan_touches_protected(self.plan_modules(task))
        return tester_is_dedicated(self.config, protected_hit)

    def scope_policies(self, task: Task) -> list[ScopePolicy]:
        """提交前复查用的授权范围:参与者**各自**的那一份。

        并成一份的话,两个员工的禁写集合会互相污染——见 `participant_policies` 的说明。
        """
        participants = self.participants(task)
        return participant_policies(
            participants,
            task.id,
            self.rules.protected,
            self.plan_mount_paths(task),
            {employee.id: self.extra_forbid(task, employee.id) for employee in participants},
        )

    def publish_branches(self, task: Task) -> list[str]:
        """把员工的改动提交到各子仓任务分支并推到远端。

        **推送凭证只由编排器持有。** 员工负责产出提交,推送是这一层的动作——员工进程被
        攻破也拿不到推送能力。
        """
        pushed = publish_task_branches(
            GitWorkspace(self.root),
            task.id,
            self.changed_paths(task),
            message=commit_message(
                CommitInputs(
                    task_id=task.id,
                    title=task.title,
                    summary=self._change_summary(task),
                    employees=tuple(item.id for item in self.participants(task)),
                )
            ),
        )
        if pushed:
            self._note(task, f"已推送任务分支: {', '.join(pushed)}")
        return pushed

    def _change_summary(self, task: Task) -> str:
        develop = self.bus(task).latest(STAGE_DEVELOP)
        result = (read_result(develop) if develop is not None else None) or {}
        return str(result.get("summary") or (result.get("impact") or {}).get("rationale") or "")

    def merge_task(self, task: Task, output_dir: Path) -> tuple[TaskEvent, dict[str, Any]]:
        """双层合并。返回该送进状态机的事件与产物。

        **幂等靠任务记录里的 PR 引用**,不靠产物目录:平台上重复开 PR 会得到两个,而产物
        目录只能告诉我们"这一轮跑过",不能告诉我们"跑到第几步"。
        """
        workspace = GitWorkspace(self.root)
        forge = self.forge()
        known = self.merge_state(task)
        # 先把两层仓库都校验完再开任何 PR。否则顶层 origin 缺失时,子仓可能已经合并,
        # 留下一个无法由 Workspace 指针提交收口的中间态。
        workspace_repository = workspace_forge_repository(
            self.root, self.config.platform.git_host
        )
        repositories = submodule_forge_repositories(
            self.root, self.config.platform.git_host
        )
        title, body = self._pr_text(task)
        try:
            result = merge_submodules(
                workspace,
                forge,
                task.id,
                submodule_remotes=repositories,
                workspace_remote=workspace_repository,
                base=self.config.platform.protected_branch,
                title=title,
                body=body,
                known=known,
            )
            self._save_merge_state(task, result)
            merge_workspace(forge, result)
        except SubmoduleMergeFailed as error:
            # 已经合掉的那些要记下来:重放时不能再合一遍,而人也得知道主干上现在有什么。
            partial = DualLayerResult(task_id=task.id)
            partial.submodules = list(error.already_merged)
            self._save_merge_state(task, partial)
            return TaskEvent.MERGE_CONFLICT, _merge_failure_payload(task, error)
        except (MergeConflict, ForgeError) as error:
            return TaskEvent.MERGE_CONFLICT, _merge_failure_payload(task, error)

        self._save_merge_state(task, result)
        payload = result.as_dict() | {
            "task_id": task.id,
            "producer": ORCHESTRATOR,
            "created_at": _now(),
            "passed": True,
            "kind": VerdictKind.NONE.value,
        }
        _ = output_dir
        return TaskEvent.MERGED, payload

    def forge(self) -> Forge:
        """开 PR 与合并走哪条路。判据见 `space.forge.select`。"""
        return select_forge(self.config.platform.git_host)

    def merge_state(self, task: Task) -> DualLayerResult | None:
        """上一次合并跑到哪儿了。没跑过就是 `None`。"""
        path = self.store.task_dir(task.id) / MERGE_STATE_FILE
        if not path.is_file():
            return None
        return DualLayerResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _save_merge_state(self, task: Task, result: DualLayerResult) -> None:
        path = self.store.task_dir(task.id) / MERGE_STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _pr_text(self, task: Task) -> tuple[str, str]:
        plan = self._plan(task) or {}
        summary = self._change_summary(task)
        return (
            f"{task.id}: {task.title}",
            pull_request_body(
                PullRequestInputs(
                    task_id=task.id,
                    title=task.title,
                    acceptance=tuple(str(item) for item in plan.get("acceptance") or []),
                    risk=task.risk_level or "low",
                    matched_rules=self._matched_rules(task),
                    reports=self._report_paths(task),
                    summary=summary,
                )
            ),
        )

    def _matched_rules(self, task: Task) -> tuple[str, ...]:
        """这次改动命中了哪些高风险规则。**从提交前检查的产物里读,不是重新评一次**——
        评级只在 `READY_TO_COMMIT` 跑一次,PR 文案生成在 `MERGING`;两次各评一次的话,
        工作树在这中间发生任何变化都会让两处结论对不上。读不到就是空,PR 里就不出现
        这一段,而不是编一个。
        """
        precheck = self.bus(task).latest(STAGE_PRECHECK)
        if precheck is None:
            return ()
        payload = read_result(precheck) or {}
        matched = payload.get("matched_rules") or []
        return tuple(str(item) for item in matched)

    def _report_paths(self, task: Task) -> dict[str, str]:
        """报告在任务目录里的相对位置。评审者不该为了找它们去翻七个目录。"""
        bus = self.bus(task)
        found = {}
        for stage, name in (("unit-gate", "gate-report.json"), ("itest", "itest-report.json")):
            slot = bus.latest(stage)
            if slot is not None and (slot.path / name).is_file():
                found[stage] = f"{slot.path.name}/{name}"
        return found

    def workdir_of(self, task_id: str) -> Path:
        """这个任务在哪儿干活。

        **结对会话要用它**:会话的 workdir 就是任务 worktree(其寿命因此等于任务)。
        公开一个方法而不是让调用方自己拼 `GitWorkspace(root).worktree_path(...)` ——
        两处各拼一遍的话,"建过就用它、没建就退回根"这条兜底规则只会有一处。
        """
        return self._workdir(self.store.get(task_id))

    def _workdir(self, task: Task, slot: str = "") -> Path:
        """员工干活的地方。建过隔离工作区就用它,否则退回 Workspace 根。

        给了槽就是**图里某个节点自己的工作树**:并行节点各写各的,互相看不见对方写到一半的
        东西。它按需建出来——图是这一轮才决定的,建工作树那个副作用发生在更早的状态迁移上。
        """
        workspace = GitWorkspace(self.root)
        if slot:
            task_tree = workspace.worktree_path(task.id)
            if not task_tree.is_dir():
                # 任务主工作树都没有的话,节点工作树无从分叉——退回今天的行为。
                return self.root
            return workspace.checkout_isolated(task.id, slot=slot)
        path = workspace.worktree_path(task.id)
        return path if path.is_dir() else self.root

    def _charge(self, task: Task, result: DispatchResult) -> None:
        """把这次的 token 记到任务头上。

        用量不可得时不加——加 0 与加"未知"在账面上一样,但把"未知"当成 0 会让预算闸
        以为还有额度。
        """
        if not result.tokens_available or result.tokens_used == 0:
            return
        current = self.store.get(task.id)
        self.store.save(current.evolve(tokens_used=current.tokens_used + result.tokens_used))

    # --- 内部 ----------------------------------------------------------------

    def _facts(self, outcome: Outcome | None) -> Facts:
        return Facts(
            max_fix_rounds=self.max_fix_rounds,
            plan_valid=self._plan_is_sound(outcome),
            plan_retryable=self._plan_failure_is_retryable(outcome),
            plan_failure_cause=(
                outcome.plan_failure_cause if outcome is not None else PlanFailureCause.NONE
            ),
            result_valid=outcome.valid if outcome else True,
            recoverable=_is_recoverable(outcome),
            enforce_budget=self.config.budgets.enforce,
        )

    @staticmethod
    def _plan_failure_is_retryable(outcome: Outcome | None) -> bool:
        """只有“计划本来通过、但生成的执行图非法”值得在原输入上再试一次。

        `requirement-analysis` 主动给出 `passed:false` 表示它缺少需求事实。再次派发拿到的
        输入完全相同，不可能补出那些事实，只会鼓励下一轮猜一个口径。
        """
        return bool(
            outcome is not None
            and outcome.event is TaskEvent.PLAN_FAILED
            and outcome.payload is not None
            and outcome.payload.get("passed") is True
        )

    def _plan_is_sound(self, outcome: Outcome | None) -> bool:
        """计划有效不只是"产物合 schema"。

        涉及模块必须是项目地图里**真实存在**的 id:编造的 id 会让基因组切片切出一份空
        认知,而员工拿着空认知照样会动手——它不知道自己少看了东西。
        """
        if outcome is None:
            return True
        if not outcome.valid or outcome.payload is None:
            return outcome.valid
        if self.planned_graph_issues(outcome.payload):
            return False
        declared = load_plan_modules(outcome.payload)
        if not declared:
            return True
        try:
            known = {module.id for module in load_project_map(self.root).modules}
        except GenomeValidationError:
            # 地图读不出来时不拿它否决计划——那是地图的问题,不是这份计划的问题。
            return True
        return set(declared) <= known

    def planned_graph_issues(self, payload: dict[str, Any] | None) -> list[str]:
        """计划拆出来的那张图有哪些毛病。合法(或者压根没拆)就是空列表。

        **纯判断,不写任何东西**:它被两个地方问到(定事实、记事件),而一个会写日志的判断
        被问两次就会记两条。
        """
        nodes = (payload or {}).get("nodes")
        if not isinstance(nodes, list) or len(nodes) < 2:
            return []
        template = dag_template(
            employee="", procedure="", nodes=[item for item in nodes if isinstance(item, dict)]
        )
        return [issue.render() for issue in validate_topology(template)]

    def _apply(self, task: Task, decision: Decision, outcome: Outcome | None) -> Task:
        assert decision.to is not None
        updated = task.evolve(
            state=decision.to,
            fix_rounds=task.fix_rounds + decision.fix_rounds_delta,
            plan_retries=task.plan_retries + decision.plan_retries_delta,
        )
        if task.state is TaskState.CREATED and outcome is not None:
            self._land_plan(task, outcome)
        if outcome is not None and outcome.payload and outcome.payload.get("risk"):
            # 风险评级跟状态一起写:分两次存的话,后写的那次用的是先前读到的旧值。
            updated = updated.evolve(risk_level=str(outcome.payload["risk"]))
        if decision.to is TaskState.ESCALATED:
            updated = updated.evolve(escalate_reason=decision.reason or "达到上限,已升级人工")
        updated = self.store.save(updated)
        self._run_effects(updated, decision, outcome)
        final = self.store.get(updated.id)
        if final.is_terminal:
            self._seal(final)
        if final.state is TaskState.COMPLETED and final.requirement_id:
            # 需求层的交付驱动(PRD 48 D4):这次交付可能解锁了树上的兄弟,或者是
            # 最后一块——收口该出场了。挂在这里而不是某个表面上,CLI 与 serve 两条
            # 推进路径才都触发(与 TRIGGER_EVOLUTION 同一个道理)。
            self._on_requirement_delivered(final)
        return final

    def _land_plan(self, task: Task, outcome: Outcome) -> None:
        """把计划产物落成 `plan.yaml`。YAML 而不是 JSON:需求方会读它。

        **这里不再定 `needs_itest`。** 此前用计划自报的 `cross_module` 当判定,那是 PRD 05
        的占位:计划阶段还没有 diff,而影响判定看的就是 diff。现在它降级成兜底判断的输入
        之一,真正的判定在门禁通过时做(`_decide_itest`)。
        """
        if outcome.payload is None or not outcome.payload.get("passed", False):
            return
        if SPLIT_KEY in outcome.payload:
            # 提案不是计划:落成 plan.yaml 的话,需求方读到的是一份"计划",而任务
            # 根本没有(也不会按它)进入开发。
            return
        target = self.store.task_dir(task.id) / PLAN_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(outcome.payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    def escalate(self, task_id: str, reason: str) -> Task:
        """从外面把一个任务升级人工。

        **公开的那一条与内部那条是同一条**:待办三段走完、预算耗尽、连续被守卫挡住,三者
        走同一个落地(含封存与蒸馏触发)。各写一遍的话,其中一条迟早漏掉封存,而漏掉的
        那一次恰好是最需要证据的那一次。
        """
        return self._escalate(task_id, reason)

    def _escalate(self, task_id: str, reason: str) -> Task:
        """升级人工的两条兜底路径共用的落地。**这条路不经过迁移表**——任务预算耗尽与
        连续被守卫挡住都是在 `advance` 内部直接判定的,不是表里的一条边——所以
        `Effect.TRIGGER_EVOLUTION` 不会自动跟着到这里,得手动补一次。ESCALATED 任务是
        最富矿的蒸馏素材,漏了这条路径的话恰恰漏掉最该学的那批任务。
        """
        escalated = self.store.save(
            self.store.get(task_id).evolve(state=TaskState.ESCALATED, escalate_reason=reason)
        )
        self.log.append(
            task_id, actor=ORCHESTRATOR, kind=LogKind.ESCALATED, payload={"reason": reason}
        )
        self._seal(escalated)
        self._trigger_evolution(escalated)
        return escalated

    def _handle_refusal(self, task: Task, decision: Decision) -> Task:
        """守卫挡住之后的兜底。

        表里没有为"产物不合契约"准备边,任务会原地不动并被反复重推。连续被挡的次数
        达到修复轮次上限即升级人工——次数从事件流里数出来,所以重启后仍然有效。

        **终态不适用。** 终态收到的任何事件都会被判为非法迁移,而"重复取消一个已取消
        的任务"正是崩溃恢复的常规动作——不排除的话,取消四次的任务会变成 ESCALATED。
        """
        if task.is_terminal or self._consecutive_refusals(task.id) < self.max_fix_rounds:
            return task
        reason = f"连续 {self.max_fix_rounds} 次无法离开 {task.state.value}: {decision.reason}"
        return self._escalate(task.id, reason)

    def _consecutive_refusals(self, task_id: str) -> int:
        count = 0
        for event in reversed(self.log.events(task_id)):
            if event.kind is LogKind.TRANSITION_REFUSED:
                count += 1
            elif event.kind is LogKind.TRANSITION:
                break
        return count

    def _run_effects(self, task: Task, decision: Decision, outcome: Outcome | None) -> None:
        """执行副作用。

        `DISPATCH_*` 不在这里做——派发是**下一个状态的处理器**的活。放在这里的话
        一次迁移会同时改状态和拉 Job,崩溃在两者之间就分不清那个 Job 算哪个状态的。
        """
        workspace = GitWorkspace(self.root)
        for effect in decision.effects:
            if effect is Effect.CREATE_WORKSPACE:
                path = workspace.checkout_isolated(task.id)
                self.store.save(self.store.get(task.id).evolve(branch=branch_name(task.id)))
                self._note(task, f"隔离工作区: {path}")
            elif effect is Effect.CLEANUP_WORKSPACE:
                # 清工作区不清任务目录:事件流、上下文包、失败报告是审计材料,
                # 它们比任务本身活得久。
                workspace.cleanup(task.id)
                self._note(task, "已清理隔离工作区")
            elif effect is Effect.FREEZE:
                # 冻结就是什么都不做:分支与工作区原样留着等人接管。
                self._note(task, "现场已冻结,分支与工作区保留")
            elif effect is Effect.PROMOTE_VERIFICATION:
                self._promote_verification(task)
            elif effect is Effect.INJECT_FAILURE_REPORT:
                self._write_failure_report(task, outcome)
            elif effect is Effect.NOTIFY_APPROVER:
                self._notify_approver(task, outcome)
            elif effect is Effect.TRIGGER_EVOLUTION:
                self._trigger_evolution(task)

    def _promote_verification(self, task: Task) -> None:
        """交付后把最后一轮冷启动规格提升到项目控制面。"""
        try:
            slots = self.bus(task).by_stage(STAGE_UNIT_GATE)
            bootstrap_slot = _latest_passing_gate(slots)
            if bootstrap_slot is None or not load_bootstrap_specs(bootstrap_slot.path):
                return
            changes = promote_bootstrap_specs(
                self.root,
                bootstrap_slot.path,
                actor=ORCHESTRATOR,
                entrance="task-delivered",
            )
        except (OSError, ValueError, RuntimeError) as error:
            # 业务代码已经交付，附加配置失败不能把完成事实改写成失败；但缺口必须可检测。
            self._note(task, f"模块验证规格提升失败(不影响任务): {error}")
            return
        if changes:
            self._note(
                task,
                "模块验证规格已固化: "
                + ", ".join(change.path.name for change in changes),
            )

    def _notify_approver(self, task: Task, outcome: Outcome | None) -> None:
        """通知审批人。**发送失败只记事件,不阻塞迁移。**

        任务停在审批态本身已经是正确状态。一个配错的 webhook 地址不该让整条流水线卡死
        在一个与代码质量完全无关的地方。
        """
        payload = (outcome.payload if outcome else None) or {}
        notification = Notification(
            task_id=task.id,
            title=task.title,
            risk=str(payload.get("risk") or task.risk_level or "high"),
            matched_rules=tuple(str(item) for item in payload.get("matched_rules") or []),
            approvers=tuple(self.config.approval.approvers),
        )
        ok, detail = send(self.config.approval.notify.webhook, notification)
        sent = "已发出" if ok else "未发出"
        self.log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.NOTE,
            payload={"note": f"审批通知{sent}: {detail}"},
        )

    def _log_gate_result(self, task: Task, outcome: Outcome | None) -> None:
        """门禁的每次执行进事件流。质量把关的历史要可查。"""
        if outcome is None or not outcome.payload or "gates" not in outcome.payload:
            return
        payload = outcome.payload
        self.log.append(
            task.id,
            actor=GATE_ACTOR,
            kind=LogKind.GATE_RESULT,
            payload={
                "module": payload.get("module"),
                "passed": payload.get("passed"),
                "kind": payload.get("kind"),
                "gates": [
                    {
                        "id": gate.get("id"),
                        "passed": gate.get("passed"),
                        "outcome": gate.get("outcome"),
                        "duration_s": gate.get("duration_s"),
                    }
                    for gate in payload.get("gates") or []
                ],
                "regressions": len(payload.get("regressions") or []),
            },
        )

    def _trigger_evolution(self, task: Task) -> None:
        """任务结束:给知识库记账、同步语义图谱、把这个任务排进蒸馏队列。

        **三件事都不许把已经终结的任务搞挂,也不许互相卡住。** 任务已经走完了,它的
        成败不该由一条附加管道决定;某一步失败不该连累后面几步——尤其是排队蒸馏这一步,
        它是自进化机制唯一的触发点,`credit_hits` 抛异常不能把它也拖没了。

        **COMPLETED 与 ESCALATED 都要排队。** ESCALATED 任务是最富矿的蒸馏素材——人类
        最终怎么修的、与 AI 的尝试差在哪,这份材料只有在任务刚终结这一刻能收集完整。
        """
        succeeded = task.state is TaskState.COMPLETED
        credited: list[str] = []
        try:
            credited = credit_hits(
                self.root / paths.LESSONS, self.root, task.id, succeeded=succeeded
            )
        except OSError as error:
            self._note(task, f"知识命中记账失败(不影响任务): {error}")
        if credited:
            # 知识命中率的趋势要能按时间窗算,而 `LessonCard.hits` 只是个单调计数器、
            # 不带时间戳。这一条事件是唯一记着"这次命中发生在什么时候"的地方。
            self.log.append(
                task.id,
                actor=ORCHESTRATOR,
                kind=LogKind.NOTE,
                payload={"note": "knowledge_hit", "card_ids": credited},
            )
        self._settle_card_hits(task, succeeded)
        self._record_suspects(task, succeeded)
        if succeeded:
            # 图谱记的是"哪些模块真的被改过并合入了",ESCALATED 任务没有合入任何东西,
            # 同步它只会往图谱里写一条指向空改动的边。
            self._sync_graph(task)
        # **没有这一句,`drain_evolution` 永远是空操作。** 之前这里只做了记账,从没写过
        # 这个字段——蒸馏管道自任务终结起再也不会被触发,而且没有任何症状:任务照常
        # 完成,只是什么都学不到。`drain_evolution` 自己的调用方(`cli.py::_advance`)
        # 一直在按预期调用它,问题只出在这里没给它东西可消费。
        self._pending_distill = task.id

    def _settle_card_hits(self, task: Task, succeeded: bool) -> None:
        """把这一轮功能卡片的曝光结清。

        **只发一条事件,不碰卡片文件。** 计数更新是对知识树的写入,必须走架构员工的知识
        更新路径——开了"编排器顺手改一下"这个口子,下一个需求会走同一个口子,几轮之后
        编排器就在随便改知识了,而"知识树只由架构员工写入"是整棵树的校验体系依赖的前提。

        **失败的任务也要把曝光取出来清掉**,只是不记账:留着的话同一个任务重跑会被重复
        计数,而那让计数从"有效性"退化成"被重跑过几次"。
        """
        try:
            refs = take_exposures(self.root, task.id)
        except (OSError, LedgerUnreadable) as error:
            self._note(task, f"功能卡片命中结算失败(不影响任务): {error}")
            return
        if not refs or not succeeded:
            return
        # 记账本,不是知识树。落进卡片由 `agctl knowledge credits` 那条路做——那条约束
        # (编排器不写知识)有一条断言守着,见 `genome.hits` 的模块说明。
        try:
            record_credits(self.root, refs)
        except (OSError, LedgerUnreadable) as error:
            # 账本坏了要留痕,但**不能把一个已经跑完的任务判失败**——计数是增值,不是流程。
            self._note(task, f"功能卡片命中记账失败(不影响任务): {error}")
            return
        self.log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.NOTE,
            payload={"note": "card_hits", "cards": [ref.key for ref in refs]},
        )

    def _record_suspects(self, task: Task, succeeded: bool) -> None:
        """变更命中了卡的覆盖范围而知识没动?记进可疑账(PRD 41)。

        **只记账,不写知识树;只发信号,不拦任何东西。** 与 `_settle_card_hits` 同一条
        约束:账住 `tasks/`,消费由知识更新工序做("改卡"或"核对过,无需更新")。

        **只看成功的任务。** 失败/取消的改动没有合入,知识没有理由跟着一份不存在的
        变更走;这与 `_sync_graph` 对 ESCALATED 的判断是同一个理由。

        **变更清单走 `_changed_files`(自述优先,git 兜底),不走 `changed_paths`。**
        终态收尾跑在工作区清理之后,worktree 已经没了——而开发自述在产物目录里,
        活得比工作区久。
        """
        if not succeeded:
            return
        try:
            tree = load_tree(self.root)
            # 变更清单读不出来也走下面的统一留痕——静默按空清单算的话,"记录失败"和
            # "确实没命中"在账面上一模一样,而那正是路由那层教过的静默零命中的坑。
            changed = self._changed_files(task)
            found = stale_suspects(tree, changed, task.id, task.fix_rounds)
            record_suspects(self.root, found)
        except Exception as error:  # noqa: BLE001 - 与 _sync_graph 同一条原则
            # 信号是增值,不是流程——记不上要留痕,但不能把一个已经跑完的任务搞挂。
            self._note(task, f"可疑账记录失败(不影响任务): {error}")
            return
        if found:
            self.log.append(
                task.id,
                actor=ORCHESTRATOR,
                kind=LogKind.NOTE,
                payload={
                    "note": "card_suspects",
                    "cards": [item.card for item in found],
                },
            )

    def _record_evaporation(self, task_id: str, result: DistillResult) -> None:
        """fix 类任务的教训没人捡 → 蒸发信号入可疑账(PRD 41)。

        判据是**产出**:蒸馏之后既没有入库的卡片也没有暂缓的提案,这条修复的教训就
        没有任何形态留下来。信号与蒸馏是提醒与出口的关系——不强制蒸馏重跑,只让
        "没人捡"从看不见变成看得见。同为软信号:只记账,不拦任何东西。

        **"fix 类"当前只按 `fix_rounds > 0` 判。** PRD 41 D5 还提了"任务类型为缺陷
        修复",但 `Task` 没有类型字段——那半句等类型字段真的存在时再接,现在按标题
        猜类型只会把信号变成噪声。

        **投递保证与蒸馏一致(尽力而为)。** 这个钩子跟着 `drain_evolution` 走,而
        蒸馏队列是单槽的 `_pending_distill`:同一轮里两个任务先后终结、或 drain 前
        重启,教训与它的蒸发信号一起丢。信号是增值不是流程,这里不为它加第二套投递;
        蒸馏队列哪天升级成落盘队列,这条自动跟上。
        """
        if result.written or result.deferred:
            return
        try:
            task = self.store.get(task_id)
        except KeyError:
            return
        if task.state is not TaskState.COMPLETED or task.fix_rounds <= 0:
            return
        try:
            record_suspects(
                self.root,
                (
                    Suspect(
                        kind=SuspectKind.EVAPORATED,
                        task_id=task_id,
                        round=task.fix_rounds,
                    ),
                ),
            )
        except (OSError, LedgerUnreadable) as error:
            self._note(task, f"蒸发信号记录失败(不影响任务): {error}")

    def _sync_graph(self, task: Task) -> None:
        """把「这个任务改过哪些模块」写进语义图谱。

        这条边是告警反查的最后一跳:从模块找到最近改过它的任务,而那正是排障第一个要看的。

        **同步失败不影响任务**,与蒸馏管道同一条原则:任务已经完成了,一次图谱写入失败
        不该让它看起来失败。没配图谱时整段跳过——图谱是增值,不是流程的一部分。
        """
        graph = self.graph
        if graph is None:
            return
        try:
            project_map = self.project_map()
            if project_map is not None:
                sync_project_map(graph, project_map)
            sync_task(graph, task.id, self.plan_modules(task), merged_at=_now())
        except Exception as error:  # noqa: BLE001
            self._note(task, f"图谱同步失败(不影响任务): {error}")

    def _write_report(self, task: Task) -> None:
        """终态汇总。人接手时先读它——事件流是全量,报告是"发生了什么"的那一页。"""
        write_task_report(self.store.task_dir(task.id), task, self.log.events(task.id))

    def _seal(self, task: Task) -> None:
        """终态收尾:写报告,然后把证据固化成审计包。

        **两条终态路径共用这一个方法。** 表内迁移与不经过迁移表的那条升级人工路径,历史上
        各自调用 `_write_report`;固化再各写一遍的话,迟早有一条会漏——而漏掉的那条大概率
        是升级人工,恰恰是最需要证据的那批任务。

        **顺序不能反。** 报告先写,它才进得了包。

        **固化不等人来导。** 一次事故能不能被复盘,不该取决于有没有人在原始日志被清掉之前
        想起来敲那条命令。而人通常是在需要复盘的时候才想起来复盘,那时候往往已经晚了——
        尤其是已升级人工的任务:它是终态但不是已了结,机器停手了人还没来,这段时间差正是
        证据最容易丢的窗口。
        """
        self._write_report(task)
        sealed = seal_terminal_evidence(
            self.root,
            task.id,
            task.state.value,
            self.config,
            manifest_extra={"escalate_reason": task.escalate_reason},
        )
        if sealed.package is None:
            # 打包失败不许把一个已经跑完的任务变成失败——与蒸馏管道同一条原则。但必须留痕:
            # 「这个任务的证据没兜住」如果连它自己都不可见,清理那一步就会把它当成正常任务
            # 处理掉。
            #
            # **成败用不同的 note 键,不靠文案区分。** 靠substring 找的话,"已固化"里也含
            # "审计包",于是"查出所有没兜住证据的任务"这个查询会把成功的也捞进来。
            self._seal_note(task, "audit_seal_failed", error=sealed.error)
            return
        self._seal_note(task, "audit_sealed", path=str(sealed.package.path))

    def _seal_note(self, task: Task, note: str, **payload: str | None) -> None:
        self.log.append(
            task.id, actor=ORCHESTRATOR, kind=LogKind.NOTE, payload={"note": note, **payload}
        )

    def _note(self, task: Task, note: str) -> None:
        self.log.append(task.id, actor=ORCHESTRATOR, kind=LogKind.NOTE, payload={"note": note})

    def _write_failure_report(self, task: Task, outcome: Outcome | None) -> None:
        """写下这一轮的失败,供下一轮的上下文包注入。

        编号用**递增之后的** `fix_rounds`:下一轮开发的 `round_` 是它加一,读的时候
        按 `before_round` 过滤,两边对得上。
        """
        if outcome is None or outcome.payload is None:
            return
        write_failure_report(
            self.store.task_dir(task.id),
            round_=task.fix_rounds,
            title=f"第 {task.fix_rounds} 轮失败",
            payload=outcome.payload,
        )


def _is_recoverable(outcome: Outcome | None) -> bool:
    """这次失败改代码能不能解决。

    产物自己说了算(`kind`):缺工具、配置被篡改、环境起不来这类失败改代码解决不了,每一轮
    都会以完全相同的方式失败。没有 `kind` 的产物按"能解决"处理——那是开发、需求解析这类
    本来就只有语义失败的阶段。

    词汇住在 `core.verdict`,门禁与集成测试共用它。编排层只读那个字段的字符串值,不依赖
    任何一个产出方的包。
    """
    if outcome is None or not outcome.payload:
        return True
    return is_recoverable(outcome.payload.get("kind"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


class WorkspaceRemoteMissing(RuntimeError):
    """顶层 Workspace 没有可供 Forge 定位仓库的 origin。"""


class SubmoduleRemoteMissing(RuntimeError):
    """子模块 checkout 没有可供 Forge 定位仓库的 origin。"""


def _origin_url(repository: Path, *, workspace: bool) -> str:
    """读取并校验 origin,把 git 的底层报错翻成可直接操作的配置提示。"""
    from agentgenome.space.gitcmd import git

    result = git(repository, "remote", "get-url", "origin", check=False)
    url = result.stdout.strip()
    if result.returncode == 0 and url:
        return url
    if workspace:
        raise WorkspaceRemoteMissing(
            "顶层 Workspace 缺少 origin；请先创建顶层远端仓库并执行 "
            "git remote add origin <URL> && git push -u origin main，然后继续执行"
        )
    raise SubmoduleRemoteMissing(
        f"子模块 {repository} 缺少 origin；请先配置远端仓库后再继续执行"
    )


def submodule_forge_repositories(root: Path, git_host: GitHost) -> dict[str, Path]:
    """返回 Forge 操作各子仓时使用的仓库定位符。

    生产 Forge (`gh`/`glab`) 在本地 checkout 中运行,由 checkout 的 origin 推断平台仓库；
    把 HTTPS URL 包成 ``Path`` 会把 ``https://`` 破坏成 ``https:/``。LocalForge 则直接
    操作测试/离线场景里的 bare repository,所以仍读取 `.gitmodules` 的文件路径。
    """
    from agentgenome.space.gitcmd import git_out

    repositories: dict[str, Path] = {}
    for module_path in sorted(submodule_paths(root)):
        if git_host == "local":
            raw = git_out(
                root,
                "config",
                "-f",
                ".gitmodules",
                f"submodule.{module_path}.url",
            )
            repositories[module_path] = Path(raw.strip())
            continue
        checkout = root / module_path
        _origin_url(checkout, workspace=False)
        repositories[module_path] = checkout
    return repositories


def workspace_forge_repository(root: Path, git_host: GitHost) -> Path:
    """返回 Forge 操作顶层仓库时使用的定位符,并在开 PR 前验证 origin。"""
    remote = _origin_url(root, workspace=True)
    return Path(remote) if git_host == "local" else root


def _merge_failure_payload(task: Task, error: Exception) -> dict[str, Any]:
    """合并失败的产物。冲突文件与 rebase 指令要进下一轮上下文——只说"合并失败了"的话,
    下一轮的员工不知道该 rebase 哪个分支。"""
    return {
        "task_id": task.id,
        "producer": ORCHESTRATOR,
        "created_at": _now(),
        "passed": False,
        "kind": VerdictKind.SEMANTIC.value,
        "failures": [
            {
                "message": f"双层合并失败: {error}",
                "evidence": {
                    "hint": (
                        "在隔离工作区里 `git fetch origin && git rebase origin/main`,"
                        "解决冲突后重新跑本轮开发。"
                    )
                },
            }
        ],
    }


__all__ = ["JobContext", "Orchestrator", "load_plan_modules", "read_plan"]


def _scope_requests(payload: dict[str, Any] | None) -> list[tuple[str, str]]:
    """从开发产物里取扩权申请。形状不对的条目直接丢——**扩权是加权限,宁可漏批不可错批**。"""
    if not payload:
        return []
    raw = payload.get("scope_request")
    if not isinstance(raw, list):
        return []
    found = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        module, reason = item.get("module"), item.get("reason")
        if isinstance(module, str) and module and isinstance(reason, str) and reason:
            found.append((module, reason))
    return found
