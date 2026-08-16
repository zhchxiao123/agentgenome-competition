"""Procedure 派发:三种 kind 的执行语义、派发前的四道校验,以及员工配置的塌缩。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agentgenome.agents.pool import AgentPool
from agentgenome.agents.runtime import FailureKind, JobResult, JobSpec
from agentgenome.config import Config
from agentgenome.core.states import TaskState
from agentgenome.employees import EmployeeConfig, ProcedureNotAllowed
from agentgenome.genome.dispatch import DispatchRefused, dispatch_procedure
from agentgenome.genome.procedures import load_registry
from tests.fixtures.procedures import (
    BAD_SCRIPT,
    FAILING_SCRIPT,
    GOOD_SCRIPT,
    INTERMEDIATE_SCRIPT,
    OUTPUT_SCHEMA,
    write_procedure,
)


class RecordingRuntime:
    """记下自己被调用了几次、收到什么 JobSpec 的假运行时。"""

    name = "replay"

    def __init__(self, result: JobResult | None = None) -> None:
        self.specs: list[JobSpec] = []
        self._result = result

    async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
        self.specs.append(spec)
        return self._result or JobResult(ok=True, tokens_used=42, tokens_available=True)

    def preflight(self) -> None:
        return None


class ClaudeRecordingRuntime(RecordingRuntime):
    name = "claude-code"


class QwenRecordingRuntime(RecordingRuntime):
    name = "qwen-code"


class AgentTeamsRecordingRuntime(RecordingRuntime):
    name = "agentteams"


def _procedure(root: Path, procedure_id: str, kind: str, **kwargs: Any) -> None:
    if "artifacts" in kwargs:
        kwargs["outputs_artifacts"] = kwargs.pop("artifacts")
    if "inputs_schema" in kwargs:
        kwargs["inputs_schema"] = kwargs.pop("inputs_schema")
    kwargs.setdefault("script", None)
    write_procedure(root, procedure_id, kind, **kwargs)


@pytest.fixture
def procedures(tmp_path: Path) -> Path:
    root = tmp_path / "procedures"
    root.mkdir()
    return root


def _employee(procedures: list[str] | None = None, **overrides: Any) -> EmployeeConfig:
    """一个把白名单开到刚好够用的员工。派发一律以某个员工的身份发生。

    默认放开全部写权限:这一组测试关心的是派发语义,越权检查本身在
    `test_scope_guard.py` 与 `test_agent_pool.py` 里测。
    """
    return EmployeeConfig.model_validate(
        {
            "id": "dev",
            "runtime": "replay",
            "prompt": "prompts/dev.md",
            "procedures": procedures or [],
            "permissions": {"write_paths": ["**"]},
        }
        | overrides
    )


def _workdir(tmp_path: Path) -> Path:
    """派发出去的 Job 带着授权范围,而越权检查看的是真实 git 工作树。"""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    if not (work / ".git").exists():
        for args in (("init", "--initial-branch=main"), ("commit", "--allow-empty", "-m", "base")):
            subprocess.run(
                ["git", "-c", "user.name=T", "-c", "user.email=t@t.test", *args],
                cwd=work,
                check=True,
                capture_output=True,
            )
    return work


def _run(
    procedures: Path,
    tmp_path: Path,
    procedure_id: str,
    runtime: RecordingRuntime | None = None,
    *,
    state: TaskState = TaskState.UNIT_TESTING,
    inputs: dict[str, Any] | None = None,
    runtime_name: str | None = None,
    employee: EmployeeConfig | None = None,
    config: Config | None = None,
    env: dict[str, str] | None = None,
):
    work = _workdir(tmp_path)
    registry = load_registry(procedures)
    # 派发经过池,并发闸与任务级预算因此对 Procedure 派发同样生效。
    selected_runtime = runtime or RecordingRuntime()
    pool = AgentPool({selected_runtime.name: selected_runtime})
    return dispatch_procedure(
        registry.get(procedure_id),
        pool=pool,
        employee=employee or _employee([procedure_id]),
        task_id="ag-1",
        state=state,
        inputs=inputs or {},
        workdir=work,
        output_dir=tmp_path / "out" / procedure_id,
        config=config or Config(),
        runtime_name=runtime_name,
        round_=1,
        env=env or {},
    )


# --- deterministic:不经 AgentRuntime ----------------------------------------


async def test_deterministic_procedure_never_touches_the_runtime(
    procedures: Path, tmp_path: Path
) -> None:
    """验证类一律确定性——零 token、可复现、毫秒级。拉 Agent 是纯粹的浪费。"""
    _procedure(procedures, "unit-gate", "deterministic", script=GOOD_SCRIPT)
    runtime = RecordingRuntime()

    result = await _run(procedures, tmp_path, "unit-gate", runtime)

    assert result.ok is True
    assert runtime.specs == [], "确定性 Procedure 不该拉起任何 Agent"


async def test_deterministic_output_still_goes_through_the_contract(
    procedures: Path, tmp_path: Path
) -> None:
    """不拉 Agent 不等于不用守契约。"""
    _procedure(procedures, "unit-gate", "deterministic", script=BAD_SCRIPT)

    result = await _run(procedures, tmp_path, "unit-gate")

    assert result.ok is False
    assert result.failure_kind is FailureKind.CONTRACT


async def test_a_failing_script_is_reported_as_a_process_failure(
    procedures: Path, tmp_path: Path
) -> None:
    _procedure(procedures, "unit-gate", "deterministic", script=FAILING_SCRIPT)

    result = await _run(procedures, tmp_path, "unit-gate")

    assert result.ok is False
    assert result.exit_code == 3


# --- agentic:塌缩成 JobSpec -------------------------------------------------


async def test_agentic_procedure_collapses_into_a_jobspec(procedures: Path, tmp_path: Path) -> None:
    """PRD 02 把 JobSpec 做成扁平的,就是为了让这一步归 Procedure 这边而不是漏进运行时层。"""
    _procedure(procedures, "code-develop", "agentic", prompt="去写代码\n")
    runtime = RecordingRuntime()

    await _run(procedures, tmp_path, "code-develop", runtime)

    assert len(runtime.specs) == 1
    spec = runtime.specs[0]
    assert spec.procedure_id == "code-develop"
    assert spec.procedure_version == "1.0.0"
    assert spec.output_schema == OUTPUT_SCHEMA
    assert spec.round == 1
    assert "去写代码" in spec.context_file.read_text()


async def test_plan_procedure_allows_one_visible_protocol_redelivery(
    procedures: Path, tmp_path: Path
) -> None:
    _procedure(
        procedures,
        "requirement-analysis",
        "agentic",
        prompt="分析需求\n",
        extra_manifest="ownership: plan\n",
    )
    runtime = RecordingRuntime()

    await _run(procedures, tmp_path, "requirement-analysis", runtime)

    assert runtime.specs[0].protocol_retries == 1
    assert runtime.specs[0].contract_retries == 0


async def test_claude_is_told_to_return_structured_data_instead_of_writing_receipts(
    procedures: Path, tmp_path: Path
) -> None:
    """旧 prompt 即使要求写 critique.json，最终交付边界也必须由运行时统一接管。"""
    _procedure(
        procedures,
        "code-critique",
        "agentic",
        prompt="评审代码并写 critique.json\n",
    )
    runtime = ClaudeRecordingRuntime()
    employee = _employee(
        ["code-critique"],
        runtime="claude-code",
        tools={"allow": ["Read", "Grep", "Glob"], "deny": ["Write", "Edit", "Bash"]},
    )

    await _run(
        procedures,
        tmp_path,
        "code-critique",
        runtime,
        runtime_name="claude-code",
        employee=employee,
    )

    context = runtime.specs[0].context_file.read_text(encoding="utf-8")
    assert "不要自行写 result.json 或 critique.json" in context
    assert "最终返回一个符合输出 schema 的 JSON 对象" in context


async def test_qwen_gets_structured_response_delivery_and_translated_tools(
    procedures: Path, tmp_path: Path
) -> None:
    """换运行时不要求把所有员工资产里的 Claude 私有工具名一起手改。"""
    _procedure(procedures, "code-critique", "agentic", prompt="评审代码\n")
    runtime = QwenRecordingRuntime()
    employee = _employee(
        ["code-critique"],
        runtime="qwen-code",
        tools={"allow": ["Read", "Grep", "Glob"], "deny": ["Write", "Edit", "Bash"]},
    )

    await _run(
        procedures,
        tmp_path,
        "code-critique",
        runtime,
        runtime_name="qwen-code",
        employee=employee,
    )

    job = runtime.specs[0]
    assert job.tools_allow == ["read_file", "grep_search", "glob"]
    context = job.context_file.read_text(encoding="utf-8")
    assert "result.json" in context
    assert str(job.output_dir / "result.json") not in context
    assert "不要自行写 result.json" in context


async def test_read_only_job_is_refused_when_runtime_cannot_enforce_it(
    procedures: Path, tmp_path: Path
) -> None:
    """平台只把 allow-list 当建议时，不能拿它承诺 reviewer 不会改代码。"""
    _procedure(procedures, "code-critique", "agentic", prompt="评审代码\n")
    runtime = AgentTeamsRecordingRuntime()
    employee = _employee(
        ["code-critique"],
        runtime="agentteams",
        tools={"allow": ["Read", "Grep", "Glob"], "deny": ["Write", "Edit", "Bash"]},
    )

    with pytest.raises(DispatchRefused, match="不能强制只读"):
        await _run(
            procedures,
            tmp_path,
            "code-critique",
            runtime,
            runtime_name="agentteams",
            employee=employee,
        )

    assert runtime.specs == []


async def test_dispatch_result_carries_the_procedure_ref(procedures: Path, tmp_path: Path) -> None:
    """三个月后的报告要能说出"当时是哪一版能力干的活"。"""
    _procedure(procedures, "code-develop", "agentic", prompt="干活\n")

    result = await _run(procedures, tmp_path, "code-develop")

    assert result.procedure_ref == "code-develop@1.0.0"


# --- hybrid:脚本先跑,失败就不拉 Agent --------------------------------------


async def test_hybrid_runs_the_script_then_the_agent(procedures: Path, tmp_path: Path) -> None:
    _procedure(procedures, "itest-run", "hybrid", script=INTERMEDIATE_SCRIPT, prompt="诊断一下\n")
    runtime = RecordingRuntime()

    await _run(procedures, tmp_path, "itest-run", runtime)

    assert len(runtime.specs) == 1


async def test_hybrid_injects_the_script_output_into_the_agent_context(
    procedures: Path, tmp_path: Path
) -> None:
    _procedure(procedures, "itest-run", "hybrid", script=INTERMEDIATE_SCRIPT, prompt="诊断一下\n")
    runtime = RecordingRuntime()

    await _run(procedures, tmp_path, "itest-run", runtime)

    assert "scanned" in runtime.specs[0].context_file.read_text()


async def test_hybrid_does_not_call_the_agent_when_the_script_fails(
    procedures: Path, tmp_path: Path
) -> None:
    """脚本失败就整个失败——省下那次调用。"""
    _procedure(procedures, "itest-run", "hybrid", script=FAILING_SCRIPT, prompt="诊断一下\n")
    runtime = RecordingRuntime()

    result = await _run(procedures, tmp_path, "itest-run", runtime)

    assert result.ok is False
    assert runtime.specs == [], "脚本已经失败了,不该再烧一次 Agent"


async def test_declared_artifacts_must_actually_be_written(
    procedures: Path, tmp_path: Path
) -> None:
    """只查 result.json 不够:声明产出却没写的 Procedure 会一路绿灯,直到下游去读它才炸。"""
    _procedure(
        procedures, "unit-gate", "deterministic", script=GOOD_SCRIPT, artifacts=["gate-report.json"]
    )

    result = await _run(procedures, tmp_path, "unit-gate")

    assert result.ok is False
    assert result.failure_kind is FailureKind.CONTRACT
    assert "gate-report.json" in (result.failure_detail or "")


async def test_a_deterministic_procedure_reports_zero_tokens_as_a_fact(
    procedures: Path, tmp_path: Path
) -> None:
    """零 token 是确定的事实,不是"拿不到用量"。"""
    _procedure(procedures, "unit-gate", "deterministic", script=GOOD_SCRIPT)

    result = await _run(procedures, tmp_path, "unit-gate")

    assert result.tokens_used == 0
    assert result.tokens_available is True
    assert result.as_dict()["tokens_available"] is True


async def test_dispatch_goes_through_the_pool_so_budgets_apply(
    procedures: Path, tmp_path: Path
) -> None:
    """绕过池等于让并发闸、同任务串行、任务级预算对 Procedure 派发全部失效。"""
    _procedure(procedures, "code-develop", "agentic", prompt="干活\n")
    runtime = RecordingRuntime(JobResult(ok=True, tokens_used=600, tokens_available=True))
    pool = AgentPool({"replay": runtime}, task_budgets={"ag-1": 1000})
    registry = load_registry(procedures)
    work = _workdir(tmp_path)
    # 单 Job 上限来自员工定义。任务预算是硬上限,所以第三次会因为"剩下的装不下
    # 一个完整 Job"而被拒。
    employee = _employee(["code-develop"], limits={"max_tokens_per_job": 400})

    async def once(n: int):
        return await dispatch_procedure(
            registry.get("code-develop"),
            pool=pool,
            employee=employee,
            task_id="ag-1",
            state=TaskState.UNIT_TESTING,
            inputs={},
            workdir=work,
            output_dir=tmp_path / "out" / str(n),
            config=Config(),
            round_=1,
        )

    await once(1)
    await once(2)
    third = await once(3)

    assert third.ok is False
    assert third.failure_kind is FailureKind.TASK_BUDGET
    assert pool.tokens_used("ag-1") == 1200


# --- 派发前三道校验:全部发生在拉起任何东西之前 ------------------------------


async def test_a_state_outside_the_trigger_is_refused(procedures: Path, tmp_path: Path) -> None:
    """防的是配置错误(把门禁 Procedure 派给开发态),不是安全边界。"""
    _procedure(
        procedures, "unit-gate", "deterministic", script=GOOD_SCRIPT, trigger=["UNIT_TESTING"]
    )
    runtime = RecordingRuntime()

    with pytest.raises(DispatchRefused) as excinfo:
        await _run(procedures, tmp_path, "unit-gate", runtime, state=TaskState.DEVELOPING)

    assert "DEVELOPING" in str(excinfo.value)
    assert runtime.specs == []


async def test_an_empty_trigger_allows_any_state(procedures: Path, tmp_path: Path) -> None:
    _procedure(procedures, "unit-gate", "deterministic", script=GOOD_SCRIPT)

    result = await _run(procedures, tmp_path, "unit-gate", state=TaskState.DEVELOPING)

    assert result.ok is True


async def test_inputs_violating_the_schema_are_refused(procedures: Path, tmp_path: Path) -> None:
    _procedure(
        procedures,
        "unit-gate",
        "deterministic",
        script=GOOD_SCRIPT,
        inputs_schema={"type": "object", "required": ["module_id"]},
    )

    with pytest.raises(DispatchRefused) as excinfo:
        await _run(procedures, tmp_path, "unit-gate", inputs={})

    assert "module_id" in str(excinfo.value)


async def test_valid_inputs_pass(procedures: Path, tmp_path: Path) -> None:
    _procedure(
        procedures,
        "unit-gate",
        "deterministic",
        script=GOOD_SCRIPT,
        inputs_schema={"type": "object", "required": ["module_id"]},
    )

    result = await _run(procedures, tmp_path, "unit-gate", inputs={"module_id": "order-service"})

    assert result.ok is True


async def test_an_incompatible_runtime_is_refused(procedures: Path, tmp_path: Path) -> None:
    """把只在一个运行时上验证过的 Procedure 派给别的,应该报错而不是试试看。"""
    _procedure(procedures, "code-develop", "agentic", prompt="干活\n", runtimes=["claude-code"])
    runtime = RecordingRuntime()

    with pytest.raises(DispatchRefused) as excinfo:
        await _run(procedures, tmp_path, "code-develop", runtime, runtime_name="qwen-code")

    assert "qwen-code" in str(excinfo.value)
    assert runtime.specs == []


async def test_a_deterministic_procedure_declaring_none_needs_no_runtime(
    procedures: Path, tmp_path: Path
) -> None:
    _procedure(procedures, "unit-gate", "deterministic", script=GOOD_SCRIPT, runtimes=["none"])

    result = await _run(procedures, tmp_path, "unit-gate", runtime_name="qwen-code")

    assert result.ok is True


async def test_an_unavailable_procedure_is_refused_with_the_missing_command(
    procedures: Path, tmp_path: Path
) -> None:
    _procedure(
        procedures,
        "secrets-scan",
        "deterministic",
        script=GOOD_SCRIPT,
        required_cmds=["definitely-not-installed"],
    )

    with pytest.raises(DispatchRefused) as excinfo:
        await _run(procedures, tmp_path, "secrets-scan")

    assert "definitely-not-installed" in str(excinfo.value)


async def test_all_checks_happen_before_anything_is_spawned(
    procedures: Path, tmp_path: Path
) -> None:
    """三道校验都在拉起任何进程之前——被拒的派发不该留下任何痕迹。"""
    _procedure(
        procedures,
        "unit-gate",
        "deterministic",
        script=GOOD_SCRIPT,
        trigger=["UNIT_TESTING"],
    )

    with pytest.raises(DispatchRefused):
        await _run(procedures, tmp_path, "unit-gate", state=TaskState.DEVELOPING)

    assert not (tmp_path / "out" / "unit-gate" / "result.json").exists()


# --- 员工配置塌缩进 JobSpec --------------------------------------------------


async def test_the_employee_tool_lists_land_on_the_job_spec(
    procedures: Path, tmp_path: Path
) -> None:
    """工具白/黑名单的唯一来源是员工定义,不是调用方顺手传的参数。"""
    _procedure(procedures, "code-develop", "agentic", prompt="干活\n")
    runtime = RecordingRuntime()
    employee = _employee(["code-develop"], tools={"allow": ["Bash", "Read"], "deny": ["WebFetch"]})

    await _run(procedures, tmp_path, "code-develop", runtime, employee=employee)

    assert runtime.specs[0].tools_allow == ["Bash", "Read"]
    assert runtime.specs[0].tools_deny == ["WebFetch"]


async def test_the_employee_id_lands_on_the_job_spec(procedures: Path, tmp_path: Path) -> None:
    """事件流里的责任归属来自员工定义本身。"""
    _procedure(procedures, "code-develop", "agentic", prompt="干活\n")
    runtime = RecordingRuntime()

    await _run(procedures, tmp_path, "code-develop", runtime)

    assert runtime.specs[0].employee_id == "dev"


async def test_only_the_declared_credentials_are_injected(procedures: Path, tmp_path: Path) -> None:
    """员工进程只拿到它这次需要的凭证,不是整个环境。"""
    _procedure(procedures, "code-develop", "agentic", prompt="干活\n")
    runtime = RecordingRuntime()
    employee = _employee(["code-develop"], credentials=["ANTHROPIC_API_KEY"])

    await _run(
        procedures,
        tmp_path,
        "code-develop",
        runtime,
        employee=employee,
        env={"ANTHROPIC_API_KEY": "sk-test", "AWS_SECRET_ACCESS_KEY": "不该被带上"},
    )

    assert runtime.specs[0].credentials == {"ANTHROPIC_API_KEY": "sk-test"}


async def test_a_declared_credential_that_is_missing_is_refused_before_anything_runs(
    procedures: Path, tmp_path: Path
) -> None:
    """缺凭证要在派发前炸,不是让 Agent 跑到一半发现推不上去。"""
    _procedure(procedures, "code-develop", "agentic", prompt="干活\n")
    runtime = RecordingRuntime()
    employee = _employee(["code-develop"], credentials=["ANTHROPIC_API_KEY"])

    with pytest.raises(DispatchRefused) as excinfo:
        await _run(procedures, tmp_path, "code-develop", runtime, employee=employee, env={})

    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
    assert runtime.specs == []


async def test_a_deterministic_procedure_needs_no_credentials(
    procedures: Path, tmp_path: Path
) -> None:
    """凭证是给 Agent 进程的。确定性脚本不该因为少一个 API key 就跑不了。"""
    _procedure(procedures, "unit-gate", "deterministic", script=GOOD_SCRIPT)
    employee = _employee(["unit-gate"], credentials=["ANTHROPIC_API_KEY"])

    result = await _run(procedures, tmp_path, "unit-gate", employee=employee, env={})

    assert result.ok is True


async def test_employee_limits_win_over_the_deployment_defaults(
    procedures: Path, tmp_path: Path
) -> None:
    _procedure(procedures, "code-develop", "agentic", prompt="干活\n")
    runtime = RecordingRuntime()
    employee = _employee(["code-develop"], limits={"job_timeout_s": 60, "max_tokens_per_job": 1234})
    config = Config.model_validate(
        {"limits": {"job_timeout_s": 9999}, "budgets": {"per_job_tokens": 999_999}}
    )

    await _run(procedures, tmp_path, "code-develop", runtime, employee=employee, config=config)

    assert runtime.specs[0].timeout_s == 60
    assert runtime.specs[0].max_tokens == 1234


async def test_deployment_defaults_apply_when_the_employee_is_silent(
    procedures: Path, tmp_path: Path
) -> None:
    _procedure(procedures, "code-develop", "agentic", prompt="干活\n")
    runtime = RecordingRuntime()
    config = Config.model_validate(
        {"limits": {"job_timeout_s": 77}, "budgets": {"per_job_tokens": 4321}}
    )

    await _run(procedures, tmp_path, "code-develop", runtime, config=config)

    assert runtime.specs[0].timeout_s == 77
    assert runtime.specs[0].max_tokens == 4321


async def test_a_procedure_off_the_whitelist_is_refused_before_anything_runs(
    procedures: Path, tmp_path: Path
) -> None:
    """员工能力边界——与 trigger.states 是不同层次的检查。"""
    _procedure(procedures, "code-develop", "agentic", prompt="干活\n")
    runtime = RecordingRuntime()
    employee = _employee(["unit-gate"])

    with pytest.raises(ProcedureNotAllowed) as excinfo:
        await _run(procedures, tmp_path, "code-develop", runtime, employee=employee)

    message = str(excinfo.value)
    assert "dev" in message
    assert "code-develop" in message
    assert "unit-gate" in message, "拒绝信息要说明白名单里有什么"
    assert runtime.specs == []
    assert not (tmp_path / "out" / "code-develop").exists(), "被拒的派发不该留下任何痕迹"


async def test_a_deterministic_procedure_off_the_whitelist_is_refused_too(
    procedures: Path, tmp_path: Path
) -> None:
    """确定性 Procedure 不烧 token,但它照样会动文件——能力边界一样要管。"""
    _procedure(procedures, "unit-gate", "deterministic", script=GOOD_SCRIPT)

    with pytest.raises(ProcedureNotAllowed):
        await _run(procedures, tmp_path, "unit-gate", employee=_employee([]))


async def test_the_runtime_defaults_to_the_one_the_employee_declares(
    procedures: Path, tmp_path: Path
) -> None:
    """员工声明了运行时,调用方就不必再传一遍——两份来源会分叉。"""
    _procedure(procedures, "code-develop", "agentic", prompt="干活\n", runtimes=["replay"])
    runtime = RecordingRuntime()

    result = await _run(
        procedures, tmp_path, "code-develop", runtime, employee=_employee(["code-develop"])
    )

    assert result.ok is True
    assert len(runtime.specs) == 1


# --- 确定性 Procedure 的越权检查 ------------------------------------------------
#
# 越权检查挂在 AgentPool 上,而确定性 Procedure 不经过池——它的脚本此前能写任何地方而
# 不被核对。门禁是确定性脚本真正大量执行的地方,这个缺口在那里会变成真的。


WRITES_OUT_OF_SCOPE = """\
import json, os, pathlib, sys
work = pathlib.Path(os.environ["AGENTGENOME_WORKDIR"])
(work / "elsewhere.py").write_text("# 不该写这儿\\n")
out = pathlib.Path(os.environ["AGENTGENOME_OUTPUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "result.json").write_text(json.dumps(
    {"task_id": "ag-1", "producer": "p", "created_at": "x", "passed": True}
))
sys.exit(0)
"""


async def test_a_deterministic_script_writing_out_of_scope_fails(
    procedures: Path, tmp_path: Path
) -> None:
    """确定性不等于可信:脚本同样在动文件,同样要被核对。"""
    _procedure(procedures, "unit-gate", "deterministic", script=WRITES_OUT_OF_SCOPE)
    employee = _employee(["unit-gate"], permissions={"write_paths": ["app/**"]})

    result = await _run(procedures, tmp_path, "unit-gate", employee=employee)

    assert result.ok is False
    assert result.failure_kind is FailureKind.SCOPE
    assert "elsewhere.py" in (result.failure_detail or "")


async def test_a_deterministic_script_within_scope_still_passes(
    procedures: Path, tmp_path: Path
) -> None:
    """只测拦得住不测放得过,等于没测。"""
    _procedure(procedures, "unit-gate", "deterministic", script=GOOD_SCRIPT)

    result = await _run(procedures, tmp_path, "unit-gate")

    assert result.ok is True


async def test_the_out_of_scope_script_is_rolled_back(procedures: Path, tmp_path: Path) -> None:
    _procedure(procedures, "unit-gate", "deterministic", script=WRITES_OUT_OF_SCOPE)
    employee = _employee(["unit-gate"], permissions={"write_paths": ["app/**"]})

    await _run(procedures, tmp_path, "unit-gate", employee=employee)

    assert not (tmp_path / "work" / "elsewhere.py").exists()
