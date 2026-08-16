"""ClaudeCodeRuntime:headless 拉起、工具映射与环境白名单。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentgenome.agents.claude_code import ClaudeCodeRuntime
from agentgenome.agents.runtime import JobSpec


def _spec(tmp_path: Path, **overrides: object) -> JobSpec:
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ctx.md").write_text("# 上下文\n干活。\n")
    from dataclasses import replace

    spec = JobSpec(
        task_id="ag-1",
        employee_id="dev-employee",
        procedure_id="code-develop",
        procedure_version="1.2.0",
        round=1,
        workdir=tmp_path / "work",
        context_file=tmp_path / "ctx.md",
        output_dir=tmp_path / "out",
        tools_allow=["Bash", "Read", "Write", "Edit"],
        tools_deny=["WebFetch", "WebSearch"],
    )
    return replace(spec, **overrides) if overrides else spec  # type: ignore[arg-type]


@pytest.fixture
def runtime() -> ClaudeCodeRuntime:
    return ClaudeCodeRuntime()


# --- argv 映射(纯函数) -----------------------------------------------------


def test_argv_runs_headless_with_stream_json(tmp_path: Path, runtime: ClaudeCodeRuntime) -> None:
    argv = runtime.build_argv(_spec(tmp_path), tmp_path / "ctx.md")

    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"


def test_argv_always_sets_a_permission_mode(tmp_path: Path, runtime: ClaudeCodeRuntime) -> None:
    """不给权限模式时它会进 plan mode:退出 0、无报错、也什么都没干。

    这是真实采集到的形态(见黄金样本 plan-mode-noop),不是假想的风险。
    """
    argv = runtime.build_argv(_spec(tmp_path), tmp_path / "ctx.md")

    assert "--permission-mode" in argv


def test_argv_makes_the_allow_list_the_exact_available_tool_set(
    tmp_path: Path, runtime: ClaudeCodeRuntime
) -> None:
    argv = runtime.build_argv(_spec(tmp_path), tmp_path / "ctx.md")

    assert argv[argv.index("--tools") + 1] == "Bash,Read,Write,Edit"
    assert "--allowedTools" not in argv
    assert "--disallowedTools" in argv
    denied = argv[argv.index("--disallowedTools") + 1 :]
    assert "WebFetch" in denied[:2]


def test_a_read_only_employee_cannot_receive_writing_tools(
    tmp_path: Path, runtime: ClaudeCodeRuntime
) -> None:
    spec = _spec(
        tmp_path,
        employee_id="reviewer-employee",
        tools_allow=["Read", "Grep", "Glob"],
        tools_deny=["WebFetch", "WebSearch"],
    )

    argv = runtime.build_argv(spec, tmp_path / "ctx.md")

    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    available = argv[argv.index("--tools") + 1].split(",")
    assert not set(available) & {"Bash", "Write", "Edit", "NotebookEdit"}
    assert "--safe-mode" in argv
    assert "--setting-sources" not in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'


def test_argv_explicitly_disables_all_tools_when_allow_list_is_empty(
    tmp_path: Path, runtime: ClaudeCodeRuntime
) -> None:
    spec = _spec(tmp_path, tools_allow=[], tools_deny=[])

    argv = runtime.build_argv(spec, tmp_path / "ctx.md")

    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--disallowedTools" not in argv


def test_argv_requests_structured_output_from_the_job_schema(
    tmp_path: Path, runtime: ClaudeCodeRuntime
) -> None:
    spec = _spec(
        tmp_path,
        output_schema={"type": "object", "required": ["task_id", "passed"]},
    )

    argv = runtime.build_argv(spec, tmp_path / "ctx.md")

    assert "--json-schema" in argv
    schema = argv[argv.index("--json-schema") + 1]
    assert '"required": ["task_id", "passed"]' in schema


def test_argv_removes_the_schema_dialect_marker_only_for_claude(
    tmp_path: Path, runtime: ClaudeCodeRuntime
) -> None:
    """Claude 的结构化输出只吃 schema 内容，不该收到 Draft 2020-12 元声明。

    本地契约仍按原 schema 校验；这里只规范化传给外部 CLI 的副本。
    """
    output_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["task_id", "passed"],
    }
    spec = _spec(tmp_path, output_schema=output_schema)

    argv = runtime.build_argv(spec, tmp_path / "ctx.md")

    delivered = json.loads(argv[argv.index("--json-schema") + 1])
    assert "$schema" not in delivered
    assert delivered["required"] == ["task_id", "passed"]
    assert "$schema" in spec.output_schema, "不能篡改本地校验使用的原 schema"


def test_argv_carries_the_context_as_the_prompt(tmp_path: Path, runtime: ClaudeCodeRuntime) -> None:
    context = tmp_path / "ctx.md"

    argv = runtime.build_argv(_spec(tmp_path), context)

    assert "干活。" in argv[argv.index("-p") + 1]


def test_argv_uses_the_configured_command_and_max_turns(tmp_path: Path) -> None:
    runtime = ClaudeCodeRuntime(command="claude-next", max_turns=7)

    argv = runtime.build_argv(_spec(tmp_path), tmp_path / "ctx.md")

    assert argv[0] == "claude-next"
    assert argv[argv.index("--max-turns") + 1] == "7"


# --- 环境白名单 -------------------------------------------------------------


def test_env_does_not_leak_orchestrator_secrets(
    tmp_path: Path, runtime: ClaudeCodeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """编排器进程里有推送凭证和别的项目的密钥,而员工进程可能被提示注入影响。

    最小权限不是靠员工自觉,是靠它根本拿不到。
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("AGENTGENOME_PUSH_KEY", "push_secret")

    env = runtime.build_env(_spec(tmp_path), tmp_path / "ctx.md")

    assert "GITHUB_TOKEN" not in env
    assert "AGENTGENOME_PUSH_KEY" not in env
    assert "ghp_secret" not in "".join(env.values())


def test_env_keeps_what_the_process_needs_to_run(
    tmp_path: Path, runtime: ClaudeCodeRuntime
) -> None:
    env = runtime.build_env(_spec(tmp_path), tmp_path / "ctx.md")

    assert env.get("PATH") == os.environ.get("PATH")


def test_env_injects_declared_credentials(tmp_path: Path, runtime: ClaudeCodeRuntime) -> None:
    spec = _spec(tmp_path, credentials={"ANTHROPIC_API_KEY": "sk-declared"})

    env = runtime.build_env(spec, tmp_path / "ctx.md")

    assert env["ANTHROPIC_API_KEY"] == "sk-declared"


def test_proxy_settings_pass_through(
    tmp_path: Path, runtime: ClaudeCodeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """隔离网络下不带代理设置的话,员工进程根本连不上 API。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://relay:10808")

    env = runtime.build_env(_spec(tmp_path), tmp_path / "ctx.md")

    assert env["HTTPS_PROXY"] == "http://relay:10808"


# --- preflight --------------------------------------------------------------


def test_preflight_rejects_a_missing_cli() -> None:
    with pytest.raises(RuntimeError, match="不存在|未找到"):
        ClaudeCodeRuntime(command="definitely-not-installed-agent").preflight()


def test_preflight_passes_when_the_cli_is_available(runtime: ClaudeCodeRuntime) -> None:
    runtime.preflight()


def test_preflight_rejects_a_cli_without_safe_mode(
    runtime: ClaudeCodeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agentgenome.agents.claude_code.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="2.1.232 (Claude Code)", stderr=""
        ),
    )

    with pytest.raises(RuntimeError, match="2.1.233"):
        runtime.preflight()


def test_preflight_wraps_a_version_probe_timeout(
    runtime: ClaudeCodeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def time_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="claude --version", timeout=5)

    monkeypatch.setattr("agentgenome.agents.claude_code.subprocess.run", time_out)

    with pytest.raises(RuntimeError, match="版本检查") as excinfo:
        runtime.preflight()

    assert isinstance(excinfo.value.__cause__, subprocess.TimeoutExpired)


# --- 归一化 -----------------------------------------------------------------


def test_runtime_parses_native_lines_into_normalized_events(runtime: ClaudeCodeRuntime) -> None:
    events = runtime.parse_line(
        {
            "type": "assistant",
            "message": {
                "id": "msg_a",
                "content": [{"type": "tool_use", "name": "Bash"}],
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        }
    )

    assert [event.kind.value for event in events] == ["tool_use", "usage"]


def test_thinking_is_its_own_kind_not_prose(runtime: ClaudeCodeRuntime) -> None:
    """原生输出里 `thinking` 与 `text` 本来就是两种 content block。

    早先这里把它们都映射成 `text`,那是**我们自己丢的信息**——丢掉之后下游再也分不出
    "它在盘算什么"和"它的答复是什么",只能把一整段内心独白当正文渲染给用户看。
    """
    events = runtime.parse_line(
        {
            "type": "assistant",
            "message": {
                "id": "msg_a",
                "content": [
                    {"type": "thinking", "thinking": "先看看有没有现成的手艺"},
                    {"type": "text", "text": "这个项目分三层"},
                ],
            },
        }
    )

    assert [event.kind.value for event in events] == ["thinking", "text"]


def test_a_sub_agent_line_carries_the_tool_call_that_spawned_it(
    runtime: ClaudeCodeRuntime,
) -> None:
    """实测每一行 assistant/user 都带 `parent_tool_use_id`:顶层是 null,子 agent 产出的
    每一行带着拉起它的那次 tool_use 的 id。**这是子 agent 的唯一标记。**
    """
    [event] = runtime.parse_line(
        {
            "type": "assistant",
            "parent_tool_use_id": "toolu_parent",
            "message": {"id": "msg_b", "content": [{"type": "text", "text": "查完了"}]},
        }
    )

    assert event.detail["parent_tool_use_id"] == "toolu_parent"


def test_a_top_level_line_claims_no_parent(runtime: ClaudeCodeRuntime) -> None:
    """顶层行的这个字段是 `null`。**原样带 `None` 过去的话**,下游要多判一次
    "有这个键但值是空"算不算子 agent——而那正是这类判断最容易判反的形状。
    """
    [event] = runtime.parse_line(
        {
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {"id": "msg_c", "content": [{"type": "text", "text": "答复"}]},
        }
    )

    assert "parent_tool_use_id" not in event.detail


def test_a_tool_call_and_its_result_share_one_identity_key(runtime: ClaudeCodeRuntime) -> None:
    """`tool_use` 带 `id`,`tool_result` 带 `tool_use_id` 指回来——同一个标识的两面。

    归到一个键上,下游要做的是"把结果接回它的调用",它不关心自己拿到的是哪一侧那份。
    """
    [call] = runtime.parse_line(
        {
            "type": "assistant",
            "message": {
                "id": "msg_d",
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "Task"}],
            },
        }
    )
    [result] = runtime.parse_line(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "查完了"}
                ]
            },
        }
    )

    assert call.detail["tool_use_id"] == result.detail["tool_use_id"] == "toolu_1"


def test_runtime_name_identifies_it(runtime: ClaudeCodeRuntime) -> None:
    assert runtime.name == "claude-code"
