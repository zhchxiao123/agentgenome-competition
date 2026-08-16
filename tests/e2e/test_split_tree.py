"""确认落树:子需求诞生、首批开工、母需求状态照旧算出来(PRD 48 issue 02)。

拆分提案被人确认的那一刻发生什么:图校验全绿 → 一笔事务落 N 个子需求 → 提案任务以
CANCELLED 了结 → `requirement_split` 进事件面 → 无前置的子需求自动建首次尝试。
母需求的状态从此要看树:自己唯一的尝试是 CANCELLED 的提案,子需求在跑就是进行中。
"""

from __future__ import annotations

from pathlib import Path

from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.requirement import RequirementState, RequirementStore, derive_forest
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskStore
from agentgenome.todo import service as todo_service
from agentgenome.todo.store import DONE, TodoStore
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    _orchestrator,
    _record,
    _submit,
    library,
    workspace,
)
from tests.e2e.test_split_proposal import SPLIT_PROPOSAL, _split_recorded, _the_split_todo

__all__ = ["library", "workspace"]


async def _walk_to_confirmed(workspace: Path, library: Path) -> tuple[object, str, str]:
    """提交 → 提案 → 人确认。返回 (orchestrator, task_id, requirement_id)。"""
    task_id = _submit(workspace, requirement="做一个兼容 SQLite 的 SQL 引擎")
    _split_recorded(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    todo = _the_split_todo(workspace, task_id)
    submission = todo_service.submit(
        workspace, todo.id, {"approved": True}, actor=todo.assignee
    )
    assert submission.ok, submission.detail
    orchestrator.resume(task_id)
    task = await orchestrator.advance(task_id)
    requirement_id = task.requirement_id
    assert requirement_id
    return orchestrator, task_id, requirement_id


def _forest(workspace: Path) -> dict[str, RequirementState]:
    requirements = RequirementStore(workspace).all()
    attempts: dict[str, list] = {}
    for task in TaskStore(workspace).all_tasks():
        if task.requirement_id:
            attempts.setdefault(task.requirement_id, []).append(task)
    return derive_forest(requirements, attempts)


# --- 确认落树 ----------------------------------------------------------------


async def test_confirming_lands_the_children_and_settles_the_proposal(
    workspace: Path, library: Path
) -> None:
    orchestrator, task_id, requirement_id = await _walk_to_confirmed(workspace, library)

    children = RequirementStore(workspace).children_of(requirement_id)
    assert [child.title for child in children] == ["预占接口", "下单接线", "对账报表"]
    assert all(child.parent_id == requirement_id for child in children)
    # 批内序号换算成了真实 id:1 号依赖 0 号。
    assert children[1].blocked_by == (children[0].id,)
    assert children[0].blocked_by == ()

    # 提案任务以 CANCELLED 了结——这次尝试不会交付,连续性由子需求承载。
    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.CANCELLED

    # 事件面:requirement_split 记在需求 id 名下,payload 带子需求清单。
    split_events = [
        event
        for event in EventLog(workspace).events(requirement_id)
        if event.kind is LogKind.REQUIREMENT_SPLIT
    ]
    assert len(split_events) == 1
    assert split_events[0].payload["children"] == [child.id for child in children]


async def test_unblocked_children_get_their_first_attempt_immediately(
    workspace: Path, library: Path
) -> None:
    """无前置的子需求(0 与 2 号)自动开工;有前置的(1 号)不动。"""
    _, _, requirement_id = await _walk_to_confirmed(workspace, library)

    children = RequirementStore(workspace).children_of(requirement_id)
    store = TaskStore(workspace)
    assert len(store.attempts_of(children[0].id)) == 1
    assert len(store.attempts_of(children[2].id)) == 1
    assert store.attempts_of(children[1].id) == ()

    # 自动发起的尝试快照 = 子需求全文,是普通任务。
    attempt = store.attempts_of(children[0].id)[0]
    assert attempt.requirement == children[0].text
    assert attempt.state is TaskState.CREATED


async def test_the_parent_state_is_in_progress_not_queued(
    workspace: Path, library: Path
) -> None:
    """R3 的语义翻转:母需求自己唯一的尝试是 CANCELLED,平面规则会说排队中——树规则压过它。"""
    _, _, requirement_id = await _walk_to_confirmed(workspace, library)

    assert _forest(workspace)[requirement_id] is RequirementState.IN_PROGRESS


async def test_editing_the_parent_text_does_not_rewrite_children(
    workspace: Path, library: Path
) -> None:
    """快照纪律扩到树上:拆分之后改母需求文本,子需求与提案快照原样。"""
    from agentgenome.core.requirement import revise

    _, task_id, requirement_id = await _walk_to_confirmed(workspace, library)
    children_before = RequirementStore(workspace).children_of(requirement_id)

    revise(workspace, requirement_id, actor="alice", text="改过的母需求文本")

    children_after = RequirementStore(workspace).children_of(requirement_id)
    assert [child.text for child in children_after] == [
        child.text for child in children_before
    ]
    assert TaskStore(workspace).get(task_id).requirement == "做一个兼容 SQLite 的 SQL 引擎"


async def test_confirming_twice_is_idempotent(workspace: Path, library: Path) -> None:
    """崩溃恢复会把确认那一步重放:子需求不能翻倍,首批尝试也不能翻倍。"""
    orchestrator, task_id, requirement_id = await _walk_to_confirmed(workspace, library)

    await orchestrator.recover()

    children = RequirementStore(workspace).children_of(requirement_id)
    assert len(children) == 3
    assert len(TaskStore(workspace).attempts_of(children[0].id)) == 1


# --- 就地编辑(issue 04)------------------------------------------------------


async def test_confirming_with_edited_children_lands_the_edit(
    workspace: Path, library: Path
) -> None:
    """人调整过的提案落的是调整稿:增一个、删一个、改一条依赖,落树与编辑一致。"""
    task_id = _submit(workspace, requirement="做一个兼容 SQLite 的 SQL 引擎")
    _split_recorded(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    todo = _the_split_todo(workspace, task_id)

    submission = todo_service.submit(
        workspace,
        todo.id,
        {
            "approved": True,
            "children": [
                {"title": "语法解析", "text": "SQL 解析器。验收:语法子集全过。"},
                {"title": "执行器", "text": "执行器。验收:select 子集全过。", "blocked_by": [0]},
                {"title": "性能基准", "text": "基准脚本。验收:不落后。", "blocked_by": [1]},
            ],
        },
        actor=todo.assignee,
    )
    assert submission.ok, submission.detail
    orchestrator.resume(task_id)
    await orchestrator.advance(task_id)

    requirement_id = TaskStore(workspace).get(task_id).requirement_id
    children = RequirementStore(workspace).children_of(requirement_id)
    assert [child.title for child in children] == ["语法解析", "执行器", "性能基准"]
    assert children[2].blocked_by == (children[1].id,)


async def test_an_edited_batch_with_a_cycle_is_refused_at_submit(
    workspace: Path, library: Path
) -> None:
    """编辑出环 → 提交被拒且指名问题;待办留在原地可继续改,一个子需求都不落。"""
    from agentgenome.todo.store import PENDING

    task_id = _submit(workspace)
    _split_recorded(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    todo = _the_split_todo(workspace, task_id)

    submission = todo_service.submit(
        workspace,
        todo.id,
        {
            "approved": True,
            "children": [
                {"title": "甲", "text": "甲。", "blocked_by": [1]},
                {"title": "乙", "text": "乙。", "blocked_by": [0]},
            ],
        },
        actor=todo.assignee,
    )

    assert submission.ok is False
    assert "环" in submission.detail
    assert TodoStore(workspace).get(todo.id).state == PENDING
    requirement_id = TaskStore(workspace).get(task_id).requirement_id
    assert RequirementStore(workspace).children_of(requirement_id) == ()


# --- 深度上限 ----------------------------------------------------------------


async def test_a_grandchilds_split_proposal_hits_the_depth_cap(
    workspace: Path, library: Path
) -> None:
    """孙需求的再拆分提案按解析失败处理——树深 ≤2,不打扰人。"""
    _, _, requirement_id = await _walk_to_confirmed(workspace, library)
    store = RequirementStore(workspace)
    children = store.children_of(requirement_id)
    # 子需求(深度 1)再拆一层:孙需求(深度 2)。
    grandchildren = store.create_children(
        children[0].id, [("孙甲", "孙甲。", []), ("孙乙", "孙乙。", [])], priority=5
    )

    # 孙需求名下发起一次尝试,它的解析又提拆分 → 深度触顶。
    task_store = TaskStore(workspace)
    attempt = task_store.create(
        title=grandchildren[0].title,
        requirement=grandchildren[0].text,
        requirement_id=grandchildren[0].id,
    )
    _record(
        library,
        "decision-employee",
        "requirement-analysis",
        1,
        SPLIT_PROPOSAL | {"task_id": attempt.id},
        {},
    )
    orchestrator = _orchestrator(workspace, library)

    task = await orchestrator.advance(attempt.id)

    assert task.plan_retries == 1
    assert not TodoStore(workspace).open_todos(task_id=attempt.id), "触顶的提案不该打扰人"


# --- REST 面:确认之后子需求真的在 serve 下开工 -------------------------------


async def test_confirming_over_rest_lands_the_tree_and_drives_the_children(
    workspace: Path, library: Path
) -> None:
    """整条环在浏览器那一侧走完:确认 → 落树 → 首批子需求被后台驱动推进。

    回放库按 (employee, procedure, round) 编址,子需求的首轮解析会重放同一份提案——
    于是被驱动的子需求各自停在自己的待确认上,这恰好证明两件事:驱动真的发生了,
    递归拆分(深度 1 → 2)是合法的。
    """
    import httpx

    from agentgenome.server.app import create_app
    from tests.fixtures.git import commit_all

    decision_yaml = workspace / "employees" / "decision-employee.yaml"
    decision_yaml.write_text(
        decision_yaml.read_text("utf-8").replace("runtime: claude-code", "runtime: replay"),
        encoding="utf-8",
    )
    commit_all(workspace, "chore: 决策员工切到回放运行时")

    task_id = _submit(workspace, requirement="做一个兼容 SQLite 的 SQL 引擎")
    _split_recorded(library, task_id)

    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        run = await client.post(f"/tasks/{task_id}/run")
        assert run.status_code == 202, run.text
        await app.state.task_runs[f"{workspace}|{task_id}"]
        card = (await client.get("/todos")).json()["items"][0]

        confirmed = await client.post(
            f"/todos/{card['id']}/submit", json={"result": {"approved": True}}
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["ok"] is True
        assert confirmed.json()["task_state"] == "CANCELLED"

        # 首批(0 与 2 号)被驱动:各自跑完首轮解析、停在自己的拆分待办上。
        # 完成的驱动会把自己从注册表里摘掉,所以按值快照等,而不是按键取。
        while app.state.task_runs:
            for run in list(app.state.task_runs.values()):
                await run
        requirement_id = TaskStore(workspace).get(task_id).requirement_id
        detail = (await client.get(f"/requirements/{requirement_id}")).json()
        assert detail["state"] == "in_progress"
        assert detail["children_total"] == 3
        assert detail["children_delivered"] == 0
        assert [child["attempts"] for child in detail["children"]] == [1, 0, 1]
        assert [child["state"] for child in detail["children"]] == [
            "in_progress",
            "queued",
            "in_progress",
        ]
        assert detail["children"][1]["blocked_by"] == [detail["children"][0]["id"]]

        open_split_todos = [
            item for item in (await client.get("/todos")).json()["items"] if item["kind"] == "split"
        ]
        assert len(open_split_todos) == 2, "被驱动的两个子需求各自停在待确认上"


def test_the_cli_tree_matches_the_rest_detail(workspace: Path, library: Path) -> None:
    """`agctl requirement tree` 与 REST 详情同一份内容;CLI 只建不推的那部分要报出来。"""
    import asyncio
    import json as jsonlib

    from typer.testing import CliRunner

    from agentgenome.cli import app as cli_app

    orchestrator, task_id, requirement_id = asyncio.run(
        _walk_to_confirmed(workspace, library)
    )
    result = CliRunner().invoke(
        cli_app,
        ["requirement", "tree", requirement_id, "--workspace", str(workspace), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = jsonlib.loads(result.output)
    assert payload["children_total"] == 3
    assert payload["children_delivered"] == 0
    # 首批已建(0 与 2 号有尝试);它们尚未被推进,但那是"已建",不算待推进。
    assert payload["pending_start"] == []
    assert [child["attempts"] for child in payload["children"]] == [1, 0, 1]

    missing = CliRunner().invoke(
        cli_app,
        ["requirement", "tree", "req-00000000-999", "--workspace", str(workspace), "--json"],
    )
    assert missing.exit_code != 0
    assert "没有这个需求" in missing.output


# --- 兼容 --------------------------------------------------------------------


async def test_a_flat_requirement_derives_exactly_as_before(
    workspace: Path, library: Path
) -> None:
    """无 parent 的存量需求走原路径:derive_forest 与平面规则同一个结果。"""
    task_id = _submit(workspace)
    requirement_id = TaskStore(workspace).get(task_id).requirement_id
    assert requirement_id

    assert _forest(workspace)[requirement_id] is RequirementState.IN_PROGRESS


async def test_confirming_lands_done_todo_and_verdict_file(
    workspace: Path, library: Path
) -> None:
    """确认的裁决与拒绝走同一条落盘纪律:verdict 文件在,待办 DONE,提案原样未动。"""
    import json

    from agentgenome.jobs.split import VERDICT_FILENAME

    orchestrator, task_id, _ = await _walk_to_confirmed(workspace, library)
    todos = [
        item for item in TodoStore(workspace).all_of(task_id) if item.kind == "split"
    ]
    assert len(todos) == 1
    todo = todos[0]
    assert todo.state == DONE
    verdict = json.loads(
        (workspace / todo.output_dir / VERDICT_FILENAME).read_text(encoding="utf-8")
    )
    assert verdict["approved"] is True
    proposal = json.loads(
        (workspace / todo.output_dir / "result.json").read_text(encoding="utf-8")
    )
    assert proposal["split"]["children"][0]["title"] == "预占接口"
