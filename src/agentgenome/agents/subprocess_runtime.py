"""基于子进程的运行时骨架。

所有 CLI Agent 运行时的公共部分:拉起进程、等它结束、校验产物。契约失败是否补交
的外环不在这里——那是任何运行时共用的显式策略,住在 `contract_loop`,这里只消费。
具体运行时(`ClaudeCodeRuntime` 等)只需要提供"怎么把 JobSpec 变成一条命令"与
"怎么解析它的输出"。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentgenome.agents.artifacts import RESULT_FILENAME, log_filename
from agentgenome.agents.contract import check_result_contract, validate_result_payload
from agentgenome.agents.contract_loop import (
    CONTRACT_RETRIES,
    run_with_contract_retry,
    write_attempt_context,
)
from agentgenome.agents.events import EventKind, NormalizedEvent, UsageAccumulator
from agentgenome.agents.runtime import FailureKind, JobResult, JobSpec

#: 允许透传给子进程的环境变量。其余一律不给。
#:
#: 不继承父进程全量环境:编排器进程里有推送凭证、平台令牌、可能还有别的项目的密钥,
#: 而这个子进程正在执行一段可能被提示注入影响的指令。
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR")

#: 中止子进程时,从"请你退出"到"强杀"之间的宽限期。
TERMINATE_GRACE_S = 2.0

#: 单行 stdout 的读缓冲上限。`asyncio` 的默认值只有 64KiB——真实 Agent 的
#: stream-json 输出里,一次读大文件或大 diff 的 `tool_result` 常常单行超过这个数,
#: 撞上就是一次不受控的 `ValueError` 崩掉整个进程,而不是一次正常的 Job 失败。
#: 16MiB 留了足够余量,仍然远小于会拖垮内存的量级。
STREAM_LIMIT = 16 * 1024 * 1024

#: 打开录制模式。构造新剧情的省力路径——不必手写一大堆 JSON。
RECORD_MODE_ENV = "AGENTGENOME_RECORD"
#: 录制库的位置。与回放读取的是同一个。
RECORDINGS_ENV = "AGENTGENOME_RECORDINGS"

@dataclass(frozen=True)
class _TerminalError:
    """外部运行时自己声明的终态失败，不允许后续契约检查覆盖。"""

    message: str
    reason: str


@dataclass(frozen=True)
class _ConsumedOutput:
    """流消费结束后，产物交付需要的两种互斥证据。"""

    structured_output: dict[str, Any] | None = None
    terminal_text: str | None = None
    terminal_error: _TerminalError | None = None


class _Abort(Exception):
    """流消费过程中决定中止本次执行。"""

    def __init__(self, kind: FailureKind, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


class SubprocessRuntime:
    """把一个外部命令当作 Agent 来跑。

    自身是可用的(测试拿它跑"真实的假子进程"),也是具体运行时的基类。
    """

    name = "subprocess"

    def __init__(self, argv: list[str]) -> None:
        self._argv = list(argv)

    # --- 子类可覆写 ----------------------------------------------------------

    def build_argv(self, spec: JobSpec, context_file: Path) -> list[str]:
        """把 JobSpec 变成一条命令。"""
        return list(self._argv)

    def env_allowlist(self) -> tuple[str, ...]:
        """允许透传的环境变量。具体运行时可以扩,但不能改成"全都给"。"""
        return ENV_ALLOWLIST

    def build_env(self, spec: JobSpec, context_file: Path) -> dict[str, str]:
        """从白名单构造子进程环境,再叠加本次声明的凭证。

        不继承父进程全量环境:编排器进程里有推送凭证、平台令牌、可能还有别的项目的
        密钥,而这个子进程正在执行一段可能被提示注入影响的指令。最小权限不靠自觉,
        靠它根本拿不到。
        """
        env = {key: os.environ[key] for key in self.env_allowlist() if key in os.environ}
        env.update(spec.credentials)
        return env

    def parse_line(self, raw: dict[str, Any]) -> list[NormalizedEvent]:
        """把一行原生输出解析成归一化事件。骨架不认识任何格式,由子类给。"""
        return []

    # --- 执行 ----------------------------------------------------------------

    def preflight(self) -> None:
        executable = self._argv[0]
        if shutil.which(executable) is None and not Path(executable).is_file():
            raise RuntimeError(f"运行时依赖的可执行文件不存在: {executable}")

    # --- 流消费 --------------------------------------------------------------

    async def _consume(
        self,
        stream: asyncio.StreamReader,
        spec: JobSpec,
        log_path: Path,
        usage: UsageAccumulator,
    ) -> _ConsumedOutput:
        """逐行读、解析、落盘,并在超预算时中止。

        逐行处理而不是先读完再解析:输出可能很大,而且预算执行必须是**实时**的
        ——事后算账时钱已经花完了。
        """
        structured_output: dict[str, Any] | None = None
        terminal_text: str | None = None
        terminal_error: _TerminalError | None = None
        with log_path.open("w", encoding="utf-8") as sink:
            while True:
                try:
                    raw_line = await stream.readline()
                except ValueError as error:
                    # 单行超过 `STREAM_LIMIT` 时 `readline()` 抛的是裸 `ValueError`,
                    # 不是这里已经在处理的任何一种失败——不接住的话它会带着一整条
                    # Python 堆栈炸穿 `agctl`,而不是变成一次能重试的普通 Job 失败。
                    raise _Abort(
                        FailureKind.PROCESS, f"单行输出超过缓冲上限({STREAM_LIMIT} 字节): {error}"
                    ) from error
                if not raw_line:
                    break
                for event in self._events_from(raw_line):
                    sink.write(event.to_json() + "\n")
                    sink.flush()  # 运行中要能被 tail
                    usage.observe_event(event)
                    if event.kind is EventKind.STRUCTURED_OUTPUT:
                        payload = event.detail.get("payload")
                        if isinstance(payload, dict):
                            structured_output = payload
                    elif event.kind is EventKind.RUNTIME_RESULT:
                        terminal_text = event.text
                    elif event.kind is EventKind.ERROR and event.detail.get("terminal") is True:
                        reason = event.detail.get("terminal_reason") or "runtime_error"
                        terminal_error = _TerminalError(message=event.text, reason=str(reason))
                    if (
                        spec.enforce_token_limit
                        and event.kind is EventKind.USAGE
                        and usage.total() > spec.max_tokens
                    ):
                        raise _Abort(
                            FailureKind.BUDGET,
                            f"token 预算耗尽: {usage.total()} > {spec.max_tokens}",
                        )
        return _ConsumedOutput(
            structured_output=structured_output,
            terminal_text=terminal_text,
            terminal_error=terminal_error,
        )

    def _events_from(self, raw_line: bytes) -> list[NormalizedEvent]:
        text = raw_line.decode("utf-8", errors="replace").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # 坏行留痕但不中断——解析是增强,不该让 Job 因为一行数据而失败。
            return [
                NormalizedEvent(
                    kind=EventKind.ERROR, text=text[:500], detail={"reason": "无法解析为 JSON"}
                )
            ]
        if not isinstance(parsed, dict):
            return [
                NormalizedEvent(
                    kind=EventKind.ERROR, text=text[:500], detail={"reason": "顶层不是映射"}
                )
            ]
        return self.parse_line(parsed)

    @staticmethod
    async def _drain(stream: asyncio.StreamReader) -> None:
        """读干净一条流并丢弃。防止管道写满把子进程卡死。"""
        while await stream.readline():
            pass

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """先请整个进程组退出，不走再强杀，避免 Agent 的孙进程变成孤儿。"""
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), TERMINATE_GRACE_S)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            await process.wait()

    async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
        spec.output_dir.mkdir(parents=True, exist_ok=True)

        if dry_run:
            started = time.monotonic()
            write_attempt_context(spec, attempt=1, rejection=None)
            return JobResult(ok=True, dry_run=True, duration_s=time.monotonic() - started)

        async def attempt(context_file: Path, attempt_no: int) -> JobResult:
            return await self._run_once(spec, context_file, attempt_no)

        result = await run_with_contract_retry(spec, attempt)
        self._maybe_record(spec, result)
        return result

    @staticmethod
    def _maybe_record(spec: JobSpec, result: JobResult) -> None:
        """录制模式:把这次运行落盘成一份可直接回放的录制。

        **只录成功的运行。** 失败的落成录制之后,回放会重现那次失败——但重现的原因
        是录制里声明的而不是真实发生的。两者混在一起,录制库很快就没法信任了。
        要录失败路径,用 `meta.yaml` 的 `failure_kind` 手写声明。
        """
        if not os.environ.get(RECORD_MODE_ENV) or not result.ok:
            return
        library = os.environ.get(RECORDINGS_ENV)
        if not library:
            # 没配录制库不该让正常运行失败——录制是增强,不是必需路径。
            return
        from agentgenome.agents.recording import record_job

        result.recorded_to = record_job(Path(library), spec, result)

    async def _run_once(self, spec: JobSpec, context_file: Path, attempt: int) -> JobResult:
        log_path = spec.output_dir / log_filename(attempt)
        usage = UsageAccumulator()

        try:
            process = await asyncio.create_subprocess_exec(
                *self.build_argv(spec, context_file),
                cwd=spec.workdir,
                env=self.build_env(spec, context_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            return JobResult(
                ok=False, failure_kind=FailureKind.PROCESS, failure_detail=f"无法拉起子进程: {exc}"
            )

        assert process.stdout is not None and process.stderr is not None
        # stderr 必须一起排掉。只读 stdout 的话,子进程往 stderr 写满管道缓冲区
        # (64KB)就会阻塞,stdout 随之停住,最后表现为莫名其妙的超时。
        drain_stderr = asyncio.create_task(self._drain(process.stderr))
        aborted: _Abort | None = None
        consumed = _ConsumedOutput()
        try:
            consumed = await asyncio.wait_for(
                self._consume(process.stdout, spec, log_path, usage), spec.timeout_s
            )
            exit_code = await asyncio.wait_for(process.wait(), TERMINATE_GRACE_S)
        except TimeoutError:
            aborted = _Abort(FailureKind.TIMEOUT, f"超过 {spec.timeout_s} 秒未结束")
            exit_code = None
        except _Abort as abort:
            aborted = abort
            exit_code = None
        finally:
            drain_stderr.cancel()

        if aborted is not None:
            # 无论哪种中止,日志都已经落盘且保留——挂死的 Job 最有诊断价值的
            # 就是它卡住前在做什么。
            await self._terminate(process)
            return self._finish(
                JobResult(ok=False, failure_kind=aborted.kind, failure_detail=aborted.detail),
                log_path,
                usage,
                exit_code,
            )

        if consumed.terminal_error is not None:
            error = consumed.terminal_error
            return self._finish(
                JobResult(
                    ok=False,
                    failure_kind=FailureKind.PROCESS,
                    failure_detail=f"运行时报告失败({error.reason}): {error.message}",
                ),
                log_path,
                usage,
                exit_code,
            )

        structured_output = consumed.structured_output
        if structured_output is None and consumed.terminal_text is not None:
            structured_output = _recover_terminal_payload(
                consumed.terminal_text, spec.output_schema
            )
        if structured_output is not None:
            self._write_structured_result(spec.output_dir, structured_output)

        protocol_failure = (
            bool(spec.output_schema)
            and consumed.terminal_text is not None
            and consumed.structured_output is None
            and structured_output is None
        )
        if protocol_failure:
            return self._finish(
                JobResult(
                    ok=False,
                    failure_kind=FailureKind.PROTOCOL,
                    failure_detail=(
                        "运行时未交付有效结构化输出 structured_output，且终态正文中没有唯一且"
                        "符合 schema 的 JSON 对象。"
                    ),
                ),
                log_path,
                usage,
                exit_code,
            )

        # 契约先于退出码:退出码 0 但没有产物是真实会发生的形态。
        check = check_result_contract(spec.output_dir, spec.output_schema, spec.output_check)
        if not check.ok:
            return self._finish(
                JobResult(
                    ok=False,
                    failure_kind=FailureKind.CONTRACT,
                    failure_detail=check.detail,
                ),
                log_path,
                usage,
                exit_code,
            )
        if exit_code != 0:
            return self._finish(
                JobResult(
                    ok=False,
                    failure_kind=FailureKind.PROCESS,
                    failure_detail=f"子进程以 {exit_code} 退出",
                    result_path=check.path,
                ),
                log_path,
                usage,
                exit_code,
            )
        return self._finish(JobResult(ok=True, result_path=check.path), log_path, usage, exit_code)

    @staticmethod
    def _write_structured_result(output_dir: Path, payload: dict[str, Any]) -> None:
        """把运行时终态原子落成统一小票，避免员工猜测 artifacts 的真实路径。"""
        target = output_dir / RESULT_FILENAME
        temporary = output_dir / f".{RESULT_FILENAME}.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(target)

    @staticmethod
    def _finish(
        result: JobResult, log_path: Path, usage: UsageAccumulator, exit_code: int | None
    ) -> JobResult:
        result.log_path = log_path if log_path.exists() else None
        result.tokens_used = usage.total()
        result.tokens_available = usage.seen_any
        result.exit_code = exit_code
        return result


def _recover_terminal_payload(text: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    """只恢复唯一、无歧义并通过同一契约的终态 JSON。

    扫描全文而不是只看 fenced block：一个 fenced JSON 加一个裸 JSON 同样有歧义。
    多个 JSON 值不猜；数组不冒充小票；不合 schema 不落盘。
    """
    candidates = _json_values(text)
    if len(candidates) != 1:
        return None
    payload = candidates[0]
    if not isinstance(payload, dict):
        return None
    if validate_result_payload(payload, schema):
        return None
    return payload


def _json_values(text: str) -> list[Any]:
    """从混合正文中取 JSON 值；损坏候选也计数，禁止递进捞它的嵌套对象。"""
    decoder = json.JSONDecoder()
    found: list[Any] = []
    invalid = object()
    index = 0
    while index < len(text):
        starts = [position for marker in "{[" if (position := text.find(marker, index)) >= 0]
        if not starts:
            break
        start = min(starts)
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            # 保留“这里出现过一个损坏候选”这件事。继续扫描只为发现是否还有别的值，
            # 但 found 已不可能唯一；不能从损坏外壳里捞一个恰好合 schema 的嵌套对象。
            found.append(invalid)
            index = start + 1
            continue
        found.append(value)
        index = end
    return found

__all__ = ["CONTRACT_RETRIES", "ENV_ALLOWLIST", "SubprocessRuntime"]
