"""重新拆分剩余(PRD 48 issue 05):纠偏不推倒已交付的。

范围 = 没有任何尝试的子需求;已交付、进行中、已搁置的一概不动。确认时被替代的旧子需求
标记搁置、原因指向新批次——不删,记录不被后来的人修饰。重拆分只由人触发。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from agentgenome.core.requirement import RequirementStore
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskStore
from agentgenome.todo import service as todo_service
from agentgenome.todo.store import TodoStore
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    library,
    workspace,
)
from tests.e2e.test_split_tree import _walk_to_confirmed

__all__ = ["library", "workspace"]

runner = CliRunner()


async def test_resplitting_replaces_only_the_unstarted_children(
    workspace: Path, library: Path
) -> None:
    """初拆后 1 号还没开工:重拆分 → 1 号搁置且原因指向新批次,0/2 号原样,新批挂进同一棵树。"""
    orchestrator, _, requirement_id = await _walk_to_confirmed(workspace, library)
    store = RequirementStore(workspace)
    before = store.children_of(requirement_id)

    proposal_task = orchestrator.start_resplit(requirement_id, actor="alice")
    assert proposal_task.requirement_id == requirement_id
    assert "重拆" in proposal_task.title
    # 快照里要有"剩余"的现文本与"保留"的清单——决策员工要照着这份现场重新切。
    assert before[1].title in proposal_task.requirement
    assert before[0].title in proposal_task.requirement

    # 提案(回放里还是那份三子需求的提案)→ 确认。
    await orchestrator.advance(proposal_task.id)
    todo = next(
        item
        for item in TodoStore(workspace).open_todos(task_id=proposal_task.id)
        if item.kind == "split"
    )
    submission = todo_service.submit(workspace, todo.id, {"approved": True}, actor=todo.assignee)
    assert submission.ok, submission.detail
    orchestrator.resume(proposal_task.id)
    landed = await orchestrator.advance(proposal_task.id)
    assert landed.state is TaskState.CANCELLED

    after = store.children_of(requirement_id)
    assert len(after) == 6, "旧 3 个(1 个被搁置)+ 新 3 个,都在树上——不删"
    replaced = store.get(before[1].id)
    assert replaced.parked, "被替代的未开工子需求要搁置"
    new_ids = [child.id for child in after[3:]]
    assert any(new_id in replaced.parked for new_id in new_ids), "搁置原因要指向新批次"
    assert store.get(before[0].id).parked == "", "已开工的原样"
    assert store.get(before[2].id).parked == ""
    # 新批次里无前置的自动开工(只建不推)。
    task_store = TaskStore(workspace)
    assert len(task_store.attempts_of(after[3].id)) == 1


async def test_resplit_refusals_are_identical_over_rest_and_cli(
    workspace: Path, library: Path
) -> None:
    """两类边界拒绝,且 REST 与 CLI 报错正文逐字相同。"""
    from agentgenome.server.app import create_app

    orchestrator, _, requirement_id = await _walk_to_confirmed(workspace, library)
    # 把唯一未开工的 1 号也手动开工 → 没有可重拆的了。
    children = RequirementStore(workspace).children_of(requirement_id)
    TaskStore(workspace).create(
        title=children[1].title,
        requirement=children[1].text,
        requirement_id=children[1].id,
    )

    with pytest.raises(ValueError) as all_started:
        orchestrator.start_resplit(requirement_id, actor="alice")

    flat_id = TaskStore(workspace).get(
        runner_submit(workspace)
    ).requirement_id
    with pytest.raises(ValueError) as not_a_tree:
        orchestrator.start_resplit(flat_id, actor="alice")

    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        rest_all_started = await client.post(f"/requirements/{requirement_id}/resplit")
        assert rest_all_started.status_code == 422
        assert rest_all_started.json()["detail"] == str(all_started.value)

        rest_not_a_tree = await client.post(f"/requirements/{flat_id}/resplit")
        assert rest_not_a_tree.status_code == 422
        assert rest_not_a_tree.json()["detail"] == str(not_a_tree.value)

        missing = await client.post("/requirements/req-00000000-999/resplit")
        assert missing.status_code == 404

    cli_all_started = runner.invoke(
        cli_app,
        ["requirement", "resplit", requirement_id, "--workspace", str(workspace)],
    )
    assert cli_all_started.exit_code != 0
    assert str(all_started.value) in cli_all_started.output

    cli_not_a_tree = runner.invoke(
        cli_app, ["requirement", "resplit", flat_id, "--workspace", str(workspace)]
    )
    assert cli_not_a_tree.exit_code != 0
    assert str(not_a_tree.value) in cli_not_a_tree.output


def runner_submit(workspace: Path) -> str:
    from tests.e2e.test_orchestrator import _submit  # noqa: PLC2701

    return _submit(workspace)


async def test_a_stale_resplit_does_not_land(workspace: Path, library: Path) -> None:
    """确认期间待替代的子需求开工了 → 提案过期,不落树。

    照落的话,新批次与刚开工的那个子需求覆盖同一块活——两份在跑,合并时必然相撞。
    """
    orchestrator, _, requirement_id = await _walk_to_confirmed(workspace, library)
    store = RequirementStore(workspace)
    before = store.children_of(requirement_id)

    proposal_task = orchestrator.start_resplit(requirement_id, actor="alice")
    await orchestrator.advance(proposal_task.id)
    todo = next(
        item
        for item in TodoStore(workspace).open_todos(task_id=proposal_task.id)
        if item.kind == "split"
    )
    # 确认之前,1 号(唯一的替代对象)被人手动开工了。
    TaskStore(workspace).create(
        title=before[1].title,
        requirement=before[1].text,
        requirement_id=before[1].id,
    )
    todo_service.submit(workspace, todo.id, {"approved": True}, actor=todo.assignee)
    orchestrator.resume(proposal_task.id)

    task = await orchestrator.advance(proposal_task.id)

    assert task.state is not TaskState.CANCELLED, "过期的提案不该以'已拆分'了结"
    assert len(store.children_of(requirement_id)) == 3, "一个新子需求都不该落"
    assert store.get(before[1].id).parked == "", "开工了的不该被搁置"


async def test_the_resplit_proposal_walks_the_same_gate(
    workspace: Path, library: Path
) -> None:
    """重拆分提案与初拆同一条链:也停在待确认,也能带反馈打回。"""
    orchestrator, _, requirement_id = await _walk_to_confirmed(workspace, library)

    proposal_task = orchestrator.start_resplit(requirement_id, actor="alice")
    await orchestrator.advance(proposal_task.id)

    todos = TodoStore(workspace).open_todos(task_id=proposal_task.id)
    assert len(todos) == 1 and todos[0].kind == "split"
    submission = todo_service.submit(
        workspace,
        todos[0].id,
        {"approved": False, "feedback": "这刀还是不对"},
        actor=todos[0].assignee,
    )
    assert submission.ok
    orchestrator.resume(proposal_task.id)
    task = await orchestrator.advance(proposal_task.id)
    assert task.state is TaskState.CREATED
    assert task.plan_retries == 1
