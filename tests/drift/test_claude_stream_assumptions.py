"""真实 Agent 漂移检测。

**默认跳过。** 它跑一次真实 `claude` 调用(实测约 0.09 美元),只在升级 CLI、发布前、
或怀疑解析出问题时手工触发:

    pytest -m drift tests/drift

## 它在防什么

黄金样本失真是**无声的**:模型或 CLI 升级之后真实输出的形状变了,而我们的解析仍然
对着旧样本测试、全绿。生产环境里 token 统计开始出错、预算执行开始失灵,但没有任何
东西会报警——直到有人发现账单不对。

这里断言的三条假设,每一条都曾经推翻过原定实现(见
`tests/fixtures/golden/claude-code/README.md`)。任一条变红时,按
`docs/golden-sample-refresh.md` 重采样本并修正解析。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agentgenome.agents.claude_code import DEFAULT_PERMISSION_MODE, parse_claude_line
from agentgenome.agents.events import EventKind, UsageAccumulator

pytestmark = pytest.mark.drift

REFRESH_DOC = "docs/golden-sample-refresh.md"

TASK = """\
你在一个隔离工作区里。请完成这个极小的任务：

1. 用 Bash 创建文件 hello.txt，内容为一行：hello from agentgenome
2. 读回 hello.txt 确认内容
3. 把结构化结果写入 out/result.json，格式为：
   {"task_id": "drift", "producer": "drift", "created_at": "<ISO8601>", "passed": true}

只做这三件事，不要做别的。
"""


def _require_claude() -> str:
    command = shutil.which("claude")
    if command is None:
        pytest.skip("环境里没有 claude,跳过漂移检测")
    return command


def _run_claude(workdir: Path, *extra: str) -> tuple[int, list[dict[str, Any]]]:
    command = _require_claude()
    (workdir / "out").mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            command,
            "-p",
            TASK,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            "12",
            "--allowedTools",
            "Bash",
            "Read",
            "Write",
            *extra,
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=600,
    )
    lines = [
        json.loads(line) for line in completed.stdout.splitlines() if line.strip().startswith("{")
    ]
    return completed.returncode, lines


@pytest.fixture(scope="module")
def live_run() -> tuple[int, list[dict[str, Any]]]:
    """一次真实调用,模块内共享——它是要花钱的。"""
    with tempfile.TemporaryDirectory(prefix="agentgenome-drift-") as tmp:
        return _run_claude(Path(tmp), "--permission-mode", DEFAULT_PERMISSION_MODE)


def _usage_by_message(lines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    per_message: dict[str, dict[str, Any]] = {}
    for raw in lines:
        for event in parse_claude_line(raw):
            if event.kind is EventKind.USAGE and event.message_id and event.usage:
                per_message[event.message_id] = event.usage
    return per_message


def _authoritative(lines: list[dict[str, Any]]) -> dict[str, Any]:
    for raw in reversed(lines):
        if raw.get("type") == "result" and isinstance(raw.get("usage"), dict):
            return dict(raw["usage"])
    pytest.fail(f"终态 result 行不见了。上游格式变了——按 {REFRESH_DOC} 重采样本")


# --- 假设一:同一次响应拆成多行,带相同 id 与相同快照 -------------------------


def test_the_same_response_still_repeats_its_usage_snapshot(live_run) -> None:
    """若这条不再成立,逐行累加就不会翻倍了——但在确认之前不要去掉去重。"""
    _, lines = live_run

    repeated = [
        raw["message"]["id"]
        for raw in lines
        if raw.get("type") == "assistant" and isinstance(raw.get("message"), dict)
    ]

    assert len(repeated) > len(set(repeated)), (
        f"同一 message.id 不再重复出现——上游可能改了分帧方式。按 {REFRESH_DOC} 重采并复核去重逻辑"
    )


# --- 假设二:去重后的输入与缓存量等于终态权威值 -------------------------------


def test_deduped_input_and_cache_still_match_the_authoritative_total(live_run) -> None:
    """这是"运行中的预算估算准不准"的唯一校验。"""
    _, lines = live_run
    per_message = _usage_by_message(lines)
    authoritative = _authoritative(lines)

    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        streamed = sum(usage.get(key, 0) for usage in per_message.values())
        assert streamed == authoritative.get(key, 0), (
            f"{key} 去重后与终态权威值对不上({streamed} vs {authoritative.get(key, 0)})。"
            f"按 {REFRESH_DOC} 重采样本并复核 UsageAccumulator"
        )


def test_the_accumulator_lands_on_the_authoritative_total(live_run) -> None:
    """端到端:把整条流喂给累加器,总数应当等于终态权威值。"""
    _, lines = live_run
    accumulator = UsageAccumulator()
    for raw in lines:
        for event in parse_claude_line(raw):
            accumulator.observe_event(event)

    authoritative = _authoritative(lines)
    expected = sum(
        authoritative.get(key, 0)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    assert accumulator.total() == expected


# --- 哨兵:output_tokens 仍是下界(变红是好消息) -----------------------------


def test_streamed_output_tokens_are_still_only_a_lower_bound(live_run) -> None:
    """**这条变红是好消息。**

    它说明上游把流里的 `output_tokens` 变准了,运行中的预算估算不再系统性低估。
    那时应该去掉 `UsageAccumulator.total()` 文档里的降级说明,并把
    `test_streamed_output_tokens_are_only_a_lower_bound` 改成相等断言。
    """
    _, lines = live_run
    streamed = sum(usage.get("output_tokens", 0) for usage in _usage_by_message(lines).values())
    authoritative = _authoritative(lines).get("output_tokens", 0)

    assert streamed < authoritative, (
        "流里的 output_tokens 变准了——这是好消息,去掉相应的降级处理即可"
    )


# --- 假设三:不给权限模式仍会静默空转 ----------------------------------------


def test_headless_without_a_permission_mode_still_silently_does_nothing() -> None:
    """退出码 0、无报错、无任何产物。若不再成立,可以考虑放宽权限模式的硬性要求。"""
    _require_claude()
    with tempfile.TemporaryDirectory(prefix="agentgenome-drift-noop-") as tmp:
        workdir = Path(tmp)
        exit_code, _ = _run_claude(workdir)

        assert exit_code == 0
        assert not (workdir / "out" / "result.json").exists(), (
            "不给 --permission-mode 也能干活了——headless 拉起可以放宽这条硬性要求,"
            f"并更新 {REFRESH_DOC}"
        )


def test_the_golden_samples_are_still_in_the_repo() -> None:
    """样本被删掉的话,常规测试会退化成对着空气断言。"""
    golden = Path(os.environ.get("AGENTGENOME_GOLDEN", "tests/fixtures/golden/claude-code"))
    assert (golden / "success.stream.jsonl").is_file()
    assert (golden / "plan-mode-noop.stream.jsonl").is_file()
