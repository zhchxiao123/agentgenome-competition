"""集成测试接进状态机:PRD 05 的验收场景 3(跨模块接口变更)。

改动碰到跨模块契约文件 → 影响规则命中 → `needs_itest=yes` → 集成测试执行 → 失败 →
三件套回传 → 第二轮的上下文里能看到建议与复现命令。

真实的库、真实的 git、真实的迁移表与产物目录;Agent 那一段回放,docker 那一段是一个假的
可执行文件(真子进程)。**docker 自己的行为不在这里验**——见 `itest/env.py` 的覆盖边界。
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
from agentgenome.core.events import LogKind
from agentgenome.core.states import TaskState
from agentgenome.core.task import ItestNeed
from agentgenome.jobs.handlers import STAGE_DEVELOP, STAGE_ITEST
from agentgenome.jobs.orchestrator import Orchestrator
from tests.fixtures.fake_docker import FakeDocker, install_fake_docker_on_path
from tests.fixtures.git import commit_all
from tests.fixtures.mall import materialize_mall
from tests.fixtures.tree import module_ids, patch_contracts, patch_module_map
from tests.fixtures.verification import write_test_verification

runner = CliRunner()

#: 契约文件住在 inventory-service 里(示例仓自带 `api/reserve.yaml`)。
#: 开发员工的授权范围是 `repos/**`,所以它够得着这个文件——把契约放在 Workspace 根的话
#: 这条链路根本走不通,那是 PRD 08 的提交流水线才处理的事。
CONTRACT = "repos/inventory-service/api/reserve.yaml"

PLAN = {
    "task_id": "",
    "producer": "decision-employee",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
    "modules": ["order-service", "inventory-service"],
    "cross_module": True,
    "acceptance": ["预占接口新增 reason 字段"],
    "risks": ["契约变更"],
}

DEV_RESULT = {
    "task_id": "",
    "producer": "dev-employee",
    "created_at": "2026-09-01T10:05:00Z",
    "passed": True,
    "changed_files": [CONTRACT],
    "self_test": {"command": "pytest -q", "exit_code": 0, "passed": True},
    "impact": {"modules": ["inventory-service"], "rationale": "改了预占接口的契约"},
    "questions": [],
}

DIAGNOSIS = {
    "task_id": "",
    "producer": "itest-employee",
    "created_at": "2026-09-01T10:20:00Z",
    "passed": False,
    "kind": "semantic",
    "failures": [
        {
            "case": "order-service: pytest -q itest",
            "message": "集成用例失败(退出码 1)",
            "log_tail": "E   AssertionError: reason 字段缺失",
            "repro_cmd": (
                "docker compose -p ag-x -f itest/compose.yaml run --rm order-service pytest"
            ),
            "suspect_files": ["repos/order-service/src/order/reserve_client.py"],
            "suggestion": "订单侧的预占客户端还没带上新增的 reason 字段,补上之后再跑。",
        }
    ],
    "env": {"project": "ag-x", "submodule_pointers": {}},
}

PASSING_TEST = "def test_ok():\n    assert True\n"

COMPOSE = """\
services:
  order-service:
    image: python:3.12-slim
  inventory-service:
    image: python:3.12-slim
"""


def _record(library: Path, employee: str, procedure: str, round_: int, result: dict, files: dict):
    directory = library / f"{employee}__{procedure}__r{round_}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    for relative, content in files.items():
        target = directory / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


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
            root, module_id, test_cmd="python -m pytest -q tests", itest_cmd="pytest -q itest"
        )
    patch_contracts(
        root,
        interfaces=[
            {
                "id": "order-to-inventory",
                "kind": "http",
                "provider": "inventory-service",
                "consumers": ["order-service"],
                "schema": CONTRACT,
            }
        ],
    )

    for module_id in modules:
        write_test_verification(
            root, module_id, ("unit", ("python", "-m", "pytest", "-q", "tests"))
        )

    (root / "itest").mkdir()
    (root / "itest" / "compose.yaml").write_text(COMPOSE, encoding="utf-8")
    commit_all(root, "chore: 补上契约、集成用例命令与编排文件")
    return root


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "lib"
    path.mkdir()
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(path))
    return path


def _orchestrator(workspace: Path, library: Path) -> Orchestrator:
    return Orchestrator(
        workspace,
        pool=AgentPool({"replay": ReplayRuntime(RecordingLibrary(library))}),
        runtime_name="replay",
    )


def _submit(workspace: Path) -> str:
    result = runner.invoke(
        app,
        [
            "task",
            "submit",
            "--requirement",
            "预占接口加一个 reason 字段",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["id"])


def _arm(library: Path, task_id: str, round_: int = 1) -> None:
    """摆好这一轮:计划 + 开发改契约文件。"""
    _record(
        library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id}, {}
    )
    _record(
        library,
        "dev-employee",
        "code-develop",
        round_,
        DEV_RESULT | {"task_id": task_id},
        {
            CONTRACT: f"openapi: 3.0.0\n# round {round_}: 新增 reason 字段\n",
            "repos/order-service/tests/test_reserve.py": PASSING_TEST,
        },
    )


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    """集成用例失败的那一套假 docker。"""
    return install_fake_docker_on_path(
        tmp_path / "bin",
        monkeypatch,
        exit_codes={"run": 1},
        stdout={"run": "E   AssertionError: reason 字段缺失\n"},
    )


# --- 场景 3 -----------------------------------------------------------------


async def test_touching_a_contract_file_routes_the_task_into_integration_testing(
    workspace: Path, library: Path, docker: FakeDocker
) -> None:
    """契约变更必然触发集成测试,而且是**规则**说的,不是 AI 猜的。

    兜底判定的录制故意不摆:摆了的话,即使规则被绕过这条测试也照样绿。
    """
    task_id = _submit(workspace)
    _arm(library, task_id)
    _record(library, "itest-employee", "itest-run", 1, DIAGNOSIS | {"task_id": task_id}, {})
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):  # plan → dev → gate
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.needs_itest is ItestNeed.YES
    assert task.state is TaskState.INTEGRATION_TESTING
    decision = next(
        event.payload
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.ITEST_DECISION
    )
    assert decision["source"] == "rule"
    # 两条都命中:改了契约文件,而且改动跨了两个模块。命中列表要全,
    # 只报第一条会让人以为其余规则没生效。
    assert decision["matched_rules"] == ["interface-schema", "cross-module"]


async def test_a_failing_integration_test_sends_the_task_back_with_all_three(
    workspace: Path, library: Path, docker: FakeDocker
) -> None:
    """三件套:日志尾部、复现命令、建议。开发员工拿到的应该是一份诊断,不是一堆日志。"""
    task_id = _submit(workspace)
    _arm(library, task_id)
    _arm(library, task_id, round_=2)
    _record(library, "itest-employee", "itest-run", 1, DIAGNOSIS | {"task_id": task_id}, {})
    orchestrator = _orchestrator(workspace, library)

    for _ in range(5):  # plan → dev → gate → itest(挂)→ dev(第二轮)
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.fix_rounds == 1
    bundle = (
        orchestrator.bus(task).by_stage(STAGE_DEVELOP)[1].path / "context-attempt-0.md"
    ).read_text(encoding="utf-8")
    assert "reason 字段" in bundle, "日志尾部没进下一轮"
    assert "docker compose" in bundle, "复现命令没进下一轮"
    assert "预占客户端" in bundle, "修复建议没进下一轮"


async def test_the_environment_is_torn_down_even_though_the_tests_failed(
    workspace: Path, library: Path, docker: FakeDocker
) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id)
    _record(library, "itest-employee", "itest-run", 1, DIAGNOSIS | {"task_id": task_id}, {})
    orchestrator = _orchestrator(workspace, library)

    for _ in range(4):
        await orchestrator.advance(task_id)

    assert docker.subcommands()[-1] == "down"


async def test_the_report_records_which_version_combination_was_tested(
    workspace: Path, library: Path, docker: FakeDocker
) -> None:
    """没有这个字段,历史报告是不可解释的。"""
    task_id = _submit(workspace)
    _arm(library, task_id)
    _record(library, "itest-employee", "itest-run", 1, DIAGNOSIS | {"task_id": task_id}, {})
    orchestrator = _orchestrator(workspace, library)

    for _ in range(4):
        await orchestrator.advance(task_id)

    slot = orchestrator.bus(orchestrator.store.get(task_id)).latest(STAGE_ITEST)
    report = json.loads((slot.path / "itest-report.json").read_text(encoding="utf-8"))
    assert set(report["env"]["submodule_pointers"]) == {
        "repos/order-service",
        "repos/inventory-service",
    }
    assert report["env"]["interfaces"] == ["order-to-inventory"]


async def test_a_broken_diagnosis_does_not_throw_away_the_test_result(
    workspace: Path, library: Path, docker: FakeDocker
) -> None:
    """测试结果是事实,诊断是增值。

    这里回放一份**不合契约**的诊断产物——它会把脚本写的 result.json 覆盖掉。任务照样
    该按集成测试的真实结论走,而不是卡在"产物无效"上空转到升级人工。
    """
    task_id = _submit(workspace)
    _arm(library, task_id)
    _arm(library, task_id, round_=2)
    _record(library, "itest-employee", "itest-run", 1, {"这不是一份合契约的产物": True}, {})
    orchestrator = _orchestrator(workspace, library)

    for _ in range(5):
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.state is not TaskState.ESCALATED, "诊断挂了把整轮集成测试的结果一起丢了"
    assert task.fix_rounds == 1, "集成测试的真实结论(挂了)没有推动状态机"


# --- 全绿的那一轮 -----------------------------------------------------------


async def test_a_green_integration_run_costs_no_tokens(
    workspace: Path, library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有失败就没有东西可诊断。

    **`itest-run` 的录制故意不摆。** 摆了的话"全绿也拉一次 Agent"这个浪费不会有任何
    症状;不摆的话回放运行时会当场炸。
    """
    install_fake_docker_on_path(tmp_path / "bin", monkeypatch)
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(4):
        await orchestrator.advance(task_id)

    assert orchestrator.store.get(task_id).state is TaskState.READY_TO_COMMIT


# --- 环境类失败 -------------------------------------------------------------


async def test_a_missing_compose_file_escalates_instead_of_looping(
    workspace: Path, library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """让开发员工去修一个它修不了的东西,只会白烧三轮 token。"""
    install_fake_docker_on_path(tmp_path / "bin", monkeypatch)
    (workspace / "itest" / "compose.yaml").unlink()
    commit_all(workspace, "chore: 删掉编排文件")
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(4):
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.state is TaskState.ESCALATED
    assert task.fix_rounds == 0, "环境问题不该吃掉修复轮次"


# --- 手工重跑 ---------------------------------------------------------------


async def test_a_manual_rerun_produces_a_report_without_moving_the_task(
    workspace: Path, library: Path, docker: FakeDocker
) -> None:
    """手工重跑是一次观察,不是一次判定。

    能改状态的话,人就有了一个绕过流程把任务推进下一态的后门,而"这个任务为什么变成
    READY_TO_COMMIT"会变得不可追溯。
    """
    task_id = _submit(workspace)
    _arm(library, task_id)
    _record(library, "itest-employee", "itest-run", 1, DIAGNOSIS | {"task_id": task_id}, {})
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)
    before = orchestrator.store.get(task_id)

    result = runner.invoke(app, ["itest", "run", task_id, "--workspace", str(workspace), "--json"])

    assert result.exit_code == 0, result.output
    after = orchestrator.store.get(task_id)
    assert after.state is before.state
    assert after.fix_rounds == before.fix_rounds
    assert json.loads(result.output)["passed"] is False


async def test_a_manual_rerun_does_not_look_like_another_round(
    workspace: Path, library: Path, docker: FakeDocker
) -> None:
    """重跑与自动跑同 stage 的话,轮次定位会把它当成"这一轮已经跑过了",
    于是崩溃恢复直接跳过真正该跑的那一次。"""
    task_id = _submit(workspace)
    _arm(library, task_id)
    _record(library, "itest-employee", "itest-run", 1, DIAGNOSIS | {"task_id": task_id}, {})
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)

    runner.invoke(app, ["itest", "run", task_id, "--workspace", str(workspace)])

    bus = orchestrator.bus(orchestrator.store.get(task_id))
    assert len(bus.by_stage(STAGE_ITEST)) == 1, "人工重跑挤进了自动跑的轮次序列"
    assert len(bus.by_stage("itest-manual")) == 1


async def test_a_manual_rerun_lands_in_the_event_stream(
    workspace: Path, library: Path, docker: FakeDocker
) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id)
    _record(library, "itest-employee", "itest-run", 1, DIAGNOSIS | {"task_id": task_id}, {})
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)

    runner.invoke(app, ["itest", "run", task_id, "--workspace", str(workspace)])

    notes = [
        event
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.NOTE and "人工重跑" in str(event.payload.get("note"))
    ]
    assert notes and notes[-1].actor == "human"


def test_rerunning_an_unknown_task_says_so(workspace: Path) -> None:
    result = runner.invoke(app, ["itest", "run", "ag-x", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_rerunning_a_terminal_task_is_refused(workspace: Path) -> None:
    task_id = _submit(workspace)
    runner.invoke(app, ["task", "cancel", task_id, "--workspace", str(workspace)])

    result = runner.invoke(app, ["itest", "run", task_id, "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_rerunning_without_a_compose_file_reports_it_as_an_environment_problem(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """编排文件缺失是环境问题。命令本身不该崩——它要能把这句话说出来。"""
    install_fake_docker_on_path(tmp_path / "bin", monkeypatch)
    (workspace / "itest" / "compose.yaml").unlink()
    task_id = _submit(workspace)

    result = runner.invoke(app, ["itest", "run", task_id, "--workspace", str(workspace), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "environment"
    assert "compose" in payload["failures"][0]["message"]


# --- 构建闭包的来源 ---------------------------------------------------------


async def test_a_change_spanning_two_modules_builds_both(
    workspace: Path, library: Path, docker: FakeDocker
) -> None:
    """一次跨两个模块的改动,两个模块都进构建闭包。

    **原本这条测的是"计划里没写、但被改过的模块照样进闭包"**,做法是让员工去改一个计划外的
    模块。写权限按任务收窄之后那条路走不通了——那正是收窄要达成的效果,不是这条测试的损失。

    「闭包 = 计划声明 ∪ diff 实际碰到」这条性质没有变(扩权之后 diff 照样可以宽于原计划),
    它挪到了 `test_itest_env.py` 里更直接的那条缝上。这里留下的是仍然走得通的那一半:
    一次改动横跨两个模块时,两个都得用新镜像参与集成测试。
    """
    task_id = _submit(workspace)
    _record(
        library,
        "decision-employee",
        "requirement-analysis",
        1,
        PLAN | {"task_id": task_id, "modules": ["inventory-service", "order-service"]},
        {},
    )
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        # 实际动手时连 order 一起改了。
        {CONTRACT: "openapi: 3.0.0\n", "repos/order-service/tests/test_reserve.py": PASSING_TEST},
    )
    _record(library, "itest-employee", "itest-run", 1, DIAGNOSIS | {"task_id": task_id}, {})
    orchestrator = _orchestrator(workspace, library)

    for _ in range(4):
        await orchestrator.advance(task_id)

    builds = [call for call in docker.calls() if "build" in call]
    assert builds, f"集成测试压根没跑起来:{orchestrator.store.get(task_id).state}"
    services = builds[0][builds[0].index("build") + 1 :]
    assert "order-service" in services, "计划没提到但确实改过的模块用的是旧镜像"


# --- 灌测试数据 -------------------------------------------------------------


async def test_seed_data_is_loaded_before_the_cases_run(
    workspace: Path, library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US 12:每次跑的初始状态一致。

    没灌上就开跑的话,失败会以「业务逻辑不对」的样子出现,而实际原因是库里没数据——
    开发员工会去改一段本来没问题的代码,而且每一轮都改不好。
    """
    docker = install_fake_docker_on_path(tmp_path / "bin", monkeypatch)
    config = workspace / "agentgenome.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\nitest:\n  seed_cmd: python seed.py\n  seed_service: order-service\n",
        encoding="utf-8",
    )
    commit_all(workspace, "chore: 配上灌数据命令")
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(4):
        await orchestrator.advance(task_id)

    calls = docker.calls()
    seeded = next(index for index, call in enumerate(calls) if "seed.py" in call)
    cased = next(index for index, call in enumerate(calls) if "pytest" in call)
    assert seeded < cased, "灌数据跑在用例之后,等于没灌"


async def test_a_failing_seed_escalates_instead_of_looping(
    workspace: Path, library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """灌不进去是环境问题,不是代码问题。"""
    install_fake_docker_on_path(tmp_path / "bin", monkeypatch, exit_codes={"run": 1})
    config = workspace / "agentgenome.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\nitest:\n  seed_cmd: python seed.py\n  seed_service: order-service\n",
        encoding="utf-8",
    )
    commit_all(workspace, "chore: 配上灌数据命令")
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(4):
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.state is TaskState.ESCALATED
    assert task.fix_rounds == 0, "环境问题不该吃掉修复轮次"
