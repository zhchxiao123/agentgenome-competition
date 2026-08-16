"""任务语义层:把 JobSpec 翻译成平台任务,把平台结果还原成 JobResult。

## 这个运行时不做什么(如实降级,不假装)

- **执行中的预算掐断做不到。** 子进程运行时逐行读流、超限实时掐进程;平台侧
  用量是事后的。任务预算的派发前拒绝仍然生效(在池里,与运行时无关),但单
  Job 的 `max_tokens` 对这个运行时是**软上限**——随任务下发,靠 Worker 侧自律。
- **开不了会话。** 能力矩阵声明 `sessions: false`,会话服务按既有路径拒绝并解释。
- **不下发真实凭证。** Worker 经平台网关持消费 token 访问外部服务;员工配置
  声明了必须直连凭证的,派发时显式报配置矛盾,不静默丢弃。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from agentgenome.agents.agentteams.transport import (
    AgentTeamsTransport,
    PlatformUnavailable,
    TransportJob,
    TransportOutcome,
)
from agentgenome.agents.agentteams.workspace import (
    WorkspaceSyncError,
    apply_changes,
    ensure_inside,
    snapshot_workspace,
)
from agentgenome.agents.artifacts import log_filename
from agentgenome.agents.contract import check_result_contract
from agentgenome.agents.contract_loop import run_with_contract_retry, write_attempt_context
from agentgenome.agents.runtime import FailureKind, JobResult, JobSpec


class AgentTeamsRuntime:
    """把一个 AgentTeams Worker 当作 Agent 来跑。"""

    name = "agentteams"

    def __init__(self, transport: AgentTeamsTransport) -> None:
        self._transport = transport

    def preflight(self) -> None:
        """平台可达性与消费 token 自检。失败抛出——配置问题在启动期暴露。"""
        self._transport.preflight()

    async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
        spec.output_dir.mkdir(parents=True, exist_ok=True)

        if dry_run:
            started = time.monotonic()
            write_attempt_context(spec, attempt=1, rejection=None)
            return JobResult(ok=True, dry_run=True, duration_s=time.monotonic() - started)

        if spec.credentials:
            # 显式报矛盾,不静默丢弃:悄悄不带凭证的表现是"员工突然推不了代码",
            # 而原因在三层之外的员工配置里。
            keys = ", ".join(sorted(spec.credentials))
            return JobResult(
                ok=False,
                failure_kind=FailureKind.PROCESS,
                failure_detail=(
                    f"配置矛盾: 员工 {spec.employee_id} 声明了直连凭证({keys}),"
                    "但 agentteams 运行时不向 Worker 下发真实凭证——Worker 只持平台"
                    "消费 token。去掉凭证声明,或把这个员工换回本地运行时。"
                ),
            )

        async def attempt(context_file: Path, attempt_no: int) -> JobResult:
            return await self._run_once(spec, context_file, attempt_no)

        return await run_with_contract_retry(spec, attempt)

    async def _run_once(self, spec: JobSpec, context_file: Path, attempt: int) -> JobResult:
        job = self._translate(spec, context_file, attempt)
        try:
            outcome = await asyncio.wait_for(self._transport.run_job(job), spec.timeout_s)
        except WorkspaceSyncError as exc:
            # 传输侧的路径校验(推送越界等)——是这个 Job 的问题,不是平台的。
            return self._unaccounted(
                JobResult(ok=False, failure_kind=FailureKind.PROCESS, failure_detail=str(exc))
            )
        except TimeoutError:
            return self._unaccounted(
                JobResult(
                    ok=False,
                    failure_kind=FailureKind.TIMEOUT,
                    failure_detail=f"超过 {spec.timeout_s} 秒未从平台拿到结果",
                )
            )
        except PlatformUnavailable as exc:
            return self._unaccounted(
                JobResult(
                    ok=False,
                    failure_kind=FailureKind.PROCESS,
                    failure_detail=f"AgentTeams 平台不可用: {exc}",
                )
            )

        log_path = self._write_log(spec, outcome, attempt)
        result = self._evaluate(spec, outcome)
        result.log_path = log_path
        # 有平台应答就有账——失败的 Job 钱也已经烧了。网关给不出用量时显式标
        # 不可得,不填 0:填 0 会让成本看板悄悄少算,比缺数据更糟。
        if outcome.tokens_used is None:
            result.tokens_used = 0
            result.tokens_available = False
        else:
            result.tokens_used = outcome.tokens_used
            result.tokens_available = True
        return result

    def _translate(self, spec: JobSpec, context_file: Path, attempt: int) -> TransportJob:
        """JobSpec → 平台任务。上下文从落盘文件读——传输层不碰本地路径。"""
        return TransportJob(
            task_id=spec.task_id,
            employee_id=spec.employee_id,
            procedure_ref=spec.procedure_ref,
            round=spec.round,
            attempt=attempt,
            subject=spec.subject,
            context_text=context_file.read_text(encoding="utf-8"),
            tools_allow=tuple(spec.tools_allow),
            tools_deny=tuple(spec.tools_deny),
            timeout_s=spec.timeout_s,
            max_tokens=spec.max_tokens,
            output_schema=dict(spec.output_schema),
            craft=_delivery_craft(spec),
            # 每次尝试都推当前现场:契约失败重试时,第一次尝试已落地的代码改动
            # 必须被第二次尝试看到——与子进程"同一工作区接着干"的语义对齐。
            workspace=snapshot_workspace(spec.workdir),
        )

    def _evaluate(self, spec: JobSpec, outcome: TransportOutcome) -> JobResult:
        """平台结果 → JobResult。契约先于 Worker 自述:自称成功但没交产物照样失败。

        记账不在这里——凡是拿到了平台应答的路径,账由 `_run_once` 按应答统一填。
        """
        if not outcome.ok:
            return JobResult(
                ok=False,
                failure_kind=FailureKind.PROCESS,
                failure_detail=f"Worker 执行失败: {outcome.detail or '(平台未给出原因)'}",
            )

        # 先落工作区改动,再校验契约:契约失败要重试,而重试必须看到第一次
        # 已写下的代码——与子进程"同一工作区接着干"的语义对齐。
        if outcome.changed_files:
            try:
                apply_changes(spec.workdir, outcome.changed_files)
            except WorkspaceSyncError as exc:
                return JobResult(
                    ok=False, failure_kind=FailureKind.PROCESS, failure_detail=str(exc)
                )

        landed = self._land_artifacts(spec, outcome)
        if landed is not None:
            return landed

        check = check_result_contract(spec.output_dir, spec.output_schema, spec.output_check)
        if not check.ok:
            return JobResult(
                ok=False, failure_kind=FailureKind.CONTRACT, failure_detail=check.detail
            )
        return JobResult(ok=True, result_path=check.path)

    def _land_artifacts(self, spec: JobSpec, outcome: TransportOutcome) -> JobResult | None:
        """把平台还回来的产物落到产物目录。路径越界即失败,不落半份。"""
        root = spec.output_dir.resolve()
        try:
            landed = {
                name: ensure_inside(root, name, "平台返回的产物") for name in outcome.artifacts
            }
        except WorkspaceSyncError as exc:
            return JobResult(
                ok=False, failure_kind=FailureKind.PROCESS, failure_detail=str(exc)
            )
        for name, target in landed.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(outcome.artifacts[name], encoding="utf-8")
        return None

    @staticmethod
    def _write_log(spec: JobSpec, outcome: TransportOutcome, attempt: int) -> Path | None:
        """归一化事件进日志面,与本地 Job 同构——审计包不因运行时不同而残缺。"""
        if not outcome.events:
            return None
        log_path = spec.output_dir / log_filename(attempt)
        with log_path.open("w", encoding="utf-8") as sink:
            for event in outcome.events:
                sink.write(json.dumps(event, ensure_ascii=False) + "\n")
        return log_path

    @staticmethod
    def _unaccounted(result: JobResult) -> JobResult:
        """没拿到平台应答的失败(超时、平台不可用)——账如实标"不可得",不填 0。"""
        result.tokens_used = 0
        result.tokens_available = False
        return result


def _delivery_craft(spec: JobSpec) -> str:
    """随每个 Job 下发的交作业手艺。

    Worker 侧运行时各异,"产物写到哪、按什么形状"不能指望它天生知道——这份
    手艺把合同教给它:结果必须作为 `result.json` 产物交回,有 schema 的连
    schema 本体一起给,Worker 不猜字段。
    """
    lines = [
        "## 交作业合同",
        "",
        "任务完成时,必须把结果写成一份名为 `result.json` 的产物交回。",
        "**没有 `result.json` 的执行会被判失败**,不论代码改得多好。",
    ]
    if spec.output_schema:
        lines += [
            "",
            "`result.json` 必须符合下面这份 JSON Schema(不符合会被退回重做):",
            "",
            "```json",
            json.dumps(spec.output_schema, ensure_ascii=False, indent=2),
            "```",
        ]
    return "\n".join(lines) + "\n"


__all__ = ["AgentTeamsRuntime"]
