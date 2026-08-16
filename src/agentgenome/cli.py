"""agctl —— AgentGenome 的命令行入口。

CLI 是薄壳:参数解析、错误渲染、退出码。业务逻辑一律在下面的包里,
这样后续接上 REST 时两条路走的是同一套代码。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import typer
import yaml

from agentgenome import paths
from agentgenome.agents.agentteams.provision import (
    ProvisionError,
    ProvisionUnavailable,
    UnknownModelTier,
    WorkerProvisioner,
    build_payload,
    build_provisioner,
    tiers_from,
)
from agentgenome.agents.agentteams.provision import (
    plan as plan_provision,
)
from agentgenome.agents.agentteams.records import record_lifecycle, record_provision
from agentgenome.agents.agentteams.transport import PlatformUnavailable
from agentgenome.agents.artifacts import context_bundle_filename
from agentgenome.agents.factory import (
    RuntimeAssemblyError,
    build_runtimes,
    build_session_runtimes,
    why_no_session,
)
from agentgenome.agents.pool import AgentPool, RuntimeNotRegistered
from agentgenome.agents.recording import RecordingNotFound
from agentgenome.agents.runtime import AgentRuntime, JobSpec
from agentgenome.agents.subprocess_runtime import SubprocessRuntime
from agentgenome.approval.service import ApprovalRefused, NotAnApprover, approve, reject
from agentgenome.config import AGENTTEAMS_RUNTIME, Config, load_config
from agentgenome.context import (
    ContextInputs,
    FailureReport,
    assemble,
    context_budget,
    load_genome_slice,
)
from agentgenome.core.diagram import render_mermaid
from agentgenome.core.events import ORCHESTRATOR, SYSTEM_SUBJECT, ActorKind, EventLog, LogKind
from agentgenome.core.genome_driver import GenomeDriver, NotWaiting
from agentgenome.core.genome_gate import AnswerInvalid, NoDraft, read_draft
from agentgenome.core.genome_task import (
    GenomeTask,
    GenomeTaskKind,
    GenomeTaskState,
    GenomeTaskStore,
    ModuleBusy,
    Origin,
)
from agentgenome.core.genome_transitions import GenomeEvent, GenomeFacts
from agentgenome.core.requirement import (
    Requirement,
    RequirementNotFound,
    RequirementScene,
    RequirementState,
    RequirementStore,
)
from agentgenome.core.requirement import intake as requirement_intake
from agentgenome.core.requirement import revise as requirement_revise
from agentgenome.core.scope import ScopePolicy
from agentgenome.core.scope_grants import effective_modules, effective_mount_paths
from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.store import task_dir
from agentgenome.core.task import (
    AttemptConflict,
    ItestNeed,
    ItestOverride,
    Task,
    TaskMode,
    TaskNotFound,
    TaskStore,
)
from agentgenome.core.task_ids import lane_budget
from agentgenome.core.topology import TopologyParseError, UnknownTopology, parse_template
from agentgenome.core.topology_validate import validate_topology
from agentgenome.employees import (
    EmployeeConfig,
    EmployeeNotFound,
    EmployeeRegistry,
    ProcedureNotAllowed,
    load_employees,
    workspace_employees_root,
)
from agentgenome.gates.config import effective_gates
from agentgenome.gates.task_gates import gate_slot, run_task_gates
from agentgenome.genome import deepen as deepen_mod
from agentgenome.genome import knowledge as knowledge_mod
from agentgenome.genome import staging as staging_mod
from agentgenome.genome.boundary import (
    NotReadyForBoundaries,
)
from agentgenome.genome.budgets import check_budgets, fragmentation_warnings
from agentgenome.genome.dispatch import DispatchRefused, dispatch_procedure
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.evolution.cards import load_cards
from agentgenome.genome.evolution.pipeline import CANDIDATES_DIR
from agentgenome.genome.evolution.promotion import (
    load_promotions,
)
from agentgenome.genome.evolution.proposals import (
    ProcedureProposal,
    failure_pattern,
    propose_rules,
)
from agentgenome.genome.evolution.trends import weekly
from agentgenome.genome.features import KnowledgeTree, no_card_declarations
from agentgenome.genome.hits import (
    LedgerUnreadable,
    apply_credits,
    pending_credits,
    take_credits,
)
from agentgenome.genome.init_entry import InitAlreadyOpen, plan_init
from agentgenome.genome.loader import load_project_map, load_tree
from agentgenome.genome.models import Module, ProjectMap
from agentgenome.genome.pipeline import GenomeOrchestrator
from agentgenome.genome.procedures import ProcedureRegistry, load_procedure, load_workspace_registry
from agentgenome.genome.roster import PLAN_PROCEDURES
from agentgenome.genome.roster_migrate import Migration, plan_migration, run_migration
from agentgenome.genome.rules import RuleSet, effective_max_fix_rounds, load_rules
from agentgenome.genome.scaffold import (
    MountFailed,
    WorkspaceExistsError,
    WorkspaceRemoteFailed,
    init_workspace,
    plan_repos,
    unmounted_refusal,
)
from agentgenome.genome.suspects import (
    ResolutionAction,
    pending_suspects,
    resolve_suspects,
)
from agentgenome.genome.tree import NothingToMigrate, migrate_flat_map, module_dir
from agentgenome.itest.decide import DecisionSource, ItestDecision, manual_decision
from agentgenome.itest.procedure_entry import involved_modules, itest_config, write_outputs
from agentgenome.itest.report import REPORT_FILE
from agentgenome.itest.task_itest import run_task_itest
from agentgenome.jobs.artifacts import ArtifactBus
from agentgenome.jobs.catalog import check_choice as check_topology_choice
from agentgenome.jobs.catalog import options as topology_options
from agentgenome.jobs.catalog import single_path_spends as topology_single_path_spends
from agentgenome.jobs.handlers import STAGE_UNIT_GATE, can_advance
from agentgenome.jobs.orchestrator import (
    TERMINAL_NOTIFY_EVENT,
    Orchestrator,
    load_plan_modules,
    read_plan,
)
from agentgenome.jobs.reports import read_failure_reports
from agentgenome.registry.library import Registry, RegistryUnavailable, check_contribution
from agentgenome.security import gaps
from agentgenome.security.audit import (
    TaskNotArchivable,
    archive_root,
    export_task_bundle,
    seal_terminal_evidence,
)
from agentgenome.security.retention import apply_prune, plan_workspace_prune
from agentgenome.server import metrics as metrics_module
from agentgenome.server import notify_prefs
from agentgenome.server.app import create_app, unregister_workspace
from agentgenome.server.employees_edit import NeedsAssignee, UnknownRung, set_execution
from agentgenome.server.models import SettingsView
from agentgenome.server.rbac import Principal, Role
from agentgenome.server.settings import HISTORY_LIMIT, Entrance, NotEditable
from agentgenome.server.settings import history as settings_history
from agentgenome.server.settings import update as update_settings
from agentgenome.server.tenancy import (
    DEFAULT_REGISTRY,
    UnknownWorkspace,
    load_registry,
    save_registry,
)
from agentgenome.space.forge import Forge, ForgeError, MergeConflict, PRRef
from agentgenome.space.forge import select as select_forge
from agentgenome.space.git_ws import GitWorkspace, scoped_worktrees_home
from agentgenome.space.gitcmd import GitError
from agentgenome.todo.service import TodoRefused
from agentgenome.todo.service import output_schema_of as todo_schema
from agentgenome.todo.service import submit as submit_todo
from agentgenome.todo.store import WORKTREE, TodoNotFound, TodoStore
from agentgenome.todo.sweep import sweep as sweep_todos
from agentgenome.verification import (
    NeedsConfirmation,
    PendingVerification,
    Ready,
    load_pending_verification,
    load_verification_spec,
    load_verification_spec_file,
    record_confirmed_spec,
    record_pending_spec,
    resolve_verification,
    seal_agent_proposal,
    validate_spec_evidence,
)
from agentgenome.verification import proposal as verification_proposal

app = typer.Typer(help="AgentGenome 编排器命令行", no_args_is_help=True)
genome_app = typer.Typer(help="基因组资产的查看与校验", no_args_is_help=True)
show_app = typer.Typer(help="打印当前生效的基因组内容", no_args_is_help=True)
genome_app.add_typer(show_app, name="show")
ws_app = typer.Typer(help="隔离工作区操作", no_args_is_help=True)
app.add_typer(genome_app, name="genome")
forge_app = typer.Typer(help="代码托管平台原语(PR 与合并)", no_args_is_help=True)
forge_pr_app = typer.Typer(help="PR 操作", no_args_is_help=True)
forge_app.add_typer(forge_pr_app, name="pr")
app.add_typer(ws_app, name="ws")
job_app = typer.Typer(help="Job 执行原语", no_args_is_help=True)
app.add_typer(forge_app, name="forge")
knowledge_app = typer.Typer(help="项目认知的生成与更新", no_args_is_help=True)
app.add_typer(job_app, name="job")
topology_app = typer.Typer(help="执行拓扑模板的校验", no_args_is_help=True)
app.add_typer(topology_app, name="topology")
todo_app = typer.Typer(help="派给人的待办", no_args_is_help=True)
app.add_typer(todo_app, name="todo")
procedure_app = typer.Typer(help="Procedure 的查看与校验", no_args_is_help=True)
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(procedure_app, name="procedure")
employee_app = typer.Typer(help="数字员工的查看", no_args_is_help=True)
app.add_typer(employee_app, name="employee")
roster_app = typer.Typer(help="花名册的迁移", no_args_is_help=True)
app.add_typer(roster_app, name="roster")
session_app = typer.Typer(help="会话链路的检查", no_args_is_help=True)
app.add_typer(session_app, name="session")
context_app = typer.Typer(help="上下文包的组装与检查", no_args_is_help=True)
app.add_typer(context_app, name="context")
task_app = typer.Typer(help="任务的提交与查看", no_args_is_help=True)
app.add_typer(task_app, name="task")
requirement_app = typer.Typer(help="需求的查看与搁置", no_args_is_help=True)
app.add_typer(requirement_app, name="requirement")
workspace_app = typer.Typer(help="项目(工作区)注册表", no_args_is_help=True)
app.add_typer(workspace_app, name="workspace")
gate_app = typer.Typer(help="质量门禁", no_args_is_help=True)
app.add_typer(gate_app, name="gate")
itest_app = typer.Typer(help="集成测试", no_args_is_help=True)
app.add_typer(itest_app, name="itest")
registry_app = typer.Typer(help="全局基因库", no_args_is_help=True)
app.add_typer(registry_app, name="registry")
evolve_app = typer.Typer(help="自进化:卡片、规则提案与周报", no_args_is_help=True)
app.add_typer(evolve_app, name="evolve")
settings_app = typer.Typer(help="根配置的查看与修改", no_args_is_help=True)
app.add_typer(settings_app, name="settings")

LoadedT = TypeVar("LoadedT")

#: 人工重跑的产物 stage。**与自动跑分开**:同一个 stage 的话,一次重跑会被轮次定位
#: 当成“这一轮已经跑过了”,于是崩溃恢复直接跳过真正该跑的那一次。
STAGE_ITEST_MANUAL = "itest-manual"

WorkspaceOption = typer.Option(
    Path("."), "--workspace", "-w", help="Workspace 根目录", show_default=False
)

RegistryOption = typer.Option(
    None, "--registry", help="全局基因库位置,缺省读 AGENTGENOME_REGISTRY", show_default=False
)


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=False)
    raise typer.Exit(code=1)


def _report_validation_error(error: GenomeValidationError) -> NoReturn:
    typer.secho("基因组校验未通过:", fg=typer.colors.RED)
    for issue in error.issues:
        typer.echo(f"  - {issue.render()}")
    raise typer.Exit(code=1)


@app.command()
def init(
    path: Path = typer.Argument(Path("."), help="Workspace 的创建位置"),
    repo: list[str] = typer.Option(
        [],
        "--repo",
        "-r",
        help="业务仓地址,可重复;挂载为 repos/<仓库名>/。用 <url>@<branch> 指定该仓要挂载的分支",
    ),
    name: str = typer.Option("", "--name", help="项目名,缺省时取 Workspace 目录名"),
    branch: str = typer.Option("main", "--branch", help="Workspace 自身的默认分支"),
    workspace_repo: str = typer.Option(
        "",
        "--workspace-repo",
        help="顶层项目 Git 仓库地址；保存治理配置、知识资产与业务仓指针",
    ),
    local_only: bool = typer.Option(
        False,
        "--local-only",
        help="只建本地 Workspace，不配置远端；仅用于离线试验，不能走真实平台合并",
    ),
) -> None:
    """初始化一个 Workspace:挂载业务仓、生成基因组骨架并提交。"""
    if not repo:
        _fail("至少需要一个 --repo:Workspace 是协作仓,本身不含业务代码。")
    if not workspace_repo and not local_only:
        _fail("必须提供 --workspace-repo；离线试验请显式使用 --local-only。")
    if workspace_repo and local_only:
        _fail("--workspace-repo 与 --local-only 不能同时使用。")

    root = path.expanduser().resolve()
    project_name = name or root.name
    repos = plan_repos(repo)

    try:
        init_workspace(
            root,
            project_name,
            repos,
            default_branch=branch,
            workspace_remote=workspace_repo or None,
        )
    except (WorkspaceExistsError, WorkspaceRemoteFailed, MountFailed) as exc:
        _fail(str(exc))

    typer.secho(f"已初始化 Workspace: {root}", fg=typer.colors.GREEN)
    for spec in repos:
        typer.echo(f"  {spec.module_id} -> {spec.path}")
    if local_only:
        typer.secho("本地模式:未配置顶层 origin，真实平台合并不可用。", fg=typer.colors.YELLOW)
    typer.echo("\n下一步:运行 knowledge-init 让架构员工补全项目地图。")


@dataclass
class LoadedGenome:
    """一次加载出来的全部基因组资产。"""

    config: Config
    rules: RuleSet
    #: 整棵知识树。项目地图装不下的那一层(功能点与卡片)在这里。
    tree: KnowledgeTree

    @property
    def project_map(self) -> ProjectMap:
        """树里那个扁平投影。两个字段各存一份的话,它们迟早不是同一个对象。"""
        return self.tree.project_map


def _collect(load: Callable[[], LoadedT], issues: list[ValidationIssue]) -> LoadedT | None:
    """跑一个加载器,把它的问题攒起来而不是当场抛出。

    三份资产各自独立校验,一次把问题报全——修一个文件跑一次的反馈回路太长。
    """
    try:
        return load()
    except GenomeValidationError as error:
        issues.extend(error.issues)
        return None


def _load_all(root: Path) -> LoadedGenome:
    issues: list[ValidationIssue] = []
    config = _collect(lambda: load_config(root), issues)
    rules = _collect(lambda: load_rules(root), issues)
    # 装的是整棵树而不只是项目地图:完备性(每个功能点要么有卡片、要么有带理由的
    # 「无需卡片」声明)只有在这一层才校验得到,而它是 ADR-0003 的可执行形式。
    tree = _collect(lambda: load_tree(root), issues)

    if config is None or rules is None or tree is None:
        _report_validation_error(GenomeValidationError(issues))
    return LoadedGenome(config=config, rules=rules, tree=tree)


@genome_app.command("validate")
def genome_validate(workspace: Path = WorkspaceOption) -> None:
    """加载并校验根配置、规则与整棵知识树。

    **完备性也在这里把关**：每个功能点要么有一张卡片，要么有一条带理由的「无需卡片」
    声明。地图上不允许存在缺口——见 ADR-0003。
    """
    root = workspace.expanduser().resolve()
    genome = _load_all(root)
    # **花名册也在这里把关。** 归属排他是一条跨文件的不变式:单看任何一份员工定义都合法,
    # 坏的是它们合在一起的样子——所以它只能在"把整个基因组读一遍"的这条命令里被发现。
    try:
        load_employees(workspace_employees_root(root), exclusive=_exclusive_procedures(root))
    except GenomeValidationError as error:
        _report_validation_error(error)
    # **预算只在门禁命令里查,不在 `_load_all` 里。** 放进去的话,一份超限的模块地图会
    # 连带让 `genome show rules` 这类只读命令也失败——而超限是知识变更要处理的事,
    # 不是每个命令都要处理的事。
    over = check_budgets(root, genome.tree, genome.config.knowledge)
    if over:
        _report_validation_error(GenomeValidationError(over))
    features = sum(len(items) for items in genome.tree.feature_index.values())
    declared = no_card_declarations(genome.tree)
    typer.secho(
        f"基因组校验通过(project-map v{genome.project_map.version},"
        f"{len(genome.project_map.modules)} 个模块,{features} 个功能点)",
        fg=typer.colors.GREEN,
    )
    if declared:
        # 校验只挡得住空理由，挡不住「不需要」三个字。**把它们列出来让人扫一眼**，
        # 是这条不变式唯一有效的补强。
        typer.secho(f"\n{len(declared)} 个功能点声明了无需卡片，值得定期复核：", bold=True)
        for item in declared:
            typer.echo(f"  {item.module_id}/{item.feature.id}：{item.feature.no_card}")
    for warning in fragmentation_warnings(genome.tree, genome.config.knowledge):
        # **只提示不拦截。** 切碎有时是对的，但它应该是被注意到的。
        typer.secho(
            f"提示：{warning.scope_prefix} 被 {warning.count} 个带卡片的功能点同时覆盖"
            f"（{'、'.join(warning.feature_ids)}），知识可能被切碎了，考虑合并。",
            fg=typer.colors.YELLOW,
        )


@genome_app.command("confirm")
def genome_confirm(
    task_id: str = typer.Argument(..., help="基因组任务编号"),
    answer: Path = typer.Option(
        None, "--answer", help="答复文件（JSON）。不给就只打印草案", show_default=False
    ),
    workspace: Path = WorkspaceOption,
    as_json: bool = typer.Option(False, "--json", help="输出结构化结果"),
) -> None:
    """回答一个基因组任务的人工闸门。

    **不给 --answer 时只打印草案。** 先看再答是常态，而"先看"不该逼人去翻产物目录。

    这条命令与 Web 写的是**同一份答复文件**：两条入口各写各的话，「哪一份算数」会变成一个
    答不上来的问题。
    """
    root = workspace.expanduser().resolve()
    store = GenomeTaskStore(root)
    try:
        task = store.get(task_id)
    except TaskNotFound as exc:
        _fail(str(exc))

    try:
        draft = read_draft(root, task_id)
    except NoDraft as exc:
        _fail(str(exc))

    if answer is None:
        _emit(
            {"task_id": task_id, "state": task.state.value, "draft": draft},
            as_json,
            json.dumps(draft, ensure_ascii=False, indent=2),
        )
        return

    payload = _read_json(answer.expanduser())
    if payload is None:
        _fail(f"读不出答复文件（要是一份 JSON 映射）：{answer}")
    driver = GenomeDriver(store, EventLog(root), enforce_budget=load_config(root).budgets.enforce)
    try:
        applied = driver.confirm(task_id, payload)
    except NotWaiting as exc:
        _fail(str(exc))
    except AnswerInvalid as exc:
        _fail(f"答复不合契约，任务留在待确认：{exc}")

    # **诚实报告有没有动。** 一律说"已确认"的话，一次没生效的回答看起来跟生效了一样。
    verb = "已确认" if applied.moved else "未生效"
    _emit(
        {"task_id": task_id, "moved": applied.moved, "state": applied.task.state.value},
        as_json,
        f"{verb}，{task_id} 现在在 {applied.task.state.value}",
    )


@genome_app.command("reinit")
def genome_reinit(
    module: list[str] = typer.Option(
        ..., "--module", help="要重建的模块 id，可重复", show_default=False
    ),
    workspace: Path = WorkspaceOption,
    as_json: bool = typer.Option(False, "--json", help="输出结构化结果"),
) -> None:
    """按模块重建知识。

    **跳过扫描、划分与闸门**——边界已经拍过板了，重建不需要再问一次。

    某个模块大改之后，不该为了刷新那一块认知而重跑全库：那既贵又慢，而且会把其余模块上人
    已经校对过的东西重新搅一遍。
    """
    root = workspace.expanduser().resolve()
    config = _collect(lambda: load_config(root), []) or Config()
    known = _collect(lambda: load_project_map(root), [])
    if known is None:
        _fail("项目地图不可读：先跑 agctl genome validate 看问题。")

    unknown = sorted(set(module) - known.module_ids())
    if unknown:
        # 凭空发明模块会让下游的影响判定失去依据。
        _fail(f"项目地图里没有这些模块：{'、'.join(unknown)}")

    store = GenomeTaskStore(root)
    created = []
    for module_id in module:
        try:
            created.append(
                store.create(
                    title=f"按模块重建：{module_id}",
                    kind=GenomeTaskKind.REINIT,
                    origin=Origin.HUMAN,
                    subject=module_id,
                    budget_tokens=config.genome_tasks.per_task_tokens,
                )
            )
        except ModuleBusy as exc:
            _fail(str(exc))

    _emit(
        {"tasks": [{"task_id": item.id, "module": item.subject} for item in created]},
        as_json,
        "\n".join(f"{item.subject}：{item.id}" for item in created),
    )


@genome_app.command("cancel")
def genome_cancel(
    task_id: str = typer.Argument(..., help="基因组任务编号"),
    workspace: Path = WorkspaceOption,
    as_json: bool = typer.Option(False, "--json", help="输出结构化结果"),
) -> None:
    """取消一个基因组任务。

    **不只是"叫停跑飞的任务"。** 同一个模块同时只允许一个基因组任务，而停在待确认的任务
    不是终态——没有这条命令的话，一个没人回答的闸门会把那个模块**永久**堵住：既跑不完，
    也重建不了。
    """
    root = workspace.expanduser().resolve()
    store = GenomeTaskStore(root)
    try:
        store.get(task_id)
    except TaskNotFound as exc:
        _fail(str(exc))

    applied = GenomeDriver(store, EventLog(root)).deliver(task_id, GenomeEvent.CANCEL)
    if not applied.moved:
        _fail(applied.decision.reason)
    _emit(
        {"task_id": task_id, "state": applied.task.state.value},
        as_json,
        f"已取消 {task_id}",
    )


@genome_app.command("migrate")
def genome_migrate(
    workspace: Path = WorkspaceOption,
    as_json: bool = typer.Option(False, "--json", help="输出结构化结果"),
) -> None:
    """把旧的单文件项目地图拆成知识树。

    确定性、不烧 token、可反复跑；它只搬位置，一个字的知识都不改。
    """
    root = workspace.expanduser().resolve()
    try:
        migrated, moved = migrate_flat_map(root)
    except NothingToMigrate as exc:
        _emit({"migrated": False, "reason": str(exc)}, as_json, str(exc))
        return
    except GenomeValidationError as error:
        _report_validation_error(error)

    _emit(
        {
            "migrated": True,
            "modules": [module.id for module in migrated.modules],
            "overviews_moved": moved,
        },
        as_json,
        f"已拆成知识树：{len(migrated.modules)} 个模块。\n"
        "功能点一个没建——那一层知识本来就不存在，跑 knowledge-init 读代码之后才有。",
    )


@show_app.command("rules")
def genome_show_rules(
    workspace: Path = WorkspaceOption,
    as_json: bool = typer.Option(False, "--json", help="输出结构化结果"),
) -> None:
    """打印当前生效的规则集(含规则文件对根配置的覆盖结果)。"""
    genome = _load_all(workspace.expanduser().resolve())
    rules = genome.rules
    effective = {"max_fix_rounds": effective_max_fix_rounds(rules, genome.config)}

    if as_json:
        payload = rules.model_dump(mode="json", by_alias=True)
        payload["effective"] = effective
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    typer.secho("架构规则", bold=True)
    typer.echo(
        f"  max_fix_rounds: {effective['max_fix_rounds']}"
        f"{'(来自规则文件)' if rules.architecture.max_fix_rounds else '(来自根配置)'}"
    )
    for dep in rules.architecture.forbidden_deps:
        typer.echo(f"  禁止依赖: {dep.from_glob} -> {dep.to_glob}")
    for line in rules.architecture.layering:
        typer.echo(f"  分层约束: {line}")

    typer.secho("\n受保护路径", bold=True)
    for entry in rules.protected.protected_paths:
        writers = f"(只有 {', '.join(entry.writable_by)} 能写)" if entry.writable_by else ""
        typer.echo(f"  {entry.path}{writers}")
    if not rules.protected.protected_paths:
        typer.echo("  (无)")

    typer.secho("\n高风险模式", bold=True)
    for pattern in rules.protected.high_risk or []:
        typer.echo(f"  {pattern.id}: {pattern.description}")
    if not rules.protected.high_risk:
        typer.echo("  (无)")

    typer.secho("\n影响规则(命中即必须跑集成测试)", bold=True)
    for rule in rules.impact.rules or []:
        typer.echo(f"  {rule.id}: {rule.description}")
    if not rules.impact.rules:
        typer.echo("  (无)")


def _open_workspace(workspace: Path) -> GitWorkspace:
    """打开一个 Workspace。隔离工作区的存放根由 `git_ws` 统一决定。"""
    return GitWorkspace(workspace.expanduser().resolve())


@ws_app.command("checkout")
def ws_checkout(
    task_id: str = typer.Argument(..., help="任务编号"),
    slug: str = typer.Option("", "--slug", help="分支名后缀"),
    workspace: Path = WorkspaceOption,
) -> None:
    """为一个任务开出隔离工作区(幂等),打印其路径。"""
    path = _open_workspace(workspace).checkout_isolated(task_id, slug or None)
    typer.echo(str(path))


@ws_app.command("diff")
def ws_diff(
    task_id: str = typer.Argument(..., help="任务编号"),
    as_json: bool = typer.Option(False, "--json", help="输出结构化结果"),
    workspace: Path = WorkspaceOption,
) -> None:
    """列出任务分支相对基线的全部改动(含未跟踪文件)。"""
    changes = _open_workspace(workspace).diff(task_id)
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "entries": [
                        {"path": e.path, "kind": e.kind.value, "old_path": e.old_path}
                        for e in changes.entries
                    ],
                    "added_lines": changes.added_lines,
                    "deleted_lines": changes.deleted_lines,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if changes.is_empty():
        typer.echo("(无改动)")
        return
    for entry in changes.entries:
        arrow = f" <- {entry.old_path}" if entry.old_path else ""
        typer.echo(f"  {entry.kind.value:<10} {entry.path}{arrow}")
    typer.echo(f"\n+{changes.added_lines} / -{changes.deleted_lines}")


@ws_app.command("commit")
def ws_commit(
    task_id: str = typer.Argument(..., help="任务编号"),
    message: str = typer.Option(..., "--message", "-m", help="提交信息"),
    path: list[str] = typer.Option([], "--path", help="只提交这些路径,可重复"),
    workspace: Path = WorkspaceOption,
) -> None:
    """在任务的隔离工作区里提交。"""
    rev = _open_workspace(workspace).commit(task_id, message, paths=list(path) or None)
    typer.echo(rev)


@ws_app.command("pointers")
def ws_pointers(
    task_id: str = typer.Option("", "--task", help="看某个任务的工作区,缺省看主 checkout"),
    as_json: bool = typer.Option(False, "--json", help="输出结构化结果"),
    workspace: Path = WorkspaceOption,
) -> None:
    """打印各子模块当前指向的 revision。"""
    pointers = _open_workspace(workspace).submodule_pointers(task_id or None)
    if as_json:
        typer.echo(json.dumps(pointers, ensure_ascii=False, indent=2))
        return
    for module_path, rev in pointers.items():
        typer.echo(f"  {module_path:<12} {rev}")


@ws_app.command("cleanup")
def ws_cleanup(
    task_id: str = typer.Argument(..., help="任务编号"),
    delete_branch: bool = typer.Option(False, "--delete-branch", help="连分支一起删掉"),
    workspace: Path = WorkspaceOption,
) -> None:
    """回收任务的隔离工作区。对不存在的任务是空操作。"""
    _open_workspace(workspace).cleanup(task_id, delete_branch=delete_branch)
    typer.secho(f"已回收 {task_id} 的隔离工作区", fg=typer.colors.GREEN)


RepoOption = typer.Option(..., "--repo", help="仓库位置(本地实现给 bare 仓路径,平台实现给工作副本)")
GitHostOption = typer.Option(
    "", "--git-host", help="覆盖根配置的 platform.git_host;local 走本地实现"
)
JsonOption = typer.Option(False, "--json", help="输出结构化结果")


def _open_forge(workspace: Path, git_host: str) -> Forge:
    """按 `platform.git_host` 选实现——本地演示与真实平台是同一条命令。"""
    host = git_host
    if not host:
        try:
            host = load_config(workspace.expanduser().resolve()).platform.git_host
        except GenomeValidationError:
            # 不在 Workspace 里也要能用:forge 是原语,不该强制先有 Workspace。
            host = "local"
    return select_forge(host)


def _task_artifacts(root: Path, task_id: str, procedure_id: str) -> Path:
    """一次 Procedure 执行的产物目录。

    路径约定集中在 `paths` 与这里,不散落成字面量——散落之后改一个约定要改四处,
    漏一处就是"产物明明写出来了但下游读不到"。
    """
    return root / paths.TASKS / task_id / "artifacts" / procedure_id


def _lane_budget(config: Config, task_id: str) -> int:
    """这个任务该用哪条泳道的 token 额度。规则在 `core.task_ids.lane_budget` 一处。"""
    return lane_budget(config.budgets.per_task_tokens, config.genome_tasks.per_task_tokens, task_id)


def _emit(payload: dict[str, object], as_json: bool, human: str) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2) if as_json else human)


@forge_pr_app.command("create")
def forge_pr_create(
    head: str = typer.Option(..., "--head", help="源分支"),
    base: str = typer.Option(..., "--base", help="目标分支"),
    title: str = typer.Option(..., "--title", help="标题"),
    body: str = typer.Option("", "--body", help="正文"),
    repo: str = RepoOption,
    git_host: str = GitHostOption,
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """开一个 PR。同一 head→base 已有开启中的 PR 时返回它,不重复创建。"""
    forge = _open_forge(workspace, git_host)
    try:
        pr = forge.open_pr(repo, head=head, base=base, title=title, body=body)
    except ForgeError as exc:
        _fail(str(exc))
    _emit(dict(pr.as_dict()), as_json, f"#{pr.number} {pr.head} -> {pr.base}")


@forge_pr_app.command("status")
def forge_pr_status(
    number: int = typer.Argument(..., help="PR 编号"),
    repo: str = RepoOption,
    git_host: str = GitHostOption,
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """查一个 PR 的状态。"""
    forge = _open_forge(workspace, git_host)
    try:
        status = forge.pr_status(PRRef(repo=repo, number=number, head="", base=""))
    except ForgeError as exc:
        _fail(str(exc))
    _emit(
        {
            "number": number,
            "state": status.state.value,
            "title": status.title,
            "merged_rev": status.merged_rev,
        },
        as_json,
        f"#{number} {status.state.value} {status.title}"
        + (f" -> {status.merged_rev}" if status.merged_rev else ""),
    )


@forge_pr_app.command("merge")
def forge_pr_merge(
    number: int = typer.Argument(..., help="PR 编号"),
    repo: str = RepoOption,
    git_host: str = GitHostOption,
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """合并一个 PR。已合并的再合一次返回同一 revision,不报错。"""
    forge = _open_forge(workspace, git_host)
    try:
        rev = forge.merge_pr(PRRef(repo=repo, number=number, head="", base=""))
    except MergeConflict as conflict:
        # 冲突要给出文件清单让人(或下一轮员工)知道该处理哪几个文件,不是抛堆栈。
        _fail(f"合并冲突,涉及 {len(conflict.files)} 个文件:\n  " + "\n  ".join(conflict.files))
    except ForgeError as exc:
        _fail(str(exc))
    _emit({"number": number, "merged_rev": rev}, as_json, rev)


@forge_app.command("protected")
def forge_protected(
    branch: str = typer.Argument(..., help="分支名"),
    repo: str = RepoOption,
    git_host: str = GitHostOption,
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """查一个分支是否受保护。"""
    protected = _open_forge(workspace, git_host).is_protected(repo, branch)
    _emit(
        {"branch": branch, "protected": protected},
        as_json,
        f"{branch}: {'受保护' if protected else '未保护'}",
    )


@job_app.command("run")
def job_run(
    spec_file: Path = typer.Argument(..., help="JobSpec 的 JSON 文件"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只落上下文包,不拉起子进程"),
    as_json: bool = JsonOption,
) -> None:
    """从一个 JobSpec 文件驱动一次执行。

    薄命令,只做原语。任务级的编排(状态机、修复轮次、审批)在 PRD 05/08。
    """
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    argv = payload.pop("argv", None)
    if not argv:
        _fail("JobSpec 文件里需要 argv:指明要拉起哪个命令。")
    for key in ("workdir", "context_file", "output_dir"):
        payload[key] = Path(payload[key])

    result = asyncio.run(SubprocessRuntime(argv=argv).run_job(JobSpec(**payload), dry_run=dry_run))
    _emit(
        result.as_dict(),
        as_json,
        f"{'成功' if result.ok else '失败'} "
        f"failure_kind={result.failure_kind.value} attempts={result.attempts}",
    )
    _warn_about_recording(result.recorded_to, as_json)
    if not result.ok:
        raise typer.Exit(code=1)


def _warn_about_recording(recorded_to: Path | None, as_json: bool) -> None:
    """录制产出必须人工过一遍再入库。

    真实输出里常带环境相关的绝对路径、临时目录名、与本次任务无关的噪声。
    悄悄写完就算了的话,这些东西会跟着录制进库,然后在别人的机器上回放失败。
    """
    if recorded_to is None or as_json:
        return
    typer.secho(f"\n已录制到 {recorded_to}", fg=typer.colors.YELLOW)
    typer.echo("  入库前请人工过一遍:真实输出里常带环境相关的绝对路径与临时目录名。")


def _build_runtimes(config: Config, root: Path | None = None) -> dict[str, AgentRuntime]:
    """按根配置搭出运行时注册表。

    装配本身住在 `agents/factory.py`——**控制面也要用它**,而那边不能依赖 typer。这里只把
    装配失败转成命令行的退出码,不重复任何判断逻辑。两个调用方共用一条路径,"配了哪些
    运行时"因此不会有两个答案。
    """
    try:
        return build_runtimes(config, root)
    except RuntimeAssemblyError as error:
        _fail(str(error))


@knowledge_app.command("plan")
def knowledge_plan(
    workspace: Path = WorkspaceOption,
    as_json: bool = typer.Option(False, "--json", help="输出结构化结果"),
) -> None:
    """扫描仓库、提出模块边界草案，然后停下来等人确认。

    **这是整条初始化里唯一必须人参与的节点。** 一个存量项目的模块划分往往不等于目录划分：
    有些目录是历史遗留该合并，有些单目录里其实塞了两个域——这个判断只有熟悉项目的人做得了。
    划错了后面所有的知识都长在歪的划分上。

    **全异步。** 草案落盘之后这条命令就返回；人可以关掉终端，隔天从命令行或 Web 回答。
    """
    root = workspace.expanduser().resolve()
    config = _collect(lambda: load_config(root), []) or Config()

    # **与 REST(`POST /genome/tasks/init`)共用同一层**:扫描、查重、建任务、落草案
    # 只有一份实现,两条入口对"已有一个在跑"给同一句报错。
    try:
        planned = plan_init(root, config.genome_tasks)
    except (NotReadyForBoundaries, InitAlreadyOpen) as exc:
        _fail(str(exc))

    _emit(
        {
            "task_id": planned.task.id,
            "state": planned.task.state.value,
            "candidates": list(planned.candidates),
        },
        as_json,
        f"边界草案已就绪，{planned.task.id} 在等你确认。\n"
        f"  看：agctl genome confirm {planned.task.id}\n"
        f"  答：agctl genome confirm {planned.task.id} --answer <改好的.json>",
    )


@knowledge_app.command("run")
def knowledge_run(
    task_id: str = typer.Argument("", help="只推这一个任务；不给就把未了结的都推一步"),
    runtime_name: str = typer.Option("claude-code", "--runtime", help="使用哪个运行时"),
    rounds: int = typer.Option(1, "--rounds", help="推几轮。每一轮把每个任务推进一步"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """推进基因组任务：扫描 → 闸门 → 逐模块深读 → 汇总写入。

    **它是那条流水线的驱动。** 此前五个阶段各自就绪但没有任何东西把它们连起来——
    `knowledge plan` 停在闸门、`genome reinit` 只建任务、深读收一个回调、汇总是纯函数。
    那种"每一节都能跑、整条跑不起来"的状态最容易被当成做完了，因为每一节的测试都是绿的。

    **待确认的任务原样不动。** 推它等于替人回答，而那个闸门存在的全部理由就是让人看一眼。
    """
    root = workspace.expanduser().resolve()
    config = _collect(lambda: load_config(root), []) or Config()
    pool = AgentPool(
        _build_runtimes(config, root),
        global_jobs=config.concurrency.global_jobs,
        genome_jobs=config.genome_tasks.concurrent_jobs,
    )
    orchestrator = GenomeOrchestrator(root, pool=pool, runtime_name=runtime_name, config=config)

    async def _run() -> list[GenomeTask]:
        moved: list[GenomeTask] = []
        for _ in range(rounds):
            moved = (
                [await orchestrator.advance(task_id)]
                if task_id
                else list(await orchestrator.drain())
            )
        return moved

    try:
        found = asyncio.run(_run())
    except TaskNotFound as exc:
        _fail(str(exc))
    except (RecordingNotFound, RuntimeNotRegistered) as exc:
        _fail(str(exc))

    _emit(
        {"tasks": [{"task_id": item.id, "state": item.state.value} for item in found]},
        as_json,
        "\n".join(f"{item.id}：{item.state.value}" for item in found) or "没有要推的基因组任务",
    )


@knowledge_app.command("credits")
def knowledge_credits(
    dry_run: bool = typer.Option(False, "--dry-run", help="只报告不写。报告内容与实际执行一致"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """把已结算的功能卡片命中写进卡片。

    **命中计数是知识写入,所以它不在编排器那条路上。** 编排器只记「这几张卡片这次算数了」，
    落进卡片由这条命令做——开了「编排器顺手改一下卡片文件」这个口子，下一个需求会走同一个
    口子，几轮之后编排器就在随便改知识了，而「知识树只由架构员工写入」是整棵树的校验体系
    依赖的前提。有一条断言守着这件事。

    **为什么是一条命令而不是一个 PR。** 计数是机械的、不带判断的数字，一次加一；要求为它开
    一个 PR 的结果是它永远不会被应用——而一个永远不更新的计数，比没有这个计数更糟：它会让
    「哪些知识真正被用上了」这个问题得到一个看起来可信的错误答案。
    """
    root = workspace.expanduser().resolve()
    tree = _collect(lambda: load_tree(root), [])
    if tree is None:
        _fail("知识树不可读：先跑 agctl genome validate 看问题。")

    try:
        peek = pending_credits(root)
    except LedgerUnreadable as exc:
        _fail(str(exc))

    if dry_run:
        # **不取走。** 取走再报告的话，一次 --dry-run 会把这批命中清掉，而它们再也回不来。
        pending = peek
        _emit(
            {"pending": [ref.key for ref in pending], "written": []},
            as_json,
            f"待记账 {len(pending)} 条：{'、'.join(ref.key for ref in pending) or '无'}",
        )
        return

    refs = take_credits(root)
    written = apply_credits(root, tree, refs)
    _emit(
        {"pending": [ref.key for ref in refs], "written": written},
        as_json,
        f"记账 {len(refs)} 条，写进 {len(written)} 张卡片"
        + ("（其余那几张已经不在树里了）" if len(written) < len(refs) else ""),
    )


@knowledge_app.command("init")
def knowledge_init(
    workspace: Path = WorkspaceOption,
    runtime_name: str = typer.Option("claude-code", "--runtime", help="使用哪个运行时"),
    round_: int = typer.Option(1, "--round", help="第几轮(回放按它选录制)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只落上下文包,不拉起员工"),
) -> None:
    """让架构员工通读代码，补全项目地图与模块认知卡片。

    **这是全仓一趟的老路。** 分层知识树那条路是 `knowledge plan` → 人确认边界 →
    `knowledge run`：它逐模块派作业、产出可回放、失败一个不拖垮其余。这条留着是因为小仓
    一趟读完更省事，而且它不需要人在闸门上等一次。
    """
    root = workspace.expanduser().resolve()
    issues: list[ValidationIssue] = []
    config = _collect(lambda: load_config(root), issues) or Config()
    project_map = _collect(lambda: load_project_map(root), issues)
    if project_map is None:
        _fail("项目地图不可读:先跑 agctl init,或用 agctl genome validate 看问题。")
    rules = _collect(lambda: load_rules(root), issues)
    if rules is None:
        # 读不出受保护路径时当作"没有受保护路径"是最糟的降级方向。
        _report_validation_error(GenomeValidationError(issues))

    # **建一条真正的基因组任务,用它的编号跑。**
    #
    # 此前这里以字符串 `knowledge-init` 当 task_id,于是 `lane_of` 把它判进研发泳道、
    # 走研发预算——而它恰恰是「基因组任务要有自己的泳道与预算」这条设计的旗舰场景:
    # 一次全量初始化会派发几十个作业,共用研发闸的话它会把整条研发流水线堵死。
    record = GenomeTaskStore(root).create(
        title="知识初始化",
        kind=GenomeTaskKind.INIT,
        origin=Origin.HUMAN,
        budget_tokens=config.genome_tasks.per_task_tokens,
    )
    task_dir = root / paths.TASKS / record.id
    (task_dir / "context").mkdir(parents=True, exist_ok=True)
    context_file = task_dir / "context" / "prompt.md"
    output_dir = task_dir / "artifacts"
    context_file.write_text(
        knowledge_mod.build_prompt(project_map, root, output_dir=output_dir), encoding="utf-8"
    )

    spec = JobSpec(
        task_id=record.id,
        employee_id=knowledge_mod.EMPLOYEE_ID,
        procedure_id=knowledge_mod.PROCEDURE_ID,
        procedure_version=knowledge_mod.PROCEDURE_VERSION,
        round=round_,
        workdir=root,
        context_file=context_file,
        output_dir=output_dir,
        output_schema=knowledge_mod.RECEIPT_SCHEMA,
        # Job 的成败判据:对 staging/ 跑确定性校验。验证产物,不验证自述(PRD 34)。
        output_check=staging_mod.knowledge_output_check(root, limits=config.knowledge),
        timeout_s=config.limits.job_timeout_s,
        max_tokens=config.budgets.per_job_tokens,
        enforce_token_limit=config.budgets.enforce,
        tools_allow=["Read", "Grep", "Glob", "Bash", "Write"],
        tools_deny=["WebFetch", "WebSearch"],
        # 架构员工只补认知,业务代码由开发员工改。带上 scope 让它走**与其他 Job
        # 同一道**越权检查:那道检查在池里无条件执行,越权即回滚并留下结构化报告。
        # 不带的话这条路径要靠一份自己的写入校验兜底,而那份没有回滚也没有报告。
        scope=ScopePolicy(write_paths=(f"{paths.GENOME}/**",)).with_protected(
            rules.protected.paths_for(knowledge_mod.EMPLOYEE_ID)
        ),
    )

    pool = AgentPool(
        _build_runtimes(config, root),
        global_jobs=config.concurrency.global_jobs,
        genome_jobs=config.genome_tasks.concurrent_jobs,
        task_budgets={
            spec.task_id: _lane_budget(config, spec.task_id) if config.budgets.enforce else None
        },
    )
    try:
        result = asyncio.run(pool.submit(spec, runtime_name=runtime_name, dry_run=dry_run))
    except (RecordingNotFound, RuntimeNotRegistered) as exc:
        _fail(str(exc))

    if dry_run:
        typer.secho(f"已写出上下文包: {context_file}", fg=typer.colors.GREEN)
        return
    if not result.ok or result.result_path is None:
        _fail(f"knowledge-init 失败({result.failure_kind.value}): {result.failure_detail}")

    try:
        # 写入边界已经由池里那道越权检查管住了(见上面的 scope),越权的 Job 根本
        # 走不到这里。契约已经裁决过 staging 合法,这里只负责原子应用进基因组。
        staged = staging_mod.load_knowledge_staging(
            root, output_dir / staging_mod.STAGING_DIR, limits=config.knowledge
        )
        update = staging_mod.apply_staged(root, staged, config.knowledge)
    except GenomeValidationError as error:
        _report_validation_error(error)

    # **把这条记录推到终态。** 不推的话它永远停在扫描中,而 `knowledge run` 的巡检会把它
    # 当成一个待推进的初始化重新扫一遍、停在一个凭空冒出来的闸门上——一个已经跑完的任务
    # 在界面上变成"等你确认"。
    driver = GenomeDriver(
        GenomeTaskStore(root), EventLog(root), enforce_budget=config.budgets.enforce
    )
    driver.deliver(record.id, GenomeEvent.READ_DONE)
    driver.deliver(record.id, GenomeEvent.SUBMITTED)

    typer.secho(
        f"项目认知已更新(project-map v{update.version},写入 {len(update.cards_written)} 张卡片)",
        fg=typer.colors.GREEN,
    )
    for preserved in update.cards_preserved:
        typer.echo(f"  保留人工编辑: {preserved}")


@knowledge_app.command("deepen")
def knowledge_deepen(
    workspace: Path = WorkspaceOption,
    feature: str = typer.Option(
        "", "--feature", help="要深化的功能点,写作 <模块id>/<功能点id>;不给则按队列取最热的"
    ),
    limit: int = typer.Option(1, "--limit", help="不指定 --feature 时,从队列头部取几张"),
    runtime_name: str = typer.Option("claude-code", "--runtime", help="使用哪个运行时"),
    round_: int = typer.Option(1, "--round", help="第几轮(回放按它选录制)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只落上下文包,不拉起员工"),
) -> None:
    """深化一个功能点的认知卡片:三源法(代码 + 测试 + 提交历史),一次一张卡。

    init 出骨架,深化出血肉。队列按 scope 覆盖文件的变更热度降序——中途停下时,
    已深化的恰好是最常被任务碰到的那批卡。
    """
    root = workspace.expanduser().resolve()
    issues: list[ValidationIssue] = []
    config = _collect(lambda: load_config(root), issues) or Config()
    tree = _collect(lambda: load_tree(root), issues)
    if tree is None:
        _fail("知识树不可读:先跑 agctl init 与 knowledge-init,或用 agctl genome validate 看问题。")
    rules = _collect(lambda: load_rules(root), issues)
    if rules is None:
        _report_validation_error(GenomeValidationError(issues))

    module_paths = tree.project_map.module_paths()
    if feature:
        module_id, _, feature_id = feature.partition("/")
        entry = next((item for item in tree.features(module_id) if item.id == feature_id), None)
        if not module_id or not feature_id or entry is None:
            _fail(f"没有这个功能点: {feature}(写作 <模块id>/<功能点id>,且它要有卡片)")
        if entry.card is None:
            _fail(f"{feature} 声明了无需卡片(no_card),没有卡可深化")
        picked = [
            deepen_mod.DeepenCandidate(
                module_id=module_id,
                feature_id=feature_id,
                summary=entry.summary,
                scope=tuple(entry.scope),
                churn=0,
            )
        ]
    else:
        queue = deepen_mod.deepen_queue(tree, deepen_mod.churn_counts(root, module_paths))
        if not queue:
            typer.secho("深化队列是空的:没有可深化的卡片。", fg=typer.colors.GREEN)
            return
        picked = list(queue[: max(1, limit)])

    for candidate in picked:
        module_path = module_paths[candidate.module_id]
        record = GenomeTaskStore(root).create(
            title=f"知识深化 {candidate.module_id}/{candidate.feature_id}",
            kind=GenomeTaskKind.DEEPEN,
            origin=Origin.HUMAN,
            budget_tokens=config.genome_tasks.per_task_tokens,
        )
        task_dir = root / paths.TASKS / record.id
        (task_dir / "context").mkdir(parents=True, exist_ok=True)
        context_file = task_dir / "context" / "prompt.md"
        output_dir = task_dir / "artifacts"
        entry = next(
            item for item in tree.features(candidate.module_id) if item.id == candidate.feature_id
        )
        card_file = module_dir(root, candidate.module_id) / (entry.card or "")
        current_card = (
            card_file.read_text(encoding="utf-8") if entry.card and card_file.is_file() else ""
        )
        context_file.write_text(
            deepen_mod.build_prompt(
                module_id=candidate.module_id,
                module_path=module_path,
                feature=entry,
                current_card=current_card,
                workspace_root=root,
                output_dir=output_dir,
                test_cmd=tree.project_map.module(candidate.module_id).test_cmd,
            ),
            encoding="utf-8",
        )

        spec = JobSpec(
            task_id=record.id,
            employee_id=deepen_mod.EMPLOYEE_ID,
            procedure_id=deepen_mod.PROCEDURE_ID,
            procedure_version=deepen_mod.PROCEDURE_VERSION,
            round=round_,
            # 并行/多张深化同 employee 同 procedure,不带 subject 会撞在一个回放键上。
            subject=f"{candidate.module_id}.{candidate.feature_id}",
            workdir=root,
            context_file=context_file,
            output_dir=output_dir,
            output_schema=deepen_mod.RECEIPT_SCHEMA,
            output_check=deepen_mod.deepen_output_check(
                root,
                candidate.module_id,
                candidate.feature_id,
                module_path,
                config.knowledge,
            ),
            timeout_s=config.limits.job_timeout_s,
            max_tokens=config.budgets.per_job_tokens,
            enforce_token_limit=config.budgets.enforce,
            tools_allow=["Read", "Grep", "Glob", "Bash", "Write"],
            tools_deny=["WebFetch", "WebSearch"],
            scope=ScopePolicy(write_paths=(f"{paths.GENOME}/**",)).with_protected(
                rules.protected.paths_for(deepen_mod.EMPLOYEE_ID)
            ),
        )

        pool = AgentPool(
            _build_runtimes(config, root),
            global_jobs=config.concurrency.global_jobs,
            genome_jobs=config.genome_tasks.concurrent_jobs,
            task_budgets={
                spec.task_id: _lane_budget(config, spec.task_id) if config.budgets.enforce else None
            },
        )
        try:
            result = asyncio.run(pool.submit(spec, runtime_name=runtime_name, dry_run=dry_run))
        except (RecordingNotFound, RuntimeNotRegistered) as exc:
            _fail(str(exc))

        if dry_run:
            typer.secho(f"已写出上下文包: {context_file}", fg=typer.colors.GREEN)
            continue
        if not result.ok or result.result_path is None:
            _fail(
                f"knowledge-deepen {candidate.module_id}/{candidate.feature_id} 失败"
                f"({result.failure_kind.value}): {result.failure_detail}"
            )

        staging_root = output_dir / staging_mod.STAGING_DIR
        card_path = (
            staging_root
            / "modules"
            / candidate.module_id
            / "features"
            / f"{candidate.feature_id}.md"
        )
        try:
            outcome = deepen_mod.apply_deepened(
                root,
                candidate.module_id,
                candidate.feature_id,
                card_path.read_text(encoding="utf-8"),
                config.knowledge,
            )
        except GenomeValidationError as error:
            _report_validation_error(error)

        driver = GenomeDriver(
            GenomeTaskStore(root), EventLog(root), enforce_budget=config.budgets.enforce
        )
        driver.deliver(record.id, GenomeEvent.READ_DONE)
        driver.deliver(record.id, GenomeEvent.SUBMITTED)

        if outcome.written:
            # 改卡是可疑账的两种合法响应之一(另一种是"核对过,无需更新",见
            # `knowledge suspects --resolve`)。深化应用成功即清这张卡的可疑,留痕带任务号。
            with contextlib.suppress(LedgerUnreadable, OSError):
                resolve_suspects(
                    root,
                    card=f"{candidate.module_id}/{candidate.feature_id}",
                    action=ResolutionAction.UPDATED,
                    note=f"深化于 {record.id}",
                )
            typer.secho(
                f"已深化 {candidate.module_id}/{candidate.feature_id} → {outcome.path}",
                fg=typer.colors.GREEN,
            )
        else:
            typer.echo(f"  保留人工编辑: {outcome.path}")


@knowledge_app.command("status")
def knowledge_status(
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """「知识树健康吗」的一个不靠自述的答案:可疑账余额 + 深化队列。**只读。**

    可疑账(ADR-0012)记两类软信号:可疑过期(变更命中卡的覆盖范围而知识没动)与
    教训蒸发(fix 任务的教训没被蒸馏出来)。深化队列按变更热度降序——队头就是
    下一张最值得深化的卡。
    """
    root = workspace.expanduser().resolve()
    tree = _collect(lambda: load_tree(root), [])
    if tree is None:
        _fail("知识树不可读:先跑 agctl genome validate 看问题。")
    try:
        pending = pending_suspects(root)
    except LedgerUnreadable as exc:
        _fail(str(exc))
    queue = deepen_mod.deepen_queue(
        tree, deepen_mod.churn_counts(root, tree.project_map.module_paths())
    )
    payload: dict[str, object] = {
        "suspects": [item.as_dict() for item in pending],
        "deepen_queue": [
            {
                "card": f"{item.module_id}/{item.feature_id}",
                "summary": item.summary,
                "churn": item.churn,
            }
            for item in queue
        ],
    }
    lines: list[str] = []
    if pending:
        lines.append(f"可疑账 {len(pending)} 条(核对走 knowledge suspects --resolve):")
        lines += [
            f"- [{item.kind.value}] {item.card or item.task_id}(来源 {item.task_id})"
            for item in pending
        ]
    else:
        lines.append("可疑账为空:没有等待核对的信号。")
    if queue:
        lines.append(f"深化队列 {len(queue)} 张(热区在前,深化走 knowledge deepen):")
        lines += [
            f"- {item.module_id}/{item.feature_id}(churn {item.churn}):{item.summary}"
            for item in queue
        ]
    else:
        lines.append("深化队列为空:没有可深化的卡片。")
    _emit(payload, as_json, "\n".join(lines))


@knowledge_app.command("suspects")
def knowledge_suspects(
    resolve: str = typer.Option(
        "",
        "--resolve",
        help="声明「核对过,无需更新」:<模块id>/<功能点id> 清可疑过期,任务 id 清蒸发信号",
    ),
    note: str = typer.Option("", "--note", help="核对说明,进留痕"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """看一眼可疑账,或声明「核对过,无需更新」。

    可疑账是软信号(ADR-0012):变更命中了卡的覆盖范围而知识没动、fix 任务的教训没被
    蒸馏出来,都记在这里。响应二选一:改卡走 `knowledge deepen`(应用成功自动清账),
    或用 `--resolve` 声明核对过——两种都算响应,声明不碰知识树一个字节。
    """
    root = workspace.expanduser().resolve()
    try:
        if resolve:
            by_card = "/" in resolve
            resolved = resolve_suspects(
                root,
                card=resolve if by_card else "",
                task_id="" if by_card else resolve,
                action=ResolutionAction.UNCHANGED,
                note=note or "核对过,无需更新",
            )
            _emit(
                {"resolved": [item.key for item in resolved]},
                as_json,
                f"已核对 {len(resolved)} 条:{'、'.join(item.key for item in resolved) or '无匹配'}",
            )
            return
        pending = pending_suspects(root)
    except LedgerUnreadable as exc:
        _fail(str(exc))
    _emit(
        {"suspects": [item.as_dict() for item in pending]},
        as_json,
        "可疑账为空:没有等待核对的信号。"
        if not pending
        else "\n".join(
            f"- [{item.kind.value}] {item.card or item.task_id}"
            f"(来源 {item.task_id};命中 {len(item.changed)} 个文件)"
            for item in pending
        ),
    )


@procedure_app.command("validate")
def procedure_validate(
    path: Path = typer.Argument(..., help="Procedure 目录"),
    as_json: bool = JsonOption,
) -> None:
    """校验一个 Procedure 目录。写新 Procedure 时的反馈回路。"""
    try:
        spec = load_procedure(path.expanduser().resolve())
    except GenomeValidationError as error:
        _report_validation_error(error)
    _emit(
        {"id": spec.id, "version": spec.version, "kind": spec.kind.value},
        as_json,
        f"{spec.ref} ({spec.kind.value}) 校验通过",
    )


@todo_app.command("list")
def todo_list(
    assignee: str = typer.Option("", "--assignee", help="只看这个人的"),
    task_id: str = typer.Option("", "--task", help="只看这个任务的"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """还等着人干的待办。

    **不列已经交掉的**:待办列表是给人看的工作面板,把交掉的混进来,真正等他的那几张会被
    淹掉。要看历史查任务的事件面。
    """
    root = workspace.expanduser().resolve()
    rows = [item.as_dict() for item in TodoStore(root).open_todos(assignee, task_id)]
    if as_json:
        typer.echo(json.dumps({"items": rows}, ensure_ascii=False, indent=2))
        return
    if not rows:
        typer.echo("没有待办。")
        return
    for row in rows:
        typer.echo(
            f"  {row['id']}  {row['task_id']}/{row['stage']}  → {row['assignee']}  ({row['kind']})"
        )


@todo_app.command("show")
def todo_show(
    todo_id: str = typer.Argument(..., help="待办 id"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """一张待办要干什么、要交什么。

    **"要交什么"必须说清楚**:人不知道产物契约的话,会交一份看起来对的东西然后被校验打回,
    而那份挫败感与"这套系统不好用"是同一个东西。
    """
    root = workspace.expanduser().resolve()
    try:
        todo = TodoStore(root).get(todo_id)
    except TodoNotFound as error:
        _fail(str(error))
    schema = todo_schema(root, todo.procedure_id)
    if as_json:
        typer.echo(json.dumps({**todo.as_dict(), "schema": schema}, ensure_ascii=False, indent=2))
        return
    typer.echo(f"{todo.id}  {todo.task_id}/{todo.stage}  → {todo.assignee}")
    typer.echo(f"  工序: {todo.procedure_id}(以 {todo.employee_id} 的身份)")
    typer.echo(f"  上下文包: {todo.context_file}")
    if todo.kind == WORKTREE:
        typer.echo(f"  去这里改代码: {todo.workdir}")
    typer.echo(f"  产物交到: {todo.output_dir}/result.json")
    typer.echo(f"  必填字段: {', '.join(schema.get('required', [])) or '(无)'}")


@todo_app.command("sweep")
def todo_sweep(
    workspace: Path = WorkspaceOption,
    escalate: bool = typer.Option(True, "--escalate/--dry-run", help="第三段动不动真格"),
    as_json: bool = JsonOption,
) -> None:
    """扫一遍到期的待办:提醒 → 改派 → 升级人工。

    **三段而不是一段**:人的待办超时是常态,不是事故。直接升级的话,"等另一个人接管"这句话
    接不回来——已升级人工是终态。
    """
    root = workspace.expanduser().resolve()
    config = _collect(lambda: load_config(root), []) or Config()
    report = sweep_todos(root, config=config, escalate=escalate)
    _emit(
        report.as_dict(),
        as_json,
        f"提醒 {len(report.reminded)} 张,改派 {len(report.reassigned)} 张,"
        f"升级 {len(report.escalated)} 张",
    )


@todo_app.command("submit")
def todo_submit(
    todo_id: str = typer.Argument(..., help="待办 id"),
    result: Path = typer.Option(None, "--result", help="产物 JSON 文件"),
    actor: str = typer.Option("", "--as", help="以谁的身份交(服务端会校验)"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """交活。**过与硅基员工完全相同的契约校验**,没有跳过这条路。"""
    root = workspace.expanduser().resolve()
    payload = None
    if result is not None:
        try:
            payload = json.loads(result.expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            _fail(f"读不出产物 JSON: {error}")
    try:
        submission = submit_todo(root, todo_id, payload=payload, actor=actor)
    except (TodoNotFound, TodoRefused) as error:
        _fail(str(error))
    if not submission.ok:
        _report_contract_failure(submission.detail)

    task = asyncio.run(_advance_after_submit(root, submission.todo.task_id))
    _emit(
        {"todo": submission.todo.id, "task": task.id, "state": task.state.value},
        as_json,
        f"已交付 {submission.todo.id};任务 {task.id} 现在是 {task.state.value}",
    )


def _report_contract_failure(detail: str) -> NoReturn:
    """打回的错误与硅基员工拿到的**是同一份**。

    人这边另写一套"友好提示"的话,两份说法会分叉,而分叉之后没人知道哪份对应真正的校验。
    """
    typer.secho("产物不合契约,没有交上去:", fg=typer.colors.RED)
    for line in (detail or "").splitlines():
        typer.echo(f"  {line}")
    raise typer.Exit(code=1)


async def _advance_after_submit(root: Path, task_id: str) -> Task:
    """交完之后照常推一步。

    **走的是崩溃恢复那条重放路**:产物已经在槽里了,处理器的幂等约定会认出它并抛出本来
    就该抛的事件。不另抛"人交活了"这种事件——那等于给状态机开第二条入口。
    """
    config = _collect(lambda: load_config(root), []) or Config()
    pool = AgentPool(
        _build_runtimes(config, root),
        global_jobs=config.concurrency.global_jobs,
        genome_jobs=config.genome_tasks.concurrent_jobs,
    )
    # **不锁定运行时**:每个员工声明自己的运行时(有的是 human)。这里硬塞一个默认值的话,
    # 人交完活之后的下一步会被派给错误的运行时。
    orchestrator = Orchestrator(root, pool=pool)
    orchestrator.resume(task_id)
    return await orchestrator.advance(task_id)


@topology_app.command("list")
def topology_list(
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """能选的执行拓扑,以及不选时会用哪个。

    **与提交页看到的是同一份名单**(`jobs.catalog`)——命令行上自己列一份的话,加模板时
    这里会悄悄少一个,而少的那个在界面上明明是可选的。
    """
    root = workspace.expanduser().resolve()
    config = _collect(lambda: load_config(root), []) or Config()
    spends = topology_single_path_spends(_open_store(workspace).all_tasks())
    found = topology_options(config, spends)
    if as_json:
        payload = {
            "default": config.topology.default,
            "options": [item.as_dict() for item in found],
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    typer.echo(f"不选时用: {config.topology.default}")
    for item in found:
        mark = "" if item.available else "  (现在选不了)"
        flag = "  实验中" if item.experimental else ""
        typer.echo(f"  {item.id:<14} {item.name}{flag}{mark}")
        typer.echo(f"    {item.summary}")
        # **贵几倍先说,绝对值只在算得出来的时候说。** 编一个绝对值出来比不说更糟:
        # 人会拿它做决定。
        if item.cost_multiplier > 1:
            cost = f"    成本约 {item.cost_multiplier} × 单路"
            if item.cost_estimate_tokens is not None:
                cost += f"(按本项目历史估 {item.cost_estimate_tokens:,} tokens)"
            typer.echo(cost)
        if not item.available:
            typer.echo(f"    {item.unavailable_reason}")


@topology_app.command("validate")
def topology_validate(
    path: Path = typer.Argument(..., help="拓扑模板文件(YAML 或 JSON)"),
    as_json: bool = JsonOption,
) -> None:
    """校验一张执行拓扑图:假边、写集冲突、环、检查节点写集。

    **派发前把图判死,而不是跑起来才知道。** 拆图的下一个读者是 LLM,所以报错逐条、
    指名到具体的边 / glob / 环路径。
    """
    target = path.expanduser().resolve()
    try:
        payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    except OSError as exc:
        _fail(f"读不到模板文件 {target}: {exc}")
    except yaml.YAMLError as exc:
        _fail(f"模板文件不是合法的 YAML/JSON: {exc}")

    try:
        template = parse_template(payload)
    except TopologyParseError as exc:
        _fail(f"模板解析失败: {exc}")

    issues = validate_topology(template)
    if issues:
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "id": template.id,
                        "ok": False,
                        "issues": [
                            {"code": item.code, "where": item.where, "message": item.message}
                            for item in issues
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            typer.secho(f"{template.id} 图校验未通过:", fg=typer.colors.RED)
            for issue in issues:
                typer.echo(f"  - {issue.render()}")
        raise typer.Exit(code=1)

    _emit(
        {"id": template.id, "ok": True, "nodes": len(template.nodes), "edges": len(template.edges)},
        as_json,
        f"{template.id} 校验通过({len(template.nodes)} 个节点,{len(template.edges)} 条边)",
    )


def _open_registry(workspace: Path) -> ProcedureRegistry:
    """两级来源的位置策略住在 genome.procedures 里,这里只是转发。"""
    return load_workspace_registry(workspace.expanduser().resolve())


@procedure_app.command("list")
def procedure_list(
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """列出当前注册的全部 Procedure。"""
    registry = _open_registry(workspace)
    rows = [
        {
            "id": spec.id,
            "version": spec.version,
            "kind": spec.kind.value,
            "source": spec.source.value,
            "available": spec.available,
            "unavailable_reason": spec.unavailable_reason,
            "overrides": registry.overrides[spec.id].value
            if spec.id in registry.overrides
            else None,
        }
        for spec in registry.all()
    ]
    if as_json:
        typer.echo(
            json.dumps(
                {"procedures": rows, "rejected": dict(registry.rejected)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if not rows:
        typer.echo("(没有已注册的 Procedure)")
    for row in rows:
        mark = "" if row["available"] else "  ✗ " + str(row["unavailable_reason"])
        covered = f"(覆盖了 {row['overrides']} 级同名 Procedure)" if row["overrides"] else ""
        head = f"  {row['id']}@{row['version']:<10} {row['kind']:<14} {row['source']:<8}"
        typer.echo(f"{head}{covered}{mark}")
    for procedure_id, reason in registry.rejected.items():
        typer.secho(f"  {procedure_id}: 加载失败,已跳过", fg=typer.colors.YELLOW)
        for line in reason.splitlines():
            typer.echo(f"      {line}")


@procedure_app.command("run")
def procedure_run(
    procedure_id: str = typer.Argument(..., help="Procedure id"),
    state: str = typer.Option("UNIT_TESTING", "--state", help="当前任务状态"),
    inputs: str = typer.Option("{}", "--inputs", help="入参 JSON"),
    task_id: str = typer.Option("ad-hoc", "--task", help="任务编号"),
    employee_id: str = typer.Option("operator", "--employee", help="以哪个员工的身份跑"),
    runtime_name: str = typer.Option(
        "", "--runtime", help="覆盖员工声明的运行时", show_default=False
    ),
    round_: int = typer.Option(1, "--round", help="第几轮"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """以某个员工的身份派发一个 Procedure。薄命令,只做原语——任务级编排在 PRD 05。"""
    root = workspace.expanduser().resolve()
    registry = _open_registry(workspace)
    try:
        spec = registry.get(procedure_id)
    except KeyError as exc:
        _fail(str(exc))

    employee = _resolve_employee(workspace, employee_id)

    try:
        task_state = TaskState(state)
    except ValueError:
        allowed = ", ".join(item.value for item in TaskState)
        _fail(f"未知的任务状态: {state}(可选: {allowed})")

    config = _collect(lambda: load_config(root), []) or Config()
    output_dir = _task_artifacts(root, task_id, procedure_id)
    pool = AgentPool(
        _build_runtimes(config, root),
        global_jobs=config.concurrency.global_jobs,
        genome_jobs=config.genome_tasks.concurrent_jobs,
    )

    try:
        result = asyncio.run(
            dispatch_procedure(
                spec,
                pool=pool,
                employee=employee,
                task_id=task_id,
                state=task_state,
                inputs=json.loads(inputs),
                workdir=root,
                output_dir=output_dir,
                config=config,
                runtime_name=runtime_name or None,
                round_=round_,
                # 计划命中的模块。**手工派发这条路同样要收窄**——它是第四个派发入口,
                # 漏掉的话"按任务收窄"就有一条绕得过去的路,而绕得过去的约束不是约束。
                modules=effective_modules(root, task_id),
            )
        )
    except (DispatchRefused, ProcedureNotAllowed, RuntimeNotRegistered, RecordingNotFound) as exc:
        _fail(str(exc))

    _emit(
        result.as_dict(),
        as_json,
        f"{result.procedure_ref} {'成功' if result.ok else '失败'} "
        f"stages={'+'.join(result.stages)} failure_kind={result.failure_kind.value}",
    )
    if not result.ok:
        raise typer.Exit(code=1)


def _exclusive_procedures(root: Path) -> tuple[str, ...]:
    """这个工作区里归属排他的工序。**内置清单与工序自己的声明取并集。**

    只认工序声明的话,存量工作区查不出问题——脚手架不覆盖已存在的文件,那些老的
    `procedure.yaml` 里根本没有 `ownership` 这个键,而它们恰好就是最需要被查出来的那批。
    只认内置清单的话,项目自己新增的 plan 类工序不受保护。
    """
    declared: tuple[str, ...] = ()
    try:
        declared = load_workspace_registry(root).exclusive_ids()
    except Exception:  # noqa: BLE001 —— 工序读不出来时不该连"员工归属对不对"都答不了
        declared = ()
    return tuple(dict.fromkeys((*PLAN_PROCEDURES, *declared)))


def _open_employees(workspace: Path) -> EmployeeRegistry:
    """非严格加载:一个手滑写坏的员工不该让整份名单看不了,坏的那份单独报出来。"""
    root = workspace.expanduser().resolve()
    return load_employees(
        workspace_employees_root(root), strict=False, exclusive=_exclusive_procedures(root)
    )


def _resolve_employee(workspace: Path, employee_id: str) -> EmployeeConfig:
    try:
        return _open_employees(workspace).get(employee_id)
    except EmployeeNotFound as exc:
        _fail(str(exc))


def _provision_targets(workspace: Path, employee_ids: list[str]) -> list[EmployeeConfig]:
    """这次要对齐哪些员工。

    显式点名的员工若跑在本地运行时,**报错而不是跳过**——点了名却什么都没发生,
    使用者会以为成功了。不点名(整份花名册)时跳过它们并如实说明。
    """
    registry = _open_employees(workspace)
    if employee_ids:
        chosen = [_resolve_employee(workspace, employee_id) for employee_id in employee_ids]
        for employee in chosen:
            if employee.runtime != AGENTTEAMS_RUNTIME:
                _fail(
                    f"员工 {employee.id} 跑在 {employee.runtime} 上,不是容器运行时——"
                    f"它没有 Worker 可言。要让它跑在容器里,先把定义里的 runtime 改成 "
                    f"{AGENTTEAMS_RUNTIME}。"
                )
        return chosen
    return [e for e in registry.all() if e.runtime == AGENTTEAMS_RUNTIME]


def _run_lifecycle(
    provisioner: WorkerProvisioner,
    targets: list[EmployeeConfig],
    sleep: bool,
    root: Path,
    as_json: bool,
) -> None:
    """休眠或删除这些员工的 Worker。**只作用于我们供应的那些**——所有权由供应层守。"""
    action = "slept" if sleep else "deleted"
    done: list[str] = []
    failed: list[dict[str, str]] = []
    for employee in targets:
        call = provisioner.sleep if sleep else provisioner.delete
        try:
            asyncio.run(call(employee.id))
        except (ProvisionError, PlatformUnavailable) as error:
            failed.append({"employee_id": employee.id, "error": str(error)})
            continue
        # 休眠与删除同样是编制变化,记录平面对它们没有例外。
        record_lifecycle(
            root,
            actor=ORCHESTRATOR,
            employee_id=employee.id,
            action=action,
            entrance=Entrance.CLI,
        )
        done.append(employee.id)
    _emit(
        {action: done, "failed": failed},
        as_json,
        "\n".join(
            [f"  {employee_id:<22} {'已休眠' if sleep else '已删除'}" for employee_id in done]
            + [f"  {row['employee_id']:<22} 失败:{row['error']}" for row in failed]
        )
        or "(没有要处理的员工)",
    )
    if failed:
        raise typer.Exit(code=1)


@employee_app.command("provision")
def employee_provision(
    employee_ids: list[str] = typer.Argument(None, help="员工 id;不给表示整份花名册"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印计划,不碰平台"),
    sleep: bool = typer.Option(False, "--sleep", help="让这些员工的 Worker 休眠"),
    delete: bool = typer.Option(False, "--delete", help="删除这些员工的 Worker"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """把数字员工对齐成容器平台上的 Worker。声明式、幂等,可反复执行。

    **显式预热与批量管理入口**:正常派发缺 Worker 时会自动幂等供应;这条命令让管理员
    提前预览、预热或批量对齐,避免第一份 Job 等待容器启动。
    """
    if sleep and delete:
        _fail("--sleep 与 --delete 只能给一个:删掉的东西没法休眠。")
    root = workspace.expanduser().resolve()
    targets = _provision_targets(workspace, list(employee_ids or []))
    skipped = [
        {"employee_id": e.id, "runtime": e.runtime, "action": "skipped"}
        for e in _open_employees(workspace).all()
        if e.runtime != AGENTTEAMS_RUNTIME and not employee_ids
    ]

    config = load_config(root)
    try:
        provisioner = build_provisioner(config)
        # 档位在动手之前全部翻译一遍:配置错误该在**一个员工都没推上去**时暴露,
        # 而不是推了一半才炸。
        tiers = tiers_from(config.runtime.runtimes[AGENTTEAMS_RUNTIME])
        for employee in targets:
            build_payload(employee, tiers)
    except (ProvisionUnavailable, UnknownModelTier) as error:
        _fail(str(error))

    if dry_run:
        # 预览**只读不写**,也不进事件面——没发生的事不该留下记录。算法与界面共用
        # 一处(`provision.plan`):两处各算一遍的话,命令行说"无变化"、界面说"更新",
        # 而没人能判断哪个是对的。
        planned = [
            {"employee_id": row.employee_id, "action": row.action, "detail": row.detail}
            for row in asyncio.run(plan_provision(provisioner, targets, tiers))
        ]
        _emit(
            {"dry_run": True, "planned": planned, "skipped": skipped},
            as_json,
            "\n".join(
                [f"  {row['employee_id']:<22} {row['action']}" for row in planned]
                + [f"  {row['employee_id']:<22} 跳过({row['runtime']})" for row in skipped]
            )
            or "(没有要对齐的员工)",
        )
        return

    if sleep or delete:
        _run_lifecycle(provisioner, targets, sleep=sleep, root=root, as_json=as_json)
        return

    done: list[dict[str, object]] = []
    failed: list[dict[str, str]] = []
    for employee in targets:
        try:
            outcome = asyncio.run(provisioner.reconcile(employee))
        except (ProvisionError, PlatformUnavailable) as error:
            # 一个失败不拖垮其余:一次小故障不该让整份花名册停在半路。
            failed.append({"employee_id": employee.id, "error": str(error)})
            continue
        # 载荷的形状与界面入口共用一处(`agentteams.records`):各写一份的话,同一次对齐
        # 从两个入口做会在事件面上留下两种形状,而按 action 统计的报表要先学会两种方言。
        record_provision(
            root,
            actor=ORCHESTRATOR,
            employee_id=employee.id,
            ref=outcome.ref,
            action=outcome.action,
            entrance=Entrance.CLI,
        )
        done.append(
            {
                "employee_id": employee.id,
                "worker": outcome.ref.name,
                "room": outcome.ref.room_id,
                "action": outcome.action,
            }
        )

    _emit(
        {"provisioned": done, "skipped": skipped, "failed": failed},
        as_json,
        "\n".join(
            [
                f"  {row['employee_id']:<22} {row['action']:<10} {row['worker']} @ {row['room']}"
                for row in done
            ]
            + [f"  {row['employee_id']:<22} 跳过({row['runtime']})" for row in skipped]
            + [f"  {row['employee_id']:<22} 失败:{row['error']}" for row in failed]
        )
        or "(没有要对齐的员工)",
    )
    if failed:
        raise typer.Exit(code=1)


@employee_app.command("list")
def employee_list(
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """列出全部员工及其运行时与 Procedure 数。"""
    registry = _open_employees(workspace)
    rows = [
        {
            "id": employee.id,
            "runtime": employee.runtime,
            "model": employee.model,
            "procedures": len(employee.procedures),
        }
        for employee in registry.all()
    ]
    if as_json:
        typer.echo(
            json.dumps(
                {"employees": rows, "rejected": dict(registry.rejected)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if not rows:
        typer.echo("(没有已定义的员工)")
    for row in rows:
        typer.echo(
            f"  {row['id']:<20} {row['runtime']:<14} {row['model']:<10} {row['procedures']} 个工序"
        )
    for employee_id, reason in registry.rejected.items():
        typer.secho(f"  {employee_id}: 加载失败,已跳过", fg=typer.colors.YELLOW)
        for line in reason.splitlines():
            typer.echo(f"      {line}")
    for conflict in registry.conflicts:
        typer.secho(f"  归属冲突:{conflict}", fg=typer.colors.RED)


@roster_app.command("migrate")
def roster_migrate(
    workspace: Path = WorkspaceOption,
    yes: bool = typer.Option(False, "--yes", "-y", help="不问,直接写"),
) -> None:
    """把花名册迁到当前形状:补齐缺的员工,收敛已移交工序的归属。

    **只改工序白名单那一个键。** 限额、提示词、权限、注释一个字都不动——它们是使用者
    调过的东西,一次迁移把它们重排的话,下次没人敢跑这条命令。
    """
    root = workspace.expanduser().resolve()
    migration = plan_migration(root)
    if migration.is_empty:
        typer.secho("花名册已经是当前形状,无需迁移。", fg=typer.colors.GREEN)
        return

    _describe_migration(migration)
    if not yes:
        typer.confirm("按上面的改动写盘?", abort=True)
    run_migration(root)
    typer.secho(
        f"已迁移:补齐 {len(migration.added)} 份定义,收敛 {len(migration.rewritten)} 份白名单。",
        fg=typer.colors.GREEN,
    )


def _describe_migration(migration: Migration) -> None:
    if migration.added:
        typer.secho("将补齐这些员工定义(已存在的一律不覆盖):", bold=True)
        for name in migration.added:
            typer.echo(f"  + {name}")
    if migration.diff:
        typer.secho("将做这些改写:", bold=True)
        typer.echo(migration.diff)
    if migration.kept:
        # 静默跳过的话,"迁移跑过了"会被读成"schema 升上去了",而它没有。
        typer.secho("这些文件你改过,迁移不动它们,需要手动合并当前版的改动:", bold=True)
        for name in migration.kept:
            typer.echo(f"  ! {name}")


@session_app.command("check")
def session_check(
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """会话链路通不通:解析运行时,逐个员工报告能不能开会话。

    **这个问题以前只能靠点界面来回答。** 而它曾经在生产里是断的——控制面攥着一个永远为空
    的运行时注册表,任何创建会话的请求都以 409 告终,却没有任何测试或命令走过那条路
    (见 PRD 29)。部署完能验一下,是这条命令存在的全部理由。

    **不发消息、不烧 token。** 装配通没通、能力判断对不对,这两件事不需要任何 token 就能
    验证完;而要烧 token 的检查进不了冒烟,那等于没有。真想跑一次问答,用 `agctl job run`。
    """
    root = workspace.expanduser().resolve()
    config = _collect(lambda: load_config(root), []) or Config()
    try:
        openable = build_session_runtimes(config)
    except RuntimeAssemblyError as error:
        _fail(str(error))

    registry = _open_employees(workspace)
    rows = [
        {
            "id": employee.id,
            "runtime": employee.runtime,
            "can_session": employee.runtime in openable,
            "reason": ""
            if employee.runtime in openable
            else why_no_session(config, employee.runtime),
        }
        for employee in registry.all()
    ]
    usable = [row for row in rows if row["can_session"]]

    if as_json:
        typer.echo(
            json.dumps(
                {"runtimes": sorted(openable), "employees": rows, "usable": len(usable)},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(f"能开会话的运行时: {', '.join(sorted(openable)) or '(无)'}")
        for row in rows:
            mark = "✓" if row["can_session"] else "✗"
            tail = "" if row["can_session"] else f"  —— {row['reason']}"
            typer.echo(f"  {mark} {row['id']:<20} {row['runtime']}{tail}")

    # **一个员工都开不了会话时以非零退出。** 冒烟检查要能靠退出码判断,而"打印了一堆
    # ✗ 然后退出码 0"会让它在流水线里静默通过。
    if not usable:
        _fail("没有任何员工能开会话——对话功能在这个工作区上不可用。")


@employee_app.command("execution")
def employee_execution(
    employee_id: str = typer.Argument(..., help="员工 id"),
    rung: str = typer.Argument(..., help="auto / assisted / manual"),
    actor: str = typer.Option(..., "--as", help="你的身份。事件面记的就是这个名字"),
    assignee: str = typer.Option("", "--assignee", help="这一档的活归谁。manual 必填"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """把一个员工挪到信任爬坡的某一档:auto / assisted / manual。

    **与界面同一条写入路径**(`server.employees_edit`):三档住在两个存储上,分派在那一层
    做完了。命令行自己拼一遍的话,拼错的那一次会造出"human 却又在确认名单里"这种状态。
    """
    root = workspace.expanduser().resolve()
    # `--as` 是自报的,与 `settings set` 同一条理由:能敲这条命令的人本来就能直接编辑
    # 那个文件。**权限的那道闸在服务端。**
    principal = Principal(actor, frozenset({Role.ADMIN}))
    try:
        change = set_execution(
            root, principal, employee_id, rung, assignee=assignee, entrance=Entrance.CLI
        )
    except EmployeeNotFound as exc:
        _fail(str(exc))
    except (UnknownRung, NeedsAssignee) as exc:
        _fail(str(exc))
    except GenomeValidationError as exc:
        _report_validation_error(exc)
    except GitError as exc:
        _fail(f"没能提交进版本库，这次修改已回滚: {exc}")

    _emit(change.as_dict(), as_json, f"{employee_id} 现在是 {rung}(提交 {change.rev[:8] or '—'})")


@employee_app.command("show")
def employee_show(
    employee_id: str = typer.Argument(..., help="员工 id"),
    task_id: str = typer.Option("", "--task", help="按这个任务展开占位符"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """打印一个员工的有效配置。

    覆盖与默认值叠加之后的结果人是算不清的——这条命令的全部意义就是让机器算。
    """
    employee = _resolve_employee(workspace, employee_id)
    payload = employee.as_dict(
        task_id or None, effective_mount_paths(workspace.expanduser().resolve(), task_id)
    )
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    typer.echo(f"{employee.id}  runtime={employee.runtime}  model={employee.model}")
    typer.echo(f"  提示词: {employee.prompt}")
    typer.echo(f"  Procedure: {', '.join(payload['procedures']) or '(空,什么都不能调用)'}")
    typer.echo(f"  工具允许: {', '.join(payload['tools']['allow']) or '(未限制条目)'}")
    typer.echo(f"  工具禁止: {', '.join(payload['tools']['deny']) or '(无)'}")
    typer.echo(f"  可写: {', '.join(payload['permissions']['write_paths']) or '(空,什么都不能写)'}")
    typer.echo(f"  禁写: {', '.join(payload['permissions']['forbid_paths']) or '(无)'}")
    typer.echo(
        f"  限额: 超时={payload['limits']['job_timeout_s'] or '按部署参数'} "
        f"token={payload['limits']['max_tokens_per_job'] or '按部署参数'}"
    )
    typer.echo(f"  凭证: {', '.join(payload['credentials']) or '(无)'}")


@context_app.command("build")
def context_build(
    employee_id: str = typer.Option(..., "--employee", help="以哪个员工的身份组装"),
    procedure_id: str = typer.Option(..., "--procedure", help="本次要执行的 Procedure"),
    task_id: str = typer.Option("ad-hoc", "--task", help="任务编号"),
    round_: int = typer.Option(1, "--round", help="第几轮"),
    module: list[str] = typer.Option(
        [], "--module", "-m", help="本次涉及的模块 id,可重复;不给则不做过滤"
    ),
    requirement: str = typer.Option("", "--requirement", help="需求原文"),
    inputs: str = typer.Option("{}", "--inputs", help="Procedure 入参 JSON"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """组装一份完整上下文包并落盘。

    "当时它到底看到了什么"必须是一个能被回答的问题,所以这一层要能被一条命令单独驱动。
    """
    root = workspace.expanduser().resolve()
    employee = _resolve_employee(workspace, employee_id)
    registry = _open_registry(workspace)
    try:
        spec = registry.get(procedure_id)
    except KeyError as exc:
        _fail(str(exc))

    config = _collect(lambda: load_config(root), []) or Config()
    try:
        genome = load_genome_slice(root, module)
    except GenomeValidationError as error:
        _report_validation_error(error)

    bundle = assemble(
        ContextInputs(
            employee=employee,
            procedure_prompt=spec.prompt,
            procedure_inputs=json.loads(inputs),
            requirement=requirement,
            failures=_load_failures(root, task_id, procedure_id, round_),
            genome=genome,
        ),
        budget_tokens=context_budget(employee, config),
    )

    output_dir = _task_artifacts(root, task_id, procedure_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / context_bundle_filename(round_)
    target.write_text(bundle.text, encoding="utf-8")

    payload = bundle.as_dict() | {"path": str(target)}
    _emit(
        payload,
        as_json,
        f"上下文包已落盘: {target}\n"
        f"  估算 {bundle.estimated_tokens} token / 预算 {bundle.budget_tokens}\n"
        f"  截断: {'是,略去 ' + ', '.join(bundle.dropped) if bundle.truncated else '否'}",
    )


def _open_store(workspace: Path) -> TaskStore:
    return TaskStore(workspace.expanduser().resolve())


def _task_row(task: Task) -> dict[str, object]:
    return {
        "id": task.id,
        "title": task.title,
        "state": task.state.value,
        "priority": task.priority,
        "fix_rounds": task.fix_rounds,
        "tokens_used": task.tokens_used,
        "needs_itest": task.needs_itest.value,
        "itest_override": task.itest_override.value,
        # 空表示跟随项目缺省。**照原样给出去**,不在这里替人展开成具体模板名。
        "topology": task.topology,
    }


def _load_failures(
    root: Path, task_id: str, procedure_id: str, round_: int
) -> tuple[FailureReport, ...]:
    """把此前各轮的失败报告读回来。**全量,不只是最近一轮。**

    位置由 `jobs.reports` 单一定义:状态机在那里写、这里读,两处各写一遍路径的话,
    改一次约定就会有一边读不到——而"读不到"表现为员工看不见上一轮的失败,没有报错。
    """
    records = read_failure_reports(task_dir(Path(root), task_id), before_round=round_)
    return tuple(
        FailureReport(round=record.round, title=record.title, body=record.body)
        for record in records
    )


@task_app.command("submit")
def task_submit(
    requirement: str = typer.Option("", "--requirement", help="需求原文"),
    requirement_file: Path = typer.Option(
        None, "--requirement-file", help="从文件读需求原文", show_default=False
    ),
    title: str = typer.Option("", "--title", help="任务标题,缺省取需求首行"),
    priority: int = typer.Option(5, "--priority", help="优先级,越大越先跑"),
    budget: int = typer.Option(0, "--budget", help="任务 token 预算,0 表示用根配置"),
    itest: ItestOverride = typer.Option(
        ItestOverride.AUTO.value, "--itest", help="集成测试:always 必跑、never 跳过、auto 自动判定"
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help="结对开发:DEVELOPING 态由会话驱动,后续照走门禁与审批",
    ),
    topology: str = typer.Option(
        "",
        "--topology",
        help="执行拓扑。留空跟随项目缺省;能选的见 `agctl topology list`",
    ),
    requirement_id: str = typer.Option(
        "",
        "--requirement-id",
        help="在这个既有需求下发起新尝试(「再试一次」)。留空则新建需求",
    ),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """提交一个需求,建出任务。"""
    root = workspace.expanduser().resolve()
    if requirement_file is not None:
        if not requirement_file.is_file():
            _fail(f"需求文件不存在: {requirement_file}")
        requirement = requirement_file.read_text(encoding="utf-8")
    if not requirement.strip():
        _fail("需求不能为空:用 --requirement 或 --requirement-file 给一份。")
    # **与 REST 共用这一个校验**(`jobs.catalog.check_choice`),于是两条入口的报错逐字
    # 相同。各判一次的话,第一次改文案就会分叉,而分叉之后没有任何测试会红。
    try:
        check_topology_choice(topology)
    except UnknownTopology as error:
        _fail(str(error))

    # 还在初始化(有业务仓没挂上)的项目提不了研发任务——与 REST 共用同一句拒绝
    # (`scaffold.unmounted_refusal`),两条入口的文案不许分叉。
    refusal = unmounted_refusal(root)
    if refusal:
        _fail(refusal)

    config = _collect(lambda: load_config(root), []) or Config()
    store = _open_store(workspace)
    if requirement_id:
        try:
            RequirementStore(root).get(requirement_id)
            retry = store.create_retry(
                requirement_id=requirement_id,
                title=title or _first_line(requirement),
                requirement=requirement,
                priority=priority,
                budget_tokens=budget or config.budgets.per_task_tokens,
                itest_override=itest,
                mode=TaskMode.INTERACTIVE if interactive else TaskMode.AUTONOMOUS,
                topology=topology,
            )
        except (RequirementNotFound, AttemptConflict) as error:
            _fail(str(error))
        task = retry.task
        born_id = requirement_id
        if retry.requirement_changed:
            EventLog(root).append(
                born_id,
                actor=ORCHESTRATOR,
                kind=LogKind.REQUIREMENT_CHANGED,
                payload={"action": "text", "via": "retry"},
            )
    else:
        born = requirement_intake(
            root,
            title=title or _first_line(requirement),
            text=requirement,
            priority=priority,
            actor=ORCHESTRATOR,
        )
        born_id = born.id
        task = store.create(
            title=title or _first_line(requirement),
            requirement=requirement,
            priority=priority,
            budget_tokens=budget or config.budgets.per_task_tokens,
            itest_override=itest,
            mode=TaskMode.INTERACTIVE if interactive else TaskMode.AUTONOMOUS,
            topology=topology,
            requirement_id=born_id,
        )
    EventLog(root).append(
        task.id,
        actor=ORCHESTRATOR,
        kind=LogKind.TASK_CREATED,
        payload={
            "title": task.title,
            "priority": task.priority,
            "budget": task.budget_tokens,
            "itest_override": task.itest_override.value,
            "mode": task.mode.value,
            "requirement_id": born_id,
        },
    )
    _emit(_task_row(task), as_json, f"已建任务 {task.id}: {task.title}")


@task_app.command("itest")
def task_itest(
    task_id: str = typer.Argument(..., help="任务编号"),
    mode: ItestOverride = typer.Argument(..., help="always 必跑、never 跳过、auto 交回自动判定"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """任务运行中改变集成测试的人工覆盖。

    改回 `auto` 会把已有判定清成"未判定",下一次门禁通过时重新走规则与兜底——不清的话
    "交回自动判定"只会保留人当初强加的那个结论,与命令名说的正好相反。
    """
    store = _open_store(workspace)
    try:
        task = store.get(task_id)
    except TaskNotFound as exc:
        _fail(str(exc))
    if task.is_terminal:
        _fail(f"{task_id} 已经是终态({task.state.value}),改集成测试开关没有意义。")

    decision = manual_decision(mode)
    updated = store.save(
        task.evolve(
            itest_override=mode,
            needs_itest=decision.need if decision else ItestNeed.UNDECIDED,
        )
    )
    EventLog(workspace.expanduser().resolve()).append(
        task_id,
        actor=ORCHESTRATOR,
        kind=LogKind.ITEST_DECISION,
        payload=(decision or _AUTO_HANDBACK).as_dict(),
    )
    _emit(
        _task_row(updated),
        as_json,
        f"{task_id} 的集成测试开关设为 {mode.value},当前判定: {updated.needs_itest.value}",
    )


#: 交回自动判定时记进事件流的那一条。判定被清空这件事本身要留痕。
_AUTO_HANDBACK = ItestDecision(
    need=ItestNeed.UNDECIDED,
    source=DecisionSource.MANUAL,
    reason="人工交回自动判定,已清空此前的结论",
)


def _first_line(requirement: str) -> str:
    """标题缺省取需求首行。去掉 Markdown 标题标记——`# 预占库存` 当标题时那个井号是噪声。"""
    for line in requirement.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return "(无标题)"


@task_app.command("status")
def task_status(
    task_id: str = typer.Argument(..., help="任务编号"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """查一个任务当前在哪。"""
    try:
        task = _open_store(workspace).get(task_id)
    except TaskNotFound as exc:
        _fail(str(exc))

    if as_json:
        typer.echo(json.dumps(task.as_dict(), ensure_ascii=False, indent=2))
        return
    typer.echo(f"{task.id}  {task.state.value}  {task.title}")
    typer.echo(f"  轮次: {task.fix_rounds}  优先级: {task.priority}")
    typer.echo(f"  集成测试: {task.needs_itest.value}(人工覆盖: {task.itest_override.value})")
    typer.echo(f"  token: {task.tokens_used} / {task.budget_tokens or '(未设上限)'}")
    typer.echo(f"  分支: {task.branch or '(未建)'}")
    if task.escalate_reason:
        typer.secho(f"  升级原因: {task.escalate_reason}", fg=typer.colors.YELLOW)


@task_app.command("list")
def task_list(
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """列出还需要人过问的任务(含已升级人工),按优先级排序。"""
    tasks = _open_store(workspace).unsettled_tasks()
    if as_json:
        typer.echo(
            json.dumps({"tasks": [_task_row(task) for task in tasks]}, ensure_ascii=False, indent=2)
        )
        return
    if not tasks:
        typer.echo("(没有要过问的任务)")
        return
    for task in tasks:
        typer.echo(f"  {task.id}  P{task.priority}  {task.state.value:<20} {task.title}")


def _requirement_row(
    requirement: Requirement, attempts: tuple[Task, ...], state: RequirementState
) -> dict[str, object]:
    return {
        "id": requirement.id,
        "title": requirement.title,
        "state": state.value,
        "priority": requirement.priority,
        "attempts": len(attempts),
        "parked": requirement.parked,
        "parent_id": requirement.parent_id,
    }


@requirement_app.command("list")
def requirement_list(
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """列出全部需求与它们算出来的状态,新的在前。"""
    root = workspace.expanduser().resolve()
    scene = RequirementScene.load(root)
    rows = [
        _requirement_row(item, scene.attempts(item.id), scene.states[item.id])
        for item in scene.requirements
    ]
    lines = [
        f"  {row['id']}  P{row['priority']}  {row['state']:<12} "
        f"尝试 {row['attempts']}  {row['title']}"
        for row in rows
    ] or ["(还没有需求)"]
    _emit({"requirements": rows}, as_json, "\n".join(lines))


@requirement_app.command("show")
def requirement_show(
    requirement_id: str = typer.Argument(..., help="需求编号(req- 前缀)"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """一个需求的详情:当前文本、尝试链、累计 token。"""
    root = workspace.expanduser().resolve()
    try:
        requirement = RequirementStore(root).get(requirement_id)
    except RequirementNotFound as error:
        _fail(str(error))
    scene = RequirementScene.load(root)
    attempts = scene.attempts(requirement_id)
    payload = _requirement_row(requirement, attempts, scene.states[requirement_id]) | {
        "text": requirement.text,
        "chain": [
            {
                "id": attempt.id,
                "state": attempt.state.value,
                "escalate_reason": attempt.escalate_reason,
                "tokens_used": attempt.tokens_used,
            }
            for attempt in attempts
        ],
        "total_tokens": sum(attempt.tokens_used for attempt in attempts),
    }
    lines = [f"{requirement.id}  {payload['state']}  {requirement.title}", requirement.text, ""]
    for attempt in attempts:
        reason = f"  ({attempt.escalate_reason})" if attempt.escalate_reason else ""
        lines.append(f"  {attempt.id}  {attempt.state.value}{reason}")
    _emit(payload, as_json, "\n".join(lines))


@requirement_app.command("tree")
def requirement_tree(
    requirement_id: str = typer.Argument(..., help="母需求编号(req- 前缀)"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """一棵需求树:子需求、依赖、进度、树级累计 token。

    与 REST 的需求详情同一份内容(PRD 48 D8)。CLI 单步语义下被解锁的子需求只建不推,
    这里把"待推进"的数报出来——不报的话,CLI 用户会以为派发规则坏了(R4)。
    """
    root = workspace.expanduser().resolve()
    try:
        requirement = RequirementStore(root).get(requirement_id)
    except RequirementNotFound as error:
        _fail(str(error))
    scene = RequirementScene.load(root)
    states = scene.states
    children = list(scene.children(requirement_id))

    delivered = sum(1 for child in children if states[child.id].value == "delivered")
    # 待推进的两类:条件齐备但尝试还没被建出来的(派发判据与编排器同一份——
    # `RequirementScene.ready_to_start`),以及尝试建了却停在可推进状态的(CLI 只建
    # 不推的产物)。两类都指名道姓,不然 CLI 用户会以为派发规则坏了(PRD 48 R4)。
    pending_start = [child.id for child in scene.ready_to_start(requirement_id)]
    pending_push = [
        child.id
        for child in children
        if scene.attempts(child.id) and can_advance(scene.attempts(child.id)[-1])
    ]
    payload: dict[str, object] = {
        "id": requirement.id,
        "title": requirement.title,
        "state": states[requirement_id].value,
        "children": [
            {
                "id": child.id,
                "title": child.title,
                "state": states[child.id].value,
                "blocked_by": list(child.blocked_by),
                "attempts": len(scene.attempts(child.id)),
                "parked": child.parked,
                "last_attempt_state": (
                    scene.attempts(child.id)[-1].state.value
                    if scene.attempts(child.id)
                    else ""
                ),
            }
            for child in children
        ],
        "children_delivered": delivered,
        "children_total": len(children),
        "tree_tokens": scene.tree_tokens(requirement_id),
        "pending_start": pending_start,
        "pending_push": pending_push,
    }
    lines = [
        f"{requirement.id}  {payload['state']}  {requirement.title}",
        f"  进度: {delivered}/{len(children)} 子需求已交付  树级 token: {payload['tree_tokens']}",
    ]
    for child in children:
        deps = f"  依赖 {', '.join(child.blocked_by)}" if child.blocked_by else ""
        parked = f"  [搁置: {child.parked}]" if child.parked else ""
        lines.append(
            f"  {child.id}  {states[child.id].value:<12} "
            f"尝试 {len(scene.attempts(child.id))}  {child.title}{deps}{parked}"
        )
    stalled = pending_start + pending_push
    if stalled:
        lines.append(
            f"  {len(stalled)} 个子需求待推进(CLI 只建不推,用 agctl task run 推): "
            + ", ".join(stalled)
        )
    _emit(payload, as_json, "\n".join(lines))


@requirement_app.command("resplit")
def requirement_resplit(
    requirement_id: str = typer.Argument(..., help="母需求编号"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """对一棵树发起「重新拆分剩余」:范围 = 没开工的子需求;已开工/已交付/已搁置的不动。

    与 REST 共用同一份判断(`Orchestrator.start_resplit`),报错正文逐字相同。
    CLI 单步语义只建不推(PRD 48 R4):提案任务要用 `agctl task run` 推。
    """
    root = workspace.expanduser().resolve()
    try:
        task = Orchestrator(root).start_resplit(requirement_id, actor=ORCHESTRATOR)
    except (RequirementNotFound, ValueError) as error:
        _fail(str(error))
    _emit(
        {"task_id": task.id, "requirement_id": requirement_id},
        as_json,
        f"已发起重拆分提案任务 {task.id}(只建不推,用 agctl task run 推进)",
    )


@requirement_app.command("park")
def requirement_park(
    requirement_id: str = typer.Argument(..., help="需求编号"),
    reason: str = typer.Option(..., "--reason", help="为什么搁置。必填,空原因会被拒"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """搁置一个需求:从排队里消失,但不假装交付了,也不取消进行中的尝试。"""
    root = workspace.expanduser().resolve()
    try:
        requirement = requirement_revise(root, requirement_id, actor=ORCHESTRATOR, park=reason)
    except (RequirementNotFound, ValueError) as error:
        _fail(str(error))
    _emit(
        {"id": requirement.id, "parked": requirement.parked},
        as_json,
        f"已搁置 {requirement.id}: {requirement.parked}",
    )


@requirement_app.command("resume")
def requirement_resume(
    requirement_id: str = typer.Argument(..., help="需求编号"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """恢复一个搁置的需求,状态回到从尝试链推导的值。"""
    root = workspace.expanduser().resolve()
    try:
        requirement = requirement_revise(root, requirement_id, actor=ORCHESTRATOR, resume=True)
    except (RequirementNotFound, ValueError) as error:
        _fail(str(error))
    # 恢复解冻派发:搁置期间交付过的子需求解锁的兄弟与收口在这里补上。
    # CLI 单步语义只建不推(PRD 48 R4),建了几个由 `requirement tree` 报出来。
    created = Orchestrator(root).sweep_requirement_tree(requirement_id)
    payload: dict[str, object] = {
        "id": requirement.id,
        "parked": "",
        "dispatched": [task.id for task in created],
    }
    note = f",补派发 {len(created)} 次尝试(只建不推)" if created else ""
    _emit(payload, as_json, f"已恢复 {requirement.id}{note}")


@task_app.command("events")
def task_events(
    task_id: str = typer.Argument(..., help="任务编号"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """打印一个任务的完整事件流。审计与回放的入口。"""
    root = workspace.expanduser().resolve()
    events = EventLog(root).events(task_id)
    if as_json:
        typer.echo(
            json.dumps(
                {"events": [event.as_dict() for event in events]}, ensure_ascii=False, indent=2
            )
        )
        return
    if not events:
        typer.echo(f"(任务 {task_id} 还没有事件)")
        return
    for event in events:
        typer.echo(f"  {event.ts.isoformat()}  {event.actor:<16} {event.kind.value}")


@task_app.command("run")
def task_run(
    task_id: str = typer.Argument(..., help="任务编号"),
    steps: int = typer.Option(1, "--steps", help="最多推进几步"),
    runtime_name: str = typer.Option("", "--runtime", help="覆盖员工声明的运行时"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """把一个任务往前推。单步驱动,便于演示与排查。"""
    root = workspace.expanduser().resolve()
    config = _collect(lambda: load_config(root), []) or Config()
    orchestrator = Orchestrator(
        root,
        pool=AgentPool(
            _build_runtimes(config, root),
            global_jobs=config.concurrency.global_jobs,
            genome_jobs=config.genome_tasks.concurrent_jobs,
        ),
        runtime_name=runtime_name or None,
        config=config,
    )
    try:
        task = asyncio.run(_advance(orchestrator, task_id, steps))
    except (TaskNotFound, DispatchRefused, RuntimeNotRegistered, RecordingNotFound) as exc:
        _fail(str(exc))
    _emit(
        _task_row(task),
        as_json,
        f"{task.id} 现在是 {task.state.value}(轮次 {task.fix_rounds})",
    )


async def _advance(orchestrator: Orchestrator, task_id: str, steps: int) -> Task:
    task = orchestrator.store.get(task_id)
    before = task.state
    for _ in range(max(steps, 1)):
        moved = await orchestrator.advance(task_id)
        if moved.state is task.state and moved.is_terminal:
            break
        task = moved
    # **蒸馏在这里被排空,而不是在副作用里。** 副作用是同步执行的,而蒸馏要派 Job;
    # 硬塞进去只能在已经跑着的事件循环里再起一个,那是死锁。
    #
    # 没有这一句的话,`drain_evolution` 就是一个永远没人调的方法——整条自进化管道从任务
    # 终结那一刻起再也不会被触发,而且没有任何症状:任务照常完成,只是什么都学不到。
    await orchestrator.drain_evolution()
    # **`ESCALATED`/`COMPLETED` 的 IM 推送必须挂在这里,不是挂在 REST 层。** 这两个终态
    # 通常是单步推进跑出来的,不是某次 REST 请求同步触发的——挂在 REST 的 `_notify_im`
    # 上的话,订阅这两个事件的人永远收不到推送,而界面上的勾选框会让他们以为自己订上了。
    # `server/app.py` 的 `POST /tasks/{id}/run` 是另一个 `advance` 调用方,同一份理由,
    # 共用 `TERMINAL_NOTIFY_EVENT` 这一份映射。
    if before is not task.state and task.state in TERMINAL_NOTIFY_EVENT:
        notify_prefs.push(orchestrator.root, task.id, task.title, TERMINAL_NOTIFY_EVENT[task.state])
    return task


@task_app.command("cancel")
def task_cancel(
    task_id: str = typer.Argument(..., help="任务编号"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """取消一个任务:清理隔离工作区,保留全部审计材料。"""
    root = workspace.expanduser().resolve()
    config = _collect(lambda: load_config(root), []) or Config()
    orchestrator = Orchestrator(root, config=config)
    try:
        task = orchestrator.deliver(task_id, TaskEvent.CANCEL)
    except TaskNotFound as exc:
        _fail(str(exc))
    _emit(_task_row(task), as_json, f"{task.id} 已取消,任务目录保留")


@task_app.command("diagram")
def task_diagram() -> None:
    """打印状态机的 Mermaid 图。它由迁移表生成,永远和表一致。"""
    typer.echo(render_mermaid(), nl=False)


@gate_app.command("show")
def gate_show(
    module_id: str = typer.Argument(..., help="模块 id"),
    candidate_file: Path | None = typer.Option(
        None, "--candidate-file", help="把唯一候选导出为可供 gate confirm 使用的 v2 YAML"
    ),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """打印已确认规格；旧配置只作为迁移线索展示，不会被执行。"""
    root = workspace.expanduser().resolve()
    try:
        confirmed = load_verification_spec(root, module_id)
        pending = load_pending_verification(root, module_id)
    except ValueError as error:
        _fail(f"验证规格不可读: {error}")

    if confirmed is not None:
        module = load_project_map(root).module(module_id)
        payload = confirmed.model_dump(mode="json") | {
            "source": "confirmed",
            "path": module.path,
        }
        if as_json:
            typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        typer.echo(f"{confirmed.module}  来源: confirmed(v2)  路径: {module.path}")
        for confirmed_gate in confirmed.gates:
            mark = "必需" if confirmed_gate.required else "可选"
            typer.echo(
                f"  {confirmed_gate.id:<12} [{mark}] ({confirmed_gate.environment}) "
                f"{shlex.join(confirmed_gate.command.argv)}"
            )
            for evidence in confirmed_gate.provenance.evidence:
                typer.echo(f"    证据: {evidence.path}#{evidence.locator}")
        return

    if pending is not None:
        payload = pending.model_dump(mode="json") | {
            "status": "needs_confirmation",
            "source": "pending",
        }
        if as_json:
            typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        typer.secho(f"{module_id}  来源: pending  状态: 待确认", fg=typer.colors.YELLOW)
        for issue in pending.issues:
            typer.echo(f"  {issue.code}: {issue.detail}")
        for index, candidate in enumerate(pending.candidates, start=1):
            typer.echo(f"  候选 {index}:")
            for candidate_gate in candidate.gates:
                typer.echo(
                    f"    {candidate_gate.id:<12} ({candidate_gate.environment}:"
                    f"{candidate.environments[candidate_gate.environment].adapter}) "
                    f"{shlex.join(candidate_gate.command.argv)}"
                )
                for evidence in candidate_gate.provenance.evidence:
                    typer.echo(f"      证据: {evidence.path}#{evidence.locator}")
        if candidate_file is not None:
            if len(pending.candidates) != 1:
                _fail(
                    f"当前有 {len(pending.candidates)} 个候选，只有唯一候选才能直接导出"
                )
            destination = candidate_file.expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                yaml.safe_dump(
                    pending.candidates[0].model_dump(mode="json"),
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            typer.echo(f"  已导出: {destination}")
        typer.echo("  下一步: agctl gate confirm <模块> --file <已审阅的规格.yaml>")
        return

    try:
        effective = effective_gates(root, module_id)
    except LookupError as exc:
        _fail(str(exc))
    except GenomeValidationError as error:
        _report_validation_error(error)

    if as_json:
        typer.echo(
            json.dumps(
                effective.as_dict()
                | {"executable": False, "migration_required": True},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    typer.secho(
        f"{effective.module_id}  兼容配置（不可执行）  来源: {effective.source.value}  "
        f"路径: {effective.module_path}",
        fg=typer.colors.YELLOW,
    )
    for legacy_gate in effective.gates:
        mark = "必需" if legacy_gate.required else "可选"
        typer.echo(f"  {legacy_gate.id:<12} [{mark}] {legacy_gate.cmd}")
    if not effective.gates:
        typer.echo("  (推导不出任何门禁)")
    for note in effective.notes:
        typer.secho(f"  提示: {note}", fg=typer.colors.YELLOW)
    typer.echo(f"  下一步: agctl gate discover {module_id} --write")


@gate_app.command("discover")
def gate_discover(
    module_id: str = typer.Argument(..., help="模块 id"),
    write: bool = typer.Option(False, "--write", help="确定性发现成功时写成已确认规格"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """从仓库标准入口发现验证命令；有歧义时停下，不让模型或执行器猜。"""
    root = workspace.expanduser().resolve()
    try:
        module = load_project_map(root).module(module_id)
    except LookupError as exc:
        _fail(str(exc))
    resolution = resolve_verification(module_id, root / module.path)
    if isinstance(resolution, Ready):
        change = (
            record_confirmed_spec(
                root, resolution.spec, actor="human", entrance="cli"
            )
            if write
            else None
        )
        target = change.path if change is not None else None
        ready_payload = {
            "status": "ready",
            "written": str(target.relative_to(root)) if target is not None else None,
            "spec": resolution.spec.model_dump(mode="json"),
        }
        if as_json:
            typer.echo(json.dumps(ready_payload, ensure_ascii=False, indent=2))
        else:
            action = f"，已写入 {ready_payload['written']}" if target is not None else ""
            typer.echo(f"{module_id}: 已确定{action}")
            for gate in resolution.spec.gates:
                typer.echo(f"  {gate.id:<12} {shlex.join(gate.command.argv)}")
        return

    assert isinstance(resolution, NeedsConfirmation)
    target = (
        record_pending_spec(
            root, module_id, resolution, actor="human", entrance="cli"
        ).path
        if write
        else None
    )
    pending_payload = {
        "status": "needs_confirmation",
        "written": str(target.relative_to(root)) if target is not None else None,
        "issues": [issue.model_dump(mode="json") for issue in resolution.issues],
        "candidates": [
            candidate.model_dump(mode="json") for candidate in resolution.candidates
        ],
    }
    if as_json:
        typer.echo(json.dumps(pending_payload, ensure_ascii=False, indent=2))
    else:
        typer.secho(f"{module_id}: 需要确认，未写入验证规格", fg=typer.colors.YELLOW)
        for issue in resolution.issues:
            typer.echo(f"  {issue.code}: {issue.detail}")
    raise typer.Exit(code=2)


@gate_app.command("confirm")
def gate_confirm(
    module_id: str = typer.Argument(..., help="模块 id"),
    spec_file: Path = typer.Option(..., "--file", help="人工审阅后的 v2 规格 YAML"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """由当前人确认一份候选规格；确认前它始终不可执行。"""
    root = workspace.expanduser().resolve()
    try:
        module = load_project_map(root).module(module_id)
        spec = load_verification_spec_file(spec_file.expanduser().resolve())
    except (LookupError, ValueError) as error:
        _fail(str(error))
    if spec.module != module.id:
        _fail(f"规格 module={spec.module}，与待确认模块 {module.id} 不一致")
    evidence_issue = validate_spec_evidence(spec, root / module.path)
    if evidence_issue is not None:
        _fail(f"规格证据不可确认: {evidence_issue}")
    pending = load_pending_verification(root, module_id)
    applied = None
    if pending is not None and pending.proposal_task_id is not None:
        store = GenomeTaskStore(root)
        log = EventLog(root)
        try:
            proposal_task = store.get(pending.proposal_task_id)
        except TaskNotFound as error:
            _fail(str(error))
        if proposal_task.state is GenomeTaskState.AWAITING_CONFIRMATION:
            applied = GenomeDriver(store, log, enforce_budget=False).deliver(
                pending.proposal_task_id,
                GenomeEvent.VERIFICATION_CONFIRMED,
                actor="human",
            )
            if not applied.moved:
                _fail(f"候选任务未能完成确认: {applied.decision.reason}")
            _seal_verification_task(root, applied.task, log)
        elif proposal_task.state is not GenomeTaskState.SUBMITTED:
            _fail(
                f"候选任务 {proposal_task.id} 不在待确认"
                f"({proposal_task.state.value})"
            )
    target = record_confirmed_spec(
        root, spec, actor="human", entrance="cli"
    ).path
    payload: dict[str, object] = {
        "status": "confirmed",
        "module": module.id,
        "written": str(target.relative_to(root)),
    }
    _emit(payload, as_json, f"{module.id}: 已确认并写入 {payload['written']}")


def _seal_verification_task(root: Path, task: GenomeTask, log: EventLog) -> None:
    """验证候选一进终态就固化上下文、Job 日志与事件，失败也必须留痕。"""
    config = _collect(lambda: load_config(root), []) or Config()
    sealed = seal_terminal_evidence(root, task.id, task.state.value, config)
    if sealed.package is None:
        log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.NOTE,
            payload={"note": "audit_seal_failed", "error": sealed.error},
        )
        return
    log.append(
        task.id,
        actor=ORCHESTRATOR,
        kind=LogKind.NOTE,
        payload={"note": "audit_sealed", "path": str(sealed.package.path)},
    )


def _finish_verification_task(
    root: Path,
    store: GenomeTaskStore,
    task_id: str,
    event: GenomeEvent,
    stop_reason: str,
) -> GenomeTask:
    """把验证任务推进终态并立即封存；所有退出路径共享这一条收尾。"""
    log = EventLog(root)
    current = store.get(task_id)
    if not current.is_terminal:
        GenomeDriver(store, log, enforce_budget=False).deliver(
            task_id, event, GenomeFacts(stop_reason=stop_reason)
        )
        current = store.get(task_id)
    if current.is_terminal:
        _seal_verification_task(root, current, log)
    return current


@gate_app.command("propose")
def gate_propose(
    module_id: str = typer.Argument(..., help="模块 id"),
    workspace: Path = WorkspaceOption,
    runtime_name: str = typer.Option("claude-code", "--runtime", help="使用哪个运行时"),
    round_: int = typer.Option(1, "--round", help="第几轮(回放按它选录制)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只生成上下文包，不拉起员工"),
    as_json: bool = JsonOption,
) -> None:
    """让架构员工调查歧义并提出候选；候选仍需 gate confirm。"""
    root = workspace.expanduser().resolve()
    try:
        module = load_project_map(root).module(module_id)
        pending = load_pending_verification(root, module_id)
        rules = load_rules(root)
    except (LookupError, ValueError, GenomeValidationError) as error:
        _fail(str(error))
    if pending is None:
        _fail(f"{module_id} 没有待补全的验证发现；先运行 gate discover --write")

    config = _collect(lambda: load_config(root), []) or Config()
    store = GenomeTaskStore(root)
    try:
        record = store.create(
            title=f"补全 {module_id} 验证规格",
            kind=GenomeTaskKind.VERIFICATION,
            origin=Origin.HUMAN,
            subject=module_id,
            budget_tokens=config.genome_tasks.per_task_tokens,
        )
    except ModuleBusy as error:
        _fail(str(error))
    proposal_space = GitWorkspace(
        root,
        worktrees_home=scoped_worktrees_home(root, "verification-proposals"),
    )
    try:
        proposal_workdir = proposal_space.checkout_isolated(record.id)
    except Exception as error:
        _finish_verification_task(
            root,
            store,
            record.id,
            GenomeEvent.FAILED,
            f"验证提案隔离工作区创建失败；检查 Git worktree: {error}",
        )
        proposal_space.cleanup(record.id)
        raise
    try:
        _run_verification_proposal(
            root=root,
            module=module,
            pending=pending,
            config=config,
            store=store,
            record=record,
            proposal_workdir=proposal_workdir,
            runtime_name=runtime_name,
            round_=round_,
            dry_run=dry_run,
            as_json=as_json,
            protected=rules.protected.paths_for(verification_proposal.EMPLOYEE_ID),
        )
    except BaseException as error:
        detail = str(error).strip()
        _finish_verification_task(
            root,
            store,
            record.id,
            GenomeEvent.FAILED,
            (
                f"验证提案执行失败；检查任务事件与 job_finished 详情: {detail}"
                if detail
                else "验证提案执行失败；检查任务事件与 job_finished 详情"
            ),
        )
        raise
    finally:
        proposal_space.cleanup(record.id)


def _run_verification_proposal(
    *,
    root: Path,
    module: Module,
    pending: PendingVerification,
    config: Config,
    store: GenomeTaskStore,
    record: GenomeTask,
    proposal_workdir: Path,
    runtime_name: str,
    round_: int,
    dry_run: bool,
    as_json: bool,
    protected: list[str],
) -> None:
    """在隔离 worktree 中完成提案；资源与失败收尾由外层唯一负责。"""
    task_dir = root / paths.TASKS / record.id
    context_file = task_dir / "context" / "prompt.md"
    output_dir = task_dir / "artifacts" / verification_proposal.PROCEDURE_ID
    context_file.parent.mkdir(parents=True, exist_ok=True)
    context_file.write_text(
        verification_proposal.build_prompt(
            record.id, module.id, module.path, pending
        ),
        encoding="utf-8",
    )
    spec = JobSpec(
        task_id=record.id,
        employee_id=verification_proposal.EMPLOYEE_ID,
        procedure_id=verification_proposal.PROCEDURE_ID,
        procedure_version=verification_proposal.PROCEDURE_VERSION,
        round=round_,
        workdir=proposal_workdir,
        context_file=context_file,
        output_dir=output_dir,
        subject=module.id,
        output_schema=verification_proposal.RECEIPT_SCHEMA,
        output_check=verification_proposal.proposal_output_check(
            record.id, module.id, proposal_workdir / module.path
        ),
        timeout_s=config.limits.job_timeout_s,
        max_tokens=config.budgets.per_job_tokens,
        enforce_token_limit=config.budgets.enforce,
        tools_allow=["Read", "Grep", "Glob", "Bash"],
        tools_deny=["Write", "Edit", "WebFetch", "WebSearch"],
        scope=ScopePolicy(write_paths=()).with_protected(
            protected
        ),
    )
    pool = AgentPool(
        _build_runtimes(config, root),
        global_jobs=config.concurrency.global_jobs,
        genome_jobs=config.genome_tasks.concurrent_jobs,
        task_budgets={
            record.id: _lane_budget(config, record.id) if config.budgets.enforce else None
        },
    )
    log = EventLog(root)
    log.append(
        record.id,
        actor=verification_proposal.EMPLOYEE_ID,
        actor_kind=ActorKind.EMPLOYEE,
        kind=LogKind.JOB_STARTED,
        payload={"procedure_ref": spec.procedure_ref, "stage": "proposal", "round": round_},
    )
    try:
        result = asyncio.run(pool.submit(spec, runtime_name=runtime_name, dry_run=dry_run))
    except (RecordingNotFound, RuntimeNotRegistered) as error:
        _fail(str(error))
    log.job_finished(
        record.id,
        verification_proposal.EMPLOYEE_ID,
        spec.procedure_ref,
        result.ok,
        result.tokens_used,
        result.tokens_available,
        result.duration_s,
        result.failure_kind.value,
        result.failure_detail,
    )
    store.save(record.evolve(tokens_used=result.tokens_used if result.tokens_available else 0))
    if dry_run:
        _finish_verification_task(
            root,
            store,
            record.id,
            GenomeEvent.CANCEL,
            "dry-run 仅生成验证提案上下文，未调用架构员工",
        )
        _emit(
            {"status": "dry_run", "task_id": record.id, "context": str(context_file)},
            as_json,
            f"已写出上下文包: {context_file}",
        )
        return
    if not result.ok or result.result_path is None:
        _fail(
            f"verification-propose 失败({result.failure_kind.value}): "
            f"{result.failure_detail or '没有详情'}"
        )
    proposal = verification_proposal.load_proposal(result.result_path)
    proposal_issue = verification_proposal.proposal_output_check(
        record.id,
        module.id,
        proposal_workdir / module.path,
        pending=pending,
    )(result.result_path.parent)
    if proposal_issue is not None:
        _fail(f"架构员工候选不可用: {proposal_issue}")
    try:
        candidate = seal_agent_proposal(proposal.spec, proposal_workdir / module.path)
    except (OSError, UnicodeError, ValueError) as error:
        _fail(f"架构员工候选证据不可定位: {error}")
    proposed = NeedsConfirmation(
        candidates=(candidate,),
        issues=pending.issues,
    )
    record_pending_spec(
        root,
        module.id,
        proposed,
        actor=verification_proposal.EMPLOYEE_ID,
        actor_kind=ActorKind.EMPLOYEE,
        entrance="gate-propose",
        proposal_task_id=record.id,
    )
    applied = GenomeDriver(store, log, enforce_budget=False).deliver(
        record.id, GenomeEvent.DRAFT_READY
    )
    if not applied.moved:
        _fail(f"验证候选未能进入待确认: {applied.decision.reason}")
    _emit(
        {
            "status": "needs_confirmation",
            "task_id": record.id,
            "module": module.id,
            "candidate": candidate.model_dump(mode="json"),
            "rationale": proposal.rationale,
        },
        as_json,
        f"{module.id}: 架构员工已提出候选，任务 {record.id} 等待人工确认",
    )


@gate_app.command("run")
def gate_run(
    task_id: str = typer.Argument(..., help="任务编号"),
    module_id: str = typer.Option("", "--module", help="只跑这一个模块"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """在任务现有的工作区上重跑门禁。

    **产出报告但不驱动状态机。** 排查环境问题时我要的是"现在还挂不挂",不是把任务往前推。
    """
    root = workspace.expanduser().resolve()
    try:
        slot = gate_slot(root, task_id, STAGE_UNIT_GATE)
        report = run_task_gates(root, task_id, slot.path, module_id or None)
    except LookupError as exc:
        _fail(str(exc))
    except GenomeValidationError as error:
        _report_validation_error(error)

    if as_json:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return
    mark = "通过" if report.passed else f"未通过({report.kind.value})"
    typer.echo(f"{report.module}: {mark}")
    for gate in report.gates:
        typer.echo(f"  {gate.id:<12} {gate.outcome.value:<12} {gate.duration_s:.1f}s")


@itest_app.command("run")
def itest_run(
    task_id: str = typer.Argument(..., help="任务编号"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """在任务当前的子模块指针组合上重跑一次集成测试。

    **产出报告但不驱动状态机。** 手工重跑是一次观察,不是一次判定。让它能改任务状态的话,
    人就有了一个绕过流程把任务推进下一态的后门,而"这个任务为什么变成 READY_TO_COMMIT"
    会变得不可追溯。

    产物落在与自动跑**不同的 stage** 里(`itest-manual`),所以它既不参与轮次定位,也不会
    被崩溃恢复当成"这一轮已经跑过了"。
    """
    root = workspace.expanduser().resolve()
    try:
        task = _open_store(workspace).get(task_id)
    except TaskNotFound as exc:
        _fail(str(exc))
    if task.is_terminal:
        _fail(f"{task_id} 已经是终态({task.state.value}),重跑集成测试没有意义。")

    # 隔离工作区还没开时退回主 checkout:排查的人在任何时候都该能跑这条命令。
    workdir = GitWorkspace(root).worktree_path(task_id)
    workdir = workdir if workdir.is_dir() else root
    slot = gate_slot(root, task_id, STAGE_ITEST_MANUAL)
    try:
        report = run_task_itest(
            workdir=workdir,
            task_id=task_id,
            output_dir=slot.path,
            modules=involved_modules(workdir, load_plan_modules(read_plan(root, task_id))),
            config=itest_config(workdir),
        )
    except GenomeValidationError as error:
        _report_validation_error(error)
    write_outputs(report, slot.path)
    slot.write_manifest(producer="human", outputs=[REPORT_FILE], summary="人工重跑集成测试")
    EventLog(root).append(
        task_id,
        actor="human",
        kind=LogKind.NOTE,
        payload={
            "note": "人工重跑集成测试(不驱动状态机)",
            "stage": slot.stage,
            "passed": report.passed,
            "kind": report.kind.value,
        },
    )

    if as_json:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return
    typer.echo(f"{task_id}: {'通过' if report.passed else f'未通过({report.kind.value})'}")
    typer.echo(f"  产物: {slot.path}")
    for failure in report.failures:
        typer.echo(f"  {failure.case}: {failure.message}")
        if failure.repro_cmd:
            typer.echo(f"    复现: {failure.repro_cmd}")


@task_app.command("approve")
def task_approve(
    task_id: str = typer.Argument(..., help="任务编号"),
    actor: str = typer.Option(..., "--as", help="你的身份，必须在审批人名单里"),
    comment: str = typer.Option("", "--comment", help="批准意见，可选"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """批准一个等审批的任务，让它进入合并。"""
    _approval(workspace, task_id, actor, approved=True, comment=comment, as_json=as_json)


@task_app.command("reject")
def task_reject(
    task_id: str = typer.Argument(..., help="任务编号"),
    actor: str = typer.Option(..., "--as", help="你的身份，必须在审批人名单里"),
    comment: str = typer.Option(..., "--comment", help="驳回意见。**必填**"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """驳回一个等审批的任务，意见会进下一轮开发的上下文。"""
    _approval(workspace, task_id, actor, approved=False, comment=comment, as_json=as_json)


def _approval(
    workspace: Path, task_id: str, actor: str, approved: bool, comment: str, as_json: bool
) -> None:
    root = workspace.expanduser().resolve()
    try:
        task = (
            approve(root, task_id, actor, comment)
            if approved
            else reject(root, task_id, actor, comment)
        )
    except TaskNotFound as exc:
        _fail(str(exc))
    except NotAnApprover as exc:
        _fail(str(exc))
    except ApprovalRefused as exc:
        _fail(str(exc))
    verb = "已批准" if approved else "已驳回"
    _emit(_task_row(task), as_json, f"{verb} {task_id}，现在在 {task.state.value}")


@settings_app.command("show")
def settings_show(
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """现在生效的配置里,能改的那几段。

    **与界面读的是同一份**(`server.models.SettingsView`)——各读各的话,命令行改完之后
    界面上看到的会是另一个答案,而两边都会声称自己是对的。
    """
    root = workspace.expanduser().resolve()
    config = _collect(lambda: load_config(root), []) or Config()
    # 命令行上没有认证,`can_edit` 在这里没有意义:能敲这条命令的人本来就能直接编辑那个
    # 文件。权限的那道闸在服务端,见 `server.rbac`。
    view = SettingsView.of(config, can_edit=True).model_dump(mode="json", exclude={"can_edit"})
    if as_json:
        typer.echo(json.dumps(view, ensure_ascii=False, indent=2))
        return
    for section, value in view.items():
        typer.echo(f"{section}:")
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@settings_app.command("set")
def settings_set(
    section: str = typer.Argument(..., help="要改的配置段，如 concurrency"),
    value: str = typer.Argument(..., help="这一段的新内容，JSON 对象"),
    actor: str = typer.Option(..., "--as", help="你的身份。事件面记的就是这个名字"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """从命令行改一段配置：内容提交进 git，这次动作与你的身份进事件面。

    **不是为了省得打开编辑器。** 直接编辑 `agentgenome.yaml` 当然也能改，但那样改完
    什么记录都不会留下；这条命令存在的理由是让命令行这个入口和界面走同一条写入路径。
    """
    root = workspace.expanduser().resolve()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        _fail(f"value 不是合法 JSON: {exc}")
    if not isinstance(payload, dict):
        _fail("value 要是一个 JSON 对象，配置段的内容是键值对")
    # 命令行上的 `--as` 是**自报的**，没有任何认证在后面——能敲这条命令的人本来就能直接
    # 编辑那个文件。所以 ADMIN 不是一道安全边界，它只是让写入路径唯一；**权限的那道闸在
    # 服务端**，见 `server.rbac`。事件里 CLI 来的 actor 该按"自称"读，Web 来的才是认证身份。
    principal = Principal(actor, frozenset({Role.ADMIN}))
    try:
        change = update_settings(root, principal, section, payload, entrance=Entrance.CLI)
    except NotEditable as exc:
        _fail(str(exc))
    except GenomeValidationError as exc:
        _report_validation_error(exc)
    except GitError as exc:
        _fail(f"提交失败，这次修改已回滚: {exc}")
    _emit(change.as_dict(), as_json, f"{change.section} 已更新，{_rev_note(change.rev)}")


def _rev_note(rev: str) -> str:
    """空 sha 有两个原因，**不能混着说**：一个是没有版本面，一个是这次什么都没改。

    统一写成「不是 git 仓库」的那版会把一次无变化的保存送去查 git 配置。
    """
    return f"提交 {rev[:8]}" if rev else "没有新提交（内容未变，或这里不是 git 仓库）"


@settings_app.command("history")
def settings_history_cmd(
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """按时间列出配置变更：谁、什么时候、从哪个入口、改的哪一段、落在哪个提交上。"""
    root = workspace.expanduser().resolve()
    found = settings_history(root)
    if as_json:
        typer.echo(json.dumps([item.as_dict() for item in found], ensure_ascii=False, indent=2))
        return
    if not found:
        typer.echo("还没有配置变更")
        return
    if found[0].truncated:
        typer.echo(f"（更早的记录没有读回来，一次最多 {HISTORY_LIMIT} 条）")
    for item in found:
        line = (
            f"{item.at}  {item.actor:<16} {item.entrance.value:<4} {item.section:<12} "
            f"{item.rev[:8]}"
        )
        if item.is_legacy:
            # 旧记录带前值后值。**标出来**：它们记的是当年前端读到的值，跟 git 的 diff
            # 不是一回事，混在一起看会把一个已知不可靠的数当成事实。
            line += f"  旧记录 {item.before} → {item.after}"
        typer.echo(line)


@app.command("audit")
def audit(
    task_id: str = typer.Argument(..., help="任务编号"),
    out: Path = typer.Option(
        None, "--out", help="归档落在哪，缺省是归档目录下", show_default=False
    ),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """导出一个任务的完整审计包：快照、事件流、全部产物、日志、上下文包。

    **未终结的任务也能导出。** 出事的时候人不会等任务走完——恰恰相反，最需要审计包的时刻
    通常是任务卡住或者刚出问题的时候。
    """
    root = workspace.expanduser().resolve()
    # 配置读不出来不该挡住导出:最需要审计包的时刻,配置本身也可能正是坏掉的那一样。
    config = _collect(lambda: load_config(root), []) or Config()
    try:
        package = export_task_bundle(root, task_id, config, target=out)
    except TaskNotArchivable as exc:
        _fail(str(exc))
    verb = "本来就在" if package.already_sealed else "已导出"
    _emit(
        {
            "path": str(package.path),
            "entries": package.count,
            "already_sealed": package.already_sealed,
        },
        as_json,
        f"审计包{verb}：{package.path}（{package.count} 个文件）",
    )


@app.command("gaps")
def gaps_scan(
    actor: str = typer.Option(..., "--as", help="你的身份。记进那条「查过了」的事件"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """列出直接改了配置、却没有对应事件的提交。

    **只报告。** 直接改仓库是合法的运维手段，这条命令不拦截、不告警、不改任何状态——
    它只是让「谁绕过界面改了配置」这个问题有地方问。

    **没有配套的定时任务。** 定时跑出来的结果没人看，却会制造「已经在监控了」的错觉。
    需要定时的话，由外部调度调这条命令。
    """
    root = workspace.expanduser().resolve()
    report = gaps.detect(root)
    gaps.record_scan(root, report, actor)
    _emit(report.as_dict(), as_json, gaps.render(report))


@app.command("prune")
def prune(
    dry_run: bool = typer.Option(False, "--dry-run", help="只报告不删。报告内容与实际执行一致"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """清理超过保留期的原始日志。审计包不动，事件面的数据库不动。

    删掉的是原始 Agent 日志与事件的 JSONL 副本——**事件面的真相来源是数据库**，副本只是
    给实时推送流式消费用的，清掉它不丢历史。

    **没有审计包的任务不清理**，哪怕远超保留期——宁可占磁盘，不可丢证据。跳过原因是
    归档故障唯一的暴露渠道，所以它在输出里排在最前面。

    **不做定时任务。** 定时清理是危险操作，由运维显式调度或手动执行。
    """
    root = workspace.expanduser().resolve()
    config = _collect(lambda: load_config(root), []) or Config()
    plan = plan_workspace_prune(
        root,
        TaskStore(root).all_tasks(),
        archive=archive_root(root, config.evidence.archive_dir),
        retention_days=config.evidence.log_retention_days,
        now=datetime.now(UTC),
    )
    report = apply_prune(plan, dry_run=dry_run)

    payload = {
        "dry_run": report.dry_run,
        "pruned": report.pruned,
        "reclaimed_bytes": report.reclaimed_bytes,
        "skipped": [
            {"task_id": item.task_id, "reason": item.reason.value, "message": item.reason.message}
            for item in report.skipped
        ],
        "failed": [{"task_id": task_id, "error": error} for task_id, error in report.failed],
    }
    verb = "将清理" if dry_run else "已清理"
    lines = [f"{verb} {len(report.pruned)} 个任务的日志，回收 {report.reclaimed_bytes} 字节"]
    for item in report.skipped:
        lines.append(f"  跳过 {item.task_id}：{item.reason.message}")
    for task_id, error in report.failed:
        lines.append(f"  失败 {task_id}：{error}")
    _emit(payload, as_json, "\n".join(lines))


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8080, "--port", help="监听端口"),
    workspace: Path = typer.Option(
        None, "--workspace", "-w", help="单项目模式:Workspace 根目录", show_default=False
    ),
    registry: Path = typer.Option(
        None,
        "--registry",
        help="注册表模式:注册表文件,缺省 ~/.agentgenome/registry.yaml",
        show_default=False,
    ),
) -> None:
    """启动控制面:REST 接口 + 编排器循环在同一进程。

    三种形态:`-w <path>` 单项目(老样子,逐字节不变);当前目录是 Workspace 时裸跑
    等价于 `-w .`;其余情况走注册表模式——**零个项目也照常起**,空实例是合法状态,
    界面上给引导页而不是报错。

    **优雅关闭**:收到信号后停止接受新 Job,等进行中的 Job 跑完或超时,落盘后退出。强制关闭时
    进行中的 Job 视为失败,由崩溃恢复接手——那条路本来就是设计好的,不必为它再造一套。
    """
    import uvicorn

    if workspace is not None and registry is not None:
        _fail("--workspace 与 --registry 互斥:单项目模式不读注册表。")

    def _serve_single(root: Path) -> None:
        typer.secho(f"控制面已启动:http://{host}:{port}(Workspace: {root})", fg=typer.colors.GREEN)
        uvicorn.run(create_app(root), host=host, port=port, log_level="info")

    if workspace is not None:
        root = workspace.expanduser().resolve()
        if not (root / paths.ROOT_CONFIG).is_file():
            _fail(f"{root} 不是一个 Workspace(没有 {paths.ROOT_CONFIG})。先跑 agctl init。")
        _serve_single(root)
        return

    if registry is None and (Path(".") / paths.ROOT_CONFIG).is_file():
        # 兼容老形态:在 Workspace 目录里裸跑 serve,服务当前目录——行为与从前一致。
        _serve_single(Path(".").resolve())
        return

    registry_file = (registry or DEFAULT_REGISTRY).expanduser().resolve()
    try:
        loaded = load_registry(registry_file)
    except ValueError as error:
        _fail(str(error))
    typer.secho(
        f"控制面已启动:http://{host}:{port}"
        f"(注册表模式,项目 {len(loaded.entries)} 个,注册表 {registry_file})",
        fg=typer.colors.GREEN,
    )
    uvicorn.run(
        create_app(workspaces=loaded, registry_path=registry_file),
        host=host,
        port=port,
        log_level="info",
    )


RegistryFileOption = typer.Option(
    None, "--registry", help="注册表文件,缺省 ~/.agentgenome/registry.yaml", show_default=False
)


@workspace_app.command("register")
def workspace_register(
    name: str = typer.Argument(..., help="项目名。字母数字与 -/_,不接受路径分隔符"),
    path: Path = typer.Argument(..., help="Workspace 根目录"),
    registry: Path = RegistryFileOption,
    as_json: bool = JsonOption,
) -> None:
    """把一个已初始化的 Workspace 登记为项目。

    **当场校验它真的能加载**:把一个坏目录注册进来的报错要发生在注册那一刻,
    不是第一个请求打过来的时候。
    """
    target = path.expanduser().resolve()
    if not (target / paths.ROOT_CONFIG).is_file():
        _fail(f"{target} 不是一个 Workspace(没有 {paths.ROOT_CONFIG})。先跑 agctl init。")
    try:
        load_config(target)
    except GenomeValidationError as error:
        _report_validation_error(error)

    registry_file = (registry or DEFAULT_REGISTRY).expanduser().resolve()
    loaded = load_registry(registry_file)
    taken = loaded.entries.get(name)
    if taken is not None and taken != target:
        _fail(f"名字 {name} 已注册到 {taken}。换个名字,或先 workspace unregister。")
    try:
        loaded.register(name, target)
    except ValueError as error:
        _fail(str(error))
    save_registry(registry_file, loaded)
    EventLog(target).append(
        SYSTEM_SUBJECT,
        actor=ORCHESTRATOR,
        kind=LogKind.WORKSPACE_CHANGED,
        payload={"action": "register", "name": name},
    )
    _emit(
        {"name": name, "root": str(target), "registry": str(registry_file)},
        as_json,
        f"已注册项目 {name} -> {target}",
    )


@workspace_app.command("unregister")
def workspace_unregister(
    name: str = typer.Argument(..., help="项目名"),
    registry: Path = RegistryFileOption,
    as_json: bool = JsonOption,
) -> None:
    """注销一个项目:只摘注册表,不动磁盘——一次误操作不是一次数据丢失。"""
    registry_file = (registry or DEFAULT_REGISTRY).expanduser().resolve()
    loaded = load_registry(registry_file)
    try:
        root = unregister_workspace(loaded, name, registry_path=registry_file, actor=ORCHESTRATOR)
    except UnknownWorkspace as error:
        _fail(str(error))
    _emit(
        {"name": name, "root": str(root)},
        as_json,
        f"已注销项目 {name}(磁盘目录原样留在 {root})",
    )


@workspace_app.command("list")
def workspace_list(
    registry: Path = RegistryFileOption,
    as_json: bool = JsonOption,
) -> None:
    """列出注册表里的项目。"""
    registry_file = (registry or DEFAULT_REGISTRY).expanduser().resolve()
    loaded = load_registry(registry_file)
    rows = [{"name": name, "root": str(root)} for name, root in sorted(loaded.entries.items())]
    lines = [f"  {row['name']}  {row['root']}" for row in rows] or ["(注册表是空的)"]
    _emit({"workspaces": rows, "registry": str(registry_file)}, as_json, "\n".join(lines))


@app.command("openapi")
def openapi(
    out: Path = typer.Option(Path("docs/openapi.json"), "--out", help="规范导出到哪"),
    workspace: Path = WorkspaceOption,
) -> None:
    """导出 OpenAPI 规范。

    规范提交进仓库,契约变更就会出现在评审的 diff 里——而不是等前端某天编译报错才发现。
    """
    root = workspace.expanduser().resolve()
    spec = create_app(root).openapi()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    typer.secho(f"OpenAPI 已导出:{out}", fg=typer.colors.GREEN)


# --- 全局基因库(PRD 17)------------------------------------------------------


def _registry(path: Path | None) -> Registry:
    """基因库的位置。命令行 > 环境变量 > 报错。

    **不给默认路径。** 猜一个 `~/.agentgenome/registry` 出来的话,一次拼错的配置会表现为
    "基因库是空的",而不是"你没配基因库"。
    """
    raw = str(path) if path else os.environ.get("AGENTGENOME_REGISTRY", "")
    if not raw:
        _fail("没有配置全局基因库。用 --registry 指定,或设 AGENTGENOME_REGISTRY。")
    found = Registry(root=Path(raw).expanduser().resolve())
    try:
        found.ensure()
    except RegistryUnavailable as exc:
        _fail(str(exc))
    return found


@registry_app.command("templates")
def registry_templates(
    registry: Path = RegistryOption,
    as_json: bool = JsonOption,
) -> None:
    """列出基因库里的模板。"""
    found = _registry(registry).templates()
    if as_json:
        typer.echo(json.dumps([item.as_dict() for item in found], ensure_ascii=False, indent=2))
        return
    if not found:
        typer.echo("(基因库里没有模板)")
        return
    for item in found:
        typer.echo(f"  {item.name:<24} {item.shape:<20} {item.summary}")


@registry_app.command("apply")
def registry_apply(
    name: str = typer.Argument(..., help="模板名"),
    registry: Path = RegistryOption,
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """把一份模板铺进当前 Workspace。

    **已存在的文件不覆盖。** 模板是起跑线,不是重置按钮——一次误用不该抹掉项目已经积累的认知。
    """
    root = workspace.expanduser().resolve()
    try:
        written = _registry(registry).apply_template(name, root)
    except KeyError as exc:
        _fail(str(exc))
    _emit(
        {"written": written},
        as_json,
        f"铺进 {len(written)} 个文件"
        + ("(已存在的都跳过了)" if not written else "")
        + "。它们是起点不是终点——本项目的进化管道会继续打磨。",
    )


@registry_app.command("pull")
def registry_pull(
    procedure_id: str = typer.Argument(..., help="Procedure id"),
    registry: Path = RegistryOption,
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """从基因库拉一个通用 Procedure。"""
    root = workspace.expanduser().resolve()
    try:
        target = _registry(registry).pull_procedure(procedure_id, root)
    except (KeyError, FileExistsError) as exc:
        _fail(str(exc))
    _emit({"path": str(target)}, as_json, f"已拉取 {procedure_id} → {target}")


@registry_app.command("contribute")
def registry_contribute(
    card_id: str = typer.Argument(..., help="经验卡片编号,如 L-0001"),
    force: bool = typer.Option(False, "--force", help="命中数为零时强行上交,需要理由"),
    reason: str = typer.Option("", "--reason", help="强行上交的理由"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """把一条经验上交回全局基因库。

    **审查标准比项目内更严**——全局基因库的污染半径是所有项目。
    """
    root = workspace.expanduser().resolve()
    found = next((item for item in load_cards(root / paths.LESSONS) if item.id == card_id), None)
    if found is None:
        _fail(f"这个项目里没有 {card_id}。")
    if force and not reason.strip():
        _fail("--force 要说明理由:命中为零的经验凭什么推给所有项目?")

    verdict = check_contribution(found, force=force)
    for warning in verdict.warnings:
        typer.secho(f"  ⚠ {warning}", fg=typer.colors.YELLOW)
    if not verdict.allowed:
        _fail("\n".join(verdict.reasons))
    _emit(
        verdict.as_dict() | {"card": found.id},
        as_json,
        f"{found.id} 可以上交。**走 PR,由基因库维护者审批**——这一步不会自动推。",
    )


# --- 进化(PRD 13)-----------------------------------------------------------


@evolve_app.command("cards")
def evolve_cards(workspace: Path = WorkspaceOption, as_json: bool = JsonOption) -> None:
    """列出经验卡片与它们的命中数。

    命中数是这套自进化机制**唯一对外可见的「这条经验到底有没有用」**——藏起来的话,自然
    选择就成了一个没人能验证的说法。
    """
    cards = load_cards(workspace.expanduser().resolve() / paths.LESSONS)
    if as_json:
        typer.echo(
            json.dumps(
                [card.model_dump(mode="json") for card in cards], ensure_ascii=False, indent=2
            )
        )
        return
    if not cards:
        typer.echo("(还没有经验卡片。任务终结时由蒸馏管道产出。)")
        return
    for card in cards:
        where = ", ".join(card.applies_to.modules) or card.applies_to.scenario or "(通用)"
        typer.echo(
            f"  {card.id}  命中 {card.hits:<4} 置信 {card.confidence:<5} {where:<28} {card.title}"
        )


@evolve_app.command("propose")
def evolve_propose(
    threshold: int = typer.Option(3, "--threshold", help="多少张同类卡片才提炼成规则"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """从重复出现的知识模式里提炼规则提案。

    **提案永远不自动合并。** 规则层是唯一能大范围改变行为的杠杆,它必须握在人手里。
    """
    root = workspace.expanduser().resolve()
    # L2 候选卡片落在 `candidates/`,不在 `lessons/`——`land_cards` 只让 L1 走完整闭环,
    # 其余分级存档等着这里消费(见 pipeline.py 的说明)。只读 `lessons/` 的话 `propose_rules`
    # 永远看不到一张 L2 卡片,规则提案这条路线从第一天起就是空的。
    cards = load_cards(root / paths.LESSONS) + load_cards(root / CANDIDATES_DIR)
    history = _change_history(root)
    proposals = propose_rules(cards, history, threshold=threshold)
    if as_json:
        typer.echo(json.dumps([item.as_dict() for item in proposals], ensure_ascii=False, indent=2))
        return
    if not proposals:
        typer.echo(f"没有够格的规则候选(需要至少 {threshold} 张同类 L2 卡片)。")
        return
    for proposal in proposals:
        typer.echo(proposal.render())


@evolve_app.command("promotions")
def evolve_promotions(
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """列出等着转正的攻击用例:红队打穿过、值得进业务仓回归集的那些。

    **只列提案,不落地。** 落地走正常提交路径——由测试员工或开发员工在一次普通任务里把用例
    加进业务仓,过门禁、过评审。这里给一条直接写进去的通道,回归集就有了绕过门禁的入口。
    """
    root = workspace.expanduser().resolve()
    found = load_promotions(root)
    if as_json:
        typer.echo(
            json.dumps(
                {"promotions": [str(item.relative_to(root)) for item in found]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not found:
        typer.echo("(还没有转正提案。红队抓到真问题时由编排器产出。)")
        return
    for item in found:
        typer.echo(item.read_text(encoding="utf-8"))


@evolve_app.command("procedures")
def evolve_procedures(
    threshold: int = typer.Option(3, "--threshold", help="同一种失败重复多少次才算模式"),
    workspace: Path = WorkspaceOption,
    as_json: bool = JsonOption,
) -> None:
    """找出反复以**同一种方式**失败的工序,产出 L3 提案。

    ## 它做什么,不做什么

    **做**:从事件流里翻出"这个工序已经第 N 次以完全相同的原因失败了",连同证据摆出来,
    并按改动路径分成 L3a(工序契约,人工审批)与 L3b(手艺内容,回归即可合)。

    **不做**:替你写那个改进后的 diff。生成改进内容需要一次 agentic 步骤,而那一步现在
    不存在——**声称它自动进化会是一句假话**。这里交付的是"哪里该改、凭什么这么说",
    改什么由人(或后续的蒸馏工序)决定。

    偶发失败没有进化价值:只有同一个 `failure_detail` 重复到阈值才算模式。
    """
    root = workspace.expanduser().resolve()
    # 跨全部任务查——失败模式恰恰是"在不同任务里以同一种方式挂",单看一个任务看不出来。
    events = EventLog(root).all_events(kind=LogKind.JOB_FINISHED, limit=100_000)

    registry = load_workspace_registry(root)
    found: list[ProcedureProposal] = []
    for spec in registry.all():
        patterns = failure_pattern(events, spec.id, threshold=threshold)
        if not patterns:
            continue
        found.append(
            ProcedureProposal(
                procedure_id=spec.id,
                reason=f"{spec.ref} 反复以同一种方式失败({len(patterns)} 种固定模式)",
                diff="",
                failure_pattern=patterns,
                # 没有改动就没有分级依据。**默认落 L3a**(更严的那档)——见 `classify_l3`。
                changed_paths=(),
            )
        )

    if as_json:
        payload = [
            {
                "procedure_id": item.procedure_id,
                "level": item.level.value,
                "needs_human": item.needs_human,
                "failure_pattern": list(item.failure_pattern),
            }
            for item in found
        ]
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not found:
        typer.echo(f"没有工序反复以同一种方式失败(阈值 {threshold} 次)。")
        return
    for item in found:
        typer.echo(item.render())
    typer.secho(
        "以上是**证据**,不是改动。改什么由人决定 —— 自动生成改进内容的那一步还不存在。",
        fg=typer.colors.YELLOW,
    )


@evolve_app.command("report")
def evolve_report(workspace: Path = WorkspaceOption) -> None:
    """生成进化周报。

    **数据不足时明确说数据不足,不硬画曲线。** 三个任务画出来的「下降趋势」是噪声,而一旦
    它被贴进汇报,后面所有判断都建立在它上面。
    """
    root = workspace.expanduser().resolve()
    snapshot = metrics_module.collect(root)
    samples = len(snapshot.fix_rounds)
    current = [
        snapshot.avg_fix_rounds,
        snapshot.gate_first_pass_ratio,
        snapshot.avg_duration_minutes,
        snapshot.escalate_rate,
        snapshot.knowledge_hit_rate,
    ]
    # 上一期的数值本期还没有落盘的地方——**给同一组值而不是编一组**,这样"没有对比基线"
    # 会表现为"全部持平",而不是一条假的趋势线。REST 的 /insights/trends 按时间窗切了
    # 真正的两期,这条 CLI 命令保留"没有历史时全部持平"这条更保守的默认值。
    typer.echo(weekly("本期", current, current, [samples] * 5).render())


def _change_history(root: Path) -> dict[str, list[str]]:
    """每个历史任务改过哪些路径。回溯分析的输入。

    **从开发产物里取,不从 git 里算。** 任务完成后隔离工作区会被清理,分支也可能已经删掉,
    而产物目录一直在——回溯分析要能在任务结束很久之后仍然跑得出来。
    """
    found: dict[str, list[str]] = {}
    tasks_root = root / paths.TASKS
    if not tasks_root.is_dir():
        return found
    for directory in sorted(tasks_root.iterdir()):
        if not directory.is_dir():
            continue
        slot = ArtifactBus(directory).latest("develop")
        payload = _read_json(slot.path / "result.json") if slot else None
        found[directory.name] = [str(item) for item in (payload or {}).get("changed_files") or []]
    return found


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


if __name__ == "__main__":  # pragma: no cover
    app()
