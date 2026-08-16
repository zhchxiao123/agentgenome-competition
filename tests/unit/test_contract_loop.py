"""契约执行外环:任何运行时共用的显式补交策略。

它是从子进程骨架里提炼出来的共用件——这里测它自己的公共接口,子进程与其它
运行时消费它之后的端到端行为由各自的测试覆盖。
"""

from __future__ import annotations

from pathlib import Path

from agentgenome.agents.contract_loop import run_with_contract_retry
from agentgenome.agents.runtime import FailureKind, JobResult, JobSpec


def _spec(tmp_path: Path) -> JobSpec:
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    context = tmp_path / "context.md"
    context.write_text("# 上下文包\n请完成任务。\n", encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    return JobSpec(
        task_id="ag-1",
        employee_id="dev-employee",
        procedure_id="code-develop",
        procedure_version="1.0.0",
        round=1,
        workdir=workdir,
        context_file=context,
        output_dir=output_dir,
    )


async def test_success_returns_after_a_single_attempt(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    calls: list[int] = []

    async def attempt(context_file: Path, attempt: int) -> JobResult:
        calls.append(attempt)
        return JobResult(ok=True)

    result = await run_with_contract_retry(spec, attempt)

    assert result.ok is True
    assert result.attempts == 1
    assert calls == [1]


async def test_non_contract_failure_is_not_retried(tmp_path: Path) -> None:
    """超时重试等于把等待翻倍,预算耗尽重试等于再烧一遍不够的钱——只有契约失败才重试。"""
    spec = _spec(tmp_path)
    calls: list[int] = []

    async def attempt(context_file: Path, attempt: int) -> JobResult:
        calls.append(attempt)
        return JobResult(ok=False, failure_kind=FailureKind.TIMEOUT, failure_detail="太慢了")

    result = await run_with_contract_retry(spec, attempt)

    assert result.failure_kind is FailureKind.TIMEOUT
    assert result.attempts == 1
    assert calls == [1]


async def test_contract_failure_is_retried_once_and_can_succeed(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    spec.contract_retries = 1

    async def attempt(context_file: Path, attempt: int) -> JobResult:
        if attempt == 1:
            return JobResult(
                ok=False, failure_kind=FailureKind.CONTRACT, failure_detail="缺 passed 字段"
            )
        return JobResult(ok=True)

    result = await run_with_contract_retry(spec, attempt)

    assert result.ok is True
    assert result.attempts == 2


async def test_contract_failure_twice_stays_a_contract_failure(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    spec.contract_retries = 1

    async def attempt(context_file: Path, attempt: int) -> JobResult:
        return JobResult(
            ok=False, failure_kind=FailureKind.CONTRACT, failure_detail="缺 passed 字段"
        )

    result = await run_with_contract_retry(spec, attempt)

    assert result.ok is False
    assert result.failure_kind is FailureKind.CONTRACT
    assert result.attempts == 2


async def test_retry_context_tells_the_agent_why_the_last_attempt_was_rejected(
    tmp_path: Path,
) -> None:
    """不告诉它哪儿错了就重试,第二次大概率错得一模一样。"""
    spec = _spec(tmp_path)
    spec.contract_retries = 1
    seen: dict[int, str] = {}

    async def attempt(context_file: Path, attempt: int) -> JobResult:
        seen[attempt] = context_file.read_text(encoding="utf-8")
        return JobResult(
            ok=False, failure_kind=FailureKind.CONTRACT, failure_detail="缺 passed 字段"
        )

    await run_with_contract_retry(spec, attempt)

    assert "上一次" not in seen[1]
    assert "上一次" in seen[2]
    assert "passed" in seen[2]


async def test_every_attempt_context_lands_in_the_output_dir(tmp_path: Path) -> None:
    """"当时它到底看到了什么"必须是能被回答的问题,重试那次尤其。"""
    spec = _spec(tmp_path)
    spec.contract_retries = 1

    async def attempt(context_file: Path, attempt: int) -> JobResult:
        return JobResult(ok=False, failure_kind=FailureKind.CONTRACT, failure_detail="没有产物")

    await run_with_contract_retry(spec, attempt)

    assert (spec.output_dir / "context-attempt-1.md").is_file()
    assert (spec.output_dir / "context-attempt-2.md").is_file()


async def test_the_output_dir_survives_into_the_retry_attempt(tmp_path: Path) -> None:
    """重试是增量修复,不是重新生成(PRD 34 D3)。

    树产出类工序的 staging 就落在 output_dir 里;外环若在重试前清掉它,"只修坏的那
    一个文件"就无从谈起——第二次尝试只能全量重写,原子失败的成本又回来了。
    """
    spec = _spec(tmp_path)
    spec.contract_retries = 1
    survived: dict[int, bool] = {}

    async def attempt(context_file: Path, attempt: int) -> JobResult:
        staged = spec.output_dir / "staging" / "modules" / "order-service" / "map.yaml"
        survived[attempt] = staged.is_file()
        if attempt == 1:
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_text("id: order-service\n", encoding="utf-8")
        return JobResult(
            ok=False, failure_kind=FailureKind.CONTRACT, failure_detail="cards/x.md: 缺 kind"
        )

    await run_with_contract_retry(spec, attempt)

    assert survived[1] is False
    assert survived[2] is True, "重试时 output_dir(含 staging/)必须保留在原地"


async def test_duration_covers_all_attempts(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    spec.contract_retries = 1

    async def attempt(context_file: Path, attempt: int) -> JobResult:
        if attempt == 1:
            return JobResult(
                ok=False, failure_kind=FailureKind.CONTRACT, failure_detail="没有产物"
            )
        return JobResult(ok=True)

    result = await run_with_contract_retry(spec, attempt)

    assert result.duration_s >= 0


async def test_usage_is_aggregated_across_explicit_contract_retries(tmp_path: Path) -> None:
    """显式补交仍是同一个 Job；账单不能只留下第二次的用量。"""
    spec = _spec(tmp_path)
    spec.contract_retries = 1

    async def attempt(context_file: Path, attempt: int) -> JobResult:
        if attempt == 1:
            return JobResult(
                ok=False,
                failure_kind=FailureKind.CONTRACT,
                failure_detail="staging 不完整",
                tokens_used=100,
            )
        return JobResult(ok=True, tokens_used=200)

    result = await run_with_contract_retry(spec, attempt)

    assert result.tokens_used == 300
    assert result.attempts == 2


async def test_an_explicit_protocol_redelivery_uses_its_own_retry(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    spec.protocol_retries = 1

    async def attempt(context_file: Path, attempt: int) -> JobResult:
        if attempt == 1:
            return JobResult(
                ok=False,
                failure_kind=FailureKind.PROTOCOL,
                failure_detail="没有 structured_output",
            )
        return JobResult(ok=True)

    result = await run_with_contract_retry(spec, attempt)

    assert result.ok is True
    assert result.attempts == 2
    assert "没有 structured_output" in (spec.output_dir / "context-attempt-2.md").read_text()


async def test_a_normal_job_does_not_retry_a_protocol_failure(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    attempts = 0

    async def attempt(context_file: Path, attempt: int) -> JobResult:
        nonlocal attempts
        attempts += 1
        return JobResult(
            ok=False,
            failure_kind=FailureKind.PROTOCOL,
            failure_detail="没有 structured_output",
        )

    result = await run_with_contract_retry(spec, attempt)

    assert result.failure_kind is FailureKind.PROTOCOL
    assert attempts == 1
