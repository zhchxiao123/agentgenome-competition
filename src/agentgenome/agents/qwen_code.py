"""以 headless 方式拉起 Qwen Code。

## 第二个运行时是用来验抽象层的

它与 `ClaudeCodeRuntime` 唯一共享的是 `SubprocessRuntime` 骨架:进程管理、超时、token 预算
中止、越权检查全在骨架里。**这个文件里如果出现任何需要动骨架的东西,那说明抽象层还没干净。**

实现下来发现的差异,逐条列在这里——它们正是 PRD 16 说的"泄漏点":

- **参数形态不同。** 提示词走 `--prompt` 而不是 `-p`;没有 `--permission-mode`,权限靠
  `--yolo` 的反面(默认就是要确认,headless 下用 `--approval-mode`)。
- **工具名不同。** `run_shell_command` / `read_file` / `grep_search`,不是 `Bash` /
  `Read` / `Grep`。翻译靠 `agents.capabilities`,不在这里硬编码。
- **拿不到逐次调用的 token 用量。** 归一化事件里标 `unavailable` 而不是填 0——填 0 会让
  预算闸以为还有额度,那是最贵的一种"默认值写错"。
- **上下文窗口只有一半。** 由能力矩阵告诉 `ContextAssembler`,不是根配置里写死的数。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentgenome.agents.capabilities import QWEN_CODE, Capability
from agentgenome.agents.events import EventKind, NormalizedEvent
from agentgenome.agents.runtime import JobSpec
from agentgenome.agents.subprocess_runtime import ENV_ALLOWLIST, SubprocessRuntime

#: 这个运行时自己的凭证与代理。与 Claude Code 那份分开——两家的变量名不一样,合成一份的话
#: 会把 A 的密钥透传给 B 的进程。
_EXTRA_ENV = (
    "DASHSCOPE_API_KEY",
    "QWEN_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

#: headless 下不逐个确认工具调用。等价于 Claude Code 的 `--permission-mode`。
DEFAULT_APPROVAL_MODE = "yolo"

_WRITING_TOOLS = frozenset(
    QWEN_CODE.tool_for(capability)
    for capability in (Capability.WRITE_FILE, Capability.EDIT_FILE, Capability.RUN_COMMAND)
)

_BLOCK_KIND = {
    "text": EventKind.TEXT,
    "thought": EventKind.TEXT,
    "tool_call": EventKind.TOOL_USE,
    "tool_result": EventKind.TOOL_RESULT,
    "error": EventKind.ERROR,
}


def parse_qwen_line(raw: dict[str, Any]) -> list[NormalizedEvent]:
    """把一行原生输出解析成零到多条归一化事件。

    未知类型跳过而非报错:上游加一个新的事件类型不该让整个 Job 挂掉。
    """
    structured = raw.get("structured_result")
    if isinstance(structured, dict):
        return [
            NormalizedEvent(
                kind=EventKind.STRUCTURED_OUTPUT,
                message_id=raw.get("uuid"),
                detail={"payload": structured},
            )
        ]

    # 当前 stream-json 的 assistant 事件把内容块放在 message.content；保留下面的扁平
    # 解析分支兼容旧录制，运行时升级不该让历史日志突然不可读。
    message = raw.get("message")
    if raw.get("type") == "assistant" and isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            events: list[NormalizedEvent] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type", ""))
                if block_type == "text":
                    events.append(
                        NormalizedEvent(
                            kind=EventKind.TEXT,
                            text=str(block.get("text", "")),
                            message_id=raw.get("uuid"),
                        )
                    )
                elif block_type in {"tool_call", "tool_use"}:
                    events.append(
                        NormalizedEvent(
                            kind=EventKind.TOOL_USE,
                            text=str(block.get("name", "")),
                            message_id=raw.get("uuid"),
                            detail={"name": block.get("name", "")},
                        )
                    )
            return events

    kind = _BLOCK_KIND.get(str(raw.get("type", "")))
    if kind is None:
        return []
    if kind is EventKind.TOOL_USE:
        return [
            NormalizedEvent(
                kind=kind,
                text=str(raw.get("name", "")),
                message_id=raw.get("id"),
                detail={"name": raw.get("name", "")},
            )
        ]
    return [
        NormalizedEvent(
            kind=kind,
            text=str(raw.get("text") or raw.get("content") or ""),
            message_id=raw.get("id"),
        )
    ]


class QwenCodeRuntime(SubprocessRuntime):
    """只实现两件事:把 JobSpec 变成一条命令,把原生输出解析成归一化事件。"""

    name = "qwen-code"

    def __init__(self, command: str = "qwen", max_turns: int = 40) -> None:
        super().__init__(argv=[command])
        self.command = command
        self.max_turns = max_turns

    def build_argv(self, spec: JobSpec, context_file: Path) -> list[str]:
        read_only = not _WRITING_TOOLS.intersection(spec.tools_allow)
        argv = [
            self.command,
            "--prompt",
            context_file.read_text(encoding="utf-8"),
            "--output-format",
            "stream-json",
            "--approval-mode",
            "plan" if read_only else DEFAULT_APPROVAL_MODE,
            # 不加载用户/项目设置、hooks、扩展能力、MCP 或自定义 subagent。Qwen 官方把
            # safe-mode 定义为 headless 隔离入口；显式 `extensions none` 再锁一层扩展面。
            "--safe-mode",
            "--extensions",
            "none",
            "--max-session-turns",
            str(self.max_turns),
        ]
        if spec.tools_allow:
            argv += ["--allowed-tools", ",".join(spec.tools_allow)]
        # 与 allowed-tools 不同，core-tools 是真正的内建工具发现白名单；空串也必须传，
        # 否则“无工具”会退化成“加载全部”。结构化终态工具由 --json-schema 单独注册。
        argv += ["--core-tools", ",".join(spec.tools_allow)]
        # `allowed-tools` 控制自动批准，不等于从工具面移除其余工具。未允许的工具也显式排除，
        # reviewer 才是真的没有写入工具，而不是只收到一句“请勿写入”的建议。
        excluded = set(spec.tools_deny)
        excluded.update(set(QWEN_CODE.tools.values()) - set(spec.tools_allow))
        if excluded:
            argv += ["--excluded-tools", ",".join(sorted(excluded))]
        if spec.output_schema:
            argv += [
                "--json-schema",
                json.dumps(spec.output_schema, ensure_ascii=False, separators=(",", ":")),
            ]
        return argv

    def env_allowlist(self) -> tuple[str, ...]:
        return (*ENV_ALLOWLIST, *_EXTRA_ENV)

    def parse_line(self, raw: dict[str, Any]) -> list[NormalizedEvent]:
        return parse_qwen_line(raw)


__all__ = ["DEFAULT_APPROVAL_MODE", "QwenCodeRuntime", "parse_qwen_line"]
