"""回归测试:`cli._build_runtimes` 按运行时名字选实现,不猜 `cmd` 字符串。

此前的实现按 `entry.cmd.startswith("claude")` 猜测,猜不中的条目被跳过;随后
`runtimes.setdefault(config.runtime.default, ClaudeCodeRuntime(...))` 会在那个被跳过的
名字底下插入一个 `ClaudeCodeRuntime`——一份合法的 `default: qwen-code` 配置会静默拿到一个
冒充 qwen-code 的 Claude 运行时,且没有任何报错。
"""

from __future__ import annotations

import pytest
import typer

from agentgenome.agents.claude_code import ClaudeCodeRuntime
from agentgenome.agents.qwen_code import QwenCodeRuntime
from agentgenome.cli import _build_runtimes
from agentgenome.config import Config, RuntimeConfig, RuntimeEntry


def test_a_qwen_code_default_yields_a_real_qwen_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    config = Config(
        runtime=RuntimeConfig(
            default="qwen-code",
            default_is_explicit=True,
            runtimes={"qwen-code": RuntimeEntry(cmd="qwen", max_turns=40)},
        )
    )

    runtimes = _build_runtimes(config)

    assert isinstance(runtimes["qwen-code"], QwenCodeRuntime)
    assert not isinstance(runtimes["qwen-code"], ClaudeCodeRuntime)


def test_mixed_runtimes_both_get_their_own_real_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """混配:一个员工用 claude-code,另一个用 qwen-code——两个都要注册成真的那个类。"""
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    config = Config(
        runtime=RuntimeConfig(
            default="claude-code",
            runtimes={
                "claude-code": RuntimeEntry(cmd="claude", max_turns=40),
                "qwen-code": RuntimeEntry(cmd="qwen", max_turns=40),
            },
        )
    )

    runtimes = _build_runtimes(config)

    assert isinstance(runtimes["claude-code"], ClaudeCodeRuntime)
    assert isinstance(runtimes["qwen-code"], QwenCodeRuntime)


def test_an_unknown_runtime_name_fails_loudly_instead_of_silently_substituting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    config = Config(
        runtime=RuntimeConfig(
            default="mystery-code",
            default_is_explicit=True,
            runtimes={"mystery-code": RuntimeEntry(cmd="mystery", max_turns=40)},
        )
    )

    with pytest.raises(typer.Exit):
        _build_runtimes(config)


def test_the_implicit_default_still_falls_back_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没人显式声明 default、也没写任何 runtimes 条目时,单机上手不该被迫先配一份完整配置。"""
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    config = Config()

    runtimes = _build_runtimes(config)

    assert isinstance(runtimes["claude-code"], ClaudeCodeRuntime)
