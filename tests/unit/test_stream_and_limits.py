"""归一化事件、token 计量,以及超时/预算两道中止闸。"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentgenome.agents.events import EventKind, UsageAccumulator
from agentgenome.agents.runtime import FailureKind, JobSpec
from agentgenome.agents.subprocess_runtime import SubprocessRuntime
from tests.fixtures import fake_agent
from tests.fixtures.fake_agent import SCRIPT_ENV

GOOD_RESULT = {
    "task_id": "ag-1",
    "producer": "dev",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
}
SCHEMA = {"type": "object", "required": ["task_id", "passed"]}


def _spec(tmp_path: Path, script: dict[str, Any], **overrides: Any) -> JobSpec:
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ctx.md").write_text("# 上下文\n")
    spec = JobSpec(
        task_id="ag-1",
        employee_id="dev",
        procedure_id="code-develop",
        procedure_version="1.0.0",
        round=1,
        workdir=tmp_path / "work",
        context_file=tmp_path / "ctx.md",
        output_dir=tmp_path / "out",
        output_schema=SCHEMA,
        timeout_s=20,
        max_tokens=1_000_000,
        credentials={
            SCRIPT_ENV: json.dumps(
                {**script, "workdir": str(tmp_path / "work"), "output_dir": str(tmp_path / "out")},
                ensure_ascii=False,
            )
        },
    )
    return replace(spec, **overrides) if overrides else spec


class _ClaudeShapedRuntime(SubprocessRuntime):
    """骨架按设计不认识任何格式,格式由具体运行时给。这里装上 claude 的解析。"""

    def parse_line(self, raw):
        from agentgenome.agents.claude_code import parse_claude_line

        return parse_claude_line(raw)


@pytest.fixture
def runtime() -> SubprocessRuntime:
    return _ClaudeShapedRuntime(argv=[sys.executable, fake_agent.__file__])


def _usage(delta: int, message_id: str) -> dict[str, Any]:
    """一行带用量的假 assistant 输出,形状照真实 stream-json。"""
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "content": [{"type": "text", "text": "干活中"}],
            "usage": {"input_tokens": delta, "output_tokens": 0},
        },
    }


# --- token 计量:黄金样本发现的坑 --------------------------------------------


def test_usage_from_the_same_message_is_counted_once() -> None:
    """同一次 API 响应会拆成多行发出,带相同 message.id 与相同 usage。

    逐行累加会翻倍多算——这是黄金样本采集时发现的,不是假想。
    """
    acc = UsageAccumulator()

    acc.observe("msg_a", {"input_tokens": 100, "output_tokens": 10})
    acc.observe("msg_a", {"input_tokens": 100, "output_tokens": 10})  # 同一次响应的第二行

    assert acc.total() == 110


def test_usage_from_different_messages_accumulates() -> None:
    acc = UsageAccumulator()

    acc.observe("msg_a", {"input_tokens": 100, "output_tokens": 10})
    acc.observe("msg_b", {"input_tokens": 50, "output_tokens": 5})

    assert acc.total() == 165


def test_usage_counts_cache_tokens_too() -> None:
    """缓存读写也是真实花掉的量,不算进去预算会失真。"""
    acc = UsageAccumulator()

    acc.observe(
        "msg_a",
        {
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
        },
    )

    assert acc.total() == 1105


def test_usage_without_a_message_id_is_still_counted() -> None:
    """终态 result 行的用量不挂在 message 上,不能因为没有 id 就丢掉。"""
    acc = UsageAccumulator()

    acc.observe(None, {"input_tokens": 7, "output_tokens": 3})

    assert acc.total() == 10


def test_accumulator_reports_when_usage_was_never_seen() -> None:
    """拿不到用量要能被区分出来——填 0 会让成本看板悄悄少算。"""
    assert UsageAccumulator().seen_any is False


# --- 真实黄金样本 -----------------------------------------------------------

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "claude-code"


def _golden_lines(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (GOLDEN / name).read_text().splitlines() if line.strip()]


def _streamed_and_authoritative() -> tuple[dict[str, int], dict[str, int]]:
    """从黄金样本里取出两份独立的用量:流里逐 message 去重后的,与终态权威的。"""
    from agentgenome.agents.claude_code import parse_claude_line

    per_message: dict[str, dict[str, Any]] = {}
    authoritative: dict[str, int] = {}
    for raw in _golden_lines("success.stream.jsonl"):
        for event in parse_claude_line(raw):
            if event.kind is not EventKind.USAGE or event.usage is None:
                continue
            if event.detail.get("authoritative"):
                authoritative = event.usage
            elif event.message_id:
                per_message[event.message_id] = event.usage
    streamed = {
        key: sum(usage.get(key, 0) for usage in per_message.values())
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    }
    return streamed, authoritative


def test_streamed_input_and_cache_tokens_match_the_authoritative_total() -> None:
    """按 message.id 去重后,输入与缓存三项与终态权威值逐字相等。

    这是"逐行累加会翻倍、去重才对"这条结论的检验——跑的是真实采集的样本,
    所以它同时也是"我们对 stream-json 的理解还成不成立"的哨兵。
    """
    streamed, authoritative = _streamed_and_authoritative()

    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        assert streamed[key] == authoritative[key], key


def test_streamed_output_tokens_are_only_a_lower_bound() -> None:
    """流里的 output_tokens 是流式过程中的部分值,不是最终值。

    实测 27 vs 真实 519。后果是运行中的预算估算**系统性低估**,而 output token
    恰恰是贵的那部分。这不是实现缺陷:结束之前这个数据根本不存在。

    能做的是诚实对待——运行中的估算是下界,Job 结束时用终态权威值替换它,
    所以任务级预算是准的,只有"中途掐断"那一下是近似的。
    """
    streamed, authoritative = _streamed_and_authoritative()

    assert streamed["output_tokens"] < authoritative["output_tokens"]


def test_golden_sample_parses_into_known_event_kinds() -> None:
    from agentgenome.agents.claude_code import parse_claude_line

    kinds = {
        event.kind
        for raw in _golden_lines("success.stream.jsonl")
        for event in parse_claude_line(raw)
    }

    assert EventKind.TEXT in kinds
    assert EventKind.TOOL_USE in kinds
    assert EventKind.TOOL_RESULT in kinds
    assert EventKind.USAGE in kinds


def test_unknown_line_types_do_not_break_parsing() -> None:
    """样本里就有 rate_limit_event 这类行。解析是增强,不该让 Job 因为一行而失败。"""
    from agentgenome.agents.claude_code import parse_claude_line

    assert parse_claude_line({"type": "totally_unknown", "x": 1}) == []


# --- 落盘与实时性 -----------------------------------------------------------


async def test_stream_is_written_to_disk_as_jsonl(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    spec = _spec(tmp_path, {"result": GOOD_RESULT, "stream": [_usage(10, "msg_a")]})

    result = await runtime.run_job(spec)

    assert result.log_path is not None and result.log_path.is_file()
    lines = [json.loads(line) for line in result.log_path.read_text().splitlines() if line]
    assert any(entry["kind"] == "usage" for entry in lines)


async def test_structured_output_is_persisted_by_the_runtime(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    spec = _spec(
        tmp_path,
        {
            "result": None,
            "raw_lines": [
                json.dumps(
                    {
                        "type": "result",
                        "is_error": False,
                        "structured_output": GOOD_RESULT,
                    }
                )
            ],
        },
    )

    result = await runtime.run_job(spec)

    assert result.ok is True
    assert result.attempts == 1
    assert json.loads((spec.output_dir / "result.json").read_text()) == GOOD_RESULT


async def test_a_single_schema_valid_json_block_in_terminal_text_is_recovered(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    """回归生产故障：CLI 退出 0，却把合法小票放进正文而非 structured_output。"""
    terminal_text = "分析完成。\n\n```json\n" + json.dumps(GOOD_RESULT) + "\n```"
    spec = _spec(
        tmp_path,
        {
            "result": None,
            "raw_lines": [
                json.dumps({"type": "result", "is_error": False, "result": terminal_text})
            ],
        },
    )

    result = await runtime.run_job(spec)

    assert result.ok is True
    assert json.loads((spec.output_dir / "result.json").read_text()) == GOOD_RESULT
    lines = [json.loads(line) for line in (result.log_path or Path()).read_text().splitlines()]
    assert any(line["kind"] == "runtime_result" for line in lines)


async def test_a_single_unfenced_json_object_in_prose_is_recovered(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    terminal_text = "分析完成，结果如下：\n" + json.dumps(GOOD_RESULT) + "\n请查收。"
    spec = _spec(
        tmp_path,
        {
            "result": None,
            "raw_lines": [
                json.dumps({"type": "result", "is_error": False, "result": terminal_text})
            ],
        },
    )

    result = await runtime.run_job(spec)

    assert result.ok is True
    assert json.loads((spec.output_dir / "result.json").read_text()) == GOOD_RESULT


async def test_ambiguous_terminal_json_is_a_protocol_failure(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    """多份 JSON 没有唯一交付语义，不能猜一份落盘。"""
    terminal_text = "裸 JSON：{}\n```json\n" + json.dumps(GOOD_RESULT) + "\n```"
    spec = _spec(
        tmp_path,
        {
            "result": None,
            "raw_lines": [
                json.dumps({"type": "result", "is_error": False, "result": terminal_text})
            ],
        },
    )

    result = await runtime.run_job(spec)

    assert result.ok is False
    assert result.failure_kind is FailureKind.PROTOCOL
    assert "结构化" in (result.failure_detail or "")
    assert not (spec.output_dir / "result.json").exists()


async def test_terminal_runtime_error_is_not_overwritten_by_protocol_validation(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    """真实故障：Claude 明确报告认证失败时，人工动作是修环境而不是改 JSON。"""
    spec = _spec(
        tmp_path,
        {
            "result": None,
            "raw_lines": [
                json.dumps(
                    {
                        "type": "result",
                        "is_error": True,
                        "terminal_reason": "api_error",
                        "result": "Not logged in · Please run /login",
                    }
                )
            ],
        },
    )

    result = await runtime.run_job(spec)

    assert result.ok is False
    assert result.failure_kind is FailureKind.PROCESS
    assert "Not logged in" in (result.failure_detail or "")
    assert "api_error" in (result.failure_detail or "")


async def test_a_stale_result_file_cannot_hide_this_attempts_protocol_failure(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    spec = _spec(
        tmp_path,
        {
            "result": None,
            "raw_lines": [
                json.dumps({"type": "result", "is_error": False, "result": "没有 JSON"})
            ],
        },
    )
    spec.output_dir.mkdir(parents=True)
    (spec.output_dir / "result.json").write_text(json.dumps(GOOD_RESULT), encoding="utf-8")

    result = await runtime.run_job(spec)

    assert result.failure_kind is FailureKind.PROTOCOL


async def test_a_valid_nested_object_is_not_recovered_from_broken_outer_json(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    broken = '{"broken": ' + json.dumps(GOOD_RESULT)
    spec = _spec(
        tmp_path,
        {
            "result": None,
            "raw_lines": [
                json.dumps({"type": "result", "is_error": False, "result": broken})
            ],
        },
    )

    result = await runtime.run_job(spec)

    assert result.failure_kind is FailureKind.PROTOCOL
    assert not (spec.output_dir / "result.json").exists()


async def test_terminal_json_that_fails_the_schema_is_not_recovered(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    spec = _spec(
        tmp_path,
        {
            "result": None,
            "raw_lines": [
                json.dumps(
                    {
                        "type": "result",
                        "is_error": False,
                        "result": "```json\n{\"task_id\": \"ag-1\"}\n```",
                    }
                )
            ],
        },
    )

    result = await runtime.run_job(spec)

    assert result.failure_kind is FailureKind.PROTOCOL
    assert not (spec.output_dir / "result.json").exists()


async def test_tokens_used_reflects_the_stream(tmp_path: Path, runtime: SubprocessRuntime) -> None:
    spec = _spec(
        tmp_path,
        {
            "result": GOOD_RESULT,
            "stream": [_usage(100, "msg_a"), _usage(100, "msg_a"), _usage(50, "msg_b")],
        },
    )

    result = await runtime.run_job(spec)

    assert result.tokens_used == 150
    assert result.tokens_available is True


async def test_tokens_marked_unavailable_when_the_stream_carries_none(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    result = await runtime.run_job(_spec(tmp_path, {"result": GOOD_RESULT, "stream": []}))

    assert result.tokens_available is False
    assert result.tokens_used == 0


async def test_a_malformed_line_does_not_abort_the_job(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    spec = _spec(
        tmp_path,
        {"result": GOOD_RESULT, "raw_lines": ["{ 这不是 JSON"], "stream": [_usage(10, "msg_a")]},
    )

    result = await runtime.run_job(spec)

    assert result.ok is True
    kinds = [
        json.loads(line)["kind"]
        for line in (result.log_path or Path()).read_text().splitlines()
        if line
    ]
    assert "error" in kinds, "坏行要留痕,而不是被静默吞掉"


# --- 两道中止闸 -------------------------------------------------------------


async def test_budget_overrun_kills_the_process_mid_flight(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    """实时中止,不是事后算账——事后发现时钱已经花完了。"""
    spec = _spec(
        tmp_path,
        {
            "result": GOOD_RESULT,
            "hang": True,
            "stream": [_usage(500, "msg_a"), _usage(500, "msg_b"), _usage(500, "msg_c")],
        },
        max_tokens=900,
    )

    result = await runtime.run_job(spec)

    assert result.ok is False
    assert result.failure_kind is FailureKind.BUDGET


async def test_disabled_budget_enforcement_keeps_counting_without_killing(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    spec = _spec(
        tmp_path,
        {
            "result": GOOD_RESULT,
            "stream": [_usage(500, "msg_a"), _usage(500, "msg_b"), _usage(500, "msg_c")],
        },
        max_tokens=900,
        enforce_token_limit=False,
    )

    result = await runtime.run_job(spec)

    assert result.ok is True
    assert result.tokens_used == 1_500


async def test_logs_survive_a_budget_kill(tmp_path: Path, runtime: SubprocessRuntime) -> None:
    spec = _spec(
        tmp_path,
        {"result": GOOD_RESULT, "hang": True, "stream": [_usage(5000, "msg_a")]},
        max_tokens=100,
    )

    result = await runtime.run_job(spec)

    assert result.log_path is not None and result.log_path.read_text().strip()


async def test_timeout_kills_a_hanging_process(tmp_path: Path, runtime: SubprocessRuntime) -> None:
    spec = _spec(tmp_path, {"result": GOOD_RESULT, "hang": True}, timeout_s=1)

    result = await runtime.run_job(spec)

    assert result.ok is False
    assert result.failure_kind is FailureKind.TIMEOUT


async def test_timeout_kills_the_agents_entire_process_group(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    """Agent 拉起的 shell/测试/搜索进程不能在父进程超时后继续占用机器。"""
    pid_file = tmp_path / "child.pid"
    spec = _spec(
        tmp_path,
        {"result": GOOD_RESULT, "hang": True, "child_pid_file": str(pid_file)},
        timeout_s=1,
    )

    await runtime.run_job(spec)

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        # 红灯阶段旧实现会留下子进程；测试自身负责清场。
        with contextlib.suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGKILL)


async def test_logs_survive_a_timeout_kill(tmp_path: Path, runtime: SubprocessRuntime) -> None:
    """挂死的 Job 最有诊断价值的就是它卡住前在做什么。"""
    spec = _spec(
        tmp_path,
        {"result": GOOD_RESULT, "hang": True, "stream": [_usage(1, "msg_a")]},
        timeout_s=1,
    )

    result = await runtime.run_job(spec)

    assert result.log_path is not None
    assert "usage" in result.log_path.read_text()


async def test_a_timed_out_job_is_not_retried(tmp_path: Path, runtime: SubprocessRuntime) -> None:
    """重试只对契约失败生效。超时重试一次等于把等待时间翻倍。"""
    spec = _spec(tmp_path, {"result": None, "hang": True}, timeout_s=1)

    result = await runtime.run_job(spec)

    assert result.attempts == 1


# --- 单行超过读缓冲上限 -------------------------------------------------------


async def test_a_line_larger_than_the_default_asyncio_limit_does_not_crash(
    tmp_path: Path, runtime: SubprocessRuntime
) -> None:
    """回归测试:`asyncio` 的默认单行读缓冲只有 64KiB。真实 Agent 的 stream-json 输出里,
    一次读大文件或大 diff 的 `tool_result` 常常单行超过这个数——此前撞上就是一次不受控的
    `ValueError`,带着整条 Python 堆栈炸穿 `agctl`,而不是一次正常的、能重试的 Job 失败。
    这里给一行 200KB 的原始输出,确认它现在能正常读完并落盘。
    """
    # 剧本整个走一条环境变量传给子进程(见 `_spec`),而环境变量本身有系统级的长度上限
    # (`E2BIG`)——90KB 刚好比 asyncio 默认的 64KiB 单行上限大一截,又远没有大到会撞上
    # 那道无关的系统限制。
    huge_text = "x" * 90_000
    huge_line = json.dumps(
        {
            "type": "assistant",
            "message": {"id": "msg_huge", "content": [{"type": "text", "text": huge_text}]},
        }
    )
    spec = _spec(tmp_path, {"result": GOOD_RESULT, "raw_lines": [huge_line]})

    result = await runtime.run_job(spec)

    assert result.ok is True
    assert result.log_path is not None
    assert len(result.log_path.read_text()) > 50_000


async def test_a_line_exceeding_even_the_raised_limit_is_a_normal_job_failure(
    tmp_path: Path, runtime: SubprocessRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """兜底:不管上限调到多大,理论上总有一行能超过它。超过时必须落到普通的
    `JobResult(ok=False, ...)` 路径,而不是让裸异常穿透 `run_job`。
    """
    from agentgenome.agents import subprocess_runtime

    monkeypatch.setattr(subprocess_runtime, "STREAM_LIMIT", 100)
    huge_line = json.dumps({"type": "text", "text": "x" * 1_000})
    spec = _spec(tmp_path, {"result": GOOD_RESULT, "raw_lines": [huge_line]})

    result = await runtime.run_job(spec)

    assert result.ok is False
    assert result.failure_kind is FailureKind.PROCESS
