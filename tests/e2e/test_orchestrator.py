"""编排器端到端:直通、修复循环、超限升级、崩溃恢复、取消。

走真实的库、真实的 git、真实的迁移表与真实的产物目录,唯独 Agent 那一段是回放的。
`unit-gate` 连回放都不需要——它是确定性的,跑的是真的 pytest。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentgenome.agents.pool import AgentPool
from agentgenome.agents.recording import RecordingLibrary
from agentgenome.agents.replay import ReplayRuntime
from agentgenome.agents.runtime import FailureKind
from agentgenome.cli import app
from agentgenome.config import load_config
from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.scope_grants import read_grants
from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.task import ItestNeed, TaskStore
from agentgenome.genome.suspects import SuspectKind, pending_suspects, record_suspects
from agentgenome.jobs.handlers import STAGE_DEVELOP, STAGE_PLAN, STAGE_UNIT_GATE
from agentgenome.jobs.orchestrator import (
    STAGE_ITEST_DECIDE,
    STAGE_VERIFICATION_ANALYSIS,
    Orchestrator,
)
from agentgenome.jobs.reports import read_failure_reports
from agentgenome.verification import (
    NeedsConfirmation,
    Ready,
    load_verification_spec,
    resolve_verification,
)
from agentgenome.verification.bootstrap import load_bootstrap_specs, record_bootstrap_spec
from agentgenome.verification.storage import write_pending_verification
from tests.fixtures.git import commit_all
from tests.fixtures.mall import materialize_mall
from tests.fixtures.tree import module_ids, patch_module_map
from tests.fixtures.verification import write_test_verification

runner = CliRunner()

PLAN = {
    "task_id": "",
    "producer": "decision-employee",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
    "modules": ["order-service"],
    "cross_module": False,
    "acceptance": ["下单时调用预占接口"],
    "risks": [],
}

DEV_RESULT = {
    "task_id": "",
    "producer": "dev-employee",
    "created_at": "2026-09-01T10:05:00Z",
    "passed": True,
    "changed_files": ["repos/order-service/src/order/reserve.py"],
    "self_test": {"command": "pytest -q", "exit_code": 0, "passed": True},
    "impact": {"modules": ["order-service"], "rationale": "只动了订单域"},
    "questions": [],
}

#: 影响规则一条都没命中时的兜底判定。这条链路里 dev 只动了 `repos/order-service/src/`,
#: 既没碰契约也没跨模块,所以规则说"不知道",判定落到架构员工头上。
ITEST_DECIDE_RESULT = {
    "task_id": "",
    "producer": "decision-employee",
    "created_at": "2026-09-01T10:06:00Z",
    "passed": True,
    "needs_itest": False,
    "reason": "只动了订单域的内部实现,没有对外行为变化",
    "confidence": 0.8,
}

#: 让 `unit-gate` 通过的测试文件。
PASSING_TEST = "def test_ok():\n    assert True\n"
#: 让 `unit-gate` 失败的测试文件。
FAILING_TEST = "def test_broken():\n    assert False, '预占没实现'\n"


def _record(library: Path, employee: str, procedure: str, round_: int, result: dict, files: dict):
    directory = library / f"{employee}__{procedure}__r{round_}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    for relative, content in files.items():
        target = directory / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _record_verification_analysis(library: Path, task_id: str) -> None:
    """架构员工从非标准 README 入口提出一份任务级候选。"""
    directory = library / "arch-employee__verification-propose__order-service__r1"
    directory.mkdir(parents=True)
    payload = {
        "task_id": task_id,
        "producer": "arch-employee",
        "rationale": "README 明确声明了项目测试命令",
        "spec": {
            "version": 2,
            "module": "order-service",
            "environments": {
                "project": {"adapter": "host.process", "options": {}},
                "host": {"adapter": "host.trusted", "options": {}},
            },
            "gates": [
                {
                    "id": "unit",
                    "environment": "project",
                    "command": {"argv": ["python", "-m", "pytest", "-q", "tests"]},
                    "provenance": {
                        "origin": "agent-proposal",
                        "producer": "arch-employee",
                        "evidence": [
                            {
                                "kind": "repository-entrypoint",
                                "path": "README.md",
                                "locator": "file",
                                "digest": "",
                            }
                        ],
                    },
                }
            ],
        },
    }
    (directory / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        app,
        [
            "init", "--local-only",
            str(root),
            "--name",
            "example-mall",
            "--repo",
            mall["order-service"].remote_url,
            "--repo",
            mall["inventory-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output

    # 项目地图补上 test_cmd —— `unit-gate` 跑的就是它。
    #
    # **必须提交。** 员工干活的隔离工作区是从 HEAD 开出来的,未提交的改动它看不见——
    # 而"看不见"的表现是门禁报"这个模块没声明 test_cmd",极容易误判成实现有问题。
    modules = module_ids(root)
    for module_id in modules:
        patch_module_map(root, module_id, test_cmd="python -m pytest -q tests")

    # 在基因组里给两个模块配一份只有单测的门禁。
    #
    # 推导出来的默认门禁带一关 gitleaks,CI 机器上通常没装——那会让每一条链路都
    # 变成"环境类失败",而这一组测试要验的是流程本身。环境类那条路在
    # `test_gates_wiring.py` 里单独验。
    for module_id in modules:
        write_test_verification(
            root, module_id, ("unit", ("python", "-m", "pytest", "-q", "tests"))
        )
    commit_all(root, "chore: 补上 test_cmd 与门禁配置")
    return root


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "lib"
    path.mkdir()
    # `agctl task run` 自己搭池,回放运行时靠这个环境变量注册。
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(path))
    return path


def _orchestrator(
    workspace: Path, library: Path, *, enforce_budget: bool = False
) -> Orchestrator:
    pool = AgentPool({"replay": ReplayRuntime(RecordingLibrary(library))})
    config = load_config(workspace)
    config = config.model_copy(
        update={"budgets": config.budgets.model_copy(update={"enforce": enforce_budget})}
    )
    return Orchestrator(workspace, pool=pool, runtime_name="replay", config=config)


@pytest.mark.parametrize("runtime", ["human", "agentteams"])
def test_verification_analysis_requires_a_read_only_silicon_runtime(
    workspace: Path, library: Path, runtime: str
) -> None:
    """human 或不能强制只读的 AgentTeams 都不能承接仓库证据调查。"""
    pool = AgentPool({runtime: ReplayRuntime(RecordingLibrary(library))})
    orchestrator = Orchestrator(workspace, pool=pool)
    employee = orchestrator.employees.get("arch-employee").model_copy(
        update={"runtime": runtime}
    )

    with pytest.raises(RuntimeError, match="强制只读"):
        orchestrator._verification_analysis_runtime(employee)


def test_verification_analysis_has_a_bounded_automatic_retry(
    workspace: Path, library: Path
) -> None:
    """持续基础设施失败不会无限分配 slot；终止时也不创建 human Job。"""
    task_id = _submit(workspace)
    orchestrator = _orchestrator(workspace, library)
    task = orchestrator.current(task_id)
    for _ in range(3):
        orchestrator.bus(task).allocate("verification-analysis").write_manifest(
            producer="arch-employee",
            inputs=["order-service"],
            task_attempt=1,
            result_ok=False,
            failure_kind="process",
            failure_detail="runtime unavailable",
        )

    blocked = orchestrator._retry_or_block_verification_analysis(
        task, "order-service", "runtime unavailable"
    )

    assert blocked is not None
    assert blocked.state is TaskState.ESCALATED
    assert "未创建人工分析待办" in blocked.escalate_reason


def _submit(workspace: Path, requirement: str = "下单时要预占库存") -> str:
    result = runner.invoke(
        app,
        ["task", "submit", "--requirement", requirement, "--workspace", str(workspace), "--json"],
    )
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["id"])


def _arm(library: Path, task_id: str, dev_test: str, round_: int = 1) -> None:
    """摆好这一轮要回放的东西:计划 + 开发产出的测试文件。"""
    plan = PLAN | {"task_id": task_id}
    _record(library, "decision-employee", "requirement-analysis", 1, plan, {})
    _record(
        library,
        "dev-employee",
        "code-develop",
        round_,
        DEV_RESULT | {"task_id": task_id},
        {"repos/order-service/tests/test_reserve.py": dev_test},
    )
    _record(
        library,
        "decision-employee",
        "itest-decide",
        round_,
        ITEST_DECIDE_RESULT | {"task_id": task_id},
        {},
    )


def _arm_ambiguous_project(workspace: Path, library: Path) -> str:
    module_id = "order-service"
    (workspace / "genome/gates" / f"{module_id}.yaml").unlink()
    module = workspace / "repos" / module_id
    (module / "Makefile").unlink()
    pending = resolve_verification(module_id, module)
    assert isinstance(pending, NeedsConfirmation)
    write_pending_verification(workspace, module_id, pending)
    commit_all(workspace, "chore: simulate an empty project")

    task_id = _submit(workspace)
    _record(
        library,
        "decision-employee",
        "requirement-analysis",
        1,
        PLAN | {"task_id": task_id},
        {},
    )
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        {
            "repos/order-service/Makefile": "lint:\n\t@true\n",
            "repos/order-service/README.md": (
                "# Order service\n\nRun `python -m pytest -q tests` before delivery.\n"
            ),
            "repos/order-service/tests/test_reserve.py": PASSING_TEST,
        },
    )
    _record(
        library,
        "decision-employee",
        "itest-decide",
        1,
        ITEST_DECIDE_RESULT | {"task_id": task_id},
        {},
    )
    return task_id


def _itest_decision(orchestrator: Orchestrator, task_id: str) -> dict:
    found = [
        event.payload
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.ITEST_DECISION
    ]
    assert found, "判定没进事件流——「为什么这次没跑集成测试」就没法回答了"
    return found[-1]


# --- 直通 -------------------------------------------------------------------


async def test_a_task_walks_from_created_to_ready_to_commit(workspace: Path, library: Path) -> None:
    """直通链路:plan → dev → gate 通过 → READY_TO_COMMIT。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)

    assert (await orchestrator.advance(task_id)).state is TaskState.DEVELOPING
    assert (await orchestrator.advance(task_id)).state is TaskState.UNIT_TESTING
    assert (await orchestrator.advance(task_id)).state is TaskState.READY_TO_COMMIT


async def test_an_ambiguous_empty_project_gets_a_deep_agent_analysis(
    workspace: Path, library: Path
) -> None:
    """确定性发现说不准时，编排器自己深挖，不把门禁配置问题退给用户。"""
    task_id = _arm_ambiguous_project(workspace, library)
    orchestrator = _orchestrator(workspace, library)

    assert (await orchestrator.advance(task_id)).state is TaskState.DEVELOPING
    assert (await orchestrator.advance(task_id)).state is TaskState.UNIT_TESTING
    waiting_for_analysis = await orchestrator.advance(task_id)

    assert waiting_for_analysis.state is TaskState.UNIT_TESTING
    assert waiting_for_analysis.fix_rounds == 0

    _record_verification_analysis(library, task_id)
    # 只有 result.json、没有池的成功终态小票，可能是 scope/进程失败后留下的残片；
    # 恢复不能把它补成成功。
    recording = (
        library / "arch-employee__verification-propose__order-service__r1" / "result.json"
    )
    orphan = orchestrator.bus(waiting_for_analysis).allocate("verification-analysis")
    (orphan.path / "result.json").write_text(
        recording.read_text(encoding="utf-8"), encoding="utf-8"
    )
    module_root = orchestrator._workdir(waiting_for_analysis) / "repos/order-service"
    assert (
        orchestrator._completed_verification_analysis(
            waiting_for_analysis, "order-service", module_root
        )
        is None
    )

    completed_gate = await orchestrator.advance(task_id)

    assert completed_gate.state is TaskState.READY_TO_COMMIT
    assert completed_gate.fix_rounds == 0
    gate_slot = orchestrator.bus(completed_gate).latest(STAGE_UNIT_GATE)
    assert gate_slot is not None
    assert load_bootstrap_specs(gate_slot.path)[0].gate("unit").provenance.origin == (
        "agent-proposal"
    )
    assert any(
        event.actor == "arch-employee" and event.kind is LogKind.JOB_STARTED
        for event in orchestrator.log.events(task_id)
    )


async def test_an_invalid_verification_proposal_retries_without_escalating_the_task(
    workspace: Path, library: Path
) -> None:
    """无效 Adapter 声明是提案契约失败，不能伪装成业务任务的环境故障。"""
    task_id = _arm_ambiguous_project(workspace, library)
    orchestrator = _orchestrator(workspace, library)
    assert (await orchestrator.advance(task_id)).state is TaskState.DEVELOPING
    assert (await orchestrator.advance(task_id)).state is TaskState.UNIT_TESTING

    _record_verification_analysis(library, task_id)
    recording = (
        library / "arch-employee__verification-propose__order-service__r1" / "result.json"
    )
    proposal = json.loads(recording.read_text(encoding="utf-8"))
    proposal["spec"]["environments"]["project"] = {
        "adapter": "python.uv",
        "options": {},
    }
    recording.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")

    first_retry = await orchestrator.advance(task_id)

    assert first_retry.state is TaskState.UNIT_TESTING
    assert first_retry.fix_rounds == 0
    slots = orchestrator.bus(first_retry).by_stage(STAGE_VERIFICATION_ANALYSIS)
    manifest = slots[-1].manifest()
    assert manifest is not None
    assert manifest["failure_kind"] == FailureKind.CONTRACT.value
    assert "缺少字符串 option: project_file" in manifest["failure_detail"]

    second_retry = await orchestrator.advance(task_id)

    assert second_retry.state is TaskState.UNIT_TESTING
    assert second_retry.fix_rounds == 0
    assert len(orchestrator.bus(second_retry).by_stage(STAGE_VERIFICATION_ANALYSIS)) > len(slots)


def test_delivery_promotes_the_last_passing_task_verification(
    workspace: Path, library: Path
) -> None:
    """冷启动规格跟着成功交付固化；不是在开发自述成功时提前污染控制面。"""
    task_id = _submit(workspace)
    orchestrator = _orchestrator(workspace, library)
    module_id = "order-service"
    module = workspace / "repos" / module_id
    resolution = resolve_verification(module_id, module)
    assert isinstance(resolution, Ready)
    slot = orchestrator.bus(orchestrator.store.get(task_id)).allocate(STAGE_UNIT_GATE)
    record_bootstrap_spec(slot.path, resolution.spec)
    (slot.path / "gate-report.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8"
    )

    confirmed = workspace / "genome/gates" / f"{module_id}.yaml"
    confirmed.unlink()
    pending = resolve_verification(module_id, workspace / "does-not-exist")
    assert isinstance(pending, NeedsConfirmation)
    write_pending_verification(workspace, module_id, pending)
    commit_all(workspace, "test: return verification to pending")
    orchestrator.store.save(
        orchestrator.store.get(task_id).evolve(state=TaskState.MERGING)
    )

    completed = orchestrator.deliver(task_id, TaskEvent.MERGED)

    assert completed.state is TaskState.COMPLETED
    assert load_verification_spec(workspace, module_id) == resolution.spec
    assert not (workspace / "genome/gates" / f"{module_id}.pending.yaml").exists()


async def test_the_plan_lands_where_a_human_can_read_it(workspace: Path, library: Path) -> None:
    """需求方要在系统开始写代码**之前**就能看到它是怎么理解的。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)

    await _orchestrator(workspace, library).advance(task_id)

    plan = yaml.safe_load((workspace / "tasks" / task_id / "plan.yaml").read_text("utf-8"))
    assert plan["modules"] == ["order-service"]
    assert plan["acceptance"] == ["下单时调用预占接口"]


async def test_a_plan_naming_a_module_that_does_not_exist_is_refused(
    workspace: Path, library: Path
) -> None:
    """编造的模块 id 会让基因组切片切出一份空认知,而员工拿着空认知照样会动手。"""
    task_id = _submit(workspace)
    _record(
        library,
        "decision-employee",
        "requirement-analysis",
        1,
        PLAN | {"task_id": task_id, "modules": ["made-up-service"]},
        {},
    )
    orchestrator = _orchestrator(workspace, library)

    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.CREATED, "编造的模块被放行了"
    assert not (workspace / "tasks" / task_id / "plan.yaml").exists()


async def test_development_refuses_to_start_when_no_module_is_known(
    workspace: Path, library: Path
) -> None:
    """**在派发前判,不在 Job 结束后判。**

    开发员工的可写范围就是计划里那份模块清单。清单算不出来时它一行业务代码都写不进去——
    而让 Job 照跑的话,要烧掉一整个 Job 才发现,症状还是一份看不懂的空 diff。

    计划产物读不出来是真会发生的:文件被删、被改坏,或者是一个 schema 收紧之前留下的老任务。
    """
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    assert (await orchestrator.advance(task_id)).state is TaskState.DEVELOPING
    (workspace / "tasks" / task_id / "plan.yaml").unlink()

    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.ESCALATED
    # 升级原因按"下一步该从哪查"的口径写:指向计划产物,不是"权限为空"。
    assert "计划" in (task.escalate_reason or "")


async def test_the_branch_is_created_when_development_starts(
    workspace: Path, library: Path
) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)

    task = await _orchestrator(workspace, library).advance(task_id)

    assert task.branch == f"task/{task_id}"


async def test_the_dev_context_carries_the_requirement_and_only_the_relevant_modules(
    workspace: Path, library: Path
) -> None:
    """计划里的 `modules` 是基因组切片的输入。不相关模块的认知不该进上下文——
    塞进去只会把真正相关的几行淹掉。"""
    task_id = _submit(workspace, "下单时要预占库存")
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)  # plan
    await orchestrator.advance(task_id)  # dev

    bus = orchestrator.bus(orchestrator.store.get(task_id))
    bundle = (bus.latest(STAGE_DEVELOP).path / "context-attempt-0.md").read_text("utf-8")
    assert "下单时要预占库存" in bundle, "需求原文没进上下文"
    assert "order-service" in bundle, "涉及模块的认知没进上下文"
    assert "inventory-service" not in bundle, "不相关模块被塞进来了"


async def test_a_change_no_rule_matches_falls_through_to_the_agent(
    workspace: Path, library: Path
) -> None:
    """规则说不知道时,判定由架构员工补位。判定发生在门禁通过那一刻,不是计划阶段。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)  # plan
    assert orchestrator.store.get(task_id).needs_itest is ItestNeed.UNDECIDED, (
        "计划阶段还没有 diff,这时判定就等于凭空猜"
    )

    await orchestrator.advance(task_id)  # dev
    await orchestrator.advance(task_id)  # gate

    assert orchestrator.store.get(task_id).needs_itest is ItestNeed.NO
    decision = _itest_decision(orchestrator, task_id)
    assert decision["source"] == "agent"
    assert decision["reason"] == "只动了订单域的内部实现,没有对外行为变化"


async def test_a_cross_module_change_is_decided_by_the_rules_without_calling_the_agent(
    workspace: Path, library: Path
) -> None:
    """规则命中就到此为止。

    **兜底那一级的录制故意不摆。** 摆了的话这条测试即使在"规则被跳过、直接问 AI"的实现
    下也照样绿——而规则优先正是这套机制的全部价值。缺录制时回放运行时会炸,于是"多问了
    一次"这件事有了硬症状。
    """
    task_id = _submit(workspace)
    plan = PLAN | {"task_id": task_id, "modules": ["order-service", "inventory-service"]}
    _record(library, "decision-employee", "requirement-analysis", 1, plan, {})
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        {
            "repos/order-service/tests/test_reserve.py": PASSING_TEST,
            "repos/inventory-service/tests/test_stock.py": PASSING_TEST,
        },
    )
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    assert orchestrator.store.get(task_id).needs_itest is ItestNeed.YES
    decision = _itest_decision(orchestrator, task_id)
    assert decision["source"] == "rule"
    assert decision["matched_rules"] == ["cross-module"]


async def test_a_human_saying_never_beats_the_rules(workspace: Path, library: Path) -> None:
    """人工覆盖优先级最高:规则都不求值,兜底也不派发。"""
    result = runner.invoke(
        app,
        [
            "task",
            "submit",
            "--requirement",
            "改两个仓的日志文案",
            "--workspace",
            str(workspace),
            "--itest",
            "never",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    task_id = str(json.loads(result.output)["id"])
    plan = PLAN | {"task_id": task_id, "modules": ["order-service", "inventory-service"]}
    _record(library, "decision-employee", "requirement-analysis", 1, plan, {})
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        {
            "repos/order-service/tests/test_reserve.py": PASSING_TEST,
            "repos/inventory-service/tests/test_stock.py": PASSING_TEST,
        },
    )
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.needs_itest is ItestNeed.NO, "跨模块规则本来会命中,人工覆盖没盖住它"
    assert task.state is TaskState.READY_TO_COMMIT
    assert _itest_decision(orchestrator, task_id)["source"] == "manual"


async def test_the_artifact_directories_carry_their_lineage(workspace: Path, library: Path) -> None:
    """三个月后看到一个 gate-report.json,要能知道它是哪一轮、谁产出的。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(3):
        await orchestrator.advance(task_id)

    bus = orchestrator.bus(orchestrator.store.get(task_id))
    stages = [slot.stage for slot in bus.all()]
    assert stages == [STAGE_PLAN, STAGE_DEVELOP, STAGE_UNIT_GATE, STAGE_ITEST_DECIDE]
    assert bus.latest(STAGE_DEVELOP).manifest()["producer"] == "dev-employee"
    assert bus.latest(STAGE_ITEST_DECIDE).manifest()["producer"] == "decision-employee"


async def test_the_event_stream_covers_the_whole_walk(workspace: Path, library: Path) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(3):
        await orchestrator.advance(task_id)

    transitions = [
        event.payload["to"]
        for event in orchestrator.log.events(task_id)
        if event.kind.value == "transition"
    ]
    assert transitions == ["DEVELOPING", "UNIT_TESTING", "READY_TO_COMMIT"]


# --- 修复循环 ---------------------------------------------------------------


async def test_a_failing_gate_sends_the_task_back_to_development(
    workspace: Path, library: Path
) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id, FAILING_TEST)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(3):
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.state is TaskState.DEVELOPING
    assert task.fix_rounds == 1


async def test_the_failure_report_reaches_the_next_round(workspace: Path, library: Path) -> None:
    """第二轮的员工要真的看到第一轮为什么挂,不是只知道"上一轮挂了"。"""
    task_id = _submit(workspace)
    _arm(library, task_id, FAILING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        2,
        DEV_RESULT | {"task_id": task_id},
        {"repos/order-service/tests/test_reserve.py": PASSING_TEST},
    )
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):  # plan → dev → gate(挂) → dev(第二轮)
        await orchestrator.advance(task_id)

    bus = orchestrator.bus(orchestrator.store.get(task_id))
    second = bus.by_stage(STAGE_DEVELOP)[1]
    bundle = (second.path / "context-attempt-0.md").read_text(encoding="utf-8")
    assert "预占没实现" in bundle, "上一轮的失败细节没进下一轮的上下文"


async def test_the_second_round_passes_and_moves_on(workspace: Path, library: Path) -> None:
    """只测拦得住不测放得过,等于没测:修好之后要真的能过。"""
    task_id = _submit(workspace)
    _arm(library, task_id, FAILING_TEST)
    _arm(library, task_id, PASSING_TEST, round_=2)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(5):
        await orchestrator.advance(task_id)

    assert orchestrator.store.get(task_id).state is TaskState.READY_TO_COMMIT


# --- 超限升级 ---------------------------------------------------------------


async def test_exhausting_the_rounds_escalates(workspace: Path, library: Path) -> None:
    """一个卡在"改 A 坏 B"循环里的任务可以无限跑下去。必须有终点。"""
    _set_max_fix_rounds(workspace, 1)
    task_id = _submit(workspace)
    _arm(library, task_id, FAILING_TEST)
    for round_ in (2, 3):
        _record(
            library,
            "dev-employee",
            "code-develop",
            round_,
            DEV_RESULT | {"task_id": task_id},
            {"repos/order-service/tests/test_reserve.py": FAILING_TEST},
        )
    orchestrator = _orchestrator(workspace, library)
    for _ in range(6):
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.state is TaskState.ESCALATED
    assert task.escalate_reason, "升级人工必须说清楚为什么"


async def test_escalating_freezes_the_workspace(workspace: Path, library: Path) -> None:
    """我接手时要能看到完整的失败历史,不是一个被清理干净的空目录。"""
    _set_max_fix_rounds(workspace, 1)
    task_id = _submit(workspace)
    _arm(library, task_id, FAILING_TEST)
    for round_ in (2, 3):
        _record(
            library,
            "dev-employee",
            "code-develop",
            round_,
            DEV_RESULT | {"task_id": task_id},
            {"repos/order-service/tests/test_reserve.py": FAILING_TEST},
        )
    orchestrator = _orchestrator(workspace, library)
    for _ in range(6):
        await orchestrator.advance(task_id)

    from agentgenome.space.git_ws import GitWorkspace

    assert GitWorkspace(workspace).worktree_path(task_id).is_dir(), "工作区被清掉了"
    assert (workspace / "tasks" / task_id / "failures").is_dir(), "失败历史被清掉了"


async def test_running_out_of_budget_escalates(workspace: Path, library: Path) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(budget_tokens=100, tokens_used=100))
    orchestrator = _orchestrator(workspace, library, enforce_budget=True)

    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.ESCALATED
    assert "预算" in (task.escalate_reason or "")


# --- 崩溃恢复 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("steps", "crashed_in", "stage"),
    [
        (1, TaskState.CREATED, STAGE_PLAN),
        (2, TaskState.DEVELOPING, STAGE_DEVELOP),
        (3, TaskState.UNIT_TESTING, STAGE_UNIT_GATE),
    ],
)
async def test_recovery_does_not_redo_a_stage_whose_artifact_landed(
    workspace: Path, library: Path, steps: int, crashed_in: TaskState, stage: str
) -> None:
    """崩溃恢复的全部意义:已经花掉的 token 不再花一遍。

    构造的是真实的崩溃现场——**Job 跑完了、产物落盘了,但状态还没写库进程就死了**。
    恢复时该认出那份产物直接推进,而不是重跑一遍。对三个状态各来一组。
    """
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(steps):
        await orchestrator.advance(task_id)
    slots_before = len(orchestrator.bus(orchestrator.store.get(task_id)).by_stage(stage))

    # 把状态改回产出这份产物的那个状态:进程就死在这两步之间。
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(state=crashed_in))
    fresh = _orchestrator(workspace, library)
    recovered = await fresh.recover()

    assert recovered[0].state is not crashed_in, "恢复之后没往前走"
    assert len(fresh.bus(recovered[0]).by_stage(stage)) == slots_before, "又跑了一遍"


async def test_recovery_does_not_promote_a_failed_single_job(
    workspace: Path, library: Path
) -> None:
    """小票存在但 Job 被 scope guard 判失败时，恢复不能把它重建成成功。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    task = await orchestrator.advance(task_id)
    assert task.state is TaskState.DEVELOPING

    slot = orchestrator.bus(task).allocate(STAGE_DEVELOP)
    (slot.path / "result.json").write_text(
        json.dumps(DEV_RESULT | {"task_id": task_id}), encoding="utf-8"
    )
    slot.write_manifest(
        producer="dev-employee",
        outputs=["result.json"],
        task_attempt=1,
        result_ok=False,
        failure_kind="scope",
        failure_detail="开发改动越权",
    )

    recovered = await _orchestrator(workspace, library).advance(task_id)

    assert recovered.state is TaskState.DEVELOPING
    assert recovered.fix_rounds == 1


async def test_a_failed_single_job_without_a_receipt_starts_one_real_fix_round(
    workspace: Path, library: Path
) -> None:
    """max_turns 一类进程失败只算一个 Job，不能靠重复迁移拒绝凑满上限。"""
    task_id = _submit(workspace)
    _record(
        library,
        "decision-employee",
        "requirement-analysis",
        1,
        PLAN | {"task_id": task_id},
        {},
    )
    failed = library / "dev-employee__code-develop__r1"
    failed.mkdir()
    (failed / "meta.yaml").write_text(
        yaml.safe_dump(
            {
                "failure_kind": "process",
                "failure_detail": "运行时达到 max_turns",
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    orchestrator = _orchestrator(workspace, library)
    assert (await orchestrator.advance(task_id)).state is TaskState.DEVELOPING

    retried = await orchestrator.advance(task_id)

    assert retried.state is TaskState.DEVELOPING
    assert retried.fix_rounds == 1
    refusals = [
        event for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.TRANSITION_REFUSED
    ]
    assert refusals == []


@pytest.mark.parametrize(
    ("steps", "crashed_in", "stage", "expected"),
    [
        (0, TaskState.CREATED, STAGE_PLAN, TaskState.DEVELOPING),
        (1, TaskState.DEVELOPING, STAGE_DEVELOP, TaskState.UNIT_TESTING),
        (2, TaskState.UNIT_TESTING, STAGE_UNIT_GATE, TaskState.READY_TO_COMMIT),
    ],
)
async def test_recovery_reruns_a_stage_whose_artifact_never_landed(
    workspace: Path,
    library: Path,
    steps: int,
    crashed_in: TaskState,
    stage: str,
    expected: TaskState,
) -> None:
    """产物不在就得重来——认成"跑过了"的话任务会带着一个空产物往下走。

    构造的是另一种崩溃现场:**目录分配了,产物没写完**。对三个状态各来一组。
    """
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(steps):
        await orchestrator.advance(task_id)
    assert orchestrator.store.get(task_id).state is crashed_in

    half_done = orchestrator.bus(orchestrator.store.get(task_id)).allocate(stage)
    assert not (half_done.path / "result.json").exists()

    recovered = await _orchestrator(workspace, library).recover()

    assert recovered[0].state is expected, f"没有重跑 {stage} 那一步"


async def test_recovery_skips_terminal_tasks(workspace: Path, library: Path) -> None:
    task_id = _submit(workspace)
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(state=TaskState.COMPLETED))

    assert await _orchestrator(workspace, library).recover() == ()


async def test_the_queue_survives_a_restart(workspace: Path, library: Path) -> None:
    """内存队列崩溃后就没了,而任务还在库里等着——表现为"重启之后有些任务再也不动了"。"""
    task_id = _submit(workspace)

    assert [task.id for task in _orchestrator(workspace, library).next_tasks()] == [task_id]


# --- 取消 -------------------------------------------------------------------


async def test_cancelling_stops_the_task_and_cleans_the_workspace(
    workspace: Path, library: Path
) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)

    task = orchestrator.deliver(task_id, TaskEvent.CANCEL)

    from agentgenome.space.git_ws import GitWorkspace

    assert task.state is TaskState.CANCELLED
    assert not GitWorkspace(workspace).worktree_path(task_id).exists()


async def test_cancelling_keeps_the_audit_trail(workspace: Path, library: Path) -> None:
    """事件流、上下文包、失败报告是审计材料,它们比任务本身活得久。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)

    orchestrator.deliver(task_id, TaskEvent.CANCEL)

    assert (workspace / "tasks" / task_id / "logs" / "events.jsonl").is_file()
    assert (workspace / "tasks" / task_id / "task.json").is_file()


async def test_cancelling_repeatedly_is_safe(workspace: Path, library: Path) -> None:
    """崩溃恢复会重放,重复取消必须无害。

    次数要够多:终态收到的事件都会被判为"非法迁移",而非法迁移的兜底是"连续 N 次
    就升级人工"——两条规则撞在一起时,取消四次的任务会变成 ESCALATED。
    """
    task_id = _submit(workspace)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(5):
        task = orchestrator.deliver(task_id, TaskEvent.CANCEL)

    assert task.state is TaskState.CANCELLED


async def test_a_completed_task_cannot_be_dragged_back_by_stray_events(
    workspace: Path, library: Path
) -> None:
    """终态就是终点。已完成的任务不该被任何事件拉回流程里,包括兜底规则。"""
    task_id = _submit(workspace)
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(state=TaskState.COMPLETED))
    orchestrator = _orchestrator(workspace, library)

    for _ in range(5):
        task = orchestrator.deliver(task_id, TaskEvent.DEV_DONE)

    assert task.state is TaskState.COMPLETED


def _set_max_fix_rounds(workspace: Path, value: int) -> None:
    path = workspace / "genome" / "rules" / "architecture.md"
    text = path.read_text(encoding="utf-8").replace(
        "layering: []", f"layering: []\nmax_fix_rounds: {value}"
    )
    path.write_text(text, encoding="utf-8")


# --- 两种 BUDGET 分开 --------------------------------------------------------


async def test_a_task_budget_that_cannot_fit_a_job_escalates(
    workspace: Path, library: Path
) -> None:
    """池在 Job 开始前拒掉是**任务级终结**:重试只会再撞一次同一堵墙。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    store = TaskStore(workspace)
    # 还有额度,但装不下一个完整 Job。
    store.save(store.get(task_id).evolve(budget_tokens=10, tokens_used=0))
    orchestrator = _orchestrator(workspace, library, enforce_budget=True)

    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.ESCALATED
    assert "预算" in (task.escalate_reason or "")


async def test_a_single_job_overrun_does_not_terminate_the_task(
    workspace: Path, library: Path
) -> None:
    """运行时中途掐断是**这一个 Job** 的事,不该把整个任务终结掉。

    与上一条对照着看:同样是 BUDGET 家族,两者的反应必须不同。
    """
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)  # → DEVELOPING

    # 开发那次 Job 被单 Job 上限掐断:任务继续,不终结。
    orchestrator._last_failure = FailureKind.BUDGET
    task = orchestrator.deliver(task_id, TaskEvent.DEV_DONE)

    assert task.state is TaskState.UNIT_TESTING


# --- 终态报告 ---------------------------------------------------------------


async def test_a_terminal_task_gets_a_readable_report(workspace: Path, library: Path) -> None:
    """接手一个升级人工的任务时,没人愿意先读三百条事件才知道发生了什么。"""
    task_id = _submit(workspace)
    orchestrator = _orchestrator(workspace, library)

    orchestrator.deliver(task_id, TaskEvent.CANCEL)

    report = (workspace / "tasks" / task_id / "report.md").read_text(encoding="utf-8")
    assert "CANCELLED" in report
    assert "经过" in report
    assert "需求原文" in report


# --- 走 CLI 的那条路 --------------------------------------------------------


def test_the_whole_walk_can_be_driven_from_the_command_line(workspace: Path, library: Path) -> None:
    """需求方与运维实际用的是这条路,不是 Python API。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)

    result = runner.invoke(
        app,
        [
            "task",
            "run",
            task_id,
            "--workspace",
            str(workspace),
            "--runtime",
            "replay",
            "--steps",
            "3",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "READY_TO_COMMIT"


# --- 终态固化审计包 ---------------------------------------------------------


def _bundle(workspace: Path, task_id: str) -> Path:
    return workspace / "archive" / task_id / f"{task_id}-audit.zip"


async def test_completing_freezes_an_audit_bundle(workspace: Path, library: Path) -> None:
    """不等人来导。一次事故能不能被复盘,不该取决于有没有人及时想起来敲那条命令。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    store = TaskStore(workspace)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    store.save(store.get(task_id).evolve(state=TaskState.MERGING))

    task = orchestrator.deliver(task_id, TaskEvent.MERGED)

    assert task.state is TaskState.COMPLETED
    assert _bundle(workspace, task_id).is_file()


async def test_escalating_freezes_an_audit_bundle(workspace: Path, library: Path) -> None:
    """**这条是这个功能存在的核心理由。** 已升级人工是终态但不是已了结:机器停手了、
    人还没来,而这段时间差正是证据最容易丢的窗口。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(budget_tokens=100, tokens_used=100))
    orchestrator = _orchestrator(workspace, library, enforce_budget=True)

    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.ESCALATED
    assert _bundle(workspace, task_id).is_file(), "最该留证据的那批任务恰好没有证据"


async def test_cancelling_freezes_an_audit_bundle(workspace: Path, library: Path) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)

    orchestrator.deliver(task_id, TaskEvent.CANCEL)

    assert _bundle(workspace, task_id).is_file()


async def test_a_running_task_has_no_bundle_yet(workspace: Path, library: Path) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)

    assert not _bundle(workspace, task_id).exists()


async def test_the_bundle_outlives_the_task_directory(workspace: Path, library: Path) -> None:
    """包必须自足——它的全部意义就是在原始材料没了之后还能查。"""
    import shutil
    import zipfile

    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    orchestrator.deliver(task_id, TaskEvent.CANCEL)

    shutil.rmtree(workspace / "tasks" / task_id)

    with zipfile.ZipFile(_bundle(workspace, task_id)) as bundle:
        names = bundle.namelist()
    assert "task.json" in names
    assert any(name.startswith("logs/") for name in names)


async def test_the_bundle_says_when_and_in_what_state_it_was_sealed(
    workspace: Path, library: Path
) -> None:
    import json as _json
    import zipfile

    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    orchestrator.deliver(task_id, TaskEvent.CANCEL)

    with zipfile.ZipFile(_bundle(workspace, task_id)) as bundle:
        manifest = _json.loads(bundle.read("audit-manifest.json"))

    assert manifest["task_id"] == task_id
    assert manifest["state"] == TaskState.CANCELLED.value
    assert manifest["sealed_at"]


async def test_a_failing_seal_does_not_fail_the_task(workspace: Path, library: Path) -> None:
    """一个辅助环节的 bug 造成的最大伤害,不该是让一个已经跑完的任务看起来挂了。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    # 归档根被一个同名文件占住:目录建不出来,打包必失败。
    (workspace / "archive").write_text("not a directory", encoding="utf-8")

    task = orchestrator.deliver(task_id, TaskEvent.CANCEL)

    assert task.state is TaskState.CANCELLED
    notes = [
        event
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.NOTE and event.payload.get("note") == "audit_seal_failed"
    ]
    assert notes, "固化失败必须留痕,否则证据没兜住这件事本身也看不见"


# --- 日志保留期 -------------------------------------------------------------


async def test_prune_keeps_logs_of_a_task_without_a_bundle(workspace: Path, library: Path) -> None:
    """**宁可占磁盘,不可丢证据。** 超期就删、不管有没有包的话,归档盘满这种故障会静默地
    把证据全删光,而且删得越干净越像一切正常。"""
    from datetime import UTC, datetime, timedelta

    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    orchestrator.deliver(task_id, TaskEvent.CANCEL)
    _bundle(workspace, task_id).unlink()
    store = TaskStore(workspace)
    store.save(store.get(task_id), now=datetime.now(UTC) - timedelta(days=400))

    result = runner.invoke(app, ["prune", "--workspace", str(workspace), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pruned"] == []
    assert payload["skipped"][0]["reason"] == "no_bundle"
    assert "宁可占磁盘" in payload["skipped"][0]["message"]
    assert (workspace / "tasks" / task_id / "logs").is_dir()


async def test_prune_drops_logs_but_keeps_the_bundle_and_the_events(
    workspace: Path, library: Path
) -> None:
    from datetime import UTC, datetime, timedelta

    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    orchestrator.deliver(task_id, TaskEvent.CANCEL)
    store = TaskStore(workspace)
    store.save(store.get(task_id), now=datetime.now(UTC) - timedelta(days=400))

    result = runner.invoke(app, ["prune", "--workspace", str(workspace), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["pruned"] == [task_id]
    assert not (workspace / "tasks" / task_id / "logs").exists()
    assert _bundle(workspace, task_id).is_file()
    # 事件面的真相来源是数据库,删掉的只是给实时推送用的 JSONL 副本。
    assert EventLog(workspace).events(task_id)


async def test_prune_dry_run_deletes_nothing(workspace: Path, library: Path) -> None:
    from datetime import UTC, datetime, timedelta

    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    orchestrator.deliver(task_id, TaskEvent.CANCEL)
    store = TaskStore(workspace)
    store.save(store.get(task_id), now=datetime.now(UTC) - timedelta(days=400))

    result = runner.invoke(app, ["prune", "--dry-run", "--workspace", str(workspace), "--json"])

    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["pruned"] == [task_id], "预演报告要与实际执行一致"
    assert (workspace / "tasks" / task_id / "logs").is_dir(), "预演删了东西"


# --- 扩权 --------------------------------------------------------------------


async def test_a_scope_request_grants_the_module_and_reruns_development(
    workspace: Path, library: Path
) -> None:
    """员工发现要动计划外的模块时,**不写它**,而是提出带理由的申请。

    批准是自动的——把人卡在这里会让每个合法的跨模块任务停摆几小时,而「这个任务该不该碰
    库存域」这个问题,人拿着 diff 在评审环节回答比在飞行途中凭一句理由回答准得多。
    """
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT
        | {
            "task_id": task_id,
            "scope_request": [{"module": "inventory-service", "reason": "预占要改库存扣减"}],
        },
        {},
    )
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)

    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.DEVELOPING, "扩权之后该重跑一轮开发"
    assert "inventory-service" in orchestrator.effective_modules(task)


async def test_the_widened_module_is_writable_in_the_next_round(
    workspace: Path, library: Path
) -> None:
    """批下来却不生效的话,这条通道等于不存在。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT
        | {
            "task_id": task_id,
            "scope_request": [{"module": "inventory-service", "reason": "预占要改库存扣减"}],
        },
        {},
    )
    # 第二轮真的去写那个刚批下来的模块。**断言必须落在派发的结果上**——
    # 断言 `plan_mount_paths` 那种辅助函数是没用的:派发路径压根不调它,于是通道死了
    # 测试照样绿。这条测试的前一版就是这么写的,而它确实放过了一个"批准从未生效"的 bug。
    _record(
        library,
        "dev-employee",
        "code-develop",
        2,
        DEV_RESULT | {"task_id": task_id},
        {"repos/inventory-service/src/inventory/reserve.py": "# 扣减库存\n"},
    )
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    await orchestrator.advance(task_id)

    workdir = orchestrator.workdir(orchestrator.store.get(task_id))
    assert (workdir / "repos/inventory-service/src/inventory/reserve.py").exists(), (
        "扩权批下来了,但那个模块在下一轮仍然写不进去——申请通道是死的"
    )


async def test_a_widening_leaves_a_trace_with_the_reason(workspace: Path, library: Path) -> None:
    """审批人面对的是一份可能横跨两个域的 diff。不留理由,他得自己从 diff 里重新推一遍。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT
        | {
            "task_id": task_id,
            "scope_request": [{"module": "inventory-service", "reason": "预占要改库存扣减"}],
        },
        {},
    )
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    grants = read_grants(workspace, task_id)

    assert [item.module for item in grants] == ["inventory-service"]
    assert grants[0].reason == "预占要改库存扣减"


async def test_a_request_for_a_module_that_does_not_exist_is_not_granted(
    workspace: Path, library: Path
) -> None:
    """扩权不能被用来指向任意路径。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT
        | {
            "task_id": task_id,
            "scope_request": [{"module": "made-up-service", "reason": "我需要它"}],
        },
        {},
    )
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)

    task = await orchestrator.advance(task_id)

    assert read_grants(workspace, task_id) == []
    assert task.state is not TaskState.DEVELOPING, "不批的申请不该把任务留在开发态空转"


async def test_a_blocked_write_teaches_the_request_channel(workspace: Path, library: Path) -> None:
    """越权被拦时,报告要告诉员工**有一条正当的出路**。

    不告诉的话它只知道"我被拦了",于是下一轮多半还是同样的写法——修复循环变成反复撞同一堵墙,
    而每一轮都在烧 token。收窄权限的收益会被这个空转吃掉一大半。
    """
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        # 计划只点了 order,这里去写 inventory:越权。
        {"repos/inventory-service/src/inventory/hack.py": "# 不该动这个仓\n"},
    )
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)

    await orchestrator.advance(task_id)

    reports = read_failure_reports(orchestrator.store.task_dir(task_id))
    body = "\n".join(item.body for item in reports)
    assert "repos/inventory-service" in body, "没说清越界的是哪条路径"
    assert "order-service" in body, "没说清当前授权了哪些模块"
    assert "scope_request" in body, "没教申请通道"


async def test_a_blocked_write_into_the_genome_does_not_suggest_requesting_it(
    workspace: Path, library: Path
) -> None:
    """基因组不可申请。建议申请只会把员工引向一次注定被拒的尝试,又白烧一轮。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        {"genome/rules/architecture.md": "# 一切皆可\n"},
    )
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)

    await orchestrator.advance(task_id)

    body = "\n".join(
        item.body for item in read_failure_reports(orchestrator.store.task_dir(task_id))
    )
    assert "scope_request" not in body


async def test_a_protected_path_in_an_authorized_module_is_not_widenable(
    workspace: Path, library: Path
) -> None:
    """模块已经授权也不能申请绕过 protected rule；那是越权，不是扩权。"""
    protected = workspace / "genome" / "rules" / "protected.yaml"
    rules = yaml.safe_load(protected.read_text(encoding="utf-8"))
    rules["protected_paths"].append(
        {"path": "repos/order-service/src/order/**", "writable_by": []}
    )
    protected.write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
    commit_all(workspace, "chore: protect order internals")
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        {"repos/order-service/src/order/reserve.py": "# protected\n"},
    )
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)

    await orchestrator.advance(task_id)

    body = "\n".join(
        item.body for item in read_failure_reports(orchestrator.store.task_dir(task_id))
    )
    assert "scope_request" not in body


async def test_a_request_beyond_the_business_code_ceiling_is_refused(
    workspace: Path, library: Path
) -> None:
    """根索引里的 `path` 是人写的。一条挂到 `genome/` 的模块条目会让扩权变成一条绕过禁写
    规则的路——而查"这个模块存不存在"挡不住它:它确实存在。"""
    import yaml

    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    raw = yaml.safe_load((workspace / "genome/knowledge/project-map.yaml").read_text("utf-8"))
    raw["modules"].append({"id": "sneaky", "path": "genome/"})
    (workspace / "genome/knowledge/project-map.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8"
    )
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id, "scope_request": [{"module": "sneaky", "reason": "要"}]},
        {},
    )
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)

    await orchestrator.advance(task_id)

    assert read_grants(workspace, task_id) == []


async def test_a_refused_request_says_why(workspace: Path, library: Path) -> None:
    """静默丢掉的话,员工下一轮不知道自己错在哪,多半原样再申请一次——又一轮 token 白烧。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT
        | {"task_id": task_id, "scope_request": [{"module": "made-up", "reason": "我需要"}]},
        {},
    )
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)

    await orchestrator.advance(task_id)

    body = "\n".join(
        item.body for item in read_failure_reports(orchestrator.store.task_dir(task_id))
    )
    assert "made-up" in body


async def test_hitting_the_grant_cap_escalates_with_a_reason(
    workspace: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "一路申请一路扩"不该变成绕过约束的常规手段。上限撞上时该转人工,并说清是这件事。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT
        | {
            "task_id": task_id,
            "scope_request": [{"module": "inventory-service", "reason": "预占要扣减"}],
        },
        {},
    )
    orchestrator = _orchestrator(workspace, library)
    monkeypatch.setattr(orchestrator.config.limits, "max_scope_grants", 0)
    await orchestrator.advance(task_id)

    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.ESCALATED
    assert "扩权次数已达上限" in (task.escalate_reason or "")


async def test_a_widening_is_a_different_event_from_a_violation(
    workspace: Path, library: Path
) -> None:
    """合并的话"这个项目的员工越权频率"会被合法申请污染——而那个比率正是判断这套机制
    该不该回调的唯一依据。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT
        | {
            "task_id": task_id,
            "scope_request": [{"module": "inventory-service", "reason": "预占要扣减"}],
        },
        {},
    )
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    kinds = [item.kind for item in orchestrator.log.events(task_id)]

    assert LogKind.SCOPE_WIDENED in kinds


# --- 可疑账:变更命中卡 scope 而知识没动(PRD 41) ----------------------------


def _seed_card(workspace: Path) -> None:
    """给 order-service 种一张覆盖全仓的功能卡。**要提交**——员工工作区从 HEAD 开出。"""
    patch_module_map(
        workspace,
        "order-service",
        features=[
            {
                "id": "reserve-flow",
                "summary": "下单预占",
                "scope": ["repos/order-service/**"],
                "card": "features/reserve-flow.md",
            }
        ],
    )
    card = workspace / "genome/knowledge/modules/order-service/features/reserve-flow.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("---\nid: reserve-flow\n---\n\n细节。\n", encoding="utf-8")
    commit_all(workspace, "chore: 种一张功能卡")


async def test_completing_a_task_that_touched_a_card_scope_records_a_suspect(
    workspace: Path, library: Path
) -> None:
    """开发碰了卡的覆盖范围、知识没动 → 终态后可疑账出现一条可疑过期。"""
    _seed_card(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    store = TaskStore(workspace)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)  # plan → DEVELOPING
    await orchestrator.advance(task_id)  # dev 跑完,自述的变更清单落进产物
    store.save(store.get(task_id).evolve(state=TaskState.MERGING))

    task = orchestrator.deliver(task_id, TaskEvent.MERGED)

    assert task.state is TaskState.COMPLETED
    found = pending_suspects(workspace)
    assert [item.card for item in found] == ["order-service/reserve-flow"]
    assert found[0].kind is SuspectKind.STALE
    assert found[0].task_id == task_id
    assert found[0].changed, "命中哪些文件要说得出来,不然消费方只能重查一遍"


async def test_a_cancelled_task_records_no_suspect(workspace: Path, library: Path) -> None:
    """取消的改动没有合入,知识没有理由跟着一份不存在的变更走。"""
    _seed_card(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)

    orchestrator.deliver(task_id, TaskEvent.CANCEL)

    assert pending_suspects(workspace) == ()


async def test_a_nonempty_ledger_blocks_nothing(workspace: Path, library: Path) -> None:
    """软信号不变式:账非空时,任务照常走完同一条链路——状态机根本不认识这本账。"""
    _seed_card(workspace)
    from agentgenome.genome.suspects import Suspect

    record_suspects(
        workspace,
        (
            Suspect(
                kind=SuspectKind.STALE,
                task_id="ag-someone-else",
                card="order-service/reserve-flow",
                changed=("repos/order-service/src/x.py",),
            ),
        ),
    )
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)

    assert (await orchestrator.advance(task_id)).state is TaskState.DEVELOPING
    assert (await orchestrator.advance(task_id)).state is TaskState.UNIT_TESTING
    assert (await orchestrator.advance(task_id)).state is TaskState.READY_TO_COMMIT
