"""状态处理器:每个状态该干什么活。

## 幂等约定(整套崩溃恢复的前提)

**每个处理器第一件事是检查本状态的预期产物是否已存在且有效;存在则直接产生对应事件推进
状态,不重复执行。** 没有这条约定,崩溃恢复等于把已经花掉的 token 再花一遍——而崩溃恢复
正是这套设计存在的理由。

约定写在这里,但**每个处理器的测试里各验一遍**:只写在文档里的话,某个处理器忘了做也不会
有任何症状,直到某次崩溃恢复把钱再烧一遍。

## 为什么只有一个处理器类

三个状态(需求解析、开发、单元门禁)干的是同一件事的三个实例:分配产物目录 → 以某个员工的
身份派发某个 Procedure → 看产物是否合契约 → 产生成功或失败事件。写成三个类的话,三份几乎一样的
代码会各自演进,然后在某个细节上悄悄分叉。差异全部收进构造参数。

## 不幂等的副作用清单

不是所有副作用都能靠"查产物"变幂等。已知需要额外幂等键的:

- **创建 PR**(PRD 08):平台上重复开 PR 会得到两个。必须在任务上记录 PR 引用,重放时先查。
- **发通知**(PRD 09):重放会重复打扰人。需要按 `(task_id, 状态, 轮次)` 去重。
- **合并**(PRD 08):`Forge.merge_pr` 自身幂等(已合并的再合返回同一结果),这条不用额外处理。
- **建 worktree / 建分支**:`checkout_isolated` 已经幂等(存在即返回)。
- **清理 worktree**:删一个不存在的目录不报错,天然幂等。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agentgenome.agents.contract import check_result_contract
from agentgenome.agents.human import HUMAN
from agentgenome.agents.runtime import FailureKind
from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.task import Task, TaskMode
from agentgenome.core.topology import (
    ATTEMPT,
    BEST_OF_N,
    DAG,
    PROBE,
    REFINE,
    SINGLE,
    STOPPED_REJECTED,
    Executor,
    NodeKind,
    NodeOutcome,
    TemplateChoice,
    TopologyNode,
    TopologyRun,
    TopologyTemplate,
    run_topology,
)
from agentgenome.core.transitions import PlanFailureCause
from agentgenome.genome.roster import DECISION_EMPLOYEE

if TYPE_CHECKING:
    from agentgenome.jobs.artifacts import ArtifactBus, Slot
    from agentgenome.jobs.orchestrator import JobContext

#: 各状态的产物 stage 名。它同时是产物目录名的一部分,所以只能用小写与连字符。
STAGE_PLAN = "plan"
STAGE_DEVELOP = "develop"
STAGE_UNIT_GATE = "unit-gate"
STAGE_ITEST = "itest"

#: 集成测试脚本写的那份报告。Agent 覆盖 result.json 时它是兜底来源。
ITEST_REPORT = "itest-report.json"


@dataclass(frozen=True)
class Outcome:
    """处理器跑完之后的结论。

    带上 `valid` 而不是让处理器自己去改任务:处理器只报告发生了什么,状态归状态机管。
    """

    event: TaskEvent
    #: 产物是否合契约。喂给迁移表的守卫。
    valid: bool = True
    #: 从产物里读出来的东西,供副作用使用(比如计划里的涉及模块)。
    payload: dict[str, Any] | None = None
    #: 这一步实际上没跑,是认出了已有产物直接推进的。
    replayed: bool = False
    #: 这一步**挂起了**:活派给了人,结果要等他回来才有。
    #:
    #: **它不是失败,也不是成功**——所以带着它的结论不进迁移表:状态不动,任务健康,
    #: 人一答复就往下走。这正是"待确认"的表示,而把它压成失败会让状态机重试或升级。
    suspended: bool = False
    #: 挂起在等哪张待办。
    todo_id: str = ""
    #: **这一轮不算,再来一轮。** 两个来源:人确认时拒了,或者图里有节点失败了。
    #:
    #: 它不是"失败"也不是"成功"——产出是好是差由下一轮回答,这一条只说"先别往下走"。
    #: 两者共用一条路是因为它们要的后续完全一样:留下现场、修复轮次 +1、回到开发态重来。
    redo: bool = False
    #: 下一轮必须重跑的节点(失败的 + 因它被冻结的)。其余的重放上一轮的产物——
    #: 一次跑通的东西不该因为别人挂了而重花一遍钱。
    redo_nodes: tuple[str, ...] = ()
    #: 为什么要重来:人的意见,或者哪些节点失败了。**全文回注下一轮的上下文**——只记一条
    #: 事件的话,下一轮的员工看不到它,于是原样再做一遍。
    note: str = ""
    #: 不能通过重跑解决的失败（例如越权）。编排器收到后立即升级，不消耗修复轮次。
    fatal_reason: str = ""
    #: Job 边界的机器可判定失败类型。计划阶段用它区分需求语义与运行时交付故障。
    failure_kind: FailureKind = FailureKind.NONE

    @property
    def plan_failure_cause(self) -> PlanFailureCause:
        """把 Job 失败收敛成计划阶段面向人工动作的原因。"""
        match self.failure_kind:
            case FailureKind.NONE:
                return PlanFailureCause.NONE
            case FailureKind.PROTOCOL:
                return PlanFailureCause.DELIVERY
            case FailureKind.CONTRACT:
                return PlanFailureCause.CONTRACT
            case FailureKind.PROCESS | FailureKind.TIMEOUT:
                return PlanFailureCause.RUNTIME
            case FailureKind.BUDGET | FailureKind.TASK_BUDGET:
                return PlanFailureCause.LIMIT
            case FailureKind.SCOPE:
                return PlanFailureCause.SCOPE
            case FailureKind.UNKNOWN:
                return PlanFailureCause.UNKNOWN
        raise AssertionError(f"未处理的计划失败类型: {self.failure_kind}")


@runtime_checkable
class Handler(Protocol):
    """一个状态该干什么活。

    **不是所有状态的活都是"派一个 Procedure"。** 提交前的那道关卡完全确定性、零 token、
    不经过任何 Agent——把它硬塞进 `ProcedureHandler` 的话,要么造一个假 Procedure,要么在派发
    路径上开一堆 if。协议把编排器需要的四样东西说清楚,各干各的。
    """

    #: 只读:处理器是不可变的,协议不该要求它可写(frozen dataclass 满足不了那个要求)。
    @property
    def stage(self) -> str: ...

    def attempt(self, task: Task) -> int: ...

    def existing_outcome(self, bus: ArtifactBus, attempt: int) -> Outcome | None: ...

    async def run(self, context: JobContext) -> Outcome: ...


@dataclass(frozen=True)
class ProcedureHandler:
    """一个状态的处理器:派发某个 Procedure,按产物产生事件。"""

    state: TaskState
    employee_id: str
    procedure_id: str
    stage: str
    on_success: TaskEvent
    on_failure: TaskEvent

    def attempt(self, task: Task) -> int:
        """这是本状态的第几次执行,从 1 起。

        开发与门禁按修复轮次数,需求解析按解析重试数——两个计数器管的是两条不同的
        上限,混用会让其中一条静默失效。
        """
        counter = task.plan_retries if self.state is TaskState.CREATED else task.fix_rounds
        return counter + 1

    def existing_outcome(self, bus: ArtifactBus, attempt: int) -> Outcome | None:
        """**这一轮**的预期产物是否已经在了。

        **崩溃恢复的全部前提。** 在了就直接给事件,不重复执行——重复执行意味着把已经
        花掉的 token 再花一遍。

        **按轮次找,不是按阶段找。** 只问"这个阶段产出过东西吗"的话,修复循环第二轮
        回到开发态时会认出第一轮的产物直接跳过——于是第二轮压根不跑,任务在两个状态
        之间空转到轮次耗尽。这里靠一条不变式定位:某个 `(stage, node, variant)` 的第 k 个
        产物目录就是**该节点**的第 k 轮。

        **不变式从"stage 的第 k 个"改写成"节点的第 k 个"**:一个 stage 底下可以住一张图,
        按 stage 数的话,一个节点的重试会被算到另一个节点头上。`single` 下节点维度取空,
        两者逐字节等价——而等价这件事必须当场证明,不能靠推理。
        """
        slots = bus.by_node(self.stage)
        if len(slots) < attempt:
            return None
        slot = slots[attempt - 1]
        payload = self.payload_of(slot)
        if payload is None:
            # 有目录但产物不合契约:那次执行没跑完,该重来。
            return None
        manifest = slot.manifest() or {}
        if manifest.get("result_ok") is False and not manifest.get("suspended", False):
            return Outcome(
                event=self.on_failure,
                valid=False,
                payload=payload,
                replayed=True,
                redo=True,
                note=str(manifest.get("failure_detail") or "上一轮 Job 执行失败"),
            )
        return Outcome(
            event=self.on_success if payload.get("passed", False) else self.on_failure,
            valid=True,
            payload=payload,
            replayed=True,
        )

    def template(self, context: JobContext) -> TemplateChoice:
        """这个状态这次要跑哪张图。

        缺省是 `single`——它塌缩回今天那一次派发。模板由配置与任务级覆盖选出,处理器自己
        不做选择:选哪张图是策略,派什么活才是处理器的事。
        """
        return context.template(
            state=self.state, employee=self.employee_id, procedure=self.procedure_id
        )

    async def run(self, context: JobContext) -> Outcome:
        """真的跑一次:解析模板 → 执行拓扑 → 聚合 → 抛既有状态机事件。

        **聚合之后抛出的仍然是既有事件,外环状态机零改动。** 这一层的价值全部来自"它什么
        都没改变":一旦泛化层顺手改了点什么,后面每一次拓扑扩展都要连带怀疑它。
        """
        choice = self.template(context)
        template = choice.template
        context.record_topology(choice, stage=self.stage)

        # 节点要看上游产出了什么。**按 `produces` 记账,按 `needs` 取用**——节点声明的
        # 产物名才是"它该拿到什么"的定义;按节点类别猜的话,图里的下游会拿到"最后跑完的
        # 那个节点"的产出,而并行下"最后跑完的"是不确定的。
        handed: dict[str, dict[str, Any]] = {}
        attempt = self.attempt(context.task)
        # 上一轮谁必须重跑。**其余节点重放**——"重试只重跑失败节点及其下游"就是这一行。
        redo_nodes = context.nodes_to_redo()
        #: 这一次执行里,每个节点被叫到第几次。环里同一个节点会被叫多次。
        calls: dict[str, int] = {}

        async def run_node(node: TopologyNode) -> NodeOutcome:
            node_key, variant_key = _slot_dimensions(template, node)
            calls[node.id] = calls.get(node.id, 0) + 1
            landed = self._already_done(
                context, node, node_key, variant_key, attempt, calls, redo_nodes
            )
            if landed is not None:
                return landed
            if template.id == DAG and attempt > 1:
                context.reset_node_worktree(node.id)
            slot = context.bus.allocate(self.stage, node_key, variant_key)
            result = await context.dispatch(
                procedure_id=node.procedure,
                employee_id=node.employee,
                slot=slot,
                state=self.state,
                inputs=_node_inputs(context, node, handed),
                subject=_replay_subject(template, node, slot),
                assignee=node.assignee or "",
                runtime=HUMAN if node.executor is Executor.MANUAL else "",
                worktree=_worktree_of(template, node),
                write_scope=node.write_scope if template.id == DAG else None,
            )
            payload = self.payload_of(slot)
            self._write_manifest(
                slot,
                result_ok=result.ok,
                payload=payload,
                node=node,
                attempt=attempt,
                failure_kind=result.failure_kind.value,
                failure_detail=result.failure_detail or "",
                suspended=result.suspended,
            )
            if payload is not None:
                for produced in node.produces or (node.kind.value,):
                    handed[produced] = payload
                handed[node.id] = payload
            return NodeOutcome(
                node=node,
                ok=result.ok,
                payload=payload,
                tokens_used=result.tokens_used,
                tokens_available=result.tokens_available,
                slot=slot.path.name,
                suspended=result.suspended,
                todo_id=result.suspended_on,
                failure_kind=result.failure_kind.value,
                failure_detail=result.failure_detail or "",
            )

        run = await run_topology(template, run_node, context.limits())
        if run.stopped_because == STOPPED_REJECTED:
            context.record_run(template, run, stage=self.stage)
            verdict = run.outcomes[-1].payload or {}
            return Outcome(
                event=self.on_failure,
                valid=False,
                redo=True,
                note=str(verdict.get("note", "")),
            )
        if run.failed and template.id == BEST_OF_N:
            # 零过闸也要留对比:**N 路都失败的对比比单路失败报告信息量大得多**,而它是
            # 这次多花的钱唯一还能产出的东西。留完才清理落选工作树。
            context.record_contrast(run)
        if run.failed:
            # **图里有节点没成:这一轮不算。** 让它照常往门禁走的话,门禁会在一份不完整的
            # 改动上跑一遍并挂掉——多花一整轮门禁往返才回到同一个结论。
            context.record_run(template, run, stage=self.stage)
            fatal_checker = next(
                (
                    item
                    for item in run.outcomes
                    if item.node.id in run.failed
                    and item.node.kind is NodeKind.CHECKER
                    and item.failure_kind
                    in {
                        FailureKind.PROCESS.value,
                        FailureKind.SCOPE.value,
                        FailureKind.TASK_BUDGET.value,
                    }
                ),
                None,
            )
            if fatal_checker is not None:
                return Outcome(
                    event=self.on_failure,
                    valid=False,
                    fatal_reason=_fatal_checker_reason(fatal_checker),
                )
            failed_work = next(
                (
                    item
                    for item in run.outcomes
                    if item.node.id in run.failed and item.node.kind is NodeKind.WORK
                ),
                None,
            )
            if self.state is TaskState.CREATED and failed_work is not None:
                # 计划 Job 自己没能交付产物，不是代码“这一轮没做好”。后者走 fix_rounds，
                # 前者必须回到计划状态机，由 plan_retries 与专用升级原因裁决。
                return Outcome(
                    event=self.on_failure,
                    valid=False,
                    note=failed_work.failure_detail,
                    failure_kind=_failure_kind(failed_work.failure_kind),
                )
            return Outcome(
                event=self.on_failure,
                valid=False,
                redo=True,
                note=_failed_nodes_note(run),
                redo_nodes=tuple(sorted({*run.failed, *run.frozen})),
            )
        if template.id == BEST_OF_N and run.winner:
            # **只合胜者那一路。** 落选的留在自己的分支上等蒸馏取材,取完才清理——
            # 它们是"为什么不这么做"的一手证据,而那是经验卡片最稀缺的素材。
            conflict = context.merge_nodes([f"{ATTEMPT}-{run.winner}"])
            if conflict:
                context.record_run(template, run, stage=self.stage)
                return Outcome(event=self.on_failure, valid=False, redo=True, note=conflict)
            context.record_contrast(run)
        if template.id == DAG:
            # 全绿了才合:合到一半发现下一个冲突的话,任务分支上会留下半张图的改动,
            # 而"这次到底合进去了什么"要靠人去 git 里翻。
            conflict = context.merge_nodes([item.node.id for item in run.outcomes])
            if conflict:
                context.record_run(template, run, stage=self.stage)
                return Outcome(event=self.on_failure, valid=False, redo=True, note=conflict)
        pending = next((item for item in run.outcomes if item.suspended), None)
        if pending is not None:
            # **挂起短路在聚合之前。** 聚合会去读产物判成败,而挂起时产物压根还不存在——
            # 让它走完的话,一次"人还没开始干"会被判成一次失败的开发。
            return Outcome(event=self.on_failure, valid=False, suspended=True,
                           todo_id=pending.todo_id)
        context.record_run(template, run, stage=self.stage)
        return self.aggregate(run)

    def aggregate(self, run: TopologyRun) -> Outcome:
        """把节点结论聚合成一个状态机事件。

        **判定留在处理器,不下沉到执行器。** 执行器懂的是图,不懂业务语义——让它去读
        `passed` 字段的话,每加一种模板就要复制一遍这段判定,然后它们各自演进。

        取的是最后一个**工作**节点:环的最后一步常常是一次批判,而批判压根不产出资产——
        让它来回答"这个状态干成了没有",答案永远是没有。
        """
        outcome = run.last_work()
        if outcome is None or outcome.payload is None:
            # 产物压根没写出来或不合契约:守卫会挡住迁移。
            return Outcome(
                event=self.on_failure,
                valid=False,
                note=outcome.failure_detail if outcome is not None else "",
                failure_kind=(
                    _failure_kind(outcome.failure_kind)
                    if outcome is not None
                    else FailureKind.NONE
                ),
            )
        return Outcome(
            event=self.on_success if self.verdict(outcome.payload, outcome.ok) else self.on_failure,
            valid=True,
            payload=outcome.payload,
        )

    def verdict(self, payload: dict[str, Any], result_ok: bool) -> bool:
        """产物说通过、且 Job 本身也成功了,才算通过。

        两个都要看:一个写了 `passed: true` 却在写完之后越权被回滚的 Job,产物还在那儿,
        但它做的事已经不算数了。
        """
        return bool(payload.get("passed", False)) and result_ok

    def payload_of(self, slot: Slot) -> dict[str, Any] | None:
        """这次执行留下的结论。读不出来就是 `None`。"""
        return read_result(slot)

    def _already_done(
        self,
        context: JobContext,
        node: TopologyNode,
        node_key: str,
        variant_key: str,
        attempt: int,
        calls: dict[str, int],
        redo_nodes: set[str] | None = None,
    ) -> NodeOutcome | None:
        """**这一轮里**这个节点已经产出过了吗?产出过就重放,不重跑。

        节点级的幂等,`existing_outcome` 那条阶段级幂等的细化版。两个地方需要它:

        - **人交完活之后的那一步**:整张图会被重新走一遍,而机器那几步的产物已经在了。
          没有这一条的话,重跑会给人再投一张待办,人的答复因此永远接不回来——一个死循环。
        - 环跑到一半崩溃后的恢复:接着上次那一步往下,而不是从头把钱再花一遍。

        **按"这一轮"筛**(产物清单里记着它属于任务的第几轮):不筛的话,门禁挂了之后回到
        开发态的新一轮会把上一轮的产物当成自己的,于是那一轮压根不跑。
        """
        slots = context.bus.by_node(self.stage, node_key, variant_key)
        landed = [
            slot for slot in slots if (slot.manifest() or {}).get("task_attempt") == attempt
        ]
        if not landed and redo_nodes and node.id not in redo_nodes and node_key:
            # **上一轮就成了的节点,这一轮直接重放。** 它不在重跑名单里,说明它的产出仍然
            # 成立;重跑一遍是把已经花过的钱再花一次,而这正是"增量修复"要省下的那部分。
            landed = [slot for slot in slots if self.payload_of(slot) is not None][-1:]
        nth = calls[node.id]
        if len(landed) < nth:
            return None
        slot = landed[nth - 1]
        manifest = slot.manifest() or {}
        payload = self.payload_of(slot)
        result_ok = bool(manifest.get("result_ok", True)) or (
            bool(manifest.get("suspended", False)) and payload is not None
        )
        if payload is None and result_ok:
            # 有目录但产物不合契约:那次执行没跑完(或者人还没交),该重来。
            return None
        return NodeOutcome(
            node=node,
            ok=result_ok,
            payload=payload,
            slot=slot.path.name,
            failure_kind=str(manifest.get("failure_kind", "")),
            failure_detail=str(manifest.get("failure_detail", "")),
        )

    def _write_manifest(
        self,
        slot: Slot,
        result_ok: bool,
        payload: dict[str, Any] | None,
        node: TopologyNode,
        attempt: int = 1,
        failure_kind: str = "",
        failure_detail: str = "",
        suspended: bool = False,
    ) -> None:
        outputs = ["result.json"] if payload is not None else []
        slot.write_manifest(
            producer=node.employee,
            inputs=[],
            outputs=outputs,
            summary=f"{node.procedure} {'成功' if result_ok else '失败'}",
            task_attempt=attempt,
            result_ok=result_ok,
            failure_kind=failure_kind,
            failure_detail=failure_detail,
            suspended=suspended,
        )


def _failed_nodes_note(run: TopologyRun) -> str:
    """下一轮要看的现场:谁失败了、谁因此没跑。

    **被冻结的要单独说**:把它们混进失败里,下一轮会以为有五个地方坏了,而实际上只坏了一个。
    """
    lines = [f"图里这些节点失败了: {', '.join(run.failed)}"]
    for outcome in run.outcomes:
        if outcome.node.id not in run.failed:
            continue
        kind = outcome.failure_kind or FailureKind.UNKNOWN.value
        detail = outcome.failure_detail or "没有提供失败详情"
        lines.append(f"{outcome.node.id} [{kind}]: {detail}")
    if run.frozen:
        lines.append(f"因此没跑的下游: {', '.join(run.frozen)}")
    return "。".join(lines)


def _fatal_checker_reason(outcome: NodeOutcome) -> str:
    """按失败类别给出下一步，不能把越权、预算与运行时故障混成一句话。"""
    match _failure_kind(outcome.failure_kind):
        case FailureKind.SCOPE:
            summary = f"{outcome.node.employee} 的改动越出授权范围"
        case FailureKind.TASK_BUDGET:
            summary = "任务预算不足以执行检查节点"
        case FailureKind.PROCESS:
            summary = f"{outcome.node.employee} 的运行时进程失败"
        case kind:
            summary = f"{outcome.node.employee} 遇到不可重试故障({kind.value})"
    return f"{summary}: {outcome.failure_detail}" if outcome.failure_detail else summary


def _worktree_of(template: TopologyTemplate, node: TopologyNode) -> str:
    """这个节点在哪棵工作树里干活。空串 = 任务主工作树。

    **只有真会并行的图才分工作树。** 环与 assisted 是顺序跑的,它们共用任务工作树——那正是
    "精化的是同一份代码"这件事在空间上的表达;给它们各分一棵的话,精化那一轮会看不到自己
    上一轮写的东西。

    N 路变体按**变体**分,不按节点分:同一路的"写代码"与"过门禁"必须看到同一棵树,否则
    门禁跑的是一份空代码,而它会绿。
    """
    if template.id == BEST_OF_N and node.variants:
        return f"{ATTEMPT}-{node.variants[0].key}"
    return node.id if template.id == DAG else ""


def _replay_subject(template: TopologyTemplate, node: TopologyNode, slot: Slot) -> str:
    """这次派发在回放缝上的第四维。

    **环与图逼出来的那一维。** 一轮任务里同 employee 同 procedure 会被派多次:环里的第一次
    与第二次批判、图里的并行节点。回放键只按 `(employee, procedure, round)` 找的话,它们
    全撞在一个键上——回放给每一次返回同一份产出,于是"批判第二轮通过了"这种事永远录不出来,
    **而测试照样是绿的**。

    `single` 下返回空串:键与改造前逐字一致,既有录制一份都不用重录。
    """
    if template.id == SINGLE:
        return ""
    if node.variants:
        # **变体也是一维。** 少了它,N 路尝试全撞在一个键上:回放给每一路返回同一份产出,
        # 于是"三路各写各的"这件事根本录不出来,而测试照样是绿的。
        return f"{node.id}.{node.variants[0].key}.{slot.attempt}"
    return f"{node.id}.{slot.attempt}"


def _node_inputs(
    context: JobContext, node: TopologyNode, handed: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """这个节点该拿到什么额外材料。`None` 表示标准入参,与改造前一模一样。

    **上游产物按 `needs` 取。** 节点自己声明了它消费什么,那就是"它该拿到什么"的定义
    (ADR-0007:边由产物背书)——按别的方式猜,图里的下游会拿到一份不属于它的上游产出。

    **批判要看改了什么,精化要看意见全文。** 评审员工手里只有只读工具,不给它
    `changed_files` 的话它只能靠猜哪些文件属于这一轮;而回注给精化的必须是 findings 原文
    ——摘要会把"第 37 行的返回值没判空"压成"有几处健壮性问题",后者改不了任何东西。
    """
    upstream = {name: handed[name] for name in node.needs if name in handed}
    work = handed.get(NodeKind.WORK.value)
    verdict = handed.get(NodeKind.CHECKER.value)
    if node.kind is NodeKind.CHECKER and work is not None:
        return {
            **context.task_inputs(),
            "changed_files": work.get("changed_files", []),
            **({"upstream": upstream} if upstream else {}),
        }
    if node.id == PROBE:
        # **红队的输入是一份清单,不是一句"去攻击"。** 不变量卡片本来就是"哪条线碰不得"
        # 的清单,攻击就是逐条验证"真的碰不得吗";不给清单的话它只能靠灵感,而灵感的产出
        # 既无法复核也无法沉淀。
        return {**context.task_inputs(), **context.probe_inputs(), "upstream": upstream}
    if node.id == REFINE and verdict is not None:
        return {
            **context.task_inputs(),
            "critique": {
                "findings": verdict.get("findings", []),
                "notes": verdict.get("notes", ""),
            },
        }
    if upstream:
        return {**context.task_inputs(), "upstream": upstream}
    return None


def _slot_dimensions(template: TopologyTemplate, node: TopologyNode) -> tuple[str, str]:
    """这个节点的产物该记在哪一维上。

    **`single` 塌缩掉节点维度。** 它只有一个节点,写进目录名的话产物布局就变了,而
    "改造前后逐字节等价"是这一层的硬验收——等价性一破,就再也说不清是泛化层动了什么。
    """
    if template.id == SINGLE:
        return "", ""
    return node.id, node.variants[0].key if node.variants else ""


def read_result(slot: Slot) -> dict[str, Any] | None:
    check = check_result_contract(slot.path, {})
    if not check.ok or check.path is None:
        return None
    payload: dict[str, Any] = json.loads(check.path.read_text(encoding="utf-8"))
    return payload


@dataclass(frozen=True)
class ItestHandler(ProcedureHandler):
    """集成测试的处理器。差别只有一条:**诊断挂了不影响测试结论。**

    `itest-run` 是 hybrid——脚本跑环境与用例并写出完整的 `result.json`,Agent 只往里补
    `suspect_files` 与 `suggestion`。Agent 那一段超时、契约不符或压根没被拉起来时,
    `result.ok` 是 False,但盘上那份测试结果仍然是完整且正确的。

    照通用规则办的话,一次诊断失败会把一轮跑通的集成测试判成失败,任务白白退回开发态改
    一个根本不存在的问题——**测试结果是事实,诊断是增值**。
    """

    def verdict(self, payload: dict[str, Any], result_ok: bool) -> bool:
        return bool(payload.get("passed", False))

    def payload_of(self, slot: Slot) -> dict[str, Any] | None:
        """`result.json` 读不出来时退回脚本写的那份报告。

        Agent 是**在脚本写完 result.json 之后**才跑的,它会把自己的产出覆盖上去。一次写
        坏了的诊断于是能把一整轮跑通的集成测试结果抹掉——那正是"诊断挂了不该丢掉测试
        结果"要防的事,而只靠 `verdict` 防不住:那时 payload 已经是 None 了。

        `itest-report.json` 是脚本写的,Agent 的授权范围里没有它。
        """
        found = read_result(slot)
        if found is not None:
            return found
        report = slot.path / ITEST_REPORT
        if not report.is_file():
            return None
        payload: dict[str, Any] = json.loads(report.read_text(encoding="utf-8"))
        # 报告本身也可能是半截的(脚本被杀在写文件中间)。缺公共字段就当没有。
        return payload if {"task_id", "passed", "kind"} <= set(payload) else None


def _failure_kind(value: str) -> FailureKind:
    """在拓扑字符串边界收紧成 Job 失败枚举，未知值保留为 UNKNOWN。"""
    if not value:
        return FailureKind.NONE
    try:
        return FailureKind(value)
    except ValueError:
        return FailureKind.UNKNOWN


#: 各状态的处理器。表里没有的状态没有处理器——它们要么是终态,要么等着外部事件
#: (审批、合并结果)推动,那些在 PRD 08。
HANDLERS: dict[TaskState, Handler] = {
    TaskState.CREATED: ProcedureHandler(
        state=TaskState.CREATED,
        # **决策员工,不是架构员工。** "这一个任务怎么打"是任务视角的决定,归因到治理
        # 项目认知的那个角色头上,复盘时"该质询谁"就没有答案(PRD 40)。
        employee_id=DECISION_EMPLOYEE,
        procedure_id="requirement-analysis",
        stage=STAGE_PLAN,
        on_success=TaskEvent.PLAN_DONE,
        on_failure=TaskEvent.PLAN_FAILED,
    ),
    # 两个事件**故意相同**:开发员工的自评不是仲裁。它说自己没做完,产物照样交给
    # 门禁去判——门禁挂了才回开发态。让自评直接决定流向的话,一个过于谨慎的员工
    # 会把自己卡在开发态里出不来。
    TaskState.DEVELOPING: ProcedureHandler(
        state=TaskState.DEVELOPING,
        employee_id="dev-employee",
        procedure_id="code-develop",
        stage=STAGE_DEVELOP,
        on_success=TaskEvent.DEV_DONE,
        on_failure=TaskEvent.DEV_DONE,
    ),
    TaskState.UNIT_TESTING: ProcedureHandler(
        state=TaskState.UNIT_TESTING,
        employee_id="dev-employee",
        procedure_id="unit-gate",
        stage=STAGE_UNIT_GATE,
        on_success=TaskEvent.GATE_PASS,
        on_failure=TaskEvent.GATE_FAIL,
    ),
    TaskState.INTEGRATION_TESTING: ItestHandler(
        state=TaskState.INTEGRATION_TESTING,
        employee_id="itest-employee",
        procedure_id="itest-run",
        stage=STAGE_ITEST,
        on_success=TaskEvent.ITEST_PASS,
        on_failure=TaskEvent.ITEST_FAIL,
    ),
}


def can_advance(task: Task) -> bool:
    """`Orchestrator.advance` 对这个任务现在调用会不会真的做事。

    **服务端(REST 校验、`TaskSummary.can_run`)与 `advance` 本身共用这一条**——各判一遍
    的话,这里加一条新规则时容易漏改一处:接口放行了、`advance` 却照旧 no-op,点了按钮
    什么都没发生;或者反过来,接口拒了一个 `advance` 其实认的状态。
    """
    if task.is_terminal or task.state not in HANDLERS:
        return False
    # 挂着待办的任务在等人。推它只会再投一张一模一样的待办,而人看到两张都"合法"。
    # **它是待确认,不是已升级人工**:任务健康,人一答复机器自己就往下走。
    if task.pending_todo_id:
        return False
    # 交互式任务的开发态由**人**驱动:结对会话在跑,机器不该同时派一个自主 Job
    # 去改同一个工作区——见 `Orchestrator.advance` 里同一条判断。
    return not (task.state is TaskState.DEVELOPING and task.mode is TaskMode.INTERACTIVE)


def register(state: TaskState, handler: Handler) -> None:
    """挂一个处理器。

    提交阶段的两个处理器住在 `jobs.commit_handlers` 里,由那边注册进来——直接写进上面
    那张表的话,这个模块要 import 提交流水线,而提交流水线又要 import 这里的 `Outcome`,
    绕成一个环。
    """
    HANDLERS[state] = handler


__all__ = [
    "HANDLERS",
    "Handler",
    "register",
    "STAGE_ITEST",
    "ItestHandler",
    "STAGE_DEVELOP",
    "STAGE_PLAN",
    "STAGE_UNIT_GATE",
    "Outcome",
    "ProcedureHandler",
    "read_result",
]
