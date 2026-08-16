"""决策员工:"这一个任务怎么打"归谁。

移交是**排他**的。两个员工都能干同一道 plan 类工序时,事件面上"该质询谁"没有答案——
而复盘时那恰恰是唯一想问的问题。所以这里验三件事:归因真的换了人、排他真的被机器判、
存量工作区真的有一条能走的迁移路。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.core.events import LogKind
from agentgenome.core.states import TaskState
from agentgenome.employees import load_employees
from agentgenome.genome.roster import DECISION_EMPLOYEE, PLAN_PROCEDURES, scaffold_roster
from agentgenome.jobs.orchestrator import Orchestrator
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    PASSING_TEST,
    _arm,
    _orchestrator,
    _submit,
    library,
    workspace,
)

__all__ = ["library", "workspace"]

runner = CliRunner()


def _definition(root: Path, employee_id: str) -> dict:
    payload = yaml.safe_load(
        (root / "employees" / f"{employee_id}.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    return payload


def _actors(orchestrator: Orchestrator, task_id: str, procedure: str) -> list[str]:
    """事件面上,这道工序是谁干的。**从事件读,不从产物读**——归因就住在事件面。"""
    return [
        event.actor
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.JOB_STARTED
        and str(event.payload.get("procedure_ref", "")).startswith(f"{procedure}@")
    ]


# --- 定义 -------------------------------------------------------------------


def test_the_workspace_gets_a_decision_employee(workspace: Path) -> None:
    assert (workspace / "employees" / f"{DECISION_EMPLOYEE}.yaml").is_file()
    assert (workspace / "employees" / "prompts" / "decision.md").is_file()


def test_the_plan_procedures_moved_off_the_architect(workspace: Path) -> None:
    """移交必须是排他的:两个员工都能干的话,归因就失去了意义。"""
    arch = _definition(workspace, "arch-employee")["procedures"]
    decision = _definition(workspace, DECISION_EMPLOYEE)["procedures"]

    assert sorted(decision) == sorted(PLAN_PROCEDURES)
    for procedure in PLAN_PROCEDURES:
        assert procedure not in arch


def test_the_decision_employee_decides_but_does_not_do(workspace: Path) -> None:
    """它决定怎么打,不下场打:业务代码与基因组一律禁写。"""
    registry = load_employees(workspace / "employees")
    decision = registry.get(DECISION_EMPLOYEE)

    assert decision.may_write("tasks/ag-1/plan.yaml", task_id="ag-1") is True
    assert decision.may_write("repos/order-service/src/app.py") is False
    assert decision.may_write("genome/knowledge/project-map.yaml") is False


# --- 归因 -------------------------------------------------------------------


async def test_the_plan_is_attributed_to_the_decision_employee(
    workspace: Path, library: Path
) -> None:
    """审计员要在事件面看到"这个任务怎么打"归谁。这是本 PRD 的目的本身。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)

    assert _actors(orchestrator, task_id, "requirement-analysis") == [DECISION_EMPLOYEE]


async def test_the_itest_decision_is_attributed_to_the_decision_employee(
    workspace: Path, library: Path
) -> None:
    """集测判定同理——它也是"这一次怎么打"的一部分,不是项目认知。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):  # plan → dev → gate(门禁过了才问要不要跑集测)
        await orchestrator.advance(task_id)

    assert _actors(orchestrator, task_id, "itest-decide") == [DECISION_EMPLOYEE]


async def test_the_architect_does_not_show_up_in_a_plain_dev_task(
    workspace: Path, library: Path
) -> None:
    """架构员工收敛为治理专职:一次普通研发任务里它一次都不该出场。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    actors = {
        event.actor
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.JOB_STARTED
    }
    assert "arch-employee" not in actors


# --- 排他与迁移 -------------------------------------------------------------


def _make_stale(root: Path) -> None:
    """把工作区改回旧形状:plan 类工序回到架构员工名下,决策员工的定义删掉。

    这正是一个存量工作区升级上来时的样子——脚手架不覆盖已有文件,所以旧白名单会原样留着。
    """
    (root / "employees" / f"{DECISION_EMPLOYEE}.yaml").unlink()
    arch = root / "employees" / "arch-employee.yaml"
    arch.write_text(
        arch.read_text(encoding="utf-8").replace(
            "procedures: [experience-distill]",
            "procedures: [requirement-analysis, itest-decide, experience-distill]",
        ),
        encoding="utf-8",
    )


def test_a_stale_roster_is_refused_with_a_way_out(workspace: Path) -> None:
    """加载不了要给得出下一步。一条不告诉人怎么修的报错就是一堵墙。"""
    # 旧形状 + 新脚手架跑过一次,正是升级到一半的样子:新员工被补进来了,旧主人的白名单
    # 却没人去改——脚手架不覆盖已存在的文件,那是刻意的。
    _make_stale(workspace)
    scaffold_roster(workspace)

    result = runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)])

    assert result.exit_code == 1
    assert "requirement-analysis" in result.output
    assert "agctl roster migrate" in result.output


def test_migrate_converges_a_stale_roster(workspace: Path) -> None:
    _make_stale(workspace)
    arch_path = workspace / "employees" / "arch-employee.yaml"
    arch_path.write_text(
        arch_path.read_text(encoding="utf-8") + "\nlimits:\n  job_timeout_s: 1800\n",
        encoding="utf-8",
    )
    limits = "  job_timeout_s: 1800"

    result = runner.invoke(app, ["roster", "migrate", "--workspace", str(workspace), "--yes"])

    assert result.exit_code == 0, result.output
    arch = (workspace / "employees" / "arch-employee.yaml").read_text(encoding="utf-8")
    assert "requirement-analysis" not in arch
    assert limits in arch, "使用者调过的限额被抹掉的话,这条命令就没人敢跑了"
    assert (workspace / "employees" / f"{DECISION_EMPLOYEE}.yaml").is_file()
    assert runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)]).exit_code == 0


def test_migrate_shows_the_diff_before_touching_anything(workspace: Path) -> None:
    """看不到 diff 的话,人只能在"信它"和"不跑它"之间二选一。"""
    _make_stale(workspace)

    result = runner.invoke(
        app, ["roster", "migrate", "--workspace", str(workspace)], input="n\n"
    )

    assert result.exit_code != 0, "没确认就不该写盘"
    assert "-procedures: [requirement-analysis, itest-decide, experience-distill]" in result.output
    assert "+procedures: [experience-distill]" in result.output
    assert not (workspace / "employees" / f"{DECISION_EMPLOYEE}.yaml").exists()


def test_migrate_is_a_no_op_on_a_current_workspace(workspace: Path) -> None:
    result = runner.invoke(app, ["roster", "migrate", "--workspace", str(workspace)])

    assert result.exit_code == 0
    assert "无需迁移" in result.output


@pytest.mark.parametrize("procedure", PLAN_PROCEDURES)
def test_the_procedure_declares_its_own_exclusivity(workspace: Path, procedure: str) -> None:
    """排他标在工序上,不标在员工上——加一个员工时不必记得同时改另一份声明。"""
    manifest = yaml.safe_load(
        (workspace / "genome" / "procedures" / procedure / "procedure.yaml").read_text("utf-8")
    )

    assert manifest["ownership"] == "plan"


async def test_a_stale_roster_stops_the_task_before_any_job_is_dispatched(
    workspace: Path, library: Path
) -> None:
    """**归属没收敛就不派活。**

    照派的话,事件面上"这个决定该质询谁"会记下一个说不清的答案——而事件是不可改的,
    错了就永远错在那儿。校验命令能报出来是不够的:没有人会在每次提交任务之前先跑一遍。
    """
    _make_stale(workspace)
    scaffold_roster(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)

    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.ESCALATED
    assert "agctl roster migrate" in task.escalate_reason, "升级原因要说得出下一步"
    assert not [
        event for event in orchestrator.log.events(task_id) if event.kind is LogKind.JOB_STARTED
    ], "花名册没收敛就已经派了活,那条事件的归因是说不清的"


async def test_a_converged_roster_dispatches_normally(workspace: Path, library: Path) -> None:
    """拦住要有对照:不然一个把所有任务都拦下来的实现也能让上面那条变绿。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)

    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.DEVELOPING
