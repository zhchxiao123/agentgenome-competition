"""契约失败的公共执行外环。

从子进程骨架里提炼出来的共用件:任何运行时的"按显式策略处理契约失败 + 把拒绝
原因回注给下一次尝试 + 每次尝试的上下文单独落盘"都走这里,不允许出现第二份
注定分叉的实现。

## 为什么默认不重跑完整进程

结构化输出由运行时直接落盘后,格式抖动不再值得重跑一次昂贵的完整 Agent 进程。
只有 staging 这类增量协议可以显式选择一次补交；"代码写错了"由状态机修复轮次管
(PRD 05)。两者混淆会让运行时悄悄绕过轮次上限。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from agentgenome.agents.artifacts import context_filename
from agentgenome.agents.runtime import FailureKind, JobResult, JobSpec

#: 普通 Job 的缺省重试次数。完整进程不会因为格式问题暗中重跑。
CONTRACT_RETRIES = 0

#: 一次尝试:拿到这次的上下文包与尝试序号,还一份 JobResult。
AttemptRunner = Callable[[Path, int], Awaitable[JobResult]]


def write_attempt_context(spec: JobSpec, attempt: int, rejection: str | None) -> Path:
    """为这一次尝试落盘一份上下文包。

    每次尝试都单独落盘:"当时它到底看到了什么"必须是一个能被回答的问题,
    重试那一次尤其——它的输入与首次不同。
    """
    body = spec.context_file.read_text(encoding="utf-8")
    if rejection:
        body += (
            "\n\n---\n\n"
            "## 上一次的产出被拒绝了\n\n"
            f"原因:{rejection}\n\n"
            "请先看清这条,再重新产出符合契约的结果。\n"
        )
    target = spec.output_dir / context_filename(attempt)
    target.write_text(body, encoding="utf-8")
    return target


async def run_with_contract_retry(spec: JobSpec, attempt_runner: AttemptRunner) -> JobResult:
    """执行 Job，并按两个独立额度处理产物契约失败与运行时协议补交。

    超时重试一次等于把等待时间翻倍；预算耗尽重试一次等于再烧一遍已经不够的钱。
    CONTRACT 与 PROTOCOL 分开计数，避免 staging 的增量修复额度扩张成完整协议重跑。
    """
    started = time.monotonic()
    rejection: str | None = None
    result = JobResult(ok=False)
    tokens_used = 0
    tokens_available = True
    contract_retries = 0
    protocol_retries = 0
    attempt = 0
    while True:
        attempt += 1
        context_file = write_attempt_context(spec, attempt, rejection)
        result = await attempt_runner(context_file, attempt)
        tokens_used += result.tokens_used
        tokens_available = tokens_available and result.tokens_available
        result.attempts = attempt
        result.duration_s = time.monotonic() - started
        result.tokens_used = tokens_used
        result.tokens_available = tokens_available
        if result.failure_kind is FailureKind.CONTRACT:
            if contract_retries >= spec.contract_retries:
                return result
            contract_retries += 1
        elif result.failure_kind is FailureKind.PROTOCOL:
            if protocol_retries >= spec.protocol_retries:
                return result
            protocol_retries += 1
        else:
            return result
        rejection = result.failure_detail


__all__ = ["CONTRACT_RETRIES", "AttemptRunner", "run_with_contract_retry", "write_attempt_context"]
