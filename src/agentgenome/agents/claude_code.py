"""Claude Code 的 stream-json 解析。

这里的每一条规则都来自对真实输出的采集,不是照文档猜的——采集结果与发现见
`tests/fixtures/golden/claude-code/README.md`。

行类型:`system`(subtype `init` / `thinking_tokens`)、`assistant`、`user`、
`result`、`rate_limit_event`。不认识的类型一律跳过:解析是增强,不该让 Job 因为
一行没见过的数据而失败。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agentgenome.agents.events import EventKind, NormalizedEvent, UsageAccumulator
from agentgenome.agents.runtime import (
    JobSpec,
    SessionHandle,
    SessionSpec,
    SessionStream,
    SessionTurn,
)
from agentgenome.agents.subprocess_runtime import (
    ENV_ALLOWLIST,
    STREAM_LIMIT,
    SubprocessRuntime,
)

_BLOCK_KIND = {
    "text": EventKind.TEXT,
    "thinking": EventKind.THINKING,
    "tool_use": EventKind.TOOL_USE,
    "tool_result": EventKind.TOOL_RESULT,
}

_WRITING_TOOLS = frozenset({"Bash", "Write", "Edit", "NotebookEdit"})
_EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
_MIN_SAFE_MODE_VERSION = (2, 1, 233)
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _read_only_isolation_args(tools_allow: list[str]) -> list[str]:
    """关闭只读员工的自定义写入旁路，同时保留 Claude 认证。"""
    if _WRITING_TOOLS.intersection(tools_allow):
        return []
    return [
        "--safe-mode",
        "--strict-mcp-config",
        "--mcp-config",
        _EMPTY_MCP_CONFIG,
    ]


def _schema_for_claude(schema: dict[str, Any]) -> dict[str, Any]:
    """去掉 Claude CLI 不消费的 dialect 元声明，不改本地契约原件。

    AgentGenome 本地仍用 Draft 2020-12 校验；外部 CLI 只需要业务约束。旧版 Claude Code
    遇到这个元字段会静默降级成普通正文，所以在运行时边界复制后移除。
    """
    delivered = dict(schema)
    delivered.pop("$schema", None)
    return delivered


def parse_claude_line(raw: dict[str, Any]) -> list[NormalizedEvent]:
    """把一行原生输出解析成零到多条归一化事件。"""
    line_type = raw.get("type")
    if line_type == "assistant":
        return _parse_message(raw, from_assistant=True)
    if line_type == "user":
        return _parse_message(raw, from_assistant=False)
    if line_type == "result":
        return _parse_result(raw)
    # system / rate_limit_event / 未来新增的类型:跳过而非报错。
    return []


def _parse_message(raw: dict[str, Any], from_assistant: bool) -> list[NormalizedEvent]:
    message = raw.get("message") or {}
    message_id = message.get("id")
    events: list[NormalizedEvent] = []

    content = message.get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = _BLOCK_KIND.get(block.get("type", ""))
        if kind is None:
            continue
        events.append(
            NormalizedEvent(
                kind=kind,
                text=_block_text(block),
                message_id=message_id,
                detail=_block_detail(raw, block),
            )
        )

    # 用量只出现在 assistant 行上,且同一次响应的多行携带相同快照。
    usage = message.get("usage")
    if from_assistant and isinstance(usage, dict):
        events.append(NormalizedEvent(kind=EventKind.USAGE, message_id=message_id, usage=usage))
    return events


def _parse_result(raw: dict[str, Any]) -> list[NormalizedEvent]:
    """终态行:权威总量 + 本次运行的结论。

    它的用量是本次运行的**总量**(实测与按 message.id 去重后的和逐字相等),所以标记
    为权威值让累加器替换估算,而不是再加一笔。但它只在结束时出现,运行中的预算执行
    仍然要靠 assistant 行。
    """
    events = [
        NormalizedEvent(
            kind=EventKind.RUNTIME_RESULT,
            text=str(raw.get("result") or ""),
            # 终态只出现一次，且是协议故障最关键的一手证据。原样保留在 Job 日志里；
            # 不能只留归一化后的 usage，否则 structured_output 为何消失永远无法回答。
            detail={"raw": raw},
        )
    ]
    structured = raw.get("structured_output")
    if isinstance(structured, dict):
        events.append(
            NormalizedEvent(
                kind=EventKind.STRUCTURED_OUTPUT,
                detail={"payload": structured},
            )
        )
    usage = raw.get("usage")
    if isinstance(usage, dict):
        # 标成权威值:它是本次运行的**总量**,不是又一笔花销。当增量加会让总数翻倍。
        events.append(
            NormalizedEvent(kind=EventKind.USAGE, usage=usage, detail={"authoritative": True})
        )
    if raw.get("is_error"):
        events.append(
            NormalizedEvent(
                kind=EventKind.ERROR,
                text=str(raw.get("result") or raw.get("api_error_status") or "运行时报告失败"),
                detail={
                    "terminal": True,
                    "terminal_reason": raw.get("terminal_reason"),
                    "api_error_status": raw.get("api_error_status"),
                    "subtype": raw.get("subtype"),
                },
            )
        )
    return events


def _block_detail(raw: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    """一个 content block 的附加信息:工具名、调用标识、以及**它是谁产出的**。

    ## `parent_tool_use_id` 是子 agent 的唯一标记

    实测(claude 2.1.220)每一行 `assistant` / `user` 都带这个字段:顶层输出是 `null`,
    而**子 agent 产出的每一行带着拉起它的那次 `tool_use` 的 id**。子 agent 自己也会
    thinking、也会调工具、也会说话——所以"谁产出的"与"这是哪一类内容"是两根**正交**的轴,
    它必须是一个附加信息,不能做成第四种事件类型。做成类型的话,"子 agent 的思考"与
    "子 agent 的正文"就没有位置了。

    ## 调用标识两边都留

    `tool_use` 块带 `id`,对应的 `tool_result` 块带 `tool_use_id` 指回来。两者都归到
    `tool_use_id` 这一个键:下游要做的是"把结果接回它的调用",而它不关心自己拿到的是
    请求侧还是结果侧的那一份。子 agent 的 `parent_tool_use_id` 也正是指向这个值——
    这条链因此能一路接起来,而不需要解析器跨行记状态。

    **手上的黄金样本里 `parent_tool_use_id` 全是 `null`**(那两次调用没有用子 agent),
    所以这条链路只有字段来源是实测的,取值不是。真跑出子 agent 之后要回来补一份样本。
    """
    detail: dict[str, Any] = {}
    if block.get("name"):
        detail["name"] = block["name"]
    # `id`(调用侧)与 `tool_use_id`(结果侧)是同一个标识的两面,归一到一个键。
    identifier = block.get("id") or block.get("tool_use_id")
    if isinstance(identifier, str) and identifier:
        detail["tool_use_id"] = identifier
    parent = raw.get("parent_tool_use_id")
    if isinstance(parent, str) and parent:
        detail["parent_tool_use_id"] = parent
    return detail


def _block_text(block: dict[str, Any]) -> str:
    for key in ("text", "thinking"):
        value = block.get(key)
        if isinstance(value, str):
            return value
    content = block.get("content")
    if isinstance(content, str):
        return content
    return ""


#: 除通用白名单外,还要放行的环境变量。
#:
#: 代理设置必须透传:隔离网络下不带它,员工进程根本连不上 API。
#: `ANTHROPIC_*` 是运行时自己的凭证来源。
_EXTRA_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "ALL_PROXY",
    "all_proxy",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

#: headless 下的权限模式。
#:
#: **不给这个参数它会进 plan mode**:写一份计划等人确认,而 headless 场景没有人;
#: 然后它退出码 0、没有报错、也没有任何产物。这是真实采集到的形态,不是假想的风险
#: (见 `tests/fixtures/golden/claude-code/plan-mode-noop.stream.jsonl`)。
DEFAULT_PERMISSION_MODE = "bypassPermissions"


class ClaudeCodeRuntime(SubprocessRuntime):
    """以 headless 方式拉起 Claude Code。

    这一层只做两件事:把 JobSpec 变成一条命令,把原生输出解析成归一化事件。
    进程管理、契约校验、限额中止全在骨架里,与具体运行时无关。
    """

    name = "claude-code"

    def __init__(
        self,
        command: str = "claude",
        max_turns: int = 40,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
    ) -> None:
        super().__init__(argv=[command])
        self.command = command
        self.max_turns = max_turns
        self.permission_mode = permission_mode
        #: 已开会话的规格。**只在进程内**——重启后会话要重新 `start_session`,
        #: 而那是廉价的(不拉进程)。持久化它等于把运行时变成有状态服务。
        self._session_spec_cache: dict[str, SessionSpec] = {}

    def build_argv(self, spec: JobSpec, context_file: Path) -> list[str]:
        argv = [
            self.command,
            "-p",
            context_file.read_text(encoding="utf-8"),
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(self.max_turns),
            "--permission-mode",
            self.permission_mode,
        ]
        if spec.output_schema:
            argv += [
                "--json-schema",
                json.dumps(_schema_for_claude(spec.output_schema), ensure_ascii=False),
            ]
        # `--allowedTools` 只是免确认列表，不是可见工具白名单；在 bypassPermissions
        # 下把它当白名单会让 reviewer 仍拿到 Bash/Write。`--tools` 才决定实际工具集。
        # 空 allow 同样必须显式传空串；省略参数会恢复 Claude 的默认全工具集。
        argv += ["--tools", ",".join(spec.tools_allow)]
        # `--setting-sources ""` 会连 OAuth 登录来源一起关掉；safe mode 保留认证与
        # 内建权限，同时关闭 hooks、插件和 MCP 这些自定义写入旁路。
        argv += _read_only_isolation_args(spec.tools_allow)
        if spec.tools_deny:
            argv += ["--disallowedTools", *spec.tools_deny]
        return argv

    def preflight(self) -> None:
        """拒绝缺少可靠结构化输出或只读 safe mode 的旧版 CLI。"""
        super().preflight()
        try:
            completed = subprocess.run(
                [self.command, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"无法执行 Claude Code 版本检查: {error}") from error
        output = f"{completed.stdout}\n{completed.stderr}".strip()
        match = _VERSION.search(output)
        if completed.returncode != 0 or match is None:
            raise RuntimeError(f"无法读取 Claude Code 版本: {output or '(没有输出)'}")
        version = tuple(int(part) for part in match.groups())
        if version < _MIN_SAFE_MODE_VERSION:
            required = ".".join(str(part) for part in _MIN_SAFE_MODE_VERSION)
            found = ".".join(str(part) for part in version)
            raise RuntimeError(
                f"Claude Code {found} 不支持只读员工所需的 safe mode，"
                f"请升级到 {required} 或更高版本"
            )

    def env_allowlist(self) -> tuple[str, ...]:
        """在通用白名单之外再放行代理与本运行时自己的凭证。

        构造逻辑在骨架里,这里只声明"多放行哪些"——两处各写一遍构造逻辑的话,
        哪天骨架收紧了白名单,这里会悄悄绕过去。
        """
        return (*ENV_ALLOWLIST, *_EXTRA_ENV)

    def parse_line(self, raw: dict[str, Any]) -> list[NormalizedEvent]:
        return parse_claude_line(raw)

    # --- 会话 ----------------------------------------------------------------

    async def start_session(self, spec: SessionSpec) -> SessionHandle:
        """开一个会话。

        **不拉起任何进程。** Claude Code 的会话状态存在磁盘上(实测:
        `~/.claude/projects/<cwd 路径转义>/<uuid>.jsonl`),第一条消息带 `--session-id`
        建立它即可。常驻一个交互进程反而要处理它的崩溃、超时与孤儿回收。
        """
        self._session_spec_cache[spec.session_id] = spec
        return SessionHandle(
            session_id=spec.session_id,
            native_session_id=spec.native_session_id or str(uuid.uuid4()),
            workdir=spec.workdir,
            employee_id=spec.employee_id,
        )

    def session_argv(self, spec: SessionSpec, handle: SessionHandle, message: str) -> list[str]:
        """一条消息 = 一次 headless 调用。

        首次用 `--session-id` 指定一个我们自己生成的 UUID,之后用 `--resume` 续接。
        两者不能同时给。
        """
        argv = [
            self.command,
            "-p",
            message,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(self.max_turns),
            "--permission-mode",
            self.permission_mode,
        ]
        if handle.started:
            assert handle.native_session_id is not None, "started 时必然已在 start_session 里赋过值"
            argv += ["--resume", handle.native_session_id]
        else:
            argv += ["--session-id", handle.native_session_id or str(uuid.uuid4())]
        argv += ["--tools", ",".join(spec.tools_allow)]
        argv += _read_only_isolation_args(spec.tools_allow)
        if spec.tools_deny:
            argv += ["--disallowedTools", *spec.tools_deny]
        return argv

    def send_message(self, handle: SessionHandle, message: str) -> SessionStream:
        """发一条消息 = 一次 headless 调用,**边读边给**。

        **单条消息失败不毁掉整个会话**:进程活不过一条消息,下一条会用 `--resume` 重新
        续接。所以失败收成 `turn.ok=False` 而不是抛出去。
        """
        turn = SessionTurn(native_session_id=handle.native_session_id)

        async def stream() -> AsyncIterator[NormalizedEvent]:
            spec = self._session_spec_cache.get(handle.session_id)
            if spec is None:
                turn.ok = False
                turn.failure_detail = f"会话 {handle.session_id} 没有开过"
                return

            usage = UsageAccumulator()
            process = None
            drain = None
            try:
                try:
                    process = await asyncio.create_subprocess_exec(
                        *self.session_argv(spec, handle, message),
                        cwd=spec.workdir,
                        env=self._session_env(spec),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=STREAM_LIMIT,
                    )
                except OSError as error:
                    turn.ok = False
                    turn.failure_detail = f"无法拉起子进程: {error}"
                    return

                assert process.stdout is not None and process.stderr is not None
                # stderr 必须一起排掉,理由同 `run_job`:不读的话子进程写满管道就阻塞了。
                drain = asyncio.create_task(self._drain(process.stderr))
                try:
                    async for line in process.stdout:
                        for event in self._events_from(line):
                            if event.usage is not None:
                                usage.observe_event(event)
                            yield event
                    await asyncio.wait_for(process.wait(), spec.timeout_s)
                except TimeoutError:
                    turn.ok = False
                    turn.failure_detail = f"超过 {spec.timeout_s} 秒未结束"
            finally:
                # **账与清理都在这里。** 调用方中途退出时生成器被 `aclose()`,这一段照样跑:
                # 不然子进程会变孤儿,而这一轮烧掉的 token 也不会记账。
                if drain is not None:
                    drain.cancel()
                if process is not None and process.returncode is None:
                    process.kill()
                turn.tokens_used = usage.total()
                turn.tokens_available = usage.seen_any

        return SessionStream(stream(), turn)

    def _session_env(self, spec: SessionSpec) -> dict[str, str]:
        """会话子进程的环境。从白名单构造,再叠加本次声明的凭证。

        与 Job 同一条规矩:不继承父进程全量环境——编排器进程里有推送凭证与别的项目的密钥,
        而这个子进程正在执行一段可能被提示注入影响的指令。
        """
        env = {key: os.environ[key] for key in self.env_allowlist() if key in os.environ}
        env.update(spec.credentials)
        return env


__all__ = ["DEFAULT_PERMISSION_MODE", "ClaudeCodeRuntime", "parse_claude_line"]
