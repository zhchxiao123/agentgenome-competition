"""结对会话:人接管方向盘,车仍在轨道上。

**本文件里最重要的是 `test_a_pair_task_still_walks_the_whole_pipeline`。** 它证明的正是
那条不能破的线——对话能产生变更,但变更入库仍然要走门禁、集测判定、安全提交与审批。
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
from agentgenome.core.task import TaskMode, TaskStore
from agentgenome.jobs.orchestrator import Orchestrator
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    ITEST_DECIDE_RESULT,
    PLAN,
    _record,
    library,
    workspace,
)

runner = CliRunner()

__all__ = ["library", "workspace"]

GREEN_TEST = "def test_reserve():\n    assert True\n"


def _orchestrator(workspace: Path, library: Path) -> Orchestrator:
    pool = AgentPool({"replay": ReplayRuntime(RecordingLibrary(library))})
    return Orchestrator(workspace, pool=pool, runtime_name="replay")


def _submit_interactive(workspace: Path) -> str:
    result = runner.invoke(
        app,
        [
            "task",
            "submit",
            "--requirement",
            "边聊边改权限校验",
            "--interactive",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["id"])


def _arm_plan(library: Path, task_id: str) -> None:
    _record(
        library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id}, {}
    )


def _arm_after_pair(library: Path, task_id: str) -> None:
    """结对之后的那些阶段照常回放——它们和自主执行走的是同一条路。"""
    _record(
        library,
        "decision-employee",
        "itest-decide",
        1,
        ITEST_DECIDE_RESULT | {"task_id": task_id},
        {},
    )


class TestSubmission:
    def test_an_interactive_task_is_marked_so(self, workspace: Path) -> None:
        task_id = _submit_interactive(workspace)

        assert TaskStore(workspace).get(task_id).mode is TaskMode.INTERACTIVE

    def test_a_normal_task_stays_autonomous(self, workspace: Path) -> None:
        result = runner.invoke(
            app,
            [
                "task",
                "submit",
                "--requirement",
                "普通需求",
                "--workspace",
                str(workspace),
                "--json",
            ],
        )

        task_id = json.loads(result.output)["id"]
        assert TaskStore(workspace).get(task_id).mode is TaskMode.AUTONOMOUS


class TestDevelopingIsDrivenByThePerson:
    async def test_the_machine_does_not_dispatch_a_job_of_its_own(
        self, workspace: Path, library: Path
    ) -> None:
        """人在结对会话里改同一个工作区,机器不该同时派一个自主 Job 去改它。"""
        task_id = _submit_interactive(workspace)
        _arm_plan(library, task_id)
        orchestrator = _orchestrator(workspace, library)
        await orchestrator.advance(task_id)  # CREATED → DEVELOPING
        assert orchestrator.store.get(task_id).state is TaskState.DEVELOPING

        # 再推几次也不动——DEVELOPING 由会话驱动。
        for _ in range(3):
            await orchestrator.advance(task_id)

        assert orchestrator.store.get(task_id).state is TaskState.DEVELOPING
        assert orchestrator.bus(orchestrator.store.get(task_id)).by_stage("develop") == ()

    async def test_the_state_machine_gained_no_new_states(self) -> None:
        """交互式只换「这一态谁来执行」,状态机与迁移表一个字没动。"""
        assert {state.value for state in TaskState} == {
            "CREATED",
            "DEVELOPING",
            "UNIT_TESTING",
            "INTEGRATION_TESTING",
            "READY_TO_COMMIT",
            "REVIEWING",
            "MERGING",
            "COMPLETED",
            "ESCALATED",
            "CANCELLED",
        }


class TestTheWholePipeline:
    async def test_a_pair_task_still_walks_the_whole_pipeline(
        self, workspace: Path, library: Path
    ) -> None:
        """**本 PRD 最重要的一条。**

        结对会话结束之后,任务照常进 UNIT_TESTING、照跑真实门禁、照做集测判定,
        一路走到可以提交。人参与不等于少走流程。
        """
        task_id = _submit_interactive(workspace)
        _arm_plan(library, task_id)
        _arm_after_pair(library, task_id)
        orchestrator = _orchestrator(workspace, library)
        await orchestrator.advance(task_id)  # → DEVELOPING

        # 结对会话干的活:往工作区里写一个真实的测试文件。
        worktree = orchestrator.workdir_of(task_id)
        target = worktree / "repos" / "order-service" / "tests" / "test_reserve.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(GREEN_TEST, encoding="utf-8")

        # 会话结束 = dev_done。
        assert orchestrator.finish_pair(task_id).state is TaskState.UNIT_TESTING

        # 推到可提交为止就停:再往下是提交流水线(要 gitleaks),那一段由
        # `test_commit_pipeline.py` 单独验,不是这条测试要证明的事。
        for _ in range(5):
            if orchestrator.store.get(task_id).state is TaskState.READY_TO_COMMIT:
                break
            await orchestrator.advance(task_id)

        assert orchestrator.store.get(task_id).state is TaskState.READY_TO_COMMIT

    async def test_the_gate_really_ran_on_what_the_pair_produced(
        self, workspace: Path, library: Path
    ) -> None:
        """门禁不是走个过场——它跑的是真实的 pytest,对着结对写出来的文件。"""
        task_id = _submit_interactive(workspace)
        _arm_plan(library, task_id)
        _arm_after_pair(library, task_id)
        orchestrator = _orchestrator(workspace, library)
        await orchestrator.advance(task_id)

        worktree = orchestrator.workdir_of(task_id)
        target = worktree / "repos" / "order-service" / "tests" / "test_reserve.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        # 故意写一个挂的用例:门禁必须发现它。
        target.write_text("def test_reserve():\n    assert False\n", encoding="utf-8")

        orchestrator.finish_pair(task_id)
        await orchestrator.advance(task_id)

        # 门禁挂了 → 退回 DEVELOPING,而不是放行。
        assert orchestrator.store.get(task_id).state is TaskState.DEVELOPING
        assert orchestrator.store.get(task_id).fix_rounds == 1


class TestGuards:
    async def test_finishing_a_pair_on_an_autonomous_task_is_refused(
        self, workspace: Path, library: Path
    ) -> None:
        result = runner.invoke(
            app,
            [
                "task",
                "submit",
                "--requirement",
                "普通需求",
                "--workspace",
                str(workspace),
                "--json",
            ],
        )
        task_id = json.loads(result.output)["id"]

        with pytest.raises(ValueError, match="不是交互式"):
            _orchestrator(workspace, library).finish_pair(task_id)

    async def test_finishing_a_pair_outside_developing_is_refused(
        self, workspace: Path, library: Path
    ) -> None:
        task_id = _submit_interactive(workspace)
        orchestrator = _orchestrator(workspace, library)

        with pytest.raises(ValueError, match="不在结对开发中"):
            orchestrator.finish_pair(task_id)


class TestScopeStillApplies:
    async def test_a_pair_change_outside_the_write_scope_is_refused(
        self, workspace: Path, library: Path
    ) -> None:
        """**人在场不放宽边界。**

        「结对的改动不用查」正好是这条线上最容易被开的口子:人参与看起来像一种担保,
        但人也会手滑,而受保护路径存在的理由与谁动的手无关。
        """
        task_id = _submit_interactive(workspace)
        _arm_plan(library, task_id)
        orchestrator = _orchestrator(workspace, library)
        await orchestrator.advance(task_id)

        # 结对会话去动规则层——开发员工的授权范围里没有它。
        worktree = orchestrator.workdir_of(task_id)
        target = worktree / "genome" / "rules" / "architecture.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# 我把规则改了\n", encoding="utf-8")

        task = orchestrator.finish_pair(task_id)

        assert task.state is TaskState.ESCALATED
        assert "越权" in (task.escalate_reason or "")
