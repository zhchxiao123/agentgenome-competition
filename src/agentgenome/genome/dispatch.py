"""Procedure 派发。

把注册表里的一个 Procedure 变成一次真正的执行,并在拉起任何东西**之前**把配置错误挡住。

## 三种 kind 的执行语义不同

- `deterministic` 直接跑脚本,**不经过 AgentRuntime**,零 token。产物同样要过 schema
  校验——不拉 Agent 不等于不用守契约。
- `agentic` 走 AgentRuntime。把 `ProcedureSpec` 塌缩成 `JobSpec` 是**这一层**的活:
  PRD 02 刻意把 `JobSpec` 做成扁平的,就是为了不让 Procedure 注册表的概念漏进运行时层。
- `hybrid` 先跑脚本产出中间产物,中间产物注入上下文后再拉 Agent。**脚本失败则整个
  Procedure 失败,不拉 Agent**——省下那次调用。

## 派发前四道校验

全部发生在拉起任何进程之前:Procedure 是否在该员工的白名单内、状态是否在 `trigger.states`
内、入参是否符合 `inputs.schema`、目标运行时是否在 `compat.runtimes` 内。

**头一道与后三道不是一个层次。** 白名单是**员工能力边界**——越界调用;后三道防的是
配置错误(比如把门禁 Procedure 派给开发态)。所以白名单排在最前,且失败抛的是
`ProcedureNotAllowed` 而不是 `DispatchRefused`:调用方要能把"派错了"与"越权了"分开记录。

## 员工配置在这里塌缩

工具白/黑名单、凭证、超时、token 上限全部由 `EmployeeConfig` 决定,调用方不再逐项传参。
这几样在 PRD 03 里是 `dispatch_procedure` 上悬空的参数,没有调用方传值——真实的消费者就是
员工定义。让它们保持独立可传的话,权限来源会有两份,而分叉的那一份往往是权限。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from agentgenome.agents.artifacts import context_filename
from agentgenome.agents.capabilities import (
    Capability,
    ResultDelivery,
    RuntimeCapabilities,
    UnsupportedCapability,
    capabilities_of,
    capability_of_tool,
)
from agentgenome.agents.contract import check_result_contract
from agentgenome.agents.human import HUMAN
from agentgenome.agents.pool import AgentPool
from agentgenome.agents.runtime import FailureKind, JobResult, JobSpec
from agentgenome.config import Config
from agentgenome.context import (
    ContextInputs,
    FailureReport,
    Fragment,
    assemble,
    context_budget,
    load_genome_slice,
)
from agentgenome.core.scope import ScopePolicy
from agentgenome.core.states import TaskState
from agentgenome.employees import EmployeeConfig, ensure_procedure_allowed
from agentgenome.genome import craft
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.loader import load_project_map
from agentgenome.genome.procedures import (
    RUNTIME_NONE,
    RUNTIME_REPLAY,
    ProcedureKind,
    ProcedureOwnership,
    ProcedureSpec,
)
from agentgenome.genome.routing import RouteInputs
from agentgenome.genome.rules import load_rules
from agentgenome.genome.staging import output_check_for

#: 脚本通过它知道该往哪写产物。
OUTPUT_DIR_ENV = "AGENTGENOME_OUTPUT_DIR"
WORKDIR_ENV = "AGENTGENOME_WORKDIR"

#: 中间产物的文件名。hybrid 的脚本写它,Agent 读它。
INTERMEDIATE = "intermediate.json"

#: 中间产物里的这个键为真时,这一轮不拉 Agent——见 `_skips_agent`。
SKIP_AGENT = "skip_agent"

#: 脚本从它读本次入参。
INPUTS_ENV = "AGENTGENOME_INPUTS"


class DispatchRefused(RuntimeError):
    """派发前校验没过。

    拒绝发生在拉起任何进程之前,所以被拒的派发不会留下任何痕迹——不然一次配置错误
    会在任务目录里留下半截产物,下次崩溃恢复看到它还以为跑过了。
    """


@dataclass
class DispatchResult:
    """一次 Procedure 执行的结果。

    **组合 `JobResult` 而不是镜像它的字段。** 镜像的两份结构会各自演进,然后在某个
    字段上悄悄分叉——第一版就已经漏掉了 `tokens_available`,于是"拿不到用量"被序列化
    成了"用量为 0"。
    """

    ok: bool
    #: 事件流里记录的工序标识。报告能精确复现"当时是哪一版能力干的活"。
    procedure_ref: str
    #: Agent 那一段的结果。确定性 Procedure 没有这一段。
    job: JobResult | None = None
    failure_kind: FailureKind = FailureKind.NONE
    failure_detail: str | None = None
    result_path: Path | None = None
    exit_code: int | None = None
    duration_s: float = 0.0
    #: 本次执行经过了哪些阶段。hybrid 会有两段。
    stages: list[str] = field(default_factory=list)
    #: 本次物化到工作区的手艺名。**进事件流**——审计要能复现"当时带着哪版手艺干的活",
    #: 而 `procedure@version` 只说清了契约那一半。
    crafts: list[str] = field(default_factory=list)

    @property
    def log_path(self) -> Path | None:
        return self.job.log_path if self.job else None

    @property
    def suspended(self) -> bool:
        """这次派发挂起了:活派给了人,结果要等他回来。

        **透传而不是镜像**(与 `tokens_available` 同一条理由):镜像的两份结构会各自演进,
        然后在某个字段上悄悄分叉。
        """
        return self.job.suspended if self.job else False

    @property
    def suspended_on(self) -> str:
        return self.job.suspended_on if self.job else ""

    @property
    def tokens_used(self) -> int:
        return self.job.tokens_used if self.job else 0

    @property
    def tokens_available(self) -> bool:
        """确定性 Procedure 零 token,这是确定的事实而不是"拿不到"。"""
        return self.job.tokens_available if self.job else True

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "procedure_ref": self.procedure_ref,
            "failure_kind": self.failure_kind.value,
            "failure_detail": self.failure_detail,
            "result_path": str(self.result_path) if self.result_path else None,
            "log_path": str(self.log_path) if self.log_path else None,
            "exit_code": self.exit_code,
            "tokens_used": self.tokens_used if self.tokens_available else None,
            "tokens_available": self.tokens_available,
            "duration_s": round(self.duration_s, 3),
            "stages": list(self.stages),
            # 审计要能复现"当时带着哪版手艺干的活"——`procedure_ref` 只说清了契约那一半。
            "crafts": list(self.crafts),
        }


def ensure_dispatchable(
    spec: ProcedureSpec,
    employee: EmployeeConfig,
    state: TaskState,
    inputs: dict[str, Any],
    runtime_name: str,
) -> None:
    """派发前的四道校验。任一不过即拒绝,不拉起任何东西。"""
    # 能力边界排在最前:越权的调用连"这个 Procedure 可不可用"都不该被回答。
    ensure_procedure_allowed(employee, spec.id)

    if not spec.available:
        raise DispatchRefused(f"{spec.ref} 当前不可用: {spec.unavailable_reason}")

    if spec.trigger.states and state not in spec.trigger.states:
        allowed = ", ".join(s.value for s in spec.trigger.states)
        raise DispatchRefused(f"{spec.ref} 不能在 {state.value} 态派发(允许: {allowed})")

    if spec.inputs.schema_:
        errors = sorted(
            Draft202012Validator(spec.inputs.schema_).iter_errors(inputs),
            key=lambda e: list(e.absolute_path),
        )
        if errors:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e.absolute_path) or '(顶层)'}: {e.message}"
                for e in errors
            )
            raise DispatchRefused(f"{spec.ref} 的入参不合法: {detail}")

    _check_runtime_compat(spec, runtime_name)
    if spec.kind is not ProcedureKind.DETERMINISTIC:
        check_runtime_guarantees(employee, runtime_name)


_WRITE_CAPABILITIES = frozenset(
    {Capability.WRITE_FILE, Capability.EDIT_FILE, Capability.RUN_COMMAND}
)


def _normalised_tools(tool_names: Sequence[str]) -> list[Capability]:
    """兼容旧员工资产里的 Claude 工具名，并收敛成运行时无关能力。"""
    normalised: list[Capability] = []
    for tool_name in tool_names:
        capability = capability_of_tool(tool_name)
        if capability is None:
            raise DispatchRefused(f"员工声明了能力矩阵不认识的工具 {tool_name!r}")
        normalised.append(capability)
    return normalised


def check_runtime_guarantees(employee: EmployeeConfig, runtime_name: str) -> None:
    """能力协商：只读要求不满足时在进程启动前拒绝。

    **公开的,因为界面也要问它。** 换运行时那一页要在点击那一刻就说清"这个运行时接不了
    这个员工",而不是等到派发——见 `server.employees_edit.runtime_blocks`。
    """
    profile = capabilities_of(runtime_name)
    if profile is None:
        raise DispatchRefused(f"运行时 {runtime_name!r} 没有能力声明，无法确认产物交付和权限边界")
    allowed = _normalised_tools(employee.tools.allow)
    if not _WRITE_CAPABILITIES.intersection(allowed) and not profile.enforces_read_only:
        raise DispatchRefused(
            f"运行时 {runtime_name} 不能强制只读，不能承接员工 {employee.id} 的只读 Job"
        )


def _translate_tools(
    tool_names: Sequence[str], profile: RuntimeCapabilities, *, required: bool
) -> list[str]:
    """旧/规范化工具名 → 目标运行时工具名；allow 缺能力拒绝，deny 缺能力可省略。"""
    translated: list[str] = []
    for capability in _normalised_tools(tool_names):
        try:
            native = profile.tool_for(capability)
        except UnsupportedCapability as error:
            if required:
                raise DispatchRefused(str(error)) from error
            continue
        if native not in translated:
            translated.append(native)
    return translated


def runtime_compatible(spec: ProcedureSpec, runtime_name: str) -> bool:
    """这道 Procedure 跑得动这个运行时吗。

    **判据只此一份。** 派发闸问它,界面上"换过去还差哪些兼容声明"那份提示也问它
    (`server.employees_edit.compat_gap`)。各判一次的话两者迟早分叉,而分叉的表现是
    "界面没提示却被挡下"或者"补完了还是被挡下"——两种都无从解释,因为人看到的那份
    提示与真正把关的那段代码根本不是同一个答案。
    """
    allowed = spec.compat.runtimes
    if not allowed:
        return True
    # 纯脚本声明 `none`:它根本不碰运行时,派给谁都无所谓。
    if RUNTIME_NONE in allowed and spec.kind is ProcedureKind.DETERMINISTIC:
        return True
    # 回放对兼容性透明:它重放的是当初某个**真实运行时**的输出,兼容性在录制那一刻
    # 就已经成立。不放行的话,任何声明了 compat 的 Procedure 都进不了主测试缝,而那正是
    # 全仓端到端测试走的唯一一条路——代价是把生产资产改成 `[claude-code, replay]`,
    # 让一个测试概念漏进项目资产里。
    if runtime_name == RUNTIME_REPLAY:
        return True
    # 人对兼容性同样透明。`compat.runtimes` 回答的是"哪些 CLI 跑得动这道工序"——把人挡在
    # 外面等于说"这道工序人干不了",而那从来不是真的:人正是这套系统里唯一什么都能干的
    # 执行者。要求每份工序资产都补一句 `human` 才是把实现细节漏进了项目资产。
    if runtime_name == HUMAN:
        return True
    return runtime_name in allowed


def _check_runtime_compat(spec: ProcedureSpec, runtime_name: str) -> None:
    """把只在一个运行时上验证过的 Procedure 派给别的,应该报错而不是试试看。"""
    if not runtime_compatible(spec, runtime_name):
        allowed = spec.compat.runtimes
        raise DispatchRefused(f"{spec.ref} 不兼容运行时 {runtime_name}(兼容: {', '.join(allowed)})")


async def dispatch_procedure(
    spec: ProcedureSpec,
    pool: AgentPool,
    employee: EmployeeConfig,
    task_id: str,
    state: TaskState,
    inputs: dict[str, Any],
    workdir: Path,
    output_dir: Path,
    config: Config,
    runtime_name: str | None = None,
    round_: int = 1,
    subject: str = "",
    assignee: str = "",
    env: Mapping[str, str] | None = None,
    requirement: str = "",
    modules: Sequence[str] = (),
    failures: Sequence[FailureReport] = (),
    route_inputs: RouteInputs | None = None,
    extra_forbid: Sequence[str] = (),
    write_scope: Sequence[str] | None = None,
) -> DispatchResult:
    """以某个员工的身份执行一个 Procedure。

    `runtime_name` 缺省用员工声明的那个;显式传值是覆盖(测试与演示用回放运行时)。

    `requirement` / `modules` / `failures` 是**任务上下文**:需求原文、本次涉及的模块、
    此前各轮的失败报告。派发这一层算不出它们(它不知道这个 Job 属于哪个任务的第几轮),
    由任务编排传进来。不传的话上下文包只有前两段——员工看不到需求,也看不到自己上一轮
    错在哪。

    `extra_forbid` 同理:任务级的禁写规则(质量线的写集分离)由任务编排算,这一层只透传。
    在这里算的话,派发要去读策略与计划,而它连这个 Job 属于哪个任务的第几轮都不知道。
    """
    runtime_name = runtime_name or employee.runtime
    ensure_dispatchable(spec, employee, state, inputs, runtime_name)

    # 缺凭证要在这里炸,不是让 Agent 跑到一半才发现推不上去。确定性 Procedure 不碰
    # Agent 进程,所以不该因为少一个 API key 就跑不了。
    credentials = (
        {} if spec.kind is ProcedureKind.DETERMINISTIC else _collect_credentials(employee, env)
    )

    started = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    result = DispatchResult(ok=False, procedure_ref=spec.ref)
    scope = _scope_for(employee, task_id, workdir, modules, extra_forbid, write_scope)

    intermediate: dict[str, Any] | None = None
    if spec.kind.needs_scripts:
        # 脚本同样在动文件。**确定性不等于可信**:越权检查挂在池上,而脚本不经过池,
        # 这一段此前是完全没人核对的——而门禁是确定性脚本真正大量执行的地方。
        baseline, preexisting = _baseline(workdir)
        script_result = _run_script(spec, workdir, output_dir, inputs)
        violated = _enforce_script_scope(
            workdir, scope, task_id, employee.id, output_dir, baseline, preexisting
        )
        if violated is not None:
            result.stages.append("script")
            result.exit_code = script_result.exit_code
            result.failure_kind = FailureKind.SCOPE
            result.failure_detail = violated
            result.duration_s = time.monotonic() - started
            return result
        result.stages.append("script")
        result.exit_code = script_result.exit_code
        if not script_result.ok:
            # 脚本已经失败了,不该再烧一次 Agent。
            result.failure_kind = script_result.failure_kind
            result.failure_detail = script_result.detail
            result.duration_s = time.monotonic() - started
            return result
        intermediate = script_result.intermediate

    if spec.kind is ProcedureKind.DETERMINISTIC or _skips_agent(intermediate):
        # 不拉 Agent 不等于不用守契约。树产出类工序在这条路上同样按 staging 裁决。
        result.ok, result.result_path, result.failure_detail = _check_outputs(
            spec, output_dir, output_check_for(spec.outputs.staging, workdir, config.knowledge)
        )
        if not result.ok:
            result.failure_kind = FailureKind.CONTRACT
        result.duration_s = time.monotonic() - started
        return result

    # 手艺物化排在**上下文渲染与拉起进程之前**,且在派发这一层做而不是在具体运行时里:
    # 派发是执行 Job 的唯一入口,放在这里"忘了物化"就不再是一种可能的状态;放进运行时的话
    # 每接一个新运行时都要重写一遍,而那正是可插拔要避免的。
    #
    # 排在渲染之前是因为降级路径要把手艺全文**内联进上下文包**——渲染完再物化就晚了。
    #
    # 集合 = 本次工序自带的 craft + 该员工声明的通用 craft。同名时工序级优先——工序对
    # 这次作业的了解比员工级声明多。开发员工因此不会看到架构员工的手艺清单。
    wanted = craft.collect(spec.craft_dir, _common_craft_root(spec), employee.crafts)
    try:
        mount = _mount_or_inline(runtime_name, workdir, wanted)
    except craft.CraftMountError as error:
        # 不静默跳过:跳过的话"手艺没挂上"会表现为"最近它好像变笨了"。
        result.stages.append("craft")
        result.failure_kind = FailureKind.PROCESS
        result.failure_detail = str(error)
        result.duration_s = time.monotonic() - started
        return result
    result.crafts = list(mount.crafts)

    job = _build_job_spec(
        spec,
        employee=employee,
        task_id=task_id,
        scope=scope,
        round_=round_,
        subject=subject,
        # 节点上的指派优先于员工定义里的:前者是"这一次交给谁",后者是这个角色平时是谁。
        assignee=assignee or employee.assignee,
        requirement=requirement,
        modules=modules,
        failures=failures,
        workdir=workdir,
        output_dir=output_dir,
        inputs=inputs,
        intermediate=intermediate,
        credentials=credentials,
        config=config,
        timeout_s=employee.effective_timeout(config),
        max_tokens=_job_max_tokens(spec, employee, config, runtime_name),
        runtime_name=runtime_name,
        crafts=_inline_bodies(wanted, mount),
    )
    # 经过池而不是直接调运行时:并发闸、同任务串行、任务级预算都在池里,
    # 绕过去等于让这三样对 Procedure 派发失效(PRD 02:池是执行 Job 的唯一入口)。
    job_result = await pool.submit(job, runtime_name=runtime_name)
    result.stages.append("agent")
    result.job = job_result
    result.ok = job_result.ok
    result.failure_kind = job_result.failure_kind
    result.failure_detail = job_result.failure_detail
    result.result_path = job_result.result_path
    result.exit_code = job_result.exit_code
    result.duration_s = time.monotonic() - started
    return result


def _common_craft_root(spec: ProcedureSpec) -> Path | None:
    """通用手艺库的位置:`genome/procedures/_common/craft/`。

    从工序自己的路径往上一级推,而不是另外传一个根进来——两处各推一次的话,项目级与全局级
    工序会算出不同的通用库,而"这个员工到底带了哪份手艺"就没有唯一答案了。
    """
    # 工序目录的父目录就是 `genome/procedures/`,通用库是它下面的 `_common/craft/`。
    root = spec.path.parent / craft.COMMON_DIR / craft.CRAFT_DIR
    return root if root.is_dir() else None


def _mount_or_inline(
    runtime_name: str, workdir: Path, sources: dict[str, Path]
) -> craft.MountReport:
    """物化,或者在运行时不支持技艺包时降级。

    **降级是内联摘要,不是不给。** 手艺内容只写一份、运行时无关——可插拔承诺不能因为多了
    手艺层就破掉。不支持挂载的运行时仍然要清掉残留目录,否则换运行时跑同一个工作区时,
    上一个运行时留下的手艺会以"没人管的文件"形式留在那儿。
    """
    profile = capabilities_of(runtime_name)
    if profile is not None and not profile.craft_mounting:
        report = craft.materialize(workdir, {})
        report.inlined = sorted(sources)
        return report
    return craft.materialize(workdir, sources)


def _inline_bodies(
    sources: dict[str, Path], mount: craft.MountReport
) -> tuple[tuple[str, str], ...]:
    """降级路径下要内联进上下文包的手艺全文。

    支持挂载的运行时返回空——它自己会按需渐进式加载,内联一遍等于同样内容塞两份、白吃预算。
    """
    bodies: list[tuple[str, str]] = []
    for name in mount.inlined:
        manifest = sources[name] / craft.CRAFT_MANIFEST
        with contextlib.suppress(OSError):
            bodies.append((name, manifest.read_text(encoding="utf-8")))
    return tuple(bodies)


def _job_max_tokens(
    spec: ProcedureSpec, employee: EmployeeConfig, config: Config, runtime_name: str | None = None
) -> int:
    """这次 Job 的 token 上限:员工额度与 Procedure 自报上限取小。

    取小而不是让 Procedure 覆盖:员工定义是被评审的那份资产,一个 Procedure 不该有能力给自己
    开一张超过所属员工限额的额度。

    **运行时可以把它压到更低**(能力矩阵里的 `job_max_tokens`):人的 Job 是 0,它一个 token
    都不烧,而这个值同时是执行池的预算预扣输入——沿用缺省额度的话,一个预算吃紧的任务会在
    待办投出去之前就被判"装不下下一个 Job"并升级人工,而那个任务本来正要交给人做。
    """
    profile = capabilities_of(runtime_name or "")
    if profile is not None and profile.job_max_tokens is not None:
        return profile.job_max_tokens
    allowed = employee.effective_max_tokens(config)
    declared = spec.budget.max_tokens
    return min(allowed, declared) if declared is not None else allowed


def _check_outputs(
    spec: ProcedureSpec,
    output_dir: Path,
    output_check: Callable[[Path], str | None] | None = None,
) -> tuple[bool, Path | None, str | None]:
    """产物契约:result.json 合规、声明过的产物真的被写出来了,树产出还要过 staging 校验。

    只查 result.json 是不够的:一个声明产出 gate-report.json 却没写的 Procedure 会一路
    绿灯过去,直到下游去读那个文件时才炸——而那时已经离现场很远了。
    """
    check = check_result_contract(output_dir, spec.output_schema, output_check)
    if not check.ok:
        return False, check.path, check.detail
    missing = [name for name in spec.outputs.artifacts if not (output_dir / name).is_file()]
    if missing:
        return False, check.path, f"声明的产物没有被写出来: {', '.join(missing)}"
    return True, check.path, None


def _collect_credentials(employee: EmployeeConfig, env: Mapping[str, str] | None) -> dict[str, str]:
    """按名单从环境里挑凭证。

    **只挑声明过的。** 直接把整个 `os.environ` 递给员工进程的话,一个只需要写代码的
    员工会同时拿到生产数据库口令——而那份口令根本不必存在于它的进程里。
    """
    source = os.environ if env is None else env
    missing = [name for name in employee.credentials if name not in source]
    if missing:
        raise DispatchRefused(f"{employee.id} 声明的凭证在环境里不存在: {', '.join(missing)}")
    return {name: source[name] for name in employee.credentials}


def _baseline(workdir: Path) -> tuple[str | None, dict[str, str]]:
    """脚本跑之前工作树停在哪、以及哪些路径当时就已经脏了。

    不是 git 仓时没有基线,也就无从核对。
    """
    from agentgenome.space.gitcmd import GitError
    from agentgenome.space.scope_guard import capture_baseline, capture_preexisting

    try:
        baseline = capture_baseline(workdir)
        return baseline, capture_preexisting(workdir, baseline)
    except GitError:
        return None, {}


def _enforce_script_scope(
    workdir: Path,
    scope: ScopePolicy,
    task_id: str,
    employee_id: str,
    output_dir: Path,
    baseline: str | None,
    preexisting: Mapping[str, str],
) -> str | None:
    """核对脚本实际碰了什么。越权则回滚并返回说明,没越权返回 `None`。

    不是 git 仓时跳过——`agctl procedure run` 可以指向任意目录,那里未必有工作树可查。
    """
    if baseline is None:
        return None
    from agentgenome.space.scope_guard import enforce_scope

    report = enforce_scope(
        workdir=workdir,
        policy=scope,
        task_id=task_id,
        employee_id=employee_id,
        output_dir=output_dir,
        baseline=baseline,
        preexisting=preexisting,
    )
    if report.ok:
        return None
    return f"{employee_id} 的脚本越出授权范围: {report.render()}"


def _scope_for(
    employee: EmployeeConfig,
    task_id: str,
    workdir: Path,
    modules: Sequence[str] = (),
    extra_forbid: Sequence[str] = (),
    write_scope: Sequence[str] | None = None,
) -> ScopePolicy:
    """这个 Job 的授权范围:员工规则展开占位符,再叠上项目的受保护路径。

    受保护路径就地从 workdir 读,不做成参数——做成参数的话,调用方漏传一次的表现
    是**保护静默失效**,而这种失效没有任何外部症状。规则文件本身坏了则直接抛错:
    读不出受保护路径时当作"没有受保护路径"是最糟的降级方向。

    模块列表同理就地翻译成挂载点:权限层说的是路径,而**身份到位置的翻译只做一次**。
    根索引读不出来时当作"一个模块都认不出",于是带 `{task_modules}` 的授权展开成空——
    方向是更窄,而更窄是可恢复的。
    """
    protected = load_rules(workdir).protected.paths_for(employee.id)
    scope = employee.scope(
        task_id,
        protected,
        module_paths=_mount_paths(workdir, modules),
        extra_forbid=extra_forbid,
    )
    return scope if write_scope is None else scope.with_write_limit(write_scope)


def _mount_paths(workdir: Path, modules: Sequence[str]) -> list[str]:
    """模块 id → 挂载点。读不出根索引就给空:方向是**更窄**,而更窄是可恢复的。"""
    if not modules:
        return []
    with contextlib.suppress(Exception):
        return load_project_map(workdir).mount_paths(modules)
    return []


def _build_job_spec(
    spec: ProcedureSpec,
    employee: EmployeeConfig,
    task_id: str,
    scope: ScopePolicy,
    round_: int,
    #: 这次作业作用在图里的哪个节点(或哪个模块)上。**回放键的一维**:同一轮里同 employee
    #: 同 procedure 被派多次时(环里的批判、图里的并行节点),不带它的话回放会给每一次返回
    #: 同一份产出,而测试照样是绿的。
    subject: str,
    #: 这份活派给谁(人)。只有 human 运行时消费它。
    assignee: str,
    workdir: Path,
    output_dir: Path,
    inputs: dict[str, Any],
    intermediate: dict[str, Any] | None,
    credentials: dict[str, str],
    config: Config,
    timeout_s: int,
    max_tokens: int,
    requirement: str,
    modules: Sequence[str],
    failures: Sequence[FailureReport],
    runtime_name: str | None = None,
    route_inputs: RouteInputs | None = None,
    #: 降级路径下要内联进上下文包的手艺全文。支持挂载的运行时这里是空的。
    crafts: tuple[tuple[str, str], ...] = (),
) -> JobSpec:
    """把 ProcedureSpec 与员工定义一起塌缩成 JobSpec。

    这一步归 Procedure 这边:PRD 02 刻意把 JobSpec 做成扁平的,就是为了不让 Procedure 注册表
    与员工定义的概念漏进运行时层。
    """
    context = output_dir / context_filename(0)
    context.write_text(
        _render_context(
            spec,
            employee,
            inputs,
            intermediate,
            workdir=workdir,
            output_dir=output_dir,
            config=config,
            requirement=requirement,
            modules=modules,
            failures=failures,
            runtime_name=runtime_name,
            task_id=task_id,
            route_inputs=route_inputs,
            crafts=crafts,
        ),
        encoding="utf-8",
    )
    return JobSpec(
        task_id=task_id,
        employee_id=employee.id,
        procedure_id=spec.id,
        procedure_version=spec.version,
        round=round_,
        subject=subject,
        assignee=assignee,
        workdir=workdir,
        context_file=context,
        output_dir=output_dir,
        output_schema=spec.output_schema,
        # 树产出类工序(`outputs.staging`)的成败由 staging 校验裁决——验证产物,
        # 不验证自述(PRD 34)。没声明的工序这里是 `None`,契约照旧只看 schema。
        output_check=output_check_for(spec.outputs.staging, workdir, config.knowledge),
        # staging 只获得产物契约重试；计划工序只获得协议补交。两个额度不能合并，
        # 否则 staging 会因为能增量修文件而顺带重跑一次完整 Agent 协议。
        contract_retries=1 if spec.outputs.staging else 0,
        protocol_retries=1 if spec.ownership is ProcedureOwnership.PLAN else 0,
        tools_allow=_translate_tools(
            employee.tools.allow, _required_runtime_profile(runtime_name), required=True
        ),
        tools_deny=_translate_tools(
            employee.tools.deny, _required_runtime_profile(runtime_name), required=False
        ),
        credentials=dict(credentials),
        timeout_s=timeout_s,
        max_tokens=max_tokens,
        enforce_token_limit=config.budgets.enforce,
        scope=scope,
    )


def _render_context(
    spec: ProcedureSpec,
    employee: EmployeeConfig,
    inputs: dict[str, Any],
    intermediate: dict[str, Any] | None,
    workdir: Path,
    output_dir: Path,
    config: Config,
    requirement: str,
    modules: Sequence[str],
    failures: Sequence[FailureReport],
    runtime_name: str | None = None,
    task_id: str = "",
    route_inputs: RouteInputs | None = None,
    crafts: tuple[tuple[str, str], ...] = (),
) -> str:
    """走 ContextAssembler,不自己拼一份。

    自己拼的那一版漏掉了**员工的角色提示词**——派下去的 Job 于是带着空人格上岗。
    两份组装逻辑必然分叉,而分叉的表现是"某条路径下员工少看了点东西",没有任何外部症状。

    基因组切片只在**知道涉及哪些模块**时才切:不知道就整份给,由预算去裁——空着不给的话,
    员工连项目规则都看不到。
    """
    rendered = ""
    if intermediate is not None:
        rendered = "```json\n" + json.dumps(intermediate, ensure_ascii=False, indent=2) + "\n```"
    context = assemble(
        ContextInputs(
            employee=employee,
            procedure_prompt=spec.prompt,
            procedure_inputs=inputs,
            requirement=requirement,
            failures=tuple(failures),
            genome=_genome_slice(
                workdir, modules, task_id, route_inputs, config.knowledge.card_context_tokens
            ),
            intermediate=rendered,
            crafts=crafts,
        ),
        budget_tokens=context_budget(employee, config, runtime_name),
    ).text
    profile = _required_runtime_profile(runtime_name)
    if spec.output_schema:
        context += _result_delivery_instruction(profile, output_dir)
    return context


def _required_runtime_profile(runtime_name: str | None) -> RuntimeCapabilities:
    profile = capabilities_of(runtime_name or "")
    if profile is None:
        raise DispatchRefused(f"运行时 {runtime_name!r} 没有能力声明")
    return profile


def _result_delivery_instruction(profile: RuntimeCapabilities, output_dir: Path) -> str:
    heading = "\n\n---\n\n## 最终产物交付（运行时强制规则）\n\n"
    if profile.result_delivery is ResultDelivery.STRUCTURED_RESPONSE:
        return (
            heading + "最终返回一个符合输出 schema 的 JSON 对象。"
            "不要自行写 result.json 或 critique.json；运行时会校验结构化输出并原子落盘。\n"
        )
    if profile.result_delivery is ResultDelivery.RESULT_FILE:
        target = output_dir.resolve() / "result.json"
        return (
            heading + f"把符合 output schema 的最终 JSON 写到 `{target}`。"
            "只写一次最终小票，不要另写 critique.json，也不要在小票缺失时重新执行任务。\n"
        )
    return (
        heading + "把符合 output schema 的最终 JSON 作为名为 `result.json` 的运行时产物交回。"
        "不要在工作区里自行猜测产物路径。\n"
    )


def _genome_slice(
    workdir: Path,
    modules: Sequence[str],
    task_id: str = "",
    route_inputs: RouteInputs | None = None,
    card_budget: int | None = None,
) -> tuple[Fragment, ...]:
    """切一份基因组。切不出来不算失败——一个还没有项目地图的 Workspace 照样能派 Job。

    **顺便记一次曝光。** 卡片进了上下文却不记录"用了哪几张",命中计数就永远是 0,自然选择
    也就无从谈起——那等于把 PRD 10 的整条自进化链路断在最后一米。

    记账失败不影响派发:它是增值,而这次 Job 已经要开跑了。
    """
    try:
        fragments = load_genome_slice(
            workdir, modules, route_inputs=route_inputs, card_budget_tokens=card_budget
        )
    except GenomeValidationError:
        return ()
    if task_id:
        from agentgenome.context import card_refs, lesson_ids
        from agentgenome.genome.evolution.lifecycle import record_exposure
        from agentgenome.genome.hits import record_exposure as record_card_exposure

        with contextlib.suppress(OSError):
            record_exposure(workdir, task_id, lesson_ids(fragments))
        with contextlib.suppress(OSError):
            record_card_exposure(workdir, task_id, card_refs(fragments))
    return fragments


@dataclass
class _ScriptResult:
    ok: bool
    exit_code: int | None = None
    detail: str | None = None
    failure_kind: FailureKind = FailureKind.NONE
    intermediate: dict[str, Any] | None = None


def _skips_agent(intermediate: dict[str, Any] | None) -> bool:
    """hybrid 的脚本可以宣布这一轮不需要 Agent。

    集成测试是典型场景:全绿的那一轮没有任何东西可诊断,而 hybrid 默认总要拉一次 Agent。
    不给脚本这个出口的话,每一次通过的集成测试都要额外烧一个 Job 去说"没什么好说的"
    ——这违反"验证类一律确定性"那条原则,而且贵。

    **只有脚本能声明它。** 不是配置项:这一轮有没有活给 Agent 干,只有刚跑完的脚本知道。
    """
    return bool(intermediate and intermediate.get(SKIP_AGENT))


def _run_script(
    spec: ProcedureSpec, workdir: Path, output_dir: Path, inputs: dict[str, Any]
) -> _ScriptResult:
    """跑确定性逻辑。不经过 AgentRuntime,零 token。"""
    entry = spec.script_entry()
    if entry is None:
        return _ScriptResult(
            ok=False,
            failure_kind=FailureKind.PROCESS,
            detail=f"{spec.ref} 声明为 {spec.kind.value} 但没有可执行的脚本入口",
        )

    env = dict(os.environ)
    env[OUTPUT_DIR_ENV] = str(output_dir)
    env[WORKDIR_ENV] = str(workdir)
    env[INPUTS_ENV] = json.dumps(inputs, ensure_ascii=False)

    completed = subprocess.run(
        [sys.executable, str(entry)], cwd=workdir, env=env, capture_output=True, text=True
    )
    if completed.returncode != 0:
        return _ScriptResult(
            ok=False,
            exit_code=completed.returncode,
            failure_kind=FailureKind.PROCESS,
            detail=(
                f"{spec.ref} 的脚本以 {completed.returncode} 退出: "
                f"{(completed.stderr or completed.stdout).strip()[:500]}"
            ),
        )

    intermediate_path = output_dir / INTERMEDIATE
    intermediate = None
    if intermediate_path.is_file():
        intermediate = json.loads(intermediate_path.read_text(encoding="utf-8"))
    return _ScriptResult(ok=True, exit_code=0, intermediate=intermediate)


__all__ = [
    "INTERMEDIATE",
    "SKIP_AGENT",
    "INPUTS_ENV",
    "OUTPUT_DIR_ENV",
    "WORKDIR_ENV",
    "DispatchRefused",
    "DispatchResult",
    "ensure_dispatchable",
    "dispatch_procedure",
]
