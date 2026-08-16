"""续接参数:第二轮走 `--resume`,不是重开一个会话。

## 为什么这条必须在这一层测

`ReplayRuntime` **结构上测不到它**——回放按 `handle.message_index` 定位录制,完全不看
`started`,也不构造任何命令行参数。这不是回放的缺陷,它作为主缝替身是称职的;但正因如此,
"第二轮到底用了哪个参数"这件事在会话 e2e 里是不可见的。

而它曾经是死代码:`handle.started` 全仓只有 `claude_code` 在**读**,没有任何一处写过。
于是每一轮都用 `--session-id` 带着同一个标识发出去,多轮记忆整个不成立——用户看到的是
"聊两句它就不记得了",极难归因。见 PRD 29。

参数构造是纯函数,不需要缝。写法同 `test_claude_code_runtime.py` 里那批 argv 断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome.agents.claude_code import ClaudeCodeRuntime
from agentgenome.agents.runtime import SessionHandle, SessionSpec

NATIVE = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def runtime() -> ClaudeCodeRuntime:
    return ClaudeCodeRuntime()


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        session_id="sess-1",
        employee_id="architect",
        workdir=tmp_path / "work",
        context_file=tmp_path / "ctx.md",
        tools_allow=["Read"],
        tools_deny=[],
    )


def _handle(started: bool) -> SessionHandle:
    return SessionHandle(
        session_id="sess-1", native_session_id=NATIVE, employee_id="architect", started=started
    )


def test_the_first_message_establishes_the_session(
    tmp_path: Path, runtime: ClaudeCodeRuntime
) -> None:
    argv = runtime.session_argv(_spec(tmp_path), _handle(started=False), "第一句")

    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == NATIVE
    # 两者不能同时给。
    assert "--resume" not in argv


def test_a_later_message_resumes_instead_of_starting_over(
    tmp_path: Path, runtime: ClaudeCodeRuntime
) -> None:
    """**这条是多轮记忆的全部依据。**

    走 `--session-id` 的话,运行时那边要么当作一个新会话(于是它不记得上一句),要么因为
    标识已存在而报错。两种都不是"接着上次聊"。
    """
    argv = runtime.session_argv(_spec(tmp_path), _handle(started=True), "第二句")

    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == NATIVE
    assert "--session-id" not in argv


def test_resuming_keeps_the_same_identifier_across_turns(
    tmp_path: Path, runtime: ClaudeCodeRuntime
) -> None:
    """续接的标识必须和建立时那个一致——换一个就是换了一场对话。"""
    spec = _spec(tmp_path)
    first = runtime.session_argv(spec, _handle(started=False), "第一句")
    later = runtime.session_argv(spec, _handle(started=True), "第二句")

    assert first[first.index("--session-id") + 1] == later[later.index("--resume") + 1]
