"""门禁接进状态机:结构化条目置顶、环境类不进修复循环、三轮循环。

走真实的门禁(真的跑 pytest)+ 回放的开发员工。门禁这一段连回放都不需要——它是确定性的。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.agents.pool import AgentPool
from agentgenome.agents.recording import RecordingLibrary
from agentgenome.agents.replay import ReplayRuntime
from agentgenome.cli import app
from agentgenome.core.states import TaskState
from agentgenome.jobs.orchestrator import Orchestrator
from agentgenome.jobs.reports import read_failure_reports
from tests.fixtures.git import commit_all
from tests.fixtures.mall import materialize_mall
from tests.fixtures.tree import module_ids, patch_module_map
from tests.fixtures.verification import GateCommand, write_test_verification

runner = CliRunner()

PLAN = {
    "task_id": "",
    "producer": "decision-employee",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
    "modules": ["order-service"],
    "cross_module": False,
    "acceptance": ["下单时调用预占接口"],
}

DEV_RESULT = {
    "task_id": "",
    "producer": "dev-employee",
    "created_at": "2026-09-01T10:05:00Z",
    "passed": True,
    "changed_files": ["repos/order-service/tests/test_reserve.py"],
    "self_test": {"command": "pytest -q", "exit_code": 0, "passed": True},
    "impact": {"modules": ["order-service"], "rationale": "只动了订单域"},
    "questions": [],
}

ITEST_DECIDE = {
    "task_id": "",
    "producer": "decision-employee",
    "created_at": "2026-09-01T10:06:00Z",
    "passed": True,
    "needs_itest": False,
    "reason": "只动了订单域的内部实现",
}

PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_reserve():\n    assert 0 == 3, '预占没实现'\n"
OTHER_FAILING = "def test_other():\n    assert 0 == 9, '这是另一个坏掉的地方'\n"
THIRD_FAILING = "def test_third():\n    assert 0 == 7, '第三处'\n"

#: 命令负责**产出** JUnit XML,项目地图的 `junit_xml_path` 负责说它落在哪。
#: 两者都要写:往用户的命令上拼参数太脆,而不写命令就永远拿不到结构化条目。
UNIT_ONLY: GateCommand = (
    "unit",
    ("python", "-m", "pytest", "-q", "tests", "--junitxml=reports/junit.xml"),
)
MISSING_TOOL: GateCommand = ("external-scan", ("definitely-not-installed", "--scan"))


def _record(library: Path, employee: str, procedure: str, round_: int, result: dict, files: dict):
    directory = library / f"{employee}__{procedure}__r{round_}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    for relative, content in files.items():
        target = directory / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "lib"
    path.mkdir()
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(path))
    return path


def _workspace(tmp_path: Path, gate: GateCommand, max_fix_rounds: int = 3) -> Path:
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        app,
        [
            "init", "--local-only",
            str(root),
            "--name",
            "mall",
            "--repo",
            mall["order-service"].remote_url,
            "--repo",
            mall["inventory-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output

    modules = module_ids(root)
    for module_id in modules:
        patch_module_map(
            root,
            module_id,
            test_cmd="python -m pytest -q tests",
            junit_xml_path="reports/junit.xml",
        )

    for module_id in modules:
        write_test_verification(root, module_id, gate)

    rules = root / "genome" / "rules" / "architecture.md"
    rules.write_text(
        rules.read_text(encoding="utf-8").replace(
            "max_fix_rounds: 3", f"max_fix_rounds: {max_fix_rounds}"
        ),
        encoding="utf-8",
    )
    commit_all(root, "chore: 门禁配置")
    return root


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    return _workspace(tmp_path, UNIT_ONLY)


def _orchestrator(workspace: Path, library: Path) -> Orchestrator:
    pool = AgentPool({"replay": ReplayRuntime(RecordingLibrary(library))})
    return Orchestrator(workspace, pool=pool, runtime_name="replay")


def _submit(workspace: Path) -> str:
    result = runner.invoke(
        app,
        [
            "task",
            "submit",
            "--requirement",
            "下单时要预占库存",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )
    return str(json.loads(result.output)["id"])


def _arm(library: Path, task_id: str, rounds: dict[int, str]) -> None:
    _record(
        library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id}, {}
    )
    for round_, test in rounds.items():
        _record(
            library,
            "dev-employee",
            "code-develop",
            round_,
            DEV_RESULT | {"task_id": task_id},
            {"repos/order-service/tests/test_reserve.py": test},
        )
        # 门禁通过那一刻会走 needs_itest 判定。这条链路只动了 repos/order-service/,影响规则
        # 一条都不命中,于是判定落到架构员工的兜底 Procedure 上。
        _record(
            library,
            "decision-employee",
            "itest-decide",
            round_,
            ITEST_DECIDE | {"task_id": task_id},
            {},
        )


# --- 修复循环:结构化条目置顶 -----------------------------------------------


async def test_a_failing_gate_produces_structured_entries(workspace: Path, library: Path) -> None:
    """把两千行日志丢回去,员工会从头读到尾然后可能抓错重点。"""
    task_id = _submit(workspace)
    _arm(library, task_id, {1: FAILING})
    orchestrator = _orchestrator(workspace, library)
    for _ in range(3):
        await orchestrator.advance(task_id)

    bus = orchestrator.bus(orchestrator.store.get(task_id))
    report = json.loads(
        (bus.latest("unit-gate").path / "gate-report.json").read_text(encoding="utf-8")
    )
    assert report["passed"] is False
    assert report["kind"] == "semantic"
    failure = report["gates"][0]["failures"][0]
    assert "test_reserve" in failure["test"]
    assert failure["file"], "拿不到出问题的文件,员工只能从头猜"


async def test_the_report_reaches_the_next_round_on_top(workspace: Path, library: Path) -> None:
    """报告注入下一轮的上下文时置顶,引导员工先看断言差异再看日志。"""
    task_id = _submit(workspace)
    _arm(library, task_id, {1: FAILING, 2: PASSING})
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)

    bus = orchestrator.bus(orchestrator.store.get(task_id))
    bundle = (bus.by_stage("develop")[1].path / "context-attempt-0.md").read_text("utf-8")
    assert "test_reserve" in bundle, "上一轮的失败用例没进下一轮的上下文"
    assert bundle.index("test_reserve") < bundle.index("基因组切片"), "失败报告没有置顶"


async def test_the_loop_closes_when_the_code_is_fixed(workspace: Path, library: Path) -> None:
    """只测拦得住不测放得过,等于没测。"""
    task_id = _submit(workspace)
    _arm(library, task_id, {1: FAILING, 2: PASSING})
    orchestrator = _orchestrator(workspace, library)
    for _ in range(5):
        await orchestrator.advance(task_id)

    assert orchestrator.store.get(task_id).state is TaskState.READY_TO_COMMIT


# --- 三轮 -------------------------------------------------------------------


async def test_the_third_round_sees_all_three_reports(workspace: Path, library: Path) -> None:
    """全量注入,不只是最近一轮——只给最近一轮会导致"改 A 坏 B、改 B 坏 A"的震荡。"""
    task_id = _submit(workspace)
    _arm(library, task_id, {1: FAILING, 2: OTHER_FAILING, 3: PASSING})
    orchestrator = _orchestrator(workspace, library)
    for _ in range(6):  # plan → dev → gate(挂) → dev → gate(挂) → dev(第三轮)
        await orchestrator.advance(task_id)

    reports = read_failure_reports(orchestrator.store.task_dir(task_id))
    assert [item.round for item in reports] == [1, 2]

    bus = orchestrator.bus(orchestrator.store.get(task_id))
    bundle = (bus.by_stage("develop")[2].path / "context-attempt-0.md").read_text("utf-8")
    assert "::test_reserve" in bundle, "第一轮的失败没进第三轮的上下文"
    assert "::test_other" in bundle, "第二轮的失败没进第三轮的上下文"
    # 按段落标题比,不按用例名——`tests.test_reserve::test_other` 里也含 `test_reserve`。
    assert bundle.index("第 2 轮失败") < bundle.index("第 1 轮失败"), "没有按轮次倒序"


async def test_a_new_failure_is_reported_as_a_regression(workspace: Path, library: Path) -> None:
    """ "这轮是修好了还是修坏了别的"——这个问题只看单轮报告答不出来。"""
    task_id = _submit(workspace)
    _arm(library, task_id, {1: FAILING, 2: OTHER_FAILING})
    orchestrator = _orchestrator(workspace, library)
    for _ in range(5):
        await orchestrator.advance(task_id)

    bus = orchestrator.bus(orchestrator.store.get(task_id))
    report = json.loads(
        (bus.by_stage("unit-gate")[1].path / "gate-report.json").read_text(encoding="utf-8")
    )
    assert [item["test"] for item in report["regressions"]] == ["tests.test_reserve::test_other"]
    assert [item["test"] for item in report["fixed"]] == ["tests.test_reserve::test_reserve"]


async def test_the_regression_is_called_out_in_the_failure_report(
    workspace: Path, library: Path
) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id, {1: FAILING, 2: OTHER_FAILING})
    orchestrator = _orchestrator(workspace, library)
    for _ in range(5):
        await orchestrator.advance(task_id)

    body = read_failure_reports(orchestrator.store.task_dir(task_id))[-1].body
    assert "引入了 1 个新失败" in body


# --- 环境类失败 -------------------------------------------------------------


@pytest.fixture
def broken_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    return _workspace(tmp_path, MISSING_TOOL)


async def test_a_missing_tool_escalates_without_burning_a_round(
    broken_env: Path, library: Path
) -> None:
    """让 AI 反复尝试去修"环境里没装 gitleaks"是最典型的预算浪费方式。"""
    task_id = _submit(broken_env)
    _arm(library, task_id, {1: PASSING})
    orchestrator = _orchestrator(broken_env, library)
    for _ in range(3):
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.state is TaskState.ESCALATED
    assert task.fix_rounds == 0, "环境问题吃掉了一轮修复额度"
    assert "环境" in (task.escalate_reason or "")


async def test_a_semantic_failure_still_burns_a_round(workspace: Path, library: Path) -> None:
    """与上一条对照:代码问题该走修复循环,不该被误判成环境问题。"""
    task_id = _submit(workspace)
    _arm(library, task_id, {1: FAILING})
    orchestrator = _orchestrator(workspace, library)
    for _ in range(3):
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.state is TaskState.DEVELOPING
    assert task.fix_rounds == 1


# --- 事件流 -----------------------------------------------------------------


async def test_the_gate_run_lands_in_the_event_stream(workspace: Path, library: Path) -> None:
    """质量把关的历史要可查。"""
    task_id = _submit(workspace)
    _arm(library, task_id, {1: PASSING})
    orchestrator = _orchestrator(workspace, library)
    for _ in range(3):
        await orchestrator.advance(task_id)

    gate_events = [
        event for event in orchestrator.log.events(task_id) if event.kind.value == "gate_result"
    ]
    assert len(gate_events) == 1
    assert gate_events[0].payload["module"] == "order-service"
    assert gate_events[0].payload["gates"][0]["id"] == "unit"
