"""运行时能力矩阵。

**这次重构才是 PRD 16 的主要价值**——实现第二个运行时只是发现泄漏的手段。所以这一组测的
不是"Qwen 能不能跑起来",是"抽象层里还有没有 Claude Code 的假设"。
"""

from __future__ import annotations

import pytest

from agentgenome.agents.capabilities import (
    AGENTTEAMS,
    CLAUDE_CODE,
    QWEN_CODE,
    Capability,
    ResultDelivery,
    RuntimeCapabilities,
    UnsupportedCapability,
    capabilities_of,
    context_window_of,
)
from agentgenome.agents.claude_code import ClaudeCodeRuntime
from agentgenome.agents.events import EventKind
from agentgenome.agents.qwen_code import QwenCodeRuntime, parse_qwen_line
from agentgenome.agents.runtime import JobSpec


def test_the_same_capability_has_different_local_names() -> None:
    """`Bash` / `Read` / `Grep` 是 **Claude Code 的命名**。它们泄漏到员工定义里之后,
    换一个运行时就要改每一份员工资产。"""
    assert CLAUDE_CODE.tool_for(Capability.RUN_COMMAND) == "Bash"
    assert QWEN_CODE.tool_for(Capability.RUN_COMMAND) == "run_shell_command"


def test_a_capability_that_does_not_map_raises(tmp_path) -> None:
    """**静默降级会让员工失去某项能力却没人知道。**

    症状是"它这次怎么没去读那个文件",而原因在三层之外的一张映射表里。
    """
    with pytest.raises(UnsupportedCapability) as error:
        QWEN_CODE.tool_for(Capability.WEB_SEARCH)

    assert "qwen-code" in str(error.value)


def test_translating_a_whole_set_fails_if_any_is_missing() -> None:
    with pytest.raises(UnsupportedCapability):
        QWEN_CODE.translate([Capability.READ_FILE, Capability.WEB_SEARCH])


def test_a_tool_name_can_be_traced_back_to_its_capability() -> None:
    """把老的员工定义翻译过来时要用。"""
    assert CLAUDE_CODE.capability_of("Grep") is Capability.SEARCH
    assert CLAUDE_CODE.capability_of("没这个工具") is None


def test_an_unknown_runtime_yields_no_capabilities() -> None:
    """**不造默认值。** 造默认值等于替一个我们一无所知的运行时打包票。"""
    assert capabilities_of("某个没见过的运行时") is None


def test_the_context_window_falls_back_when_the_runtime_is_unknown() -> None:
    assert context_window_of("某个没见过的运行时", 123) == 123
    assert context_window_of("qwen-code", 999_999) == QWEN_CODE.context_window


def test_a_runtime_that_cannot_report_usage_says_so() -> None:
    """填 0 会让预算闸以为还有额度——那是最贵的一种"默认值写错"。"""
    assert QWEN_CODE.usage_available is False
    assert CLAUDE_CODE.usage_available is True


def test_result_delivery_and_read_only_guarantees_are_explicit_capabilities() -> None:
    """调度层不应靠运行时名字猜测交付协议或 reviewer 是否真的只读。"""
    assert CLAUDE_CODE.result_delivery is ResultDelivery.STRUCTURED_RESPONSE
    assert QWEN_CODE.result_delivery is ResultDelivery.STRUCTURED_RESPONSE
    assert CLAUDE_CODE.enforces_read_only is True
    assert QWEN_CODE.enforces_read_only is True
    assert AGENTTEAMS.enforces_read_only is False


# --- 第二个运行时 -----------------------------------------------------------


def _spec(tmp_path) -> JobSpec:
    context = tmp_path / "context.md"
    context.write_text("干活", encoding="utf-8")
    return JobSpec(
        task_id="ag-1",
        employee_id="dev-employee",
        procedure_id="code-develop",
        procedure_version="1.0.0",
        round=1,
        workdir=tmp_path,
        context_file=context,
        output_dir=tmp_path,
        tools_allow=["read_file"],
        tools_deny=["run_shell_command"],
    )


def test_the_two_runtimes_build_different_command_lines(tmp_path) -> None:
    """参数形态是泄漏点之一:`-p` / `--tools` 都是 Claude Code 的形状。"""
    spec = _spec(tmp_path)

    claude = ClaudeCodeRuntime().build_argv(spec, spec.context_file)
    qwen = QwenCodeRuntime().build_argv(spec, spec.context_file)

    assert "-p" in claude and "--tools" in claude
    assert "--prompt" in qwen and "--allowed-tools" in qwen


def test_qwen_turns_the_allow_list_into_an_exact_tool_surface(tmp_path) -> None:
    """只读边界同时关闭扩展/MCP、使用 plan mode，并排除内建写入工具。"""
    spec = _spec(tmp_path)
    spec.tools_allow = ["read_file", "grep_search", "glob"]
    spec.tools_deny = []
    spec.output_schema = {"type": "object"}

    argv = QwenCodeRuntime().build_argv(spec, spec.context_file)

    excluded = set(argv[argv.index("--excluded-tools") + 1].split(","))
    assert {"write_file", "edit", "run_shell_command"} <= excluded
    assert argv[argv.index("--core-tools") + 1] == "read_file,grep_search,glob"
    assert argv[argv.index("--approval-mode") + 1] == "plan"
    assert "--safe-mode" in argv
    assert argv[argv.index("--extensions") + 1] == "none"
    assert "--json-schema" in argv
    assert "--max-session-turns" in argv
    assert "--max-turns" not in argv


def test_qwen_explicitly_disables_all_core_tools_for_an_empty_allowlist(tmp_path) -> None:
    spec = _spec(tmp_path)
    spec.tools_allow = []

    argv = QwenCodeRuntime().build_argv(spec, spec.context_file)

    assert argv[argv.index("--core-tools") + 1] == ""


def test_the_second_runtime_only_implements_two_methods() -> None:
    """**它与 ClaudeCodeRuntime 唯一共享的应该是骨架。**

    这里如果需要覆写别的方法,说明抽象层还没干净——而那正是本 PRD 要发现的事。

    **只保留 `__init__`,滤掉其余全部 dunder。** 解释器自己往类 `__dict__` 里塞的东西
    (`__module__`/`__qualname__`/`__doc__`,3.13 起还有 `__firstlineno__`/
    `__static_attributes__`)会随 Python 版本变化——按名字挨个排除的话,换一个解释器
    版本这条测试就会无缘无故地红,而那不是这条测试想验的东西。
    """
    own = {
        name
        for name in vars(QwenCodeRuntime)
        if name == "__init__" or not (name.startswith("__") and name.endswith("__"))
    }
    own.discard("name")

    assert own <= {"__init__", "build_argv", "parse_line", "env_allowlist"}


@pytest.mark.parametrize(
    "raw,kind",
    [
        ({"type": "text", "text": "在改代码"}, EventKind.TEXT),
        ({"type": "tool_call", "name": "read_file"}, EventKind.TOOL_USE),
        ({"type": "tool_result", "content": "ok"}, EventKind.TOOL_RESULT),
        ({"type": "error", "text": "炸了"}, EventKind.ERROR),
    ],
)
def test_the_native_output_normalises_to_the_shared_event_shape(raw: dict, kind) -> None:
    """日志查看器与 token 统计不该区分运行时。"""
    events = parse_qwen_line(raw)

    assert [event.kind for event in events] == [kind]


def test_qwen_native_structured_result_becomes_the_shared_output_event() -> None:
    events = parse_qwen_line(
        {
            "type": "result",
            "subtype": "success",
            "structured_result": {"task_id": "ag-1", "passed": True},
        }
    )

    assert events[-1].kind is EventKind.STRUCTURED_OUTPUT
    assert events[-1].detail["payload"] == {"task_id": "ag-1", "passed": True}


def test_qwen_official_assistant_envelope_is_normalised() -> None:
    events = parse_qwen_line(
        {
            "type": "assistant",
            "uuid": "msg-1",
            "message": {"content": [{"type": "text", "text": "正在检查"}]},
        }
    )

    assert [(event.kind, event.text) for event in events] == [(EventKind.TEXT, "正在检查")]


def test_an_unknown_line_type_is_skipped_not_fatal() -> None:
    """上游加一个新的事件类型不该让整个 Job 挂掉。"""
    assert parse_qwen_line({"type": "某个未来才有的类型"}) == []


def test_the_credentials_of_the_two_runtimes_do_not_leak_into_each_other() -> None:
    """合成一份白名单的话,会把 A 的密钥透传给 B 的进程。"""
    claude = set(ClaudeCodeRuntime().env_allowlist())
    qwen = set(QwenCodeRuntime().env_allowlist())

    assert "ANTHROPIC_API_KEY" in claude
    assert "ANTHROPIC_API_KEY" not in qwen
    assert "DASHSCOPE_API_KEY" in qwen


# --- 上下文预算 -------------------------------------------------------------


def test_the_context_budget_respects_the_runtime_window() -> None:
    """此前预算只看两份配置。换一个窗口小一半的运行时,表现是**静默截断**——员工看不到
    该看的东西,而且没有任何报错。"""
    from agentgenome.config import Config
    from agentgenome.context import context_budget
    from agentgenome.employees import EmployeeConfig

    employee = EmployeeConfig.model_validate(
        {"id": "dev-employee", "runtime": "qwen-code", "prompt": "p.md"}
    )
    config = Config()

    budget = context_budget(employee, config, runtime_name="qwen-code")

    assert budget <= QWEN_CODE.context_window


def test_a_matrix_entry_can_be_added_for_a_third_runtime() -> None:
    extra = RuntimeCapabilities(name="第三个", tools={Capability.READ_FILE: "read"})

    assert extra.tool_for(Capability.READ_FILE) == "read"


# --- human -------------------------------------------------------------------


def test_human_is_registered_like_any_other_runtime() -> None:
    """不认识的运行时会走一条与我们想要的不同的默认路径(比如给人物化技艺包)。"""
    profile = capabilities_of("human")

    assert profile is not None
    assert profile.name == "human"


def test_human_usage_is_unavailable_not_zero() -> None:
    """填 0 会让成本看板把人的工时算成"免费"。

    "这件事没有 token 账"与"这件事花了 0 个 token"是两句不同的话。
    """
    profile = capabilities_of("human")

    assert profile is not None and profile.usage_available is False


def test_human_does_not_get_craft_packages_materialised() -> None:
    """给人物化一堆方法论目录是噪音:他要读的是上下文包与产物契约。"""
    profile = capabilities_of("human")

    assert profile is not None and profile.craft_mounting is False


def test_human_can_do_every_capability() -> None:
    """矩阵在这里回答的不是"他手里有哪个命令",而是"这份活能不能派给他"。"""
    profile = capabilities_of("human")

    assert profile is not None
    assert set(profile.tools) == set(Capability)
