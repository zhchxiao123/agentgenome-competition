"""REST 控制面。

## 一套服务层,两个薄壳

CLI 与 REST 都是壳,业务逻辑一律在 `core` / `jobs` / `commit` / `approval` 里。

**PRD 的首选方案是"CLI 通过 REST 调服务端",这里走的是它给出的退路。** 理由与代价写在
`.scratch/09-control-plane-api/issues/01` 里,一句话:让一个终端工具依赖"有个服务端在跑"是
真实的可用性倒退,而防分叉真正靠的是那组一致性测试,不是架构选择。

## 请求与响应模型全部显式

不用 `dict` 兜底。`dict` 会让 OpenAPI 里的类型退化成 `object`,生成的 TypeScript 客户端就此
失去全部价值——而那是选 FastAPI 的唯一理由。

## Workspace 从哪来

每个进程服务一个 Workspace,由启动参数决定。多 Workspace 是 PRD 18 的事;现在把它做成
应用状态里的一个路径,换成多租户时换的是这一处的解析,不是每个端点。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from agentgenome import __version__ as package_version
from agentgenome import paths
from agentgenome.agents.agentteams.provision import (
    ProvisionError,
    ProvisionUnavailable,
    UnknownModelTier,
    WorkerProvisioner,
    build_provisioner,
    tiers_from,
)
from agentgenome.agents.agentteams.provision import (
    plan as plan_provision,
)
from agentgenome.agents.agentteams.provision import (
    survey as survey_workers,
)
from agentgenome.agents.agentteams.readiness import check_readiness
from agentgenome.agents.agentteams.records import record_lifecycle, record_provision
from agentgenome.agents.agentteams.transport import PlatformUnavailable
from agentgenome.agents.artifacts import RESULT_FILENAME
from agentgenome.agents.factory import RuntimeAssemblyError, build_runtimes
from agentgenome.agents.pool import AgentPool
from agentgenome.approval.service import (
    ApprovalRefused,
    NotAnApprover,
    approve,
    reject,
    render_rejection,
)
from agentgenome.approval.service import (
    LineComment as ServiceLineComment,
)
from agentgenome.config import AGENTTEAMS_RUNTIME, Config, GenomeTaskConfig, load_config
from agentgenome.core.events import (
    ALERT_ACTOR,
    IM_ACTOR,
    ORCHESTRATOR,
    SYSTEM_SUBJECT,
    ActorKind,
    EventLog,
    LogKind,
)
from agentgenome.core.genome_driver import GenomeDriver, NotWaiting
from agentgenome.core.genome_gate import AnswerInvalid, NoDraft, read_answer, read_draft
from agentgenome.core.genome_task import (
    GenomeTask,
    GenomeTaskKind,
    GenomeTaskState,
    GenomeTaskStore,
    ModuleBusy,
    Origin,
    overdue_confirmations,
)
from agentgenome.core.genome_transitions import GenomeEvent
from agentgenome.core.intervention import InterventionError, resolve_dev, resolve_genome
from agentgenome.core.requirement import (
    Requirement,
    RequirementNotFound,
    RequirementScene,
    RequirementState,
    RequirementStore,
    intake,
    revise,
)
from agentgenome.core.scope_grants import effective_modules, read_grants
from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.store import task_dir
from agentgenome.core.task import AttemptConflict, Task, TaskNotFound, TaskRunStatus, TaskStore
from agentgenome.core.topology import UnknownTopology
from agentgenome.core.transitions import visible_escalation_reason
from agentgenome.employees import EmployeeNotFound, load_employees, workspace_employees_root
from agentgenome.genome import history as project_map_history
from agentgenome.genome.boundary import NotReadyForBoundaries
from agentgenome.genome.deep_read import read_progress
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.evolution import lifecycle as lesson_lifecycle
from agentgenome.genome.evolution.cards import (
    Applicability,
    Evidence,
    LessonCard,
    Level,
    load_cards,
    next_number,
)
from agentgenome.genome.evolution.trends import weekly
from agentgenome.genome.features import Confidence, no_card_declarations
from agentgenome.genome.init_entry import InitAlreadyOpen, plan_init
from agentgenome.genome.loader import load_project_map, load_tree
from agentgenome.genome.pipeline import GenomeOrchestrator
from agentgenome.genome.procedures import load_workspace_registry
from agentgenome.genome.rule_edit import RuleChangeRequest, submit_rule_change
from agentgenome.genome.rules import load_rules
from agentgenome.genome.scaffold import (
    MountFailed,
    WorkspaceExistsError,
    WorkspaceRemoteFailed,
    configure_workspace_remote,
    init_workspace,
    mark_workspace_push_pending,
    mount_planned,
    pending_mounts,
    plan_repos,
    push_workspace_remote,
    unmounted_refusal,
    workspace_initializing,
)
from agentgenome.integrations.ticket_import import UnsupportedTicketUrl, import_ticket
from agentgenome.jobs import catalog as topology_catalog
from agentgenome.jobs.driver import DriveResult, DriveStop, TaskDriver
from agentgenome.jobs.handlers import HANDLERS, can_advance
from agentgenome.jobs.orchestrator import TERMINAL_NOTIFY_EVENT, Orchestrator
from agentgenome.jobs.reports import TASK_REPORT, render_task_report
from agentgenome.jobs.split import VERDICT_SCHEMA, proposal_of
from agentgenome.jobs.trace import read_module_trace, read_trace
from agentgenome.security import gaps
from agentgenome.security.audit import TaskNotArchivable, export_task_bundle
from agentgenome.server import metrics as metrics_module
from agentgenome.server import notify_prefs
from agentgenome.server.bus import EventBus
from agentgenome.server.employees_edit import (
    NeedsAssignee,
    RuntimeNotConfigured,
    UnknownRung,
    compat_gap,
    declare_compat,
    machine_runtimes,
    runtime_blocks,
    set_execution,
    set_runtime,
)
from agentgenome.server.models import (
    AlertRequest,
    AlertResponse,
    ApprovalPreview,
    ApprovalRequest,
    ArtifactEntry,
    ArtifactList,
    AttemptView,
    AuditEventItem,
    AuditEventPage,
    BlockItem,
    BoundaryModule,
    CardHit,
    CompatDeclareRequest,
    CostReport,
    CostSlice,
    DeepenQueueEntry,
    EventItem,
    EventPage,
    EvidenceItem,
    ExecutionRequest,
    GapItem,
    GapReportResponse,
    GateAnswer,
    GateDraft,
    GateResult,
    GenomeTaskList,
    GenomeTaskProgress,
    GenomeTaskSummary,
    Health,
    ImportRequest,
    ImportResult,
    ImWebhookRequest,
    ImWebhookResponse,
    InterfaceEdge,
    InterventionResolveRequest,
    InterventionRetryRequest,
    KnowledgeStatus,
    LessonCardResponse,
    LessonCreateRequest,
    LessonList,
    LineComment,
    LogLine,
    LogPage,
    ModuleKnowledge,
    ModuleNode,
    ModuleProgress,
    NoCardDeclaration,
    NotificationPreference,
    NotificationPreferenceList,
    PendingTodo,
    ProcedureStat,
    ProcedureStatsList,
    ProjectMapDiffResponse,
    ProjectMapResponse,
    ProjectMapVersionItem,
    ProjectMapVersionList,
    QualityDial,
    ReadinessItemView,
    ReadinessView,
    ReinitRequest,
    RejectionPreviewRequest,
    ReportResponse,
    RequirementChildView,
    RequirementDetail,
    RequirementPatch,
    RequirementSummary,
    RosterMember,
    RosterReport,
    RuleProposalRequest,
    RuleProposalResponse,
    RuleSetResponse,
    RuntimeBlockView,
    RuntimeChoiceRequest,
    RuntimeChoiceView,
    ScopeGrantView,
    SettingsChange,
    SettingsRequest,
    SettingsView,
    SubmitRequest,
    SuspectEntry,
    TaskDetail,
    TaskSummary,
    TaskTrace,
    TaskTraceStage,
    TodoDetail,
    TodoItem,
    TodoList,
    TodoSubmitRequest,
    TodoSubmitResponse,
    TopologyCatalog,
    TopologyOption,
    TrendMetric,
    TrendReport,
    Version,
    WorkerLifecycleResult,
    WorkerPlanRowView,
    WorkerPlanView,
    WorkerProvisionResult,
    WorkerStatusListView,
    WorkerStatusView,
    WorkspaceCreated,
    WorkspaceCreateRequest,
    WorkspaceEntry,
    WorkspaceList,
)
from agentgenome.server.rbac import ANONYMOUS, SINGLE_MACHINE, Action, Forbidden, Principal
from agentgenome.server.sessions_api import mount_sessions
from agentgenome.server.settings import Change, Entrance, NotEditable
from agentgenome.server.settings import history as settings_history
from agentgenome.server.settings import update as update_settings
from agentgenome.server.tenancy import (
    DEFAULT_REGISTRY,
    DEFAULT_WORKSPACES_HOME,
    AmbiguousWorkspace,
    UnknownWorkspace,
    WorkspaceRegistry,
    save_registry,
)
from agentgenome.server.tenancy import check_name as check_workspace_name
from agentgenome.space.forge import Forge
from agentgenome.space.forge import select as select_forge
from agentgenome.space.gitcmd import GitError, git_out
from agentgenome.todo.service import TodoRefused, output_schema_of
from agentgenome.todo.service import submit as todo_submit
from agentgenome.todo.store import SPLIT as SPLIT_TODO
from agentgenome.todo.store import Todo, TodoNotFound, TodoStore
from agentgenome.umodel.graph import InMemoryGraph
from agentgenome.umodel.sync import Alert, AlertGate, localise

#: OpenAPI 契约的版本。前后端对齐看它,不看包版本——包发布节奏与接口变更节奏不是一回事。
API_VERSION = "1"

#: SSE 心跳间隔,秒。中间的代理常在 30~60 秒无数据时掐断连接。
HEARTBEAT_S = 15

EnumT = TypeVar("EnumT", bound=StrEnum)


def unregister_workspace(
    registry: WorkspaceRegistry,
    name: str,
    *,
    registry_path: Path | None,
    actor: str,
) -> Path:
    """摘掉一个项目:注册表少一条、事件面记一条、持久化跟着变,**磁盘不动**。

    CLI 与 REST 共用这一个函数——事件的形状只该有一份。事件写进**被摘工作区自己的
    事件面**:文件都留着,这条记录因此留得住,而"这个项目谁什么时候注销的"正是它答的。
    """
    root = registry.unregister(name)
    EventLog(root).append(
        SYSTEM_SUBJECT,
        actor=actor,
        kind=LogKind.WORKSPACE_CHANGED,
        payload={"action": "unregister", "name": name},
    )
    if registry_path is not None:
        save_registry(registry_path, registry)
    return root


def _spawn_dev_drive(request: Request, root: Path, task_id: str, config: Config) -> bool:
    """在外部事件之后继续驱动研发任务。已经在运行或无需推进时返回 False。"""
    runs: dict[str, Any] = request.app.state.task_runs
    key = f"{root}|{task_id}"
    running = runs.get(key)
    if running is not None and not running.done():
        return False
    before = TaskStore(root).get(task_id)
    if not can_advance(before):
        return False
    pool = _task_pool(request, root, config)
    bus: EventBus = request.app.state.bus
    workspace_name = _workspace_name(request)
    TaskStore(root).save(before.evolve(run_status=TaskRunStatus.RUNNING))

    async def _run() -> None:
        final_status = TaskRunStatus.INTERRUPTED

        def publish_change(changed_task_id: str, kind: str) -> None:
            bus.publish(changed_task_id, kind, workspace=workspace_name)

        def publish_step(task: Task) -> None:
            publish_change(task.id, "task_changed")

        try:
            result = await TaskDriver(
                Orchestrator(root, pool=pool, config=config, on_change=publish_change),
                on_step=publish_step,
            ).drive(task_id)
            moved = result.task
            if result.stop is DriveStop.SAFETY_LIMIT:
                EventLog(root).append(
                    task_id,
                    actor=ORCHESTRATOR,
                    kind=LogKind.NOTE,
                    payload={"note": "自动推进达到 64 步安全上限，已中断以避免流程空转"},
                )
            elif result.stop is DriveStop.STALLED:
                EventLog(root).append(
                    task_id,
                    actor=ORCHESTRATOR,
                    kind=LogKind.NOTE,
                    payload={"note": "自动推进没有产生新状态或新执行轮次，已中断以避免重复回放"},
                )
            final_status = _drive_run_status(result)
            event = TERMINAL_NOTIFY_EVENT.get(moved.state)
            if before.state is not moved.state and event is not None:
                notify_prefs.push(root, task_id, moved.title, event)
            if moved.state is TaskState.COMPLETED and moved.requirement_id:
                # 这次交付可能解锁了树上的亲属(兄弟或收口)——编排器已经把尝试**建**
                # 出来了,serve 的承诺是它们自动开工:接上驱动(PRD 48 D4 的 serve 半边)。
                _drive_the_kin(request, root, moved, config)
        except Exception as error:  # noqa: BLE001 - 后台异常必须进入任务事件面
            EventLog(root).append(
                task_id,
                actor=ORCHESTRATOR,
                kind=LogKind.NOTE,
                payload={"note": f"推进失败: {error}"},
            )
        finally:
            current = TaskStore(root).get(task_id)
            if current.is_terminal:
                final_status = TaskRunStatus.FINISHED
            TaskStore(root).save(current.evolve(run_status=final_status))
            runs.pop(key, None)
            bus.publish(task_id, "run_finished", workspace=workspace_name)

    runs[key] = asyncio.create_task(_run())
    bus.publish(task_id, "run_started", workspace=workspace_name)
    return True


def _drive_run_status(result: DriveResult) -> TaskRunStatus:
    """把驱动停止原因统一投影成 API 的瞬时运行状态。"""
    if result.task.is_terminal:
        return TaskRunStatus.FINISHED
    if result.stop is DriveStop.WAITING:
        return TaskRunStatus.WAITING
    return TaskRunStatus.INTERRUPTED


def _dev_execution_status(request: Request, task: Task) -> TaskRunStatus:
    """生命周期之外的瞬时执行投影，刷新页面后仍以服务端运行句柄为准。"""
    running = request.app.state.task_runs.get(f"{_root(request)}|{task.id}")
    if isinstance(running, asyncio.Task) and not running.done():
        return TaskRunStatus.RUNNING
    if task.run_status is TaskRunStatus.RUNNING:
        return TaskRunStatus.INTERRUPTED
    if task.is_terminal:
        return TaskRunStatus.FINISHED
    if not can_advance(task):
        return TaskRunStatus.WAITING
    if task.plan_retries > 0:
        return TaskRunStatus.RETRY_PENDING
    return task.run_status


def _pending_todo(root: Path, task: Task) -> PendingTodo | None:
    """把任务的内部挂起指针投影成前端可操作的待办摘要。"""
    if not task.pending_todo_id:
        return None
    try:
        todo = TodoStore(root).get(task.pending_todo_id)
    except TodoNotFound:
        return None
    return PendingTodo(id=todo.id, kind=todo.kind, assignee=todo.assignee)


def _task_summary(request: Request, task: Task) -> TaskSummary:
    root = _root(request)
    return TaskSummary.of(
        task,
        execution_status=_dev_execution_status(request, task),
    ).model_copy(update={"pending_todo": _pending_todo(root, task)})


def _task_detail(request: Request, task: Task) -> TaskDetail:
    root = _root(request)
    return TaskDetail.of(
        task,
        execution_status=_dev_execution_status(request, task),
    ).model_copy(update={"pending_todo": _pending_todo(root, task)})


def _spawn_genome_drive(request: Request, root: Path, task_id: str, config: Config) -> bool:
    """把一个基因组任务的推进丢进后台跑。已经在推返回 False。

    **这补的是「人一答复,机器自己就往下走」的后半句。** 待确认不是终态,词表承诺答复
    之后机器自己继续——此前这句话只在 CLI(`agctl knowledge run`)里成立,网页上答完
    闸门任务就停在 DEEP_READ 干等。驱动循环一步一落盘(`GenomeOrchestrator.advance`
    的既有语义),进程中途死掉不丢进度,重启后从「继续推进」接上。

    运行时**用员工声明的**(`runtime_name=None`),不在这里另配一个旋钮——与研发任务的
    派发同一条纪律,测试切回放的方式也因此一致:改员工定义,不塞替身。
    """
    runs: dict[str, Any] = request.app.state.task_runs
    key = f"{root}|{task_id}"
    if key in runs:
        return False
    pool = _task_pool(request, root, config)
    orchestrator = GenomeOrchestrator(root, pool=pool, runtime_name=None, config=config)

    async def _run() -> None:
        try:
            previous: GenomeTaskState | None = None
            while True:
                task = await orchestrator.advance(task_id)
                if task.is_terminal or task.state is GenomeTaskState.AWAITING_CONFIRMATION:
                    break
                if task.state is previous:
                    # 状态没动也没终结:这一步推不动(比如没有对应的 Handler)。
                    # 干等着再推一次只会原地打转。
                    break
                previous = task.state
        except Exception as error:  # noqa: BLE001 - 后台任务没人等着接异常,必须自己落地
            EventLog(root).append(
                task_id,
                actor=ORCHESTRATOR,
                kind=LogKind.NOTE,
                payload={"note": f"推进失败: {error}"},
            )
        finally:
            runs.pop(key, None)

    runs[key] = asyncio.create_task(_run())
    return True


def _spawn_mounts(request: Request, root: Path, task_id: str) -> None:
    """把挂载丢进后台线程跑:任务记录负责可见性,线程负责干活。

    **线程而不是事件循环任务**:clone 是阻塞的子进程调用,而且它必须在请求返回之后
    继续活着——挂在请求的事件循环上,测试客户端与短命 worker 都不保证还会调度它。
    键上 `|mount` 后缀防重复:同一个工作区同时跑两轮挂载,第二轮会在第一轮已挂上的
    仓上撞 `submodule add`。

    线程里只碰 SQLite(每次调用各开连接)与文件,**不碰事件总线**——订阅者的 asyncio
    队列不是线程安全的,而前端的拉取策略本来就能自愈,少这一条推送死不了人。
    """
    runs: dict[str, Any] = request.app.state.task_runs
    key = f"{root}|mount"

    def _run() -> None:
        try:
            _run_mounts(root, task_id)
        finally:
            runs.pop(key, None)

    thread = threading.Thread(target=_run, name=f"mount:{root.name}", daemon=True)
    runs[key] = thread
    thread.start()


def _run_mounts(root: Path, task_id: str) -> None:
    """逐仓挂载。**单仓失败不放弃其余仓**——三个仓里一个地址填错,另外两个照常挂上,
    人修的只是那一个。全部成功推到 SUBMITTED;有失败推到 FAILED,`origin=human`
    让它进异常队列(有人在等这个结果)。"""
    log = EventLog(root)
    driver = GenomeDriver(GenomeTaskStore(root), log)
    failures = []
    # 先落意图再产生挂载提交；最后一个 mount commit 与最终 push 之间崩溃也不会伪装成就绪。
    mark_workspace_push_pending(root)
    for spec in pending_mounts(root):
        try:
            mount_planned(root, spec)
            log.append(
                task_id,
                actor=ORCHESTRATOR,
                kind=LogKind.NOTE,
                payload={"note": "mounted", "module": spec.module_id, "url": spec.url},
            )
        except MountFailed as error:
            failures.append(spec.module_id)
            log.append(
                task_id,
                actor=ORCHESTRATOR,
                kind=LogKind.NOTE,
                payload={
                    "note": "mount_failed",
                    "module": spec.module_id,
                    "url": spec.url,
                    # 失败原因原样进事件:认证被拒/地址不存在/分支不存在,下一步该查什么
                    # 从这里读,不用去翻服务端日志。
                    "error": str(error),
                },
            )
    if not failures:
        try:
            push_workspace_remote(root)
        except WorkspaceRemoteFailed as error:
            failures.append("workspace")
            log.append(
                task_id,
                actor=ORCHESTRATOR,
                kind=LogKind.NOTE,
                payload={"note": "workspace_push_failed", "error": str(error)},
            )
    if failures:
        driver.deliver(task_id, GenomeEvent.FAILED)
    else:
        driver.deliver(task_id, GenomeEvent.READ_DONE)
        driver.deliver(task_id, GenomeEvent.SUBMITTED)


def create_app(
    workspace_root: Path | None = None,
    bus: EventBus | None = None,
    workspaces: WorkspaceRegistry | None = None,
    principals: dict[str, Principal] | None = None,
    graph: InMemoryGraph | None = None,
    registry_path: Path | None = None,
    workspaces_home: Path | None = None,
) -> FastAPI:
    """建应用。

    `workspace_root` 是单工作区部署的快捷写法;`workspaces` 是多工作区。**两者都给时以
    注册表为准**——不然"我明明注册了三个"会静默变成只服务一个。

    `registry_path` 是注册表的持久化落点:注册/注销在这里写回,重启后不丢。没给表示
    这次部署的注册表只活在内存里(单工作区快捷写法就是这种)。

    `principals` 是身份表。没给的话所有请求都是匿名的,而匿名**什么都不能做**——默认拒绝。
    真正的认证在 PRD 18 的 SSO 那一半,这里换的只是身份来源。
    """
    app = FastAPI(
        title="AgentGenome 控制面",
        version=package_version,
        summary="任务提交、查询、审批、事件流与指标",
    )
    registry = workspaces or WorkspaceRegistry()
    if workspace_root is not None and not registry.entries:
        registry.register("default", Path(workspace_root))
    app.state.workspaces = registry
    app.state.registry_path = registry_path
    # 界面上建的项目落在这个受管根目录下(`<home>/<name>`)。调用方永远只说名字。
    app.state.workspaces_home = workspaces_home or DEFAULT_WORKSPACES_HOME
    app.state.principals = principals or {}
    app.state.bus = bus or EventBus()

    app.state.graph = graph
    app.state.alert_gate = AlertGate()
    # 会话服务按工作区缓存:它持有已开会话的运行时句柄,每次新建一个的话第二条消息会
    # 因为"会话还没开起来"而失败。
    app.state.session_services = {}
    app.state.session_runtimes = {}
    # 任务推进的执行池,同样按工作区缓存。`AgentPool` 的并发闸(`asyncio.Semaphore`)是
    # 进程内状态——每次请求都新建一个的话,"同时最多跑几个 Job"这条限制形同虚设,
    # 因为每个新池都从零开始数。
    app.state.task_pools = {}
    # 正在后台推进的任务。**键是 `工作区路径|任务号`**——task_id 只在单个工作区内保证
    # 唯一,不同工作区完全可能撞号。存在这里是为了挡重复点击:同一个任务还没推完一步,
    # 再点一次不该再起一个 Job 去抢同一个隔离工作区。
    app.state.task_runs = {}

    _mount_tasks(app)
    _mount_streams(app)
    _mount_integrations(app)
    _mount_settings(app)
    _mount_ops(app)
    _mount_genome(app)
    _mount_insights(app)
    _mount_audit(app)
    _mount_requirements(app)
    _mount_notifications(app)
    mount_sessions(app)
    return app


# --- 任务 -------------------------------------------------------------------


def _mount_tasks(app: FastAPI) -> None:
    @app.post("/tasks", response_model=TaskDetail, status_code=201, tags=["tasks"])
    def submit(body: SubmitRequest, request: Request) -> TaskDetail:
        principal = _ensure(request, Action.SUBMIT_TASK)
        root = _root(request)
        # **在提交这一刻拒绝拼错的拓扑名。** 不校验的话它会一路活到派发那一步才炸,而那时
        # 人已经离开提交页很久了,现场也从"你刚填的表单"变成了"某个任务跑到一半"。
        try:
            topology_catalog.check_choice(body.topology)
        except UnknownTopology as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # 还在初始化(有业务仓没挂上)的项目提不了研发任务:员工没有代码可读,任务只会
        # 在派发那一刻以更难懂的方式失败。设置、事件、基因组任务页不受此限。
        refusal = unmounted_refusal(root)
        if refusal:
            raise HTTPException(status_code=409, detail=refusal)
        store = TaskStore(root)
        actor = _actor_of(request, principal)
        if body.requirement_id is not None:
            # 再试一次必须把「更新当前文本」与「创建任务」放在同一事务里；否则后一步失败
            # 会留下一个从未被任何尝试读取过的新文本。
            requirement_store = RequirementStore(root)  # 确保老 Workspace 已迁移出表。
            try:
                requirement_store.get(body.requirement_id)
            except RequirementNotFound as error:
                # 保持 REST 与 CLI 的既有契约：引用不存在的需求属于输入错误，不是状态冲突。
                raise HTTPException(status_code=422, detail=str(error)) from error
            try:
                retry = store.create_retry(
                    requirement_id=body.requirement_id,
                    title=body.title or _first_line(body.requirement),
                    requirement=body.requirement,
                    priority=body.priority,
                    budget_tokens=body.budget_tokens,
                    itest_override=body.itest,
                    mode=body.mode,
                    topology=body.topology,
                )
            except AttemptConflict as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            task = retry.task
            requirement_id = body.requirement_id
            if retry.requirement_changed:
                EventLog(root).append(
                    requirement_id,
                    actor=actor,
                    kind=LogKind.REQUIREMENT_CHANGED,
                    payload={"action": "text", "via": "retry"},
                )
        else:
            # 首次提交保持既有入口；每个新任务自动生出自己的需求容器。
            requirement = intake(
                root,
                title=body.title or _first_line(body.requirement),
                text=body.requirement,
                priority=body.priority,
                actor=actor,
            )
            requirement_id = requirement.id
            task = store.create(
                title=body.title or _first_line(body.requirement),
                requirement=body.requirement,
                priority=body.priority,
                budget_tokens=body.budget_tokens,
                itest_override=body.itest,
                mode=body.mode,
                topology=body.topology,
                requirement_id=requirement_id,
            )
        EventLog(root).append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.TASK_CREATED,
            payload={
                "title": task.title,
                "priority": task.priority,
                "mode": task.mode.value,
                # 所属需求只带指针:内容在需求实体与它自己的事件里,不存两份。
                "requirement_id": requirement_id,
                # 从哪次对话来的。**双向都有用**:回头看一个任务时能找到当初把它讲清楚的
                # 那次会话,而不是只剩一段被誊写过的需求。
                **({"source_session_id": body.source_session_id} if body.source_session_id else {}),
            },
        )
        _publish(request, task.id, "task_created")
        _notify_im(request, task, "task_created")
        return TaskDetail.of(task)

    @app.get("/tasks", response_model=list[TaskSummary], tags=["tasks"])
    def list_tasks(
        request: Request,
        settled: bool = Query(
            default=False, description="连已完成/已取消的一起列。缺省只列人还需要看见的"
        ),
    ) -> list[TaskSummary]:
        # **`unsettled_tasks` 而不是 `open_tasks`。** 后者是调度队列,把 `ESCALATED` 也排除
        # 掉——而看板给 `ESCALATED` 留了一整列。用错的那版里,升级人工的任务在界面上不存在。
        store = TaskStore(_root(request))
        found = store.all_tasks() if settled else store.unsettled_tasks()
        return [_task_summary(request, task) for task in found]

    @app.get("/tasks/{task_id}", response_model=TaskDetail, tags=["tasks"])
    def get_task(task_id: str, request: Request) -> TaskDetail:
        task = _task(request, task_id)
        detail = _task_detail(request, task)
        root = _root(request)
        detail.effective_modules = effective_modules(root, task_id)
        detail.scope_grants = [
            ScopeGrantView(module=item.module, reason=item.reason, round=item.round_)
            for item in read_grants(root, task_id)
        ]
        return detail

    @app.post(
        "/tasks/{task_id}/intervention/resolve",
        response_model=TaskSummary,
        tags=["tasks"],
    )
    def resolve_task_intervention(
        task_id: str, body: InterventionResolveRequest, request: Request
    ) -> TaskSummary:
        principal = _ensure(request, Action.RESOLVE_INTERVENTION)
        try:
            task = resolve_dev(
                _root(request),
                task_id,
                actor=_actor_of(request, principal),
                note=body.note,
            )
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InterventionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        _publish(request, task_id, "intervention_resolved")
        return TaskSummary.of(task)

    @app.post(
        "/tasks/{task_id}/intervention/retry",
        response_model=TaskDetail,
        status_code=201,
        tags=["tasks"],
    )
    def retry_task_intervention(
        task_id: str, body: InterventionRetryRequest, request: Request
    ) -> TaskDetail:
        principal = _ensure(request, Action.RESOLVE_INTERVENTION)
        root = _root(request)
        store = TaskStore(root)
        try:
            source = store.get(task_id)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if source.requirement_id is None:
            raise HTTPException(status_code=409, detail=f"{task_id} 是没有所属需求的存量任务")
        RequirementStore(root)
        try:
            retry = store.create_retry(
                requirement_id=source.requirement_id,
                title=source.title,
                requirement=body.requirement,
                priority=source.priority,
                budget_tokens=source.budget_tokens,
                itest_override=source.itest_override,
                mode=source.mode,
                topology=source.topology,
                source_task_id=source.id,
            )
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except AttemptConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if not retry.created:
            return TaskDetail.of(retry.task)

        actor = _actor_of(request, principal)
        log = EventLog(root)
        if retry.requirement_changed:
            log.append(
                source.requirement_id,
                actor=actor,
                kind=LogKind.REQUIREMENT_CHANGED,
                payload={"action": "text", "via": "intervention_retry"},
            )
        log.append(
            retry.task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.TASK_CREATED,
            payload={
                "title": retry.task.title,
                "priority": retry.task.priority,
                "mode": retry.task.mode.value,
                "requirement_id": source.requirement_id,
                "predecessor_task_id": source.id,
            },
        )
        log.append(
            source.id,
            actor=actor,
            kind=LogKind.INTERVENTION_RESOLVED,
            payload={
                "note": "已修改需求并创建后继尝试",
                "successor_task_id": retry.task.id,
            },
        )
        _publish(request, source.id, "intervention_resolved")
        _publish(request, retry.task.id, "task_created")
        _notify_im(request, retry.task, "task_created")
        return TaskDetail.of(retry.task)

    @app.post("/tasks/{task_id}/cancel", response_model=TaskSummary, tags=["tasks"])
    def cancel(task_id: str, request: Request) -> TaskSummary:
        """取消。**已取消的再取消是幂等的**——崩溃恢复会重放这个动作。"""
        _ensure(request, Action.CANCEL_TASK)
        root = _root(request)
        task = _task(request, task_id)
        if task.is_terminal:
            return TaskSummary.of(task)
        running = request.app.state.task_runs.get(f"{root}|{task_id}")
        if isinstance(running, asyncio.Task) and not running.done():
            running.cancel()
        updated = Orchestrator(root).deliver(task_id, TaskEvent.CANCEL)
        _publish(request, task_id, "cancelled")
        _notify_im(request, updated, "cancelled")
        return TaskSummary.of(updated)

    @app.post("/tasks/{task_id}/run", response_model=TaskSummary, status_code=202, tags=["tasks"])
    async def run(task_id: str, request: Request) -> TaskSummary:
        """启动任务并持续推进到外部闸门或终态。**在后台跑,不占着这次请求。**

        一条流程包含多个分钟级 Job,同步等在 HTTP 请求里的话浏览器与反向代理会先超时。
        后台驱动每一步仍单独落盘，前端靠事件推送刷新，不靠本次响应体等待最终结果。

        没有处理器的状态(等审批、等合并结果、结对会话进行中……)不接受这个调用——
        `Orchestrator.advance` 对这些状态是静默 no-op,原样把任务读出来又存回去,点了按钮
        却什么都没发生,界面上却看不出区别。在这里挡住,错误消息比静默的"没反应"有用。
        `can_advance` 与 `advance` 本身共用同一个判断,这个接口不会放行一个 `advance`
        转头就 no-op 的状态。
        """
        _ensure(request, Action.RUN_TASK)
        root = _root(request)
        task = _task(request, task_id)
        if not can_advance(task):
            raise HTTPException(status_code=409, detail=_why_not_runnable(task))
        config = load_config(root)
        if not _spawn_dev_drive(request, root, task_id, config):
            raise HTTPException(
                status_code=409, detail=f"{task_id} 已经在推进中,等当前流程停下再试。"
            )
        return TaskSummary.of(task, execution_status=TaskRunStatus.RUNNING)

    @app.post("/tasks/{task_id}/approval", response_model=TaskSummary, tags=["tasks"])
    async def decide(task_id: str, body: ApprovalRequest, request: Request) -> TaskSummary:
        # 审批的身份校验**在审批服务里**(PRD 08),这里只挡住「连审批入口都不该碰」的角色。
        principal = _ensure(request, Action.APPROVE_TASK)
        root = _root(request)
        _task(request, task_id)
        # **配置了身份表之后,审批人身份来自认证后的 principal,不再信任 `body.actor`。**
        # `_ensure` 只核对"这个认证身份有没有审批权限",而审批服务(`approve`/`reject`)
        # 只核对"`body.actor` 这个名字在不在审批人名单里"——两处校验的是不同的字符串,
        # 一个有审批权的人能在 body 里填另一个审批人的名字,系统会把这次决定记成后者做的。
        # 没配身份表(单机开发)时行为不变:那种模式下 `body.actor` 本来就是唯一的身份来源。
        actor = principal.subject if request.app.state.principals else body.actor
        line_comments = tuple(_service_line_comment(item) for item in body.line_comments)
        try:
            task = (
                approve(root, task_id, actor, body.comment)
                if body.approved
                else reject(root, task_id, actor, body.comment, line_comments)
            )
        except NotAnApprover as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ApprovalRefused as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        kind = "approved" if body.approved else "rejected"
        _publish(request, task_id, kind)
        _notify_im(request, task, kind)
        resumed = _spawn_dev_drive(request, root, task_id, load_config(root))
        return TaskSummary.of(task, execution_status=TaskRunStatus.RUNNING if resumed else None)

    @app.post("/tasks/{task_id}/approval/preview", response_model=ApprovalPreview, tags=["tasks"])
    def preview_rejection(
        task_id: str, body: RejectionPreviewRequest, request: Request
    ) -> ApprovalPreview:
        """提交驳回前,AI 将实际收到的完整意见文本。**与真正驳回时用的是同一个渲染函数**——
        预览显示的就是实际发送的,不是一份近似。
        """
        _task(request, task_id)
        line_comments = tuple(_service_line_comment(item) for item in body.line_comments)
        return ApprovalPreview(text=render_rejection(body.comment, line_comments))

    @app.get("/tasks/{task_id}/events", response_model=EventPage, tags=["tasks"])
    def events(
        task_id: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> EventPage:
        _ensure_any_task_exists(request, task_id)
        found = EventLog(_root(request)).events(task_id)
        window = found[offset : offset + limit]
        return EventPage(
            items=[
                EventItem(
                    task_id=item.task_id,
                    ts=item.ts.isoformat(),
                    actor=item.actor,
                    kind=item.kind.value,
                    payload=item.payload,
                )
                for item in window
            ],
            total=len(found),
            offset=offset,
            limit=limit,
        )

    @app.get("/tasks/{task_id}/logs", response_model=LogPage, tags=["tasks"])
    def logs(
        task_id: str,
        request: Request,
        cursor: int = Query(default=0, ge=0, description="从这一行之后开始,行号从 1 起"),
        limit: int = Query(default=200, ge=1, le=5000),
    ) -> LogPage:
        """历史日志,游标翻页。

        **游标是行号而不是字节偏移。** 日志在追加,偏移量会随内容变化错位;而行号一旦写下
        就不会再变,于是翻页期间新写入的行不会让已经看过的内容重复出现。
        """
        _ensure_any_task_exists(request, task_id)
        path = task_dir(_root(request), task_id) / "logs" / "events.jsonl"
        raw = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        window = raw[cursor : cursor + limit]
        used = cursor + len(window)
        return LogPage(
            items=[
                LogLine(line=cursor + index + 1, text=text) for index, text in enumerate(window)
            ],
            next_cursor=used if used < len(raw) else None,
            total=len(raw),
        )

    # --- 待办:派给人的那些 Job -------------------------------------------------

    @app.get("/todos", response_model=TodoList, tags=["todos"])
    def list_todos(request: Request, assignee: str = "", task_id: str = "") -> TodoList:
        """还等着人干的待办。

        **不列已经交掉的**:待办列表是工作面板,把交掉的混进来会把真正等他的那几张淹掉。
        """
        root = _root(request)
        return TodoList(
            items=[
                TodoItem(**_todo_fields(root, item))
                for item in TodoStore(root).open_todos(assignee, task_id)
            ]
        )

    @app.get("/todos/{todo_id}", response_model=TodoDetail, tags=["todos"])
    def get_todo(todo_id: str, request: Request) -> TodoDetail:
        root = _root(request)
        try:
            todo = TodoStore(root).get(todo_id)
        except TodoNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return TodoDetail(
            **_todo_fields(root, todo),
            context_file=todo.context_file,
            output_dir=todo.output_dir,
            workdir=todo.workdir,
            # 拆分待办交的不是产物而是裁决:给人看的契约必须是裁决的形状,拿工序产物
            # schema 出去的话,人会照着交一份"计划",然后被校验打回。
            schema=(
                VERDICT_SCHEMA
                if todo.kind == SPLIT_TODO
                else output_schema_of(root, todo.procedure_id)
            ),
        )

    @app.post("/todos/{todo_id}/submit", response_model=TodoSubmitResponse, tags=["todos"])
    async def submit_todo(
        todo_id: str, body: TodoSubmitRequest, request: Request
    ) -> TodoSubmitResponse:
        """交活。

        **RBAC 在服务端判**:一个能构造 HTTP 请求的人不受前端约束,而这条通路的产物会直接
        进流水线。身份表没配时(单机开发)不强判——与别的写接口同一条规矩。
        """
        root = _root(request)
        principal = _principal(request)
        actor = principal.subject if request.app.state.principals else ""
        try:
            submission = todo_submit(
                root,
                todo_id,
                payload=body.result,
                actor=actor,
                # 指派人可以是一个角色:一份活派给"审批组"而不是某个具体的人是团队常态。
                roles=frozenset(role.value for role in principal.roles),
            )
        except TodoNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except TodoRefused as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

        if not submission.ok:
            # 契约没过**不是 4xx 的权限问题也不是 5xx**:活交上来了,只是产物不合格,
            # 人改一改还能再交——这与硅基员工的契约重试是同一件事。
            return TodoSubmitResponse(
                ok=False,
                todo=TodoItem(**_todo_fields(root, submission.todo)),
                detail=submission.detail,
            )

        task = await _resume_after_todo(request, root, submission.todo.task_id)
        if submission.todo.kind == SPLIT_TODO:
            # 确认落树之后,首批子需求的尝试已经被建出来了(编排器只建不推)——推进是
            # 表面自己的事,serve 的表面就是这里:给每个能推的子需求尝试接上后台驱动。
            _drive_split_children(request, root, task)
        return TodoSubmitResponse(
            ok=True,
            todo=TodoItem(**_todo_fields(root, submission.todo)),
            task_state=task.state.value,
        )

    @app.get("/tasks/{task_id}/artifacts", response_model=ArtifactList, tags=["tasks"])
    def artifacts(task_id: str, request: Request) -> ArtifactList:
        _task(request, task_id)
        root = task_dir(_root(request), task_id)
        items = [
            ArtifactEntry(path=str(item.relative_to(root)), size=item.stat().st_size)
            for item in sorted(root.rglob("*"))
            if item.is_file()
        ]
        return ArtifactList(items=items)

    @app.get("/tasks/{task_id}/artifacts/{path:path}", tags=["tasks"])
    def artifact(task_id: str, path: str, request: Request) -> PlainTextResponse:
        """读一个产物。

        `{path}` 来自 URL,是最典型的不可信输入——一个 `../../etc/passwd` 能把服务器上任何
        文件读出去。所以解析之后必须落回任务目录内。
        """
        _task(request, task_id)
        root = task_dir(_root(request), task_id).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=404, detail=f"没有这个产物: {path}")
        try:
            return PlainTextResponse(target.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            # 二进制产物不强行解码——返回一段说明比返回一堆乱码有用。
            raise HTTPException(
                status_code=415, detail=f"{path} 不是文本产物,请直接从产物目录取"
            ) from None

    @app.get("/tasks/{task_id}/trace", response_model=TaskTrace, tags=["tasks"])
    def trace(task_id: str, request: Request) -> TaskTrace:
        """执行轨迹:每个 stage 的 Job 实际读了什么、调了什么工具、模型说了什么。

        与"实时日志"(`/tasks/{id}/logs`)不是同一份数据——那份是任务生命周期事件
        (创建/开工/收工/状态转移),这份是 Job 内部真正的对话过程,读的是
        `job-attempt-*.jsonl`(见 `jobs.trace.read_trace`)。确定性执行(如 `unit-gate`
        真的跑 pytest)没有这份文件,对应 stage 的 `blocks` 就是空的,不是接口坏了。
        """
        root = _root(request)
        _task(request, task_id)
        stages = [
            TaskTraceStage(
                stage=item.stage,
                number=item.number,
                blocks=[BlockItem(**block.as_dict()) for block in item.blocks],
            )
            for item in read_trace(task_dir(root, task_id))
        ]
        return TaskTrace(task_id=task_id, stages=stages)

    @app.get("/tasks/{task_id}/report", response_model=ReportResponse, tags=["tasks"])
    def report(task_id: str, request: Request) -> ReportResponse:
        root = _root(request)
        task = _task(request, task_id)
        landed = task_dir(root, task_id) / TASK_REPORT
        if landed.is_file():
            return ReportResponse(task_id=task_id, markdown=landed.read_text(encoding="utf-8"))
        # 还没到终态时现算一份:需求方问"现在到哪了"的时候,任务通常正好还没走完。
        orchestrator = Orchestrator(root)
        return ReportResponse(
            task_id=task_id,
            markdown=render_task_report(task, orchestrator.log.events(task_id)),
        )


# --- 实时 -------------------------------------------------------------------


async def stream_notices(
    bus: EventBus,
    task_id: str | None,
    last_id: int | None,
    heartbeat_s: float = HEARTBEAT_S,
    workspace: str | None = None,
) -> AsyncIterator[str]:
    """SSE 的正文。**独立成函数是为了能被直接测。**

    挂在路由里的话,测它只能起一条真 HTTP 流,而这个生成器永不结束——读一行就会阻塞到超时。
    拆出来之后,订阅、补发、心跳、过滤都能用 `asyncio` 逐条断言,而路由退化成一个三行的适配器。

    代价写清楚:HTTP 那一层(响应头、媒体类型、断开清理)没有被这组测试覆盖,它靠 FastAPI
    自己的 `StreamingResponse` 保证。
    """
    async with bus.subscribe(task_id, workspace) as subscription:
        for missed in bus.since(last_id, task_id, workspace):
            yield missed.as_sse()
        while True:
            try:
                notice = await asyncio.wait_for(subscription.get(), timeout=heartbeat_s)
            except TimeoutError:
                # 心跳:中间的代理常在 30~60 秒无数据时掐断连接。
                yield ": ping\n\n"
                continue
            yield notice.as_sse()


def _mount_streams(app: FastAPI) -> None:
    @app.get("/events/stream", tags=["stream"])
    async def stream(
        request: Request,
        task_id: str | None = Query(default=None, description="只订阅这一个任务"),
    ) -> StreamingResponse:
        # 订阅按项目过滤,解析纪律与 `_root` 相同:多于一个项目而没说要哪个 → 拒绝。
        # 通知只带 task_id 与 kind,但 task_id 本身就是数据,不该跨项目可见。
        registry: WorkspaceRegistry = request.app.state.workspaces
        name = _requested_workspace(request)
        try:
            workspace = registry.resolve(name)[0] if (name or len(registry.entries) > 1) else None
        except AmbiguousWorkspace as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except UnknownWorkspace as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return StreamingResponse(
            stream_notices(
                request.app.state.bus, task_id, _last_event_id(request), workspace=workspace
            ),
            media_type="text/event-stream",
        )


# --- 运维 -------------------------------------------------------------------


def _mount_integrations(app: FastAPI) -> None:
    @app.post("/alerts", response_model=AlertResponse, tags=["integrations"])
    def alert(body: AlertRequest, request: Request) -> AlertResponse:
        """生产告警进来,沿图谱定位到模块与最近改过它的任务。

        **定位不到就不建任务。** 瞎猜一个模块会让数字员工去改一段与故障无关的代码,而人还
        以为系统在处理这次告警——那比什么都不做糟得多。

        **按服务与时间窗去重。** 一次告警风暴能创建几百个任务,把预算烧光。
        """
        graph = request.app.state.graph
        gate: AlertGate = request.app.state.alert_gate
        if graph is None:
            return AlertResponse(
                alert_id=body.id,
                located=False,
                modules=[],
                recent_tasks=[],
                reason="没有配置语义图谱,只记录不建任务",
            )

        found = localise(graph, Alert(id=body.id, service=body.service, summary=body.summary))
        if not found.located:
            return AlertResponse(
                **found.as_dict() | {"reason": "定位不到模块,不建任务——瞎猜比不建更糟"}
            )

        admitted, reason = gate.admit(Alert(id=body.id, service=body.service, summary=body.summary))
        if not admitted:
            return AlertResponse(**found.as_dict() | {"reason": reason})

        root = _root(request)
        task = TaskStore(root).create(
            title=f"修复生产告警 {body.id}",
            requirement=found.requirement(),
            priority=8,
        )
        EventLog(root).append(
            task.id,
            actor=ALERT_ACTOR,
            # 监控推过来的,既不是人也不是员工。算进任何一边都会让那一边的统计失真。
            actor_kind=ActorKind.INTEGRATION,
            kind=LogKind.TASK_CREATED,
            payload={"alert_id": body.id, "service": body.service, "modules": list(found.modules)},
        )
        _publish(request, task.id, "task_created")
        return AlertResponse(**found.as_dict() | {"task_id": task.id, "reason": "已建修复任务"})


def _entrance_of(request: Request) -> Entrance:
    """这次请求是从浏览器界面来的,还是脚本直接打接口。

    **看 `Sec-Fetch-*` 而不是让调用方在 body 里自报。** 浏览器一定会发这组头,页面脚本也
    压不掉它(受限请求头);curl 与 SDK 默认不发。

    **这只在一个方向上是硬的:没有这组头,基本可以断定不是从界面来的。** 反过来不成立——
    任何非浏览器客户端都能手工加上它,冒充成 `web`。写清楚是因为这是一条会被当成"防篡改"
    读的判据,而它不是:它省掉的是"body 里填什么就信什么"这种连痕迹都不留的自报,不是伪造。
    真正扛得住抵赖的是 `actor`,那一位来自认证过的身份。
    """
    return Entrance.WEB if "sec-fetch-mode" in request.headers else Entrance.API


def _mount_settings(app: FastAPI) -> None:
    @app.get("/topologies", response_model=TopologyCatalog, tags=["ops"])
    def topologies(request: Request) -> TopologyCatalog:
        """提交时能选的执行拓扑。

        **可选项由服务端给,前端不硬编码一份。** 硬编码的话,加第六个模板要改两处,而漏改
        的那一处不会报错——它只会让一个明明能跑的策略在界面上不存在。
        """
        root = _root(request)
        config = load_config(root)
        return TopologyCatalog(
            default=config.topology.default,
            options=[
                # 摊平那一步住在 `catalog`,不在这里重写一遍:各摊一遍的话,加一个字段
                # 时漏掉的那一处会安静地少给一样东西。
                TopologyOption(**option.as_dict())
                for option in topology_catalog.options(
                    config, topology_catalog.single_path_spends(TaskStore(root).all_tasks())
                )
            ],
        )

    @app.get("/settings", response_model=SettingsView, tags=["ops"])
    def current(request: Request) -> SettingsView:
        """现在生效的配置里能从界面改的那几段,**加上"你改不改得动"**。

        没有这条路径的话,界面上的旋钮只能从空白开始——那不是"改一个已知状态",那是
        "重填一份配置",而重填会把人没打算动的字段一起写成默认值。

        **不拿员工管理页那三个 dial 顶替**:dial 是给人看的档位摘要(精化环那一项的值是
        合成出来的开/关),阈值与确认名单在里面根本不存在。拿它当表单的数据源,等于让前端
        从一个形状读、往另一个形状写。
        """
        can_edit = _may(request, Action.EDIT_SETTINGS)
        return SettingsView.of(load_config(_root(request)), can_edit=can_edit)

    @app.post("/settings/container-runtime/readiness", response_model=ReadinessView, tags=["ops"])
    async def container_runtime_readiness(request: Request) -> ReadinessView:
        """探一遍容器运行时这条链路,**分项报**。

        只读:它不改任何东西,所以它的意义正是"在改任何东西之前就知道通不通"。
        没配这个运行时时直接说没配——去探一个不存在的配置只会得到一堆无从解释的失败。
        """
        _ensure(request, Action.EDIT_SETTINGS)
        entry = load_config(_root(request)).runtime.runtimes.get(AGENTTEAMS_RUNTIME)
        if entry is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"这个工作区没有配置 {AGENTTEAMS_RUNTIME} 运行时——先在设置里"
                    "填上容器运行时那一段。"
                ),
            )
        checker = getattr(request.app.state, "readiness_checker", check_readiness)
        report = await checker(entry)
        return ReadinessView(
            ok=report.ok,
            items=[
                ReadinessItemView(name=item.name, ok=item.ok, detail=item.detail)
                for item in report.items
            ],
        )

    @app.put("/settings", response_model=SettingsChange, tags=["ops"])
    def edit(body: SettingsRequest, request: Request) -> SettingsChange:
        """改一段配置。**内容提交进 git,这次动作与操作人进事件面。**"""
        principal = _ensure(request, Action.EDIT_SETTINGS)
        try:
            change = update_settings(
                _root(request),
                principal,
                body.section,
                body.value,
                entrance=_entrance_of(request),
            )
        except NotEditable as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except Forbidden as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except GenomeValidationError as error:
            raise HTTPException(
                status_code=400,
                detail="；".join(issue.render() for issue in error.issues),
            ) from error
        except GitError as error:
            # **配置已经改回去了**(见 `server.settings`),所以这里说的是"没改成",不是
            # "改了但没记上"。不告诉人的话,他会以为保存成功了。
            raise HTTPException(
                status_code=503, detail=f"配置没能提交进版本库，这次修改已回滚: {error}"
            ) from error
        return _settings_change(change)

    def _container_employees(root: Path) -> list[Any]:
        """花名册里跑在容器运行时上的员工。**只有他们有 Worker 可言。**"""
        registry = load_employees(workspace_employees_root(root), strict=False)
        return [e for e in registry.all() if e.runtime == AGENTTEAMS_RUNTIME]

    def _provisioner(request: Request) -> WorkerProvisioner:
        """这次调用用哪条供应通道。

        默认按根配置装配——**与命令行同一处装配**,两份的话迟早对"配了什么"给出两个
        答案。测试从 `app.state` 换掉它,而不是去起一个真平台。
        """
        existing = getattr(request.app.state, "provisioner", None)
        if existing is not None:
            return existing  # type: ignore[no-any-return]
        try:
            return build_provisioner(load_config(_root(request)))
        except ProvisionUnavailable as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/employees/workers", response_model=WorkerStatusListView, tags=["ops"])
    async def worker_statuses(request: Request) -> WorkerStatusListView:
        """每个容器员工此刻的状态、Worker 名与房间。**只读,且每次去问平台**。

        不缓存是刻意的:真机实测 Worker 重建会换房间 id,而缓存下来的那个仍是一个格式
        正确的 id——发进去的消息不报错,只是没人在那边听。
        """
        root = _root(request)
        employees = _container_employees(root)
        rows = await survey_workers(_provisioner(request), employees) if employees else []
        return WorkerStatusListView(
            items=[
                WorkerStatusView(
                    employee_id=row.employee_id,
                    status=row.status,
                    worker=row.worker,
                    room=row.room,
                    detail=row.detail,
                )
                for row in rows
            ],
            can_provision=_may(request, Action.EDIT_SETTINGS),
        )

    @app.get("/employees/workers/plan", response_model=WorkerPlanView, tags=["ops"])
    async def worker_plan(request: Request) -> WorkerPlanView:
        """对齐一次**会**做什么。读一遍平台现状再算,**平台侧零写动作**。

        "将对齐"对管理员没有信息量:新建会真的拉起容器并花掉一次模型探活,更新不会——
        他要决定的正是这一点。
        """
        root = _root(request)
        config = load_config(root)
        employees = _container_employees(root)
        entry = config.runtime.runtimes.get(AGENTTEAMS_RUNTIME)
        tiers = tiers_from(entry) if entry is not None else {}
        rows = await plan_provision(_provisioner(request), employees, tiers) if employees else []
        return WorkerPlanView(
            items=[
                WorkerPlanRowView(employee_id=row.employee_id, action=row.action, detail=row.detail)
                for row in rows
            ],
            can_provision=_may(request, Action.EDIT_SETTINGS),
        )

    def _container_employee(root: Path, employee_id: str) -> Any:
        """点了名的这个员工,且必须是容器员工。

        跑在本地的**拒绝而不是静默跳过**——点了名却什么都没发生,使用者会以为成功了。
        与命令行 `_provision_targets` 同一条规矩。
        """
        try:
            employee = load_employees(workspace_employees_root(root)).get(employee_id)
        except EmployeeNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if employee.runtime != AGENTTEAMS_RUNTIME:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"员工 {employee_id} 跑在 {employee.runtime} 上,不是容器运行时——"
                    f"它没有 Worker 可言。要让它跑在容器里,先把它的运行时改成 "
                    f"{AGENTTEAMS_RUNTIME}。"
                ),
            )
        return employee

    @app.post("/employees/{employee_id}/worker", response_model=WorkerProvisionResult, tags=["ops"])
    async def provision_worker(employee_id: str, request: Request) -> WorkerProvisionResult:
        """把这个员工对齐成平台上一个就绪的 Worker。**幂等**,可反复点。

        ## 一次一个员工,不是一次一整份花名册

        供应是长动作:每建一个 Worker 都会真的拉起容器并触发一次模型探活(实测约十秒)。
        整份花名册由调用方逐个走完——那样进度是**真的**(第几个做完了),而不是一个转到
        底的圈;一个员工失败也天然不拖垮其余,不需要在服务端再造一套"部分成功"的语义。

        ## 权限沿用改设置那一个动作

        供应改的是这个部署的运行编制,与改根配置是同一类权力。新增一个权限项的代价是
        每个部署都要重新想一遍"谁该有它",而答案与"谁能改设置"完全一致。
        """
        principal = _ensure(request, Action.EDIT_SETTINGS)
        root = _root(request)
        employee = _container_employee(root, employee_id)
        try:
            outcome = await _provisioner(request).reconcile(employee)
        except (ProvisionError, PlatformUnavailable) as error:
            # 两种成因都指向平台,但指向的排查动作不同——原文照传,别合成一句。
            raise HTTPException(status_code=503, detail=str(error)) from error
        except UnknownModelTier as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        record_provision(
            root,
            actor=_actor_of(request, principal),
            employee_id=employee_id,
            ref=outcome.ref,
            action=outcome.action,
            entrance=_entrance_of(request),
        )
        return WorkerProvisionResult(
            employee_id=employee_id,
            action=outcome.action,
            worker=outcome.ref.name,
            room=outcome.ref.room_id,
        )

    async def _recycle(request: Request, employee_id: str, action: str) -> WorkerLifecycleResult:
        """休眠或删除。**两个动作一条路径**——所有权、留痕、错误映射只写一次。

        所有权由供应层守(名字前缀),不在这里再拼一次名字:拼错的那一次会去动别人的
        容器,而那正是"绝不触碰不是本系统供应的 Worker"要挡的事。
        """
        principal = _ensure(request, Action.EDIT_SETTINGS)
        root = _root(request)
        _container_employee(root, employee_id)
        provisioner = _provisioner(request)
        # **不可逆的那个不做默认分支。** 写成 `sleep if ... else delete` 的话,任何拼错的
        # 动作名都会落到删除上——而删除找不回来。
        calls = {"slept": provisioner.sleep, "deleted": provisioner.delete}
        call = calls[action]
        try:
            await call(employee_id)
        except (ProvisionError, PlatformUnavailable) as error:
            # **没做成就不记。** 事后按事件面复盘时,那条记录会指向一件没发生的事。
            raise HTTPException(status_code=503, detail=str(error)) from error
        record_lifecycle(
            root,
            actor=_actor_of(request, principal),
            employee_id=employee_id,
            action=action,
            entrance=_entrance_of(request),
        )
        return WorkerLifecycleResult(employee_id=employee_id, action=action)

    @app.post(
        "/employees/{employee_id}/worker/sleep",
        response_model=WorkerLifecycleResult,
        tags=["ops"],
    )
    async def sleep_worker(employee_id: str, request: Request) -> WorkerLifecycleResult:
        """让这个员工的容器休眠。容器停掉、资源还回去。

        **不需要二次确认。** 休眠可逆:休眠的 Worker 在下一次派发时自动唤醒(PRD 32),
        所以这是纯粹的成本动作,不会变成"任务莫名不动了"。给可逆动作加确认,只会训练人
        闭着眼点确认——而删除那一个真的需要他看清。
        """
        return await _recycle(request, employee_id, "slept")

    @app.delete(
        "/employees/{employee_id}/worker", response_model=WorkerLifecycleResult, tags=["ops"]
    )
    async def delete_worker(
        employee_id: str, request: Request, confirm: bool = False
    ) -> WorkerLifecycleResult:
        """删掉这个员工的容器。**不可逆**,所以要显式确认。

        `confirm=true` 不是给界面用的仪式:重建会换掉房间,而房间 id 是不落盘的——真机
        实测过。确认放在服务端,是因为界面上的二次确认弹窗**不是边界**:一条手滑的 curl
        一样能删,而删掉之后没有任何东西能把那个房间找回来。
        """
        if not confirm:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"删除 {employee_id} 的 Worker 不可逆(重建会换房间 id),"
                    "要删请带上 confirm=true。休眠是可逆的那个,用 /worker/sleep。"
                ),
            )
        return await _recycle(request, employee_id, "deleted")

    @app.get("/employees/{employee_id}/runtime", response_model=RuntimeChoiceView, tags=["ops"])
    def runtime_choice(
        employee_id: str, request: Request, candidate: str = ""
    ) -> RuntimeChoiceView:
        """这个员工跑在哪儿、能跑在哪儿,以及换过去还差哪些兼容声明。**只读**。"""
        root = _root(request)
        try:
            employee = load_employees(workspace_employees_root(root)).get(employee_id)
        except EmployeeNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        options = machine_runtimes(load_config(root))
        # **兼容缺口不是派发前唯一那道闸。** 只报缺口的话,一个只读员工会走完
        # "选运行时 → 补声明 → 提交进 git"整条路,再撞上一句关于只读的报错。
        blocked = runtime_blocks(root, employee_id, options)
        # 接不了的运行时**不再列缺口**:两条互相拆台的提示只能留一条。列出缺口 + 给一个
        # 补声明按钮,说的是"照这几步做就能跑",而那是假的——照做只会把一个它永远跑不到的
        # 运行时写进版本化资产。
        blocked_names = {name for name, _ in blocked}
        gap = (
            compat_gap(root, employee_id, candidate)
            if candidate not in ("", *blocked_names)
            else []
        )
        return RuntimeChoiceView(
            current=employee.runtime,
            options=options,
            can_edit=_may(request, Action.EDIT_SETTINGS),
            compat_gap=gap,
            blocked=[RuntimeBlockView(runtime=name, reason=reason) for name, reason in blocked],
        )

    @app.put("/employees/{employee_id}/runtime", response_model=SettingsChange, tags=["ops"])
    def set_employee_runtime(
        employee_id: str, body: RuntimeChoiceRequest, request: Request
    ) -> SettingsChange:
        """把一个员工挪到另一个机器运行时上。**只管"跑在哪儿"**,不碰指派人与确认名单。"""
        principal = _ensure(request, Action.EDIT_SETTINGS)
        try:
            change = set_runtime(
                _root(request),
                principal,
                employee_id,
                body.runtime,
                entrance=_entrance_of(request),
            )
        except EmployeeNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RuntimeNotConfigured as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except GenomeValidationError as error:
            raise HTTPException(
                status_code=400,
                detail="；".join(issue.render() for issue in error.issues),
            ) from error
        except GitError as error:
            raise HTTPException(
                status_code=503, detail=f"没能提交进版本库，这次修改已回滚: {error}"
            ) from error
        return _settings_change(change)

    @app.post(
        "/employees/{employee_id}/runtime/compat",
        response_model=SettingsChange,
        tags=["ops"],
    )
    def declare_employee_compat(
        employee_id: str, body: CompatDeclareRequest, request: Request
    ) -> SettingsChange:
        """给这个员工的工序补上兼容声明。

        **显式动作**:切换运行时时不会自动发生——自动补等于把兼容闸变成摆设。
        """
        principal = _ensure(request, Action.EDIT_GENOME)
        root = _root(request)
        try:
            # **藏起按钮不是边界。** 一个接不了这个员工的运行时,补声明只会往版本化资产
            # 里写一句不成立的话,而且提交进 git——所以在服务端也拒一次。
            refused = runtime_blocks(root, employee_id, [body.runtime])
            if refused:
                raise HTTPException(
                    status_code=422,
                    detail=f"{refused[0][1]}。补兼容声明改变不了这一点,那是另一道闸。",
                )
            # **算差距也在 try 里。** 它同样会因为"没有这个员工"而抛,而摆在外面的话,
            # 同一个未知 id 带 `procedures` 时是 404、不带时是 500。
            targets = body.procedures or compat_gap(root, employee_id, body.runtime)
            change = declare_compat(
                root, principal, targets, body.runtime, entrance=_entrance_of(request)
            )
        except EmployeeNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except GitError as error:
            raise HTTPException(
                status_code=503, detail=f"没能提交进版本库，这次修改已回滚: {error}"
            ) from error
        return _settings_change(change)

    @app.put("/employees/{employee_id}/execution", response_model=SettingsChange, tags=["ops"])
    def set_rung(employee_id: str, body: ExecutionRequest, request: Request) -> SettingsChange:
        """把一个员工挪到 auto / assisted / manual 里的一档。

        **三档住在两个存储上,但这里只有一个动作。** 分派由服务端做:摊给调用方的话,
        命令行、界面、脚本各拼一次,而拼错的那一处会造出"human 却又在确认名单里"这种
        谁都解释不了的状态。
        """
        principal = _ensure(request, Action.EDIT_SETTINGS)
        try:
            change = set_execution(
                _root(request),
                principal,
                employee_id,
                body.execution,
                assignee=body.assignee,
                entrance=_entrance_of(request),
            )
        except EmployeeNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (UnknownRung, NeedsAssignee) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except GenomeValidationError as error:
            # 写坏的定义会在下一次启动时让整个工作区起不来,所以这道校验挡在落盘之后、
            # 提交之前,文件已经改回去了。
            raise HTTPException(
                status_code=400,
                detail="；".join(issue.render() for issue in error.issues),
            ) from error
        except GitError as error:
            raise HTTPException(
                status_code=503, detail=f"没能提交进版本库，这次修改已回滚: {error}"
            ) from error
        return _settings_change(change)

    @app.get("/settings/history", response_model=list[SettingsChange], tags=["ops"])
    def audit(request: Request) -> list[SettingsChange]:
        """谁改过配置。**要审计权限**——它回答的是"谁调的并发数",跟事件检索是同一类问题。"""
        _ensure(request, Action.EXPORT_AUDIT)
        return [_settings_change(item) for item in settings_history(_root(request))]


def _settings_change(change: Change) -> SettingsChange:
    return SettingsChange(
        actor=change.actor,
        section=change.section,
        at=change.at,
        entrance=change.entrance.value,
        rev=change.rev,
    )


def _mount_ops(app: FastAPI) -> None:
    @app.get("/health", response_model=Health, tags=["ops"])
    def health() -> Health:
        """**不依赖数据库以外的任何东西。** 健康检查挂在一个可选依赖上,等于把可选变必需。"""
        return Health()

    @app.get("/workspaces", response_model=WorkspaceList, tags=["ops"])
    def workspaces(request: Request) -> WorkspaceList:
        """能切到哪几个工作空间。

        没有它的话,"按工作空间过滤"只能靠人手打名字——而打错一个字的结果是一页空的检索
        结果,与"这个空间里真的没有记录"分不开。
        """
        registry: WorkspaceRegistry = request.app.state.workspaces
        return WorkspaceList(
            items=registry.names(),
            entries=[
                WorkspaceEntry(
                    name=name, initializing=workspace_initializing(registry.entries[name])
                )
                for name in registry.names()
            ],
        )

    @app.post("/workspaces", response_model=WorkspaceCreated, status_code=201, tags=["ops"])
    async def create_workspace(body: WorkspaceCreateRequest, request: Request) -> WorkspaceCreated:
        """界面上建项目:骨架同步就位,clone 作为 MOUNT 基因组任务异步跑。

        同步的那一半快且不碰网络:建骨架、写注册表、记事件——响应返回时项目已经在
        切换器里。慢的那一半(clone)有人在等结果:失败进异常队列,进度看基因组任务页。

        **同名目录已经是一个能加载的 Workspace 时,认领它而不是 409。** 这种孤儿的典型
        来源是"内存注册表 + 重启":单项目模式下建的项目重启后从切换器消失,磁盘目录还
        好好的——那时用户手里唯一的按钮就是再点一次"创建",把他挡在"目标位置已存在"
        上等于把恢复的路也堵死。认领只做注册,不动目录里的任何东西;挂载没完成的接着挂。
        """
        principal = _ensure(request, Action.EDIT_SETTINGS)
        registry: WorkspaceRegistry = request.app.state.workspaces
        if body.name in registry.entries:
            raise HTTPException(status_code=409, detail=f"已有这个项目: {body.name}")
        root = Path(request.app.state.workspaces_home) / body.name
        adopted = (root / paths.ROOT_CONFIG).is_file()
        try:
            # 先校验名字形状再动磁盘:名字会进目录路径,路径分隔符与 `..` 是穿越入口。
            check_workspace_name(body.name)
            if adopted:
                # 真的能加载才认领。加载不了的目录不该被静默接进来——那是把一个
                # 起不来的 Workspace 挂上切换器,第一个请求就 500。
                existing_config = load_config(root)
                configure_workspace_remote(
                    root, body.workspace_repo, existing_config.platform.protected_branch
                )
            else:
                specs = plan_repos(body.repos)
                init_workspace(
                    root,
                    body.name,
                    specs,
                    mount_repos=False,
                    workspace_remote=body.workspace_repo,
                )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except WorkspaceExistsError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except WorkspaceRemoteFailed as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except GenomeValidationError as error:
            raise HTTPException(
                status_code=409,
                detail=f"目标位置已存在但加载不了,不能认领:{error.render()}",
            ) from error
        registry.register(body.name, root)
        # **没配注册表文件也要留痕(落缺省位置)。** 只注册在内存的项目会在重启后从
        # 切换器消失而磁盘还在——这个端点自己修的就是那种孤儿,不能再生产它。
        save_registry(request.app.state.registry_path or DEFAULT_REGISTRY, registry)
        EventLog(root).append(
            SYSTEM_SUBJECT,
            actor=_actor_of(request, principal),
            kind=LogKind.WORKSPACE_CHANGED,
            payload={"action": "adopt" if adopted else "create", "name": body.name},
        )
        # 认领时沿用目录里原有的挂载计划,不拿这次表单里的仓库清单覆盖——计划是当初
        # 创建时的事实,已挂上的部分就是按它挂的。全部挂完就不再建挂载任务。
        if adopted and not workspace_initializing(root):
            return WorkspaceCreated(name=body.name, mount_task_id=None, adopted=True)
        record = GenomeTaskStore(root).create(
            title="挂载业务仓", kind=GenomeTaskKind.MOUNT, origin=Origin.HUMAN
        )
        _spawn_mounts(request, root, record.id)
        return WorkspaceCreated(name=body.name, mount_task_id=record.id, adopted=adopted)

    @app.post(
        "/workspaces/{name}/mount",
        response_model=GenomeTaskSummary,
        status_code=202,
        tags=["ops"],
    )
    async def remount_workspace(name: str, request: Request) -> GenomeTaskSummary:
        """重试挂载:把计划里还没挂上的仓再跑一遍。修好凭证或远端之后从这里收尾。"""
        _ensure(request, Action.EDIT_SETTINGS)
        registry: WorkspaceRegistry = request.app.state.workspaces
        try:
            root = registry.resolve(name)[1]
        except (UnknownWorkspace, AmbiguousWorkspace) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if not workspace_initializing(root):
            raise HTTPException(status_code=409, detail="没有待挂载的仓,这个项目已经就绪。")
        if f"{root}|mount" in request.app.state.task_runs:
            raise HTTPException(status_code=409, detail="挂载正在进行,等它跑完。")
        record = GenomeTaskStore(root).create(
            title="挂载业务仓(重试)", kind=GenomeTaskKind.MOUNT, origin=Origin.HUMAN
        )
        _spawn_mounts(request, root, record.id)
        return _genome_summary(record, overdue=False)

    @app.delete("/workspaces/{name}", response_model=WorkspaceList, tags=["ops"])
    def delete_workspace(name: str, request: Request) -> WorkspaceList:
        """注销一个项目:只摘注册表,不动磁盘。

        真删除是 `rm -rf` 级别的不可逆动作,不给它做按钮——要删去服务器上删,审计包的
        归档根本来就设计成在 Workspace 之外幸存。
        """
        principal = _ensure(request, Action.EDIT_SETTINGS)
        registry: WorkspaceRegistry = request.app.state.workspaces
        try:
            root = unregister_workspace(
                registry,
                name,
                registry_path=request.app.state.registry_path,
                actor=_actor_of(request, principal),
            )
        except UnknownWorkspace as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        # 丢掉这个工作区的缓存(执行池、会话服务)。留着的话,同名重注册会拿到指向
        # 旧路径的池;进行中的后台推进不强杀,让它跑完——文件都还在。
        for cache_name in ("task_pools", "session_services", "session_runtimes"):
            cache = getattr(request.app.state, cache_name, None)
            if isinstance(cache, dict):
                cache.pop(str(root), None)
        return WorkspaceList(items=registry.names())

    @app.get("/api/version", response_model=Version, tags=["ops"])
    def version() -> Version:
        return Version(version=package_version, api=API_VERSION)

    @app.get("/metrics", response_class=PlainTextResponse, tags=["ops"])
    def metrics(request: Request) -> PlainTextResponse:
        snapshot = metrics_module.collect(_root(request))
        return PlainTextResponse(metrics_module.render(snapshot))


# --- 基因组管理(PRD 12)------------------------------------------------------


def _mount_genome(app: FastAPI) -> None:
    @app.get("/genome/project-map", response_model=ProjectMapResponse, tags=["genome"])
    def project_map(request: Request) -> ProjectMapResponse:
        root = _root(request)
        try:
            found = load_project_map(root)
        except GenomeValidationError as error:
            raise HTTPException(status_code=422, detail=error.render()) from error
        return ProjectMapResponse(
            version=found.version,
            updated_at=found.updated_at.isoformat() if found.updated_at else None,
            project_name=found.project.name,
            modules=[
                ModuleNode(
                    id=module.id,
                    path=module.path,
                    lang=module.lang,
                    summary=module.summary,
                    depends_on=module.depends_on,
                    confidence=module.confidence,
                    doc=module.doc,
                )
                for module in found.modules
            ],
            interfaces=[
                InterfaceEdge(
                    id=interface.id,
                    kind=interface.kind,
                    provider=interface.provider,
                    consumers=interface.consumers,
                    confidence=interface.confidence,
                )
                for interface in found.interfaces
            ],
        )

    @app.get("/genome/project-map/versions", response_model=ProjectMapVersionList, tags=["genome"])
    def project_map_versions(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        asset: str = Query(default="knowledge", description="knowledge / rules / procedures"),
    ) -> ProjectMapVersionList:
        """某一层基因组资产的改动历史。

        **三层都要能回溯。** 只给知识那一层的话,"规则是什么时候被谁改成这样的"在界面上
        无解——而规则层恰恰是唯一能大范围改变系统行为的杠杆。
        """
        try:
            found = project_map_history.list_versions(_root(request), limit=limit, asset=asset)
        except project_map_history.UnknownAsset as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except GitError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return ProjectMapVersionList(
            items=[ProjectMapVersionItem(**item.as_dict()) for item in found]
        )

    @app.get("/genome/project-map/diff", response_model=ProjectMapDiffResponse, tags=["genome"])
    def project_map_diff(
        request: Request,
        from_rev: str = Query(alias="from", min_length=1),
        to_rev: str = Query(alias="to", min_length=1),
        asset: str = Query(default="knowledge", description="knowledge / rules / procedures"),
    ) -> ProjectMapDiffResponse:
        try:
            found = project_map_history.diff(_root(request), from_rev, to_rev, asset=asset)
        except project_map_history.UnknownAsset as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except GitError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return ProjectMapDiffResponse(from_rev=found.from_rev, to_rev=found.to_rev, diff=found.diff)

    @app.get("/genome/tasks", response_model=GenomeTaskList, tags=["genome"])
    def genome_tasks(
        request: Request,
        kind: str = Query(default="", description="init / reinit / distill / backfill"),
        state: str = Query(default=""),
        settled: bool = Query(
            default=False, description="连已了结的一起列。缺省只列人还需要看见的"
        ),
    ) -> GenomeTaskList:
        """列出基因组任务。

        **缺省只列未了结的。** 全量列表里,系统自发的蒸馏失败会随时间累积成一大堆没人需要
        处理的记录,把真正在跑的那几个淹没——"人还用不用管它"的判定在 `GenomeTask.is_settled`
        一处,这里只选用哪个查询。
        """
        wanted_kind = _parse_enum(GenomeTaskKind, kind, "kind")
        wanted_state = _parse_enum(GenomeTaskState, state, "state")
        root = _root(request)
        store = GenomeTaskStore(root)
        found = store.all_tasks(kind=wanted_kind) if settled else store.unsettled_tasks()
        if wanted_kind is not None:
            found = tuple(task for task in found if task.kind is wanted_kind)
        if wanted_state is not None:
            found = tuple(task for task in found if task.state is wanted_state)
        overdue = {task.id for task in overdue_confirmations(found, _reminder_cutoff(root))}
        return GenomeTaskList(
            items=[_genome_summary(task, overdue=task.id in overdue) for task in found]
        )

    @app.get("/genome/tasks/{task_id}", response_model=GenomeTaskSummary, tags=["genome"])
    def genome_task(task_id: str, request: Request) -> GenomeTaskSummary:
        root = _root(request)
        try:
            task = GenomeTaskStore(root).get(task_id)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        cutoff = _reminder_cutoff(root)
        return _genome_summary(task, overdue=bool(overdue_confirmations((task,), cutoff)))

    @app.get("/genome/tasks/{task_id}/trace", response_model=TaskTrace, tags=["genome"])
    def genome_task_trace(task_id: str, request: Request) -> TaskTrace:
        """基因组作业的执行轨迹:深读的员工真正读了什么、调了什么工具、说了什么。

        与研发任务的 `/tasks/{id}/trace` 是同一份数据形状,但**产物目录的铺法不同**:
        基因组作业按模块铺目录,不走槽位编址,所以读取器是 `read_module_trace`。
        """
        root = _root(request)
        try:
            GenomeTaskStore(root).get(task_id)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return TaskTrace(
            task_id=task_id,
            stages=[
                TaskTraceStage(
                    stage=item.stage,
                    number=item.number,
                    blocks=[BlockItem(**block.as_dict()) for block in item.blocks],
                )
                for item in read_module_trace(task_dir(root, task_id))
            ],
        )

    @app.get("/genome/tasks/{task_id}/progress", response_model=GenomeTaskProgress, tags=["genome"])
    def genome_task_progress(task_id: str, request: Request) -> GenomeTaskProgress:
        """这个初始化跑到哪了、哪几个模块挂了。

        **失败的模块要带原因。** 只说"三个失败了"的话,人的下一步是去翻日志——而这个页面
        存在的理由就是让他不必翻。
        """
        root = _root(request)
        try:
            GenomeTaskStore(root).get(task_id)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        # 知识 PR 从事件面读:那条事件本来就只记指针(见 `core.events` 的平面分工表),
        # 而"改成了什么"去那个 PR 里看。
        prs = [
            str(event.payload.get("pr", {}).get("number", ""))
            for event in EventLog(root).all_events(task_id=task_id, kind=LogKind.GENOME_PR)
        ]
        found = read_progress(root, task_id)
        if found is None:
            return GenomeTaskProgress(task_id=task_id, started=False, pull_requests=prs)
        failed = {item.module_id: item for item in found.failed}
        rows = [
            ModuleProgress(module_id=item, status="done", duration_s=found.timing.get(item, 0.0))
            for item in found.done
        ]
        rows += [
            ModuleProgress(
                module_id=item.module_id,
                status="failed",
                detail=item.detail,
                duration_s=item.duration_s,
            )
            for item in failed.values()
        ]
        rows += [ModuleProgress(module_id=item, status="pending") for item in found.pending]
        return GenomeTaskProgress(task_id=task_id, started=True, modules=rows, pull_requests=prs)

    @app.post("/genome/tasks/{task_id}/cancel", response_model=GenomeTaskSummary, tags=["genome"])
    def genome_task_cancel(task_id: str, request: Request) -> GenomeTaskSummary:
        """取消一个基因组任务。

        **不只是"叫停跑飞的任务"。** 同一个模块同时只允许一个基因组任务,而停在待确认的任务
        不是终态——没有这条路的话,一个没人回答的闸门会把那个模块永久堵住:既跑不完,也
        重建不了。
        """
        principal = _ensure(request, Action.EDIT_GENOME)
        root = _root(request)
        store = GenomeTaskStore(root)
        try:
            store.get(task_id)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        # **记那个人,不记编排器。** 取消是一次人为介入,而"人对系统的每次介入都可见"是
        # 活动流那一页的全部意义。
        applied = GenomeDriver(store, EventLog(root)).deliver(
            task_id, GenomeEvent.CANCEL, actor=_actor_of(request, principal)
        )
        if not applied.moved:
            raise HTTPException(status_code=409, detail=applied.decision.reason)
        return _genome_summary(applied.task, overdue=False)

    @app.post(
        "/genome/tasks/{task_id}/intervention/resolve",
        response_model=GenomeTaskSummary,
        tags=["genome"],
    )
    def resolve_genome_task_intervention(
        task_id: str, body: InterventionResolveRequest, request: Request
    ) -> GenomeTaskSummary:
        principal = _ensure(request, Action.RESOLVE_INTERVENTION)
        try:
            task = resolve_genome(
                _root(request),
                task_id,
                actor=_actor_of(request, principal),
                note=body.note,
            )
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InterventionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        _publish(request, task_id, "intervention_resolved")
        return _genome_summary(task, overdue=False)

    @app.post(
        "/genome/tasks/init",
        response_model=GenomeTaskSummary,
        status_code=201,
        tags=["genome"],
    )
    def genome_init(request: Request) -> GenomeTaskSummary:
        """发起知识初始化:扫描 → 建任务 → 草案落盘 → 停在闸门。

        **与 `agctl knowledge plan` 共用同一层**(`genome.init_entry.plan_init`),
        闸门确认与进度都在既有页面——这个端点只是把那条流水线的起点接进界面。
        """
        principal = _ensure(request, Action.EDIT_GENOME)
        root = _root(request)
        try:
            planned = plan_init(root, _reminder_config(root), actor=_actor_of(request, principal))
        except InitAlreadyOpen as error:
            # 409:重试同样的请求不会成功;下一步是去答(或取消)在跑的那个。
            raise HTTPException(status_code=409, detail=str(error)) from error
        except NotReadyForBoundaries as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _genome_summary(planned.task, overdue=False)

    @app.post("/genome/tasks/reinit", response_model=GenomeTaskList, tags=["genome"])
    def genome_reinit(body: ReinitRequest, request: Request) -> GenomeTaskList:
        """对几个模块发起重建。**跳过扫描、划分与闸门**——边界已经拍过板了。

        与命令行走同一层的判断:模块要在项目地图里(凭空发明模块会让下游的影响判定失去依据),
        同一个模块上不能已经有在跑的任务。
        """
        _ensure(request, Action.EDIT_GENOME)
        root = _root(request)
        try:
            known = load_project_map(root).module_ids()
        except GenomeValidationError as error:
            raise HTTPException(status_code=422, detail=error.render()) from error
        unknown = sorted(set(body.modules) - known)
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"项目地图里没有这些模块:{'、'.join(unknown)}"
            )
        store = GenomeTaskStore(root)
        budget = _reminder_config(root).per_task_tokens
        created = []
        for module_id in body.modules:
            try:
                created.append(
                    store.create(
                        title=f"按模块重建:{module_id}",
                        kind=GenomeTaskKind.REINIT,
                        origin=Origin.HUMAN,
                        subject=module_id,
                        budget_tokens=budget,
                    )
                )
            except ModuleBusy as error:
                # 409:这个模块上已经有一个在跑,重试同样的请求不会成功,除非那个先结束。
                raise HTTPException(status_code=409, detail=str(error)) from error
        return GenomeTaskList(items=[_genome_summary(task, overdue=False) for task in created])

    @app.get("/genome/tasks/{task_id}/gate", response_model=GateDraft, tags=["genome"])
    def genome_gate_draft(task_id: str, request: Request) -> GateDraft:
        """看这个基因组任务的边界草案。**先看再答是常态。**

        已经答过的话,返回的是**答复里的那份列表**而不是原始草案:人回来复看时想看到的是
        自己提交的那一版,拿原草案糊他一脸等于把他的修改藏起来。
        """
        root = _root(request)
        try:
            task = GenomeTaskStore(root).get(task_id)
            draft = read_draft(root, task_id)
        except (TaskNotFound, NoDraft) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        answer = read_answer(root, task_id)
        source = answer if isinstance(answer, dict) and answer.get("modules") else draft
        return GateDraft(
            task_id=task_id,
            state=task.state.value,
            modules=[_boundary(item) for item in source.get("modules", [])],
            note=str(draft.get("note", "")),
            answered=answer is not None,
        )

    @app.post("/genome/tasks/{task_id}/gate", response_model=GateResult, tags=["genome"])
    async def genome_gate_confirm(task_id: str, body: GateAnswer, request: Request) -> GateResult:
        """回答闸门,然后**机器自己往下走**。

        **与命令行写的是同一份答复文件。** 两条入口各写各的话,「哪一份算数」会变成一个
        答不上来的问题——而这个闸门存在的全部理由,就是让人从任何入口都能推进它。

        答复推动了状态就当场接上后台驱动:词表对「待确认」的承诺是"人一答复,机器自己就
        往下走",此前这后半句只在 CLI 里成立——网页上答完闸门,任务停在 DEEP_READ 干等
        一条没人知道要敲的命令。
        """
        principal = _ensure(request, Action.EDIT_GENOME)
        root = _root(request)
        driver = GenomeDriver(
            GenomeTaskStore(root),
            EventLog(root),
            enforce_budget=load_config(root).budgets.enforce,
        )
        try:
            # **`exclude_defaults` 而不是整份 dump。** 落盘的答复要贴着人真的给了什么;
            # 把没填的字段补成空串写进去,与命令行那条入口写出的文件就不再是同一份,而
            # 「哪一份算数」正是这个闸门最不该出现的问题。
            applied = driver.confirm(
                task_id,
                body.model_dump(exclude_defaults=True),
                actor=_actor_of(request, principal),
            )
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (NoDraft, NotWaiting) as error:
            # 409:任务本身没问题,只是现在没有闸门要回答——重试同样的请求也不会成功,
            # 除非任务的状态先变。
            raise HTTPException(status_code=409, detail=str(error)) from error
        except AnswerInvalid as error:
            # 422 而不是 500:人答错了不是系统出错,任务也没失败——它还在待确认等下一次。
            raise HTTPException(status_code=422, detail=str(error)) from error
        if applied.moved and not applied.task.is_terminal:
            # 配置读不出来时闸门答复本身仍然成立;推进留给「继续推进」按钮,那条路
            # 会把装配错误原样报给人,而不是让答复 500。
            with contextlib.suppress(GenomeValidationError):
                _spawn_genome_drive(request, root, task_id, load_config(root))
        return GateResult(task_id=task_id, moved=applied.moved, state=applied.task.state.value)

    @app.post(
        "/genome/tasks/{task_id}/run",
        response_model=GenomeTaskSummary,
        status_code=202,
        tags=["genome"],
    )
    async def genome_run(task_id: str, request: Request) -> GenomeTaskSummary:
        """把一个基因组任务的推进接上后台驱动。

        闸门答复后的自动驱动断了(进程重启、当时配置坏了)时从这里接回——**待确认的任务
        不收**:推它等于替人回答,而那个闸门存在的全部理由就是让人看一眼。
        """
        _ensure(request, Action.EDIT_GENOME)
        root = _root(request)
        try:
            task = GenomeTaskStore(root).get(task_id)
        except TaskNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if task.is_terminal:
            raise HTTPException(
                status_code=409, detail=f"{task_id} 已经终结({task.state.value}),没有下一步。"
            )
        if task.state is GenomeTaskState.AWAITING_CONFIRMATION:
            raise HTTPException(
                status_code=409, detail=f"{task_id} 在等人确认——去答闸门,推它等于替人回答。"
            )
        try:
            config = load_config(root)
        except GenomeValidationError as error:
            raise HTTPException(status_code=422, detail=error.render()) from error
        if not _spawn_genome_drive(request, root, task_id, config):
            raise HTTPException(status_code=409, detail=f"{task_id} 正在推进中,等它跑完。")
        return _genome_summary(task, overdue=False)

    @app.get("/genome/lessons", response_model=LessonList, tags=["genome"])
    def lessons(
        request: Request,
        q: str = Query(default=""),
        module: str = Query(default=""),
        min_hits: int = Query(default=0, ge=0),
        status: Literal["active", "archived", "all"] = Query(default="active"),
        sort: Literal["hits", "created", "confidence"] = Query(default="hits"),
    ) -> LessonList:
        root = _root(request)
        lessons_dir = root / paths.LESSONS
        found = []
        if status in ("active", "all"):
            found += load_cards(lessons_dir)
        if status in ("archived", "all"):
            found += load_cards(lessons_dir / lesson_lifecycle.ARCHIVED_DIR)
        if module:
            found = [card for card in found if module in card.applies_to.modules]
        if q:
            needle = q.lower()
            found = [
                card
                for card in found
                if needle in card.title.lower() or needle in card.conclusion.lower()
            ]
        found = [card for card in found if card.hits >= min_hits]
        sort_key: dict[str, Any] = {
            "hits": lambda card: -card.hits,
            "created": lambda card: card.id,
            "confidence": lambda card: -card.confidence,
        }
        found.sort(key=sort_key[sort])
        return LessonList(items=[_lesson_response(card) for card in found], total=len(found))

    @app.post(
        "/genome/lessons", response_model=LessonCardResponse, status_code=201, tags=["genome"]
    )
    def add_lesson(body: LessonCreateRequest, request: Request) -> LessonCardResponse:
        _ensure(request, Action.EDIT_GENOME)
        root = _root(request)
        lessons_dir = root / paths.LESSONS
        applies_to = Applicability(
            modules=body.modules, path_globs=body.path_globs, scenario=body.scenario
        )
        if applies_to.is_empty():
            raise HTTPException(
                status_code=422,
                detail="适用条件不能全空——会被每次切片选中,然后把真正相关的内容淹掉",
            )
        if not body.evidence:
            raise HTTPException(
                status_code=422,
                detail="没有证据链接——防污染的第一道,人工添加同样要过",
            )
        card = LessonCard(
            id=f"L-{next_number(load_cards(lessons_dir)):04d}",
            title=body.title,
            applies_to=applies_to,
            conclusion=body.conclusion,
            evidence=[
                Evidence(task_id=item.task_id, path=item.path, note=item.note)
                for item in body.evidence
            ],
            confidence=body.confidence,
            level=Level.L1,
            created_from="manual",
        )
        lesson_lifecycle.add_manual(lessons_dir, card)
        return _lesson_response(card)

    @app.post(
        "/genome/lessons/{card_id}/deprecate", response_model=LessonCardResponse, tags=["genome"]
    )
    def deprecate_lesson(card_id: str, request: Request) -> LessonCardResponse:
        _ensure(request, Action.EDIT_GENOME)
        root = _root(request)
        lessons_dir = root / paths.LESSONS
        card = next((item for item in load_cards(lessons_dir) if item.id == card_id), None)
        if card is None:
            raise HTTPException(status_code=404, detail=f"没有这张卡片: {card_id}")
        lesson_lifecycle.archive(lessons_dir, card)
        return _lesson_response(card.model_copy(update={"archived": True}))

    @app.post(
        "/genome/lessons/{card_id}/restore", response_model=LessonCardResponse, tags=["genome"]
    )
    def restore_lesson(card_id: str, request: Request) -> LessonCardResponse:
        _ensure(request, Action.EDIT_GENOME)
        root = _root(request)
        lessons_dir = root / paths.LESSONS
        try:
            lesson_lifecycle.restore(lessons_dir, card_id)
        except lesson_lifecycle.CardNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        card = next(item for item in load_cards(lessons_dir) if item.id == card_id)
        return _lesson_response(card)

    @app.get("/genome/rules", response_model=RuleSetResponse, tags=["genome"])
    def rules(request: Request) -> RuleSetResponse:
        root = _root(request)
        try:
            found = load_rules(root)
        except GenomeValidationError as error:
            raise HTTPException(status_code=422, detail=error.render()) from error
        return RuleSetResponse(
            architecture=found.architecture, protected=found.protected, impact=found.impact
        )

    @app.post("/genome/rules/proposal", response_model=RuleProposalResponse, tags=["genome"])
    def propose_rule_change(body: RuleProposalRequest, request: Request) -> RuleProposalResponse:
        """**提交生成 PR,不直接写入。** 规则层是唯一能大范围改变行为的杠杆,它必须握在
        人手里——界面编辑的价值只在"改的时候就拦住语法与引用错误",合并权仍然在人工审批。
        """
        principal = _ensure(request, Action.EDIT_GENOME)
        root = _root(request)
        # **配置了身份表之后,记进事件的是认证身份,不是 body 里自报的名字。** 与审批那一处
        # 同一条:一个有权改规则的人能在 body 里填同事的名字,而事件面存在的全部理由就是记准
        # 这一位。没配身份表(单机开发)时行为不变——那种模式下 `body.actor` 本来就是唯一来源。
        actor = principal.subject if request.app.state.principals else body.actor
        source_task_id = _known_task(root, body.source_task_id)
        forge, remote = _forge_and_remote(root)
        try:
            pr = submit_rule_change(
                remote,
                forge,
                RuleChangeRequest(
                    section=body.section,
                    payload=body.payload,
                    description=body.description,
                    actor=actor,
                    source_task_id=source_task_id,
                ),
                root,
            )
        except GenomeValidationError as error:
            raise HTTPException(status_code=422, detail=error.render()) from error
        except GitError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # **只记指针,不记内容。** 规则改成了什么在那个 PR 里,而事件面记的是"谁在什么时候
        # 为哪个任务提了这个提案"——把 payload 也抄一份进来的话,同一份内容会在两个平面上
        # 各存一份,然后随 PR 的修改慢慢对不上。
        EventLog(root).append(
            source_task_id or SYSTEM_SUBJECT,
            actor=actor,
            kind=LogKind.GENOME_PR,
            payload={
                "asset": "rules",
                "section": body.section,
                "pr": pr.as_dict(),
                "source_task_id": source_task_id,
            },
        )
        return RuleProposalResponse(repo=pr.repo, number=pr.number, head=pr.head, base=pr.base)

    @app.get("/genome/knowledge", response_model=KnowledgeStatus, tags=["genome"])
    def knowledge_status(
        request: Request, q: str = Query(default="", description="按功能点 id 或标题检索")
    ) -> KnowledgeStatus:
        """知识现在是什么状态:每模块建了多少、哪些要复核、哪些声明了不需要卡片。

        **「无需卡片」的声明要能被一眼扫到。** 带理由的声明也算完备(ADR-0003),但不摆出来
        的话,"不值得写"会变成偷懒的托词而没有人发现。
        """
        from agentgenome.genome.deepen import churn_counts, deepen_queue
        from agentgenome.genome.hits import LedgerUnreadable
        from agentgenome.genome.suspects import pending_suspects

        root = _root(request)
        try:
            tree = load_tree(root)
        except GenomeValidationError as error:
            # 知识树坏了要说清楚坏在哪。给一页空的话,"还没建知识"与"知识树读不出来"
            # 在界面上是同一个样子——而后者是要立刻修的。
            raise HTTPException(status_code=422, detail=error.render()) from error
        try:
            pending = pending_suspects(root)
        except LedgerUnreadable as error:
            # 账本坏了是服务端的数据问题,不是请求的问题——话要说全,它是要人修的。
            raise HTTPException(status_code=500, detail=str(error)) from error
        queue = deepen_queue(tree, churn_counts(root, tree.project_map.module_paths()))

        wanted = q.strip().lower()
        cards = [
            CardHit(
                module_id=module_id,
                feature_id=feature_id,
                title=card.summary,
                hits=card.hits,
                confidence=card.confidence.value if card.confidence else None,
            )
            for (module_id, feature_id), card in sorted(tree.cards.items())
            if not wanted or wanted in feature_id.lower() or wanted in card.summary.lower()
        ]
        modules = [
            ModuleKnowledge(
                module_id=module.id,
                features=len(tree.features(module.id)),
                cards=sum(1 for item in tree.features(module.id) if item.card),
                no_cards=sum(1 for item in tree.features(module.id) if item.no_card is not None),
                confidence=module.confidence,
            )
            for module in tree.project_map.modules
        ]
        return KnowledgeStatus(
            modules=modules,
            # **复核清单不受检索影响。** 它回答的是"你不能漏看这几条",而检索框里打了字
            # 就把它清空的话,一次找卡片的操作会顺手把那份清单藏起来。
            review=[
                CardHit(
                    module_id=module_id,
                    feature_id=feature_id,
                    title=card.summary,
                    hits=card.hits,
                    confidence=card.confidence.value if card.confidence else None,
                )
                for (module_id, feature_id), card in sorted(tree.cards.items())
                if card.confidence is Confidence.LOW
            ],
            no_cards=[
                NoCardDeclaration(
                    module_id=ref.module_id,
                    feature_id=ref.feature.id,
                    reason=ref.feature.no_card or "(没写理由)",
                )
                for ref in no_card_declarations(tree)
            ],
            cards=cards,
            # 可疑账与深化队列(PRD 41):**只读透出**,清账只走消费路径
            # (`knowledge deepen` / `knowledge suspects --resolve`)。
            suspects=[
                SuspectEntry(
                    kind=item.kind.value,
                    card=item.card,
                    task_id=item.task_id,
                    changed=list(item.changed),
                    round=item.round,
                )
                for item in pending
            ],
            deepen_queue=[
                DeepenQueueEntry(
                    card=f"{item.module_id}/{item.feature_id}",
                    summary=item.summary,
                    churn=item.churn,
                )
                for item in queue
            ],
        )

    @app.get("/genome/procedures/stats", response_model=ProcedureStatsList, tags=["genome"])
    def procedure_stats(request: Request) -> ProcedureStatsList:
        root = _root(request)
        registry = load_workspace_registry(root)
        log = EventLog(root)
        finished = log.all_events(kind=LogKind.JOB_FINISHED, limit=100_000)
        calls: dict[str, int] = {}
        failures: dict[str, int] = {}
        for event in finished:
            ref = str(event.payload.get("procedure_ref") or "")
            if not ref:
                continue
            calls[ref] = calls.get(ref, 0) + 1
            if not event.payload.get("ok"):
                failures[ref] = failures.get(ref, 0) + 1
        items = []
        for spec in registry.all():
            call_count = calls.get(spec.ref, 0)
            failure_count = failures.get(spec.ref, 0)
            items.append(
                ProcedureStat(
                    id=spec.id,
                    version=spec.version,
                    source=spec.source.value,
                    kind=spec.kind.value,
                    available=spec.available,
                    call_count=call_count,
                    failure_count=failure_count,
                    failure_rate=failure_count / call_count if call_count else 0.0,
                )
            )
        return ProcedureStatsList(items=items)


# --- 观测中心(PRD 12)--------------------------------------------------------


def _mount_insights(app: FastAPI) -> None:
    @app.get("/insights/trends", response_model=TrendReport, tags=["insights"])
    def trends(request: Request, window_days: int = Query(default=7, ge=1, le=90)) -> TrendReport:
        root = _root(request)
        now = datetime.now(UTC)
        window = timedelta(days=window_days)
        current = metrics_module.collect(root, since=now - window, until=now)
        previous = metrics_module.collect(root, since=now - 2 * window, until=now - window)
        report = weekly(
            period=f"最近 {window_days} 天",
            previous=_metric_values(previous),
            current=_metric_values(current),
            samples=[current.terminal_tasks] * 5,
        )
        return TrendReport(
            period=report.period,
            metrics=[TrendMetric(**metric.as_dict()) for metric in report.metrics],
            has_enough=report.has_enough,
        )

    @app.get("/insights/costs", response_model=CostReport, tags=["insights"])
    def costs(request: Request) -> CostReport:
        root = _root(request)
        snapshot = metrics_module.collect(root)
        by_employee: dict[str, int] = {}
        by_task: dict[str, int] = {}
        for (task_id, actor), used in snapshot.tokens.items():
            by_employee[actor] = by_employee.get(actor, 0) + used
            by_task[task_id] = by_task.get(task_id, 0) + used
        return CostReport(
            by_employee=sorted(
                (CostSlice(key=key, tokens=value) for key, value in by_employee.items()),
                key=lambda item: -item.tokens,
            ),
            by_task=sorted(
                (CostSlice(key=key, tokens=value) for key, value in by_task.items()),
                key=lambda item: -item.tokens,
            )[:20],
            total_tokens=sum(snapshot.tokens.values()),
        )

    @app.get("/insights/roster", response_model=RosterReport, tags=["insights"])
    def roster(request: Request) -> RosterReport:
        """七类员工的出场与花费,加上质量线三个旋钮的当前档位。

        **"质量线拧多紧"从此是一个可观测的事实**,不是一句配置注释。出场为 0 的员工照样
        列出来:"这个项目根本没用过对抗"与"页面上没有这一行"是完全不同的两件事。
        """
        root = _root(request)
        snapshot = metrics_module.collect(root)
        tokens: dict[str, int] = {}
        appearances: dict[str, int] = {}
        for (_task_id, actor), used in snapshot.tokens.items():
            tokens[actor] = tokens.get(actor, 0) + used
        for (_task_id, actor), count in snapshot.jobs.items():
            appearances[actor] = appearances.get(actor, 0) + count

        registry = load_employees(workspace_employees_root(root), strict=False)
        config = load_config(root)
        assisted = config.topology.assisted
        members = [
            RosterMember(
                id=employee.id,
                name=employee.display_name,
                runtime=employee.runtime,
                execution=employee.execution_mode(assisted.employees),
                assignee=employee.assignee,
                # 确认人为空时退回员工自己的指派人——配置里那个空串的含义就是这个,
                # 让前端再解释一遍的话,它会解释成"没人",而那是另一件事。
                confirmer=assisted.confirmer or employee.assignee,
                summary=_role_summary(employee.prompt_text),
                appearances=appearances.get(employee.id, 0),
                tokens=tokens.get(employee.id, 0),
            )
            for employee in registry.all()
        ]
        return RosterReport(employees=members, dials=_quality_dials(config))


def _role_summary(prompt: str) -> str:
    """提示词里第一段正文。**取现成的,不另写一份角色简介**——两份介绍迟早分叉,而分叉
    之后界面上那份会是错的那个(没人会为了改界面去读提示词)。

    跳过标题行:标题是"决策数字员工"这类名字,而名字那一列已经有了。
    """
    for raw in prompt.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            return line[:160]
    return ""


def _quality_dials(config: Config) -> list[QualityDial]:
    """三个旋钮当前拧在哪。**评审那一档从精化环配置读**,不在质量线配置节里另存一份。"""
    critique = config.topology.critique
    return [
        QualityDial(
            key="tester",
            value=config.quality_line.tester.value,
            note="dev=开发兼任 / dedicated=专职出题 / risk-based=命中受保护路径才专职",
        ),
        QualityDial(
            key="adversary",
            value=config.quality_line.adversary.value,
            note="off=不上场 / protected-hit=命中受保护路径才上 / always=每次都上",
        ),
        QualityDial(
            key="reviewer",
            value="on" if critique.enabled else "off",
            note="配置住在 topology.critique,这里只读展示",
        ),
    ]


def _mount_audit(app: FastAPI) -> None:
    @app.get("/audit/events", response_model=AuditEventPage, tags=["audit"])
    def audit_events(
        request: Request,
        task_id: str = Query(default=""),
        actor: str = Query(default=""),
        actor_kind: str = Query(default="", description="人 / 员工 / 编排器 / 门禁 / 集成入口"),
        kind: str = Query(default=""),
        kinds: str = Query(default="", description="一次查几类,逗号分隔"),
        since: str = Query(default="", description="ISO8601;不带时区按 UTC 补全"),
        until: str = Query(default="", description="ISO8601;不带时区按 UTC 补全"),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> AuditEventPage:
        _ensure(request, Action.EXPORT_AUDIT)
        since_at = _parse_query_time(since)
        until_at = _parse_query_time(until)
        # 认不出来的枚举值是**调用方写错了**,不是服务端坏了。不转的话它是 500,
        # 而 500 会让人去查服务端日志——那里什么都没有。
        wanted_kind = _parse_enum(LogKind, kind, "kind")
        wanted_actor_kind = _parse_enum(ActorKind, actor_kind, "actor_kind")
        wanted_kinds = tuple(
            parsed
            for item in kinds.split(",")
            if (parsed := _parse_enum(LogKind, item.strip(), "kinds")) is not None
        )
        root = _root(request)
        found = EventLog(root).all_events(
            task_id=task_id or None,
            actor=actor or None,
            actor_kind=wanted_actor_kind,
            kind=wanted_kind,
            kinds=wanted_kinds,
            since=since_at,
            until=until_at,
            limit=limit,
        )
        return AuditEventPage(
            items=[
                AuditEventItem(
                    task_id=event.task_id,
                    ts=event.ts.isoformat(),
                    actor=event.actor,
                    actor_kind=event.actor_kind.value,
                    kind=event.kind.value,
                    payload=event.payload,
                )
                for event in found[:limit]
            ]
        )

    @app.post("/audit/gaps", response_model=GapReportResponse, tags=["audit"])
    def audit_gaps(request: Request) -> GapReportResponse:
        """哪些提交直接改了配置却没有对应事件。

        **只报告,不拦截。** 直接改仓库是合法的运维手段;这个端点回答的是"谁绕过界面改的",
        不是"谁不许改"。**没有配套的定时任务**——定时跑出来的结果没人看,却会让人以为已经
        在监控了。需要定时的话由外部调度调它。

        **是 POST 不是 GET**:检测本身纯读,但"查过了"这条事件是要写的。做成 GET 的话,
        一个轮询的看板或者一次客户端重试就能把事件面刷满 `gap_scan`,而它们随后会出现在
        审计检索里、并占掉保留期——一次查询不该在被查的那份记录里留下这么大的印子。
        """
        principal = _ensure(request, Action.EXPORT_AUDIT)
        root = _root(request)
        report = gaps.detect(root)
        # "什么时候查过、查出什么"也要有记录。检测本身是纯读的,这条事件是显式的第二步。
        gaps.record_scan(root, report, principal.subject or ORCHESTRATOR)
        return GapReportResponse(
            watched=list(report.watched),
            commits=report.commits,
            gaps=[GapItem(**item.as_dict()) for item in report.gaps],
            notes=list(report.notes),
            unavailable=report.unavailable,
        )

    @app.get("/audit/export/{task_id}", tags=["audit"])
    def audit_export(task_id: str, request: Request) -> FileResponse:
        _ensure(request, Action.EXPORT_AUDIT)
        root = _root(request)
        try:
            # 与命令行走同一层:归档根按根配置解析,包里带同一份清单。各写各的话,
            # 从界面下的包和从命令行导的包会长成两种结构。
            package = export_task_bundle(root, task_id, load_config(root))
        except TaskNotArchivable as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(
            package.path, media_type="application/zip", filename=f"{task_id}-audit.zip"
        )


# --- 体验补齐(PRD 12)--------------------------------------------------------


def _requirement_view(requirement: Requirement, scene: RequirementScene) -> RequirementSummary:
    """把需求实体加尝试链摆成列表要的样子。详情在这份概览上加字段。

    读盘与状态推导都在 `core.requirement.RequirementScene`——server、CLI、编排器共用
    那一份(PRD 48 R3),这里只做摆盘。
    """
    attempts = scene.attempts(requirement.id)
    children = scene.children(requirement.id)
    return RequirementSummary(
        id=requirement.id,
        title=requirement.title,
        text=requirement.text,
        priority=requirement.priority,
        state=scene.states[requirement.id].value,
        parked=requirement.parked,
        attempts=len(attempts),
        parent_id=requirement.parent_id,
        blocked_by=list(requirement.blocked_by),
        children_total=len(children),
        children_delivered=sum(
            1 for child in children if scene.states[child.id] is RequirementState.DELIVERED
        ),
        created_at=requirement.created_at.isoformat(),
        updated_at=requirement.updated_at.isoformat(),
    )


def _mount_requirements(app: FastAPI) -> None:
    @app.get("/requirements", response_model=list[RequirementSummary], tags=["requirements"])
    def list_requirements(request: Request) -> list[RequirementSummary]:
        """全部需求,新的在前,状态与尝试数现算。

        任务只读一次、在内存里按需求分组——按需求逐个查任务的话,这个列表的成本会随
        需求数线性长出 N 次连接。
        """
        root = _root(request)
        scene = RequirementScene.load(root)
        return [_requirement_view(requirement, scene) for requirement in scene.requirements]

    @app.get(
        "/requirements/{requirement_id}", response_model=RequirementDetail, tags=["requirements"]
    )
    def requirement_detail(requirement_id: str, request: Request) -> RequirementDetail:
        root = _root(request)
        scene = RequirementScene.load(root)
        found = next((item for item in scene.requirements if item.id == requirement_id), None)
        if found is None:
            raise HTTPException(status_code=404, detail=f"没有这个需求: {requirement_id}")
        attempts = scene.attempts(requirement_id)
        return RequirementDetail(
            **_requirement_view(found, scene).model_dump(),
            chain=[
                AttemptView(
                    id=attempt.id,
                    state=attempt.state.value,
                    execution_status=_dev_execution_status(request, attempt),
                    escalate_reason=visible_escalation_reason(attempt),
                    tokens_used=attempt.tokens_used,
                    created_at=attempt.created_at.isoformat(),
                )
                for attempt in attempts
            ],
            total_tokens=sum(attempt.tokens_used for attempt in attempts),
            children=[
                RequirementChildView(
                    id=child.id,
                    title=child.title,
                    state=scene.states[child.id].value,
                    blocked_by=list(child.blocked_by),
                    parked=child.parked,
                    attempts=len(scene.attempts(child.id)),
                    last_attempt_state=(
                        scene.attempts(child.id)[-1].state.value if scene.attempts(child.id) else ""
                    ),
                )
                for child in scene.children(requirement_id)
            ],
            tree_tokens=scene.tree_tokens(requirement_id),
        )

    @app.patch(
        "/requirements/{requirement_id}", response_model=RequirementDetail, tags=["requirements"]
    )
    async def patch_requirement(
        requirement_id: str, body: RequirementPatch, request: Request
    ) -> RequirementDetail:
        """改文本/优先级、搁置、恢复。校验在服务层(`core.requirement.revise`),CLI 同一套。"""
        principal = _ensure(request, Action.SUBMIT_TASK)
        root = _root(request)
        try:
            revise(
                root,
                requirement_id,
                # 搁置与改文本是人的判断,审计要的恰恰是"是谁判断的"。
                actor=_actor_of(request, principal),
                text=body.text,
                priority=body.priority,
                park=body.park,
                resume=body.resume,
            )
        except RequirementNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if body.resume and RequirementStore(root).children_of(requirement_id):
            # 恢复解冻派发(PRD 48 issue 03):搁置期间可能有子需求交付过,被冻住的
            # 兄弟与收口在这里补上;serve 的表面顺手把它们推起来。平面需求没有这一步。
            config = load_config(root)
            orchestrator = Orchestrator(root, pool=_task_pool(request, root, config), config=config)
            orchestrator.sweep_requirement_tree(requirement_id)
            _drive_the_tree(request, root, requirement_id, config)
        return requirement_detail(requirement_id, request)

    @app.post(
        "/requirements/{requirement_id}/resplit", response_model=TaskDetail, tags=["requirements"]
    )
    def resplit_requirement(requirement_id: str, request: Request) -> TaskDetail:
        """对一棵树发起「重新拆分剩余」(PRD 48 D7):建一个重拆分提案任务并推进它。

        范围与拒绝的判断全在编排器(`start_resplit`),CLI 共用同一份——报错正文逐字相同。
        """
        principal = _ensure(request, Action.SUBMIT_TASK)
        root = _root(request)
        config = load_config(root)
        orchestrator = Orchestrator(root, pool=_task_pool(request, root, config), config=config)
        try:
            task = orchestrator.start_resplit(requirement_id, actor=_actor_of(request, principal))
        except RequirementNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        _publish(request, task.id, "task_created")
        _spawn_dev_drive(request, root, task.id, config)
        return TaskDetail.of(task)

    @app.post("/requirements/import", response_model=ImportResult, tags=["requirements"])
    def import_requirement(body: ImportRequest, request: Request) -> ImportResult:
        _ensure(request, Action.SUBMIT_TASK)
        try:
            found = import_ticket(body.url)
        except UnsupportedTicketUrl as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except OSError as error:
            raise HTTPException(status_code=502, detail=f"抓取工单失败: {error}") from error
        return ImportResult(title=found.title, body=found.body, source=found.source)

    @app.post("/webhooks/im", response_model=ImWebhookResponse, tags=["requirements"])
    def im_webhook(body: ImWebhookRequest, request: Request) -> ImWebhookResponse:
        """IM 机器人的入口:一条消息进来,创建一个需求,回一个任务链接。

        **不做身份校验**——`x-actor` 这套是给控制台用的,IM 机器人的调用方是平台自己
        (Slack/飞书的出站 webhook),真正的准入控制是"谁能在 IM 里 @ 这个机器人",
        不是这层 API。
        """
        root = _root(request)
        text = body.text.strip()
        if not text:
            return ImWebhookResponse(task_id=None, reply="需求内容是空的,没有创建任务")
        # IM 入口与提交页同一条路:先落成需求,再建第一次尝试(PRD 43「三入口统一」)。
        requirement = intake(
            root,
            title=_first_line(text),
            text=text,
            priority=5,
            actor=IM_ACTOR,
            actor_kind=ActorKind.INTEGRATION,
        )
        task = TaskStore(root).create(
            title=_first_line(text), requirement=text, priority=5, requirement_id=requirement.id
        )
        EventLog(root).append(
            task.id,
            # **不是编排器建的。** 与告警回调同一类:任务从平台的出站 webhook 进来,真正的
            # 提出人只在 payload 里(IM 侧的身份与这里的身份表对不上)。记成编排器的话,
            # "哪些任务是从集成入口进来的"这个问题查不出来。
            actor=IM_ACTOR,
            actor_kind=ActorKind.INTEGRATION,
            kind=LogKind.TASK_CREATED,
            payload={
                "title": task.title,
                "priority": task.priority,
                "requirement_id": requirement.id,
                "via": "im",
                "user": body.user,
            },
        )
        _publish(request, task.id, "task_created")
        return ImWebhookResponse(task_id=task.id, reply=f"已创建任务 {task.id}:{task.title}")


def _mount_notifications(app: FastAPI) -> None:
    @app.get(
        "/notifications/preferences",
        response_model=NotificationPreferenceList,
        tags=["requirements"],
    )
    def list_preferences(request: Request) -> NotificationPreferenceList:
        root = _root(request)
        found = notify_prefs.load_all(root)
        return NotificationPreferenceList(
            items=[
                NotificationPreference(
                    actor=item.actor, events=item.events, webhook_url=item.webhook_url
                )
                for item in found
            ]
        )

    @app.put(
        "/notifications/preferences",
        response_model=NotificationPreference,
        tags=["requirements"],
    )
    def put_preference(body: NotificationPreference, request: Request) -> NotificationPreference:
        root = _root(request)
        try:
            saved = notify_prefs.save(
                root,
                notify_prefs.Preference(
                    actor=body.actor, events=body.events, webhook_url=body.webhook_url
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return NotificationPreference(
            actor=saved.actor, events=saved.events, webhook_url=saved.webhook_url
        )


# --- 内部 -------------------------------------------------------------------


def _boundary(item: object) -> BoundaryModule:
    """草案文件里的一行 → 模型。

    **多出来的键丢掉,少的补空。** 草案是落在磁盘上的产物,它的形状会随生成它的那一层演进;
    严格按模型解析的话,一个多写了字段的旧草案会让整个闸门页面打不开——而那时人正等着回答它。

    草案里一个模块一个目录(`path`),答复里可以是几个(`paths`)——合并之后就是这样。
    两种写法都收,归一成列表。
    """
    row = item if isinstance(item, dict) else {}
    paths = row.get("paths")
    if not isinstance(paths, list):
        paths = [row["path"]] if row.get("path") else []
    return BoundaryModule(
        id=str(row.get("id") or ""),
        paths=[str(found) for found in paths],
        summary=str(row.get("summary") or ""),
        rationale=str(row.get("rationale") or ""),
    )


def _reminder_config(root: Path) -> GenomeTaskConfig:
    """基因组任务那一段配置。

    **读不出来退回默认值,不往上抛。** 配置坏掉的时刻恰恰最需要看见系统在干什么;让一个
    读路径因此整页 500,是把"配置有问题"放大成"什么都看不见"。
    """
    try:
        return load_config(root).genome_tasks
    except GenomeValidationError:
        return GenomeTaskConfig()


def _reminder_cutoff(root: Path) -> datetime:
    """闸门等多久开始提醒。"""
    return datetime.now(UTC) - timedelta(hours=_reminder_config(root).confirmation_reminder_hours)


def _genome_summary(task: GenomeTask, overdue: bool) -> GenomeTaskSummary:
    """一个基因组任务 → 接口模型。

    **一处映射。** 列表与详情各拼一遍的话,加字段时改一处漏一处,而漏掉的那一处只会表现为
    "在详情页里这个字段总是空的"。
    """
    payload = task.as_dict()
    return GenomeTaskSummary(**payload, overdue=overdue)


#: 待办里对外公开的字段。**白名单,不是黑名单**——用黑名单的话,以后给 `Todo` 加一个内部
#: 字段会自动出现在公开接口上,而没有任何地方会提醒这件事。
_TODO_PUBLIC = (
    "id",
    "task_id",
    "stage",
    "node",
    "assignee",
    "employee_id",
    "procedure_id",
    "kind",
    "state",
    "reminded",
    "reassignments",
    "created_at",
    "updated_at",
)


def _todo_fields(root: Path, todo: Todo) -> dict[str, Any]:
    """待办的公共字段。**一份映射**,列表与详情共用——各写一遍的话,详情里少一个字段
    没有任何症状,直到有人发现界面上那一栏一直是空的。
    """
    payload = todo.as_dict()
    fields = {key: payload[key] for key in _TODO_PUBLIC}
    fields["due_at"] = _due_at(root, todo)
    if todo.kind == SPLIT_TODO:
        # 拆分待办的"活"就是读提案本身:不带提案的话,人要去产物目录里翻 result.json
        # 才知道自己在裁决什么。
        fields["proposal"] = _split_proposal_of(root, todo)
    return fields


def _split_proposal_of(root: Path, todo: Todo) -> dict[str, Any] | None:
    """这张拆分待办对应的提案(计划槽里的 `result.json` 的 `split` 一节)。"""
    target = root / todo.output_dir / RESULT_FILENAME
    if not target.is_file():
        return None
    with contextlib.suppress(json.JSONDecodeError, OSError):
        payload = json.loads(target.read_text(encoding="utf-8"))
        return proposal_of(payload)
    return None


def _due_at(root: Path, todo: Todo) -> str:
    """这张待办什么时候之前要交。

    **算出来的,不是存下来的**:窗口配置改了之后,存着的那个截止时间会与真正的到期扫描
    对不上——而人看的是前者,系统按的是后者。
    """
    with contextlib.suppress(GenomeValidationError):
        window = load_config(root).human.reassign_after_days
        if window > 0:
            return (todo.created_at + timedelta(days=window)).isoformat()
    return ""


def _drive_split_children(request: Request, root: Path, proposal_task: Task) -> None:
    """确认落树之后,给首批子需求的尝试接上后台驱动(PRD 48 D4 的 serve 半边)。

    编排器落树时只**建**首批尝试;CLI 单步语义也只建不推——那是记录在案的不对称(R4)。
    serve 的承诺是"确认之后子需求自动开工",兑现在这里。
    """
    if not proposal_task.requirement_id:
        return
    _drive_the_tree(request, root, proposal_task.requirement_id, load_config(root))


def _drive_the_kin(request: Request, root: Path, delivered: Task, config: Config) -> None:
    """一个子需求交付之后,驱动它解锁的亲属:同批兄弟,以及母需求自己的收口尝试。"""
    try:
        requirement = RequirementStore(root).get(delivered.requirement_id or "")
    except RequirementNotFound:
        return
    if requirement.parent_id:
        _drive_the_tree(request, root, requirement.parent_id, config)


def _drive_the_tree(request: Request, root: Path, parent_id: str, config: Config) -> None:
    """给一棵树上所有能推的尝试(子需求的与母需求自己的)接上后台驱动。幂等:
    已在跑的驱动不重复起,推不动的任务被 `can_advance` 挡下。"""
    store = TaskStore(root)
    pending = list(store.attempts_of(parent_id))
    for child in RequirementStore(root).children_of(parent_id):
        pending.extend(store.attempts_of(child.id))
    for attempt in pending:
        if can_advance(attempt):
            _spawn_dev_drive(request, root, attempt.id, config)


async def _resume_after_todo(request: Request, root: Path, task_id: str) -> Task:
    """人交完活之后自动推进到下一个外部闸门。

    **走的是崩溃恢复那条重放路**:产物已经在槽里,处理器的幂等约定会认出它并抛出本来就该
    抛的事件。不另抛"人交活了"这种事件——那等于给状态机开第二条入口。
    """
    config = load_config(root)
    bus: EventBus = request.app.state.bus
    workspace_name = _workspace_name(request)

    def publish_change(changed_task_id: str, kind: str) -> None:
        bus.publish(changed_task_id, kind, workspace=workspace_name)

    orchestrator = Orchestrator(
        root,
        pool=_task_pool(request, root, config),
        config=config,
        on_change=publish_change,
    )
    orchestrator.resume(task_id)
    store = TaskStore(root)
    store.save(store.get(task_id).evolve(run_status=TaskRunStatus.RUNNING))
    try:
        result = await TaskDriver(
            orchestrator,
            on_step=lambda task: publish_change(task.id, "task_changed"),
        ).drive(task_id)
    except Exception:
        store.save(store.get(task_id).evolve(run_status=TaskRunStatus.INTERRUPTED))
        raise
    status = _drive_run_status(result)
    saved = store.save(store.get(task_id).evolve(run_status=status))
    publish_change(task_id, "run_finished")
    return saved


def _actor_of(request: Request, principal: Principal) -> str:
    """这次介入记在谁头上。

    **配了身份表就用认证身份。** 没配(单机开发)时退回编排器——那种模式下没有可信的身份
    来源,记一个自称的名字会让"这个人干过什么"这个查询看起来能答,而它答的是假的。
    """
    return principal.subject if request.app.state.principals else ORCHESTRATOR


def _known_task(root: Path, task_id: str) -> str:
    """确认这个任务真的存在。空字符串原样放行——没有来源任务是合法的。

    **不是形式上的存在性检查。** 任务 id 会被当成路径拼进 `tasks/<id>/`(事件流的 JSONL
    副本写在那里),一个 `../../` 开头的 id 能把文件写到 Workspace 之外去;而它同时也是往
    任意任务的时间线里注入事件的入口——恰恰是"别污染任务时间线"这条要挡的事,只是从另一扇
    门进来。查一次表两样一起挡住,比拼一个字符白名单更难写错。
    """
    if not task_id:
        return ""
    for store in (TaskStore(root), GenomeTaskStore(root)):
        with contextlib.suppress(TaskNotFound):
            return str(store.get(task_id).id)
    raise HTTPException(status_code=404, detail=f"没有这个任务: {task_id}")


def _root(request: Request) -> Path:
    """这个请求作用在哪个工作区上。

    **没说清楚就拒绝,不落到默认工作区。** 默认工作区是跨租户数据泄漏最常见的来源:调用方
    忘了带参数,于是它读到了别人的任务——而这件事没有任何症状,直到有人发现自己的需求出现
    在别人的看板上。
    """
    registry: WorkspaceRegistry = request.app.state.workspaces
    try:
        return registry.resolve(_requested_workspace(request))[1]
    except AmbiguousWorkspace as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except UnknownWorkspace as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _principal(request: Request) -> Principal:
    """这个请求是谁发的。

    认证在这一层之外。没有身份信息时是 `ANONYMOUS`,而它**什么都不能做**。
    """
    subject = request.headers.get("x-actor", "")
    found: dict[str, Principal] = request.app.state.principals
    return found.get(subject, ANONYMOUS)


def _acting(request: Request) -> Principal:
    """这次请求算谁。**"没配账号算谁"这条规则只在这里写一次。**

    身份表为空时是 `SINGLE_MACHINE`(admin):单机开发不该被迫先配一套账号,而放行必须
    一路走到底——见 `rbac.SINGLE_MACHINE`。注册了任何身份之后立刻开始按头里的 `x-actor` 认。
    """
    if not request.app.state.principals:
        return SINGLE_MACHINE
    return _principal(request)


def _may(request: Request, action: Action) -> bool:
    """这个调用方能不能做这件事。**只给界面做展示裁剪用**——闸门是 `_ensure`。

    问的是 `_acting(request)`,与 `_ensure` 同一个身份。"身份表为空时算谁"这条规则只写
    一次:各写一份的话,单机开发下界面会把一堆其实点得动的按钮画成灰色,而两处都会觉得
    自己是对的。
    """
    return _acting(request).allows(action)


def _ensure(request: Request, action: Action) -> Principal:
    """写接口的统一入口。

    **前端藏起按钮不是安全边界**——一个只在界面上消失的按钮,用 curl 一样能调。所以每个写
    操作都过这里。

    身份表为空时放行:单机开发不该被迫先配一套账号。**注册了任何身份之后立刻开始强制**,
    所以生产部署一旦配了人,匿名请求就进不来了。

    放行给的是 `SINGLE_MACHINE` 而不是 `ANONYMOUS`:下游的服务层(设置写入、员工定义写入)
    会拿这个身份**再判一次**,而匿名什么都不能做——那会变成"入口放行、写入层拒绝",表现为
    界面上旋钮是亮的而按下保存 403。
    """
    principal = _acting(request)
    try:
        principal.ensure(action)
    except Forbidden as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return principal


def _task(request: Request, task_id: str) -> Any:
    try:
        return TaskStore(_root(request)).get(task_id)
    except TaskNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _ensure_any_task_exists(request: Request, task_id: str) -> None:
    """事件面与任务目录是两类任务共用的;查"存在不存在"就要两张表都问。

    事件日志、原始日志这两个读端点服务的是**主体 id**,不是某一类任务:基因组任务的
    事件与日志落在与研发任务完全相同的位置(`tasks/<id>/logs/events.jsonl`、事件库按
    subject 检索)。只问研发任务那张表的话,`gn-` 开头的 id 一律 404——日志明明在盘上,
    界面却说"没有这个任务"。存在性检查仍然保留:它区分的是"任务不存在"与"任务存在
    但还没有日志",后者是空列表不是报错。
    """
    root = _root(request)
    try:
        TaskStore(root).get(task_id)
        return
    except TaskNotFound:
        pass
    try:
        GenomeTaskStore(root).get(task_id)
    except TaskNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _why_not_runnable(task: Any) -> str:
    """`can_advance(task)` 已经是 `False` 时,把原因说清楚。

    **只用来措辞,不用来判断。** 判断权在 `can_advance`——这里的三个分支只是把它内部
    互斥的三种情况翻译成人话,分支少写一种或者判断顺序颠倒,最坏结果是这句话不够精确,
    不会让一个本该被拒绝的请求被放行。
    """
    if task.is_terminal:
        return f"{task.id} 已经是终态({task.state.value}),没有下一步。"
    if task.state not in HANDLERS:
        return (
            f"{task.id} 现在在 {task.state.value},这一态在等外部事件推动"
            "(审批、合并结果、结对会话结束),不是能直接推进的。"
        )
    return f"{task.id} 的结对会话正在进行,这一步由人在会话里推动,不是这个接口。"


@dataclass(frozen=True)
class _TaskPoolSignature:
    """会改变池内运行时或并发闸的配置快照。"""

    runtime: str
    global_jobs: int
    genome_jobs: int


@dataclass(frozen=True)
class _TaskPoolCacheEntry:
    """配置快照与按它装配出的池必须成对替换。"""

    signature: _TaskPoolSignature
    pool: AgentPool


def _task_pool_signature(config: Config) -> _TaskPoolSignature:
    return _TaskPoolSignature(
        runtime=json.dumps(
            config.runtime.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        global_jobs=config.concurrency.global_jobs,
        genome_jobs=config.genome_tasks.concurrent_jobs,
    )


def _task_pool(request: Request, root: Path, config: Config) -> AgentPool:
    """这个工作区推进任务用的执行池。**按工作区路径缓存,装配路径与命令行共用一条。**

    不搭一条"测试专用"的旁路(往应用状态塞替身运行时之类)——PRD 29 记录过这条教训:
    会话平面早先就是这样,`session_runtimes` 常年是空字典,生产代码里没有任何地方往里写,
    于是"配置能不能变成运行时"这条装配路径在一年多里从没被真正跑过,直到有人发现任何
    `POST /sessions` 都以 409 告终。这里直接复用 `build_runtimes`——命令行也走这条函数,
    测试要练的话经 `AGENTGENOME_RECORDINGS` 走真实装配选出 replay 运行时,不是另开一道口子。
    """
    cache: dict[str, _TaskPoolCacheEntry] = request.app.state.task_pools
    key = str(root)
    signature = _task_pool_signature(config)
    cached = cache.get(key)
    if cached is None or cached.signature != signature:
        try:
            runtimes = build_runtimes(config, root)
        except RuntimeAssemblyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        # 先完整装配,最后一次赋值替换。已经拿到旧池的在途推进可正常收尾;
        # 后续请求只会看见完整的新池,不会撞上半装配状态。
        cached = _TaskPoolCacheEntry(
            signature=signature,
            pool=AgentPool(
                runtimes,
                global_jobs=config.concurrency.global_jobs,
                genome_jobs=config.genome_tasks.concurrent_jobs,
            ),
        )
        cache[key] = cached
    return cached.pool


def _requested_workspace(request: Request) -> str | None:
    """请求里点名的工作区参数,query 优先(EventSource 加不了请求头)。"""
    return request.query_params.get("workspace") or request.headers.get("x-workspace")


def _workspace_name(request: Request) -> str:
    """这个请求作用的工作区**名字**(不是路径),给通知打标用。

    发布不该因为标打不上而失败,但失败时的方向必须是**收紧**:解析不出来打一个不可能
    命中的名字,宁可这条通知没人收到,也不广播——task_id 本身就是数据。实际上这条分支
    几乎不可达:每个发布点之前 `_root` 已经解析成功过(失败早就 4xx 了);单项目实例的
    唯一解也不会失败。空串(对所有订阅者可见)只留给"没有发布方标注"的老形态。"""
    registry: WorkspaceRegistry = request.app.state.workspaces
    try:
        return registry.resolve(_requested_workspace(request))[0]
    except (AmbiguousWorkspace, UnknownWorkspace):
        return "(unresolved)"


def _publish(request: Request, task_id: str, kind: str) -> None:
    bus: EventBus = request.app.state.bus
    bus.publish(task_id, kind, workspace=_workspace_name(request))


def _notify_im(request: Request, task: Any, event: str) -> None:
    """任务状态变化推送到 IM。**失败不阻塞任何状态迁移**——与 PRD 08 审批通知同一条原则。

    只覆盖 REST 请求内同步触发的那几种事件(创建/取消/审批)。`ESCALATED`/`COMPLETED` 走
    `agctl task run`(CLI)或 `POST /tasks/{id}/run`(这个模块的 `run`)——两条推进路径各自
    直接调 `notify_prefs.push`,原因见 `jobs.orchestrator.TERMINAL_NOTIFY_EVENT` 的文档。
    """
    notify_prefs.push(_root(request), task.id, task.title, event)


def _service_line_comment(item: LineComment) -> ServiceLineComment:
    return ServiceLineComment(file=item.file, line=item.line, side=item.side, content=item.content)


def _lesson_response(card: LessonCard) -> LessonCardResponse:
    return LessonCardResponse(
        id=card.id,
        title=card.title,
        modules=card.applies_to.modules,
        path_globs=card.applies_to.path_globs,
        scenario=card.applies_to.scenario,
        conclusion=card.conclusion,
        evidence=[
            EvidenceItem(task_id=item.task_id, path=item.path, note=item.note)
            for item in card.evidence
        ],
        confidence=card.confidence,
        level=card.level.value,
        hits=card.hits,
        created_from=card.created_from,
        archived=card.archived,
    )


def _metric_values(snapshot: metrics_module.Snapshot) -> list[float]:
    """顺序必须与 `trends.DEFINITIONS` 一致:修复轮次、门禁一次通过率、任务时长、
    升级人工率、知识命中率。"""
    return [
        snapshot.avg_fix_rounds,
        snapshot.gate_first_pass_ratio,
        snapshot.avg_duration_minutes,
        snapshot.escalate_rate,
        snapshot.knowledge_hit_rate,
    ]


def _forge_and_remote(root: Path) -> tuple[Forge, Path]:
    """规则提案走哪个 Forge、开去哪个远端。

    判据见 `space.forge.select`——与 `Orchestrator.forge()`、CLI 的 `_open_forge` 共用同一个
    函数。远端地址从 Workspace 自己的 `origin` 读,不是配出来的第二份地方。
    """
    config = load_config(root)
    forge = select_forge(config.platform.git_host)
    remote = Path(git_out(root, "remote", "get-url", "origin"))
    return forge, remote


def _parse_enum(enum: type[EnumT], raw: str, field: str) -> EnumT | None:
    """检索参数里的枚举值。**空字符串就是不限定**,认不出来的是 422。

    直接 `Enum(raw)` 的话,一个拼错的筛选条件是 500——而 500 说的是"服务端出问题了",
    于是排查会从服务端日志开始,那里什么都没有。
    """
    if not raw:
        return None
    try:
        return enum(raw)
    except ValueError as error:
        allowed = ", ".join(item.value for item in enum)
        raise HTTPException(
            status_code=422, detail=f"{field} 只能是这几个之一({allowed}),收到: {raw}"
        ) from error


def _parse_query_time(raw: str) -> datetime | None:
    """审计检索的时间边界。**空字符串就是不限定**,不是"从公元 1 年开始"。

    前端的 `datetime-local` 输入没有时区,`fromisoformat` 解析出来是 naive datetime——
    与事件流里存的 UTC 时间戳直接比较会抛 `TypeError`,这里补一道,不强求调用方总带时区。
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"不是合法的 ISO8601 时间: {raw}") from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _last_event_id(request: Request) -> int | None:
    raw = request.headers.get("last-event-id")
    if raw is None or not raw.strip().isdigit():
        return None
    return int(raw.strip())


def _first_line(requirement: str) -> str:
    for line in requirement.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return "(无标题)"


__all__ = ["API_VERSION", "HEARTBEAT_S", "create_app", "stream_notices"]
