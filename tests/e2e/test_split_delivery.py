"""交付驱动到收口(PRD 48 issue 03)。

树自己往前走的那半边:一个子需求交付,解锁它挡着的兄弟;全部交付,母需求名下自动
长出收口尝试;收口的终局决定母需求的终局。升级人工的子需求阻断下游不烧钱;搁置冻结
派发;CLI 单步语义只建不推——那是记录在案的不对称(R4),这里有测试钉住,防止将来
被当 bug"修"成常驻循环。
"""

from __future__ import annotations

from pathlib import Path

from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.requirement import RequirementState, RequirementStore, revise
from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.task import Task, TaskStore
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    library,
    workspace,
)
from tests.e2e.test_split_tree import _forest, _walk_to_confirmed

__all__ = ["library", "workspace"]


def _complete(orchestrator, task_id: str) -> Task:
    """把一次尝试走到 COMPLETED:真实迁移表、真实终态副作用(封存、树钩子)。"""
    store = orchestrator.store
    store.save(store.get(task_id).evolve(state=TaskState.MERGING))
    return orchestrator.deliver(task_id, TaskEvent.MERGED)


def _only_attempt(workspace: Path, requirement_id: str) -> Task:
    attempts = TaskStore(workspace).attempts_of(requirement_id)
    assert len(attempts) == 1, f"该有且只有一次尝试: {[item.id for item in attempts]}"
    return attempts[0]


# --- 交付解锁 ----------------------------------------------------------------


async def test_a_delivery_unlocks_exactly_the_blocked_sibling(
    workspace: Path, library: Path
) -> None:
    """0 号交付 → 只有 blocked_by=[0] 的 1 号自动开工;2 号(已开工)不重复建。"""
    orchestrator, _, requirement_id = await _walk_to_confirmed(workspace, library)
    children = RequirementStore(workspace).children_of(requirement_id)
    store = TaskStore(workspace)
    assert store.attempts_of(children[1].id) == ()

    _complete(orchestrator, _only_attempt(workspace, children[0].id).id)

    assert len(store.attempts_of(children[1].id)) == 1
    assert len(store.attempts_of(children[2].id)) == 1
    unlocked = store.attempts_of(children[1].id)[0]
    assert unlocked.requirement == children[1].text
    # 事件面说清是谁解锁的:交付驱动派发,不是人提交的。
    created = [
        event
        for event in EventLog(workspace).events(unlocked.id)
        if event.kind is LogKind.TASK_CREATED
    ]
    assert created and created[0].payload.get("via") == "split-dispatch"


async def test_an_escalated_child_blocks_downstream_without_burning(
    workspace: Path, library: Path
) -> None:
    """0 号升级人工 → 1 号不开工;人在 0 号需求下再试并交付 → 1 号开工(链在需求层)。"""
    orchestrator, _, requirement_id = await _walk_to_confirmed(workspace, library)
    children = RequirementStore(workspace).children_of(requirement_id)
    store = TaskStore(workspace)

    orchestrator.escalate(_only_attempt(workspace, children[0].id).id, reason="环境坏了")
    orchestrator.escalate(_only_attempt(workspace, children[2].id).id, reason="环境坏了")

    assert store.attempts_of(children[1].id) == ()
    # 全部开工的子需求都停在升级人工、没有任何东西在跑:母需求如实回到排队中。
    assert _forest(workspace)[requirement_id] is RequirementState.QUEUED

    # 人接管:同一个子需求下再试一次(手动发起),交付后下游解锁。
    retry = store.create(
        title=children[0].title,
        requirement=children[0].text,
        requirement_id=children[0].id,
    )
    _complete(orchestrator, retry.id)

    assert len(store.attempts_of(children[1].id)) == 1


# --- 收口 --------------------------------------------------------------------


async def _deliver_all_children(orchestrator, workspace: Path, requirement_id: str) -> list:
    children = RequirementStore(workspace).children_of(requirement_id)
    store = TaskStore(workspace)
    _complete(orchestrator, _only_attempt(workspace, children[0].id).id)
    _complete(orchestrator, _only_attempt(workspace, children[2].id).id)
    _complete(orchestrator, store.attempts_of(children[1].id)[0].id)
    return list(children)


async def test_the_last_delivery_births_the_closing_attempt(
    workspace: Path, library: Path
) -> None:
    """全部子需求交付 → 母需求名下自动多一次尝试,快照 = 原文 + 各子需求交付摘要。"""
    orchestrator, proposal_task_id, requirement_id = await _walk_to_confirmed(
        workspace, library
    )
    children = await _deliver_all_children(orchestrator, workspace, requirement_id)

    own = [
        item
        for item in TaskStore(workspace).attempts_of(requirement_id)
        if item.id != proposal_task_id
    ]
    assert len(own) == 1, "该有且只有一次收口尝试"
    closing = own[0]
    assert closing.state is TaskState.CREATED
    assert "做一个兼容 SQLite 的 SQL 引擎" in closing.requirement, "母需求原文必须在快照里"
    for child in children:
        assert child.title in closing.requirement, "每个子需求的交付摘要必须在快照里"
    assert "回归资产" in closing.requirement, "收口的职责要写清:验收固化成回归资产"
    # 母需求此刻是进行中(收口在跑),不是已交付——"所有子需求都交付了"不偷换。
    assert _forest(workspace)[requirement_id] is RequirementState.IN_PROGRESS


async def test_the_closing_outcome_decides_the_parent(
    workspace: Path, library: Path
) -> None:
    """收口 COMPLETED → 母需求已交付;收口 ESCALATED → 母需求排队中。"""
    orchestrator, proposal_task_id, requirement_id = await _walk_to_confirmed(
        workspace, library
    )
    await _deliver_all_children(orchestrator, workspace, requirement_id)
    closing = next(
        item
        for item in TaskStore(workspace).attempts_of(requirement_id)
        if item.id != proposal_task_id
    )

    _complete(orchestrator, closing.id)
    assert _forest(workspace)[requirement_id] is RequirementState.DELIVERED


async def test_an_escalated_closing_returns_the_parent_to_queued_and_is_not_recreated(
    workspace: Path, library: Path
) -> None:
    """收口升级人工:母需求回排队等人;重启恢复**不**自动再造一个收口——机器认输了,
    下一步是人的(再试一次),不是机器换个马甲再来。"""
    orchestrator, proposal_task_id, requirement_id = await _walk_to_confirmed(
        workspace, library
    )
    await _deliver_all_children(orchestrator, workspace, requirement_id)
    closing = next(
        item
        for item in TaskStore(workspace).attempts_of(requirement_id)
        if item.id != proposal_task_id
    )
    orchestrator.escalate(closing.id, reason="全量回归挂了")

    assert _forest(workspace)[requirement_id] is RequirementState.QUEUED

    await orchestrator.recover()
    own = TaskStore(workspace).attempts_of(requirement_id)
    assert len(own) == 2, "恢复不该再造收口:一次提案 + 一次升级了的收口"


async def test_recovery_resumes_a_dropped_dispatch(workspace: Path, library: Path) -> None:
    """崩在"子需求交付了但兄弟还没解锁"的缝里:重启恢复要把树接上。"""
    orchestrator, _, requirement_id = await _walk_to_confirmed(workspace, library)
    children = RequirementStore(workspace).children_of(requirement_id)
    store = TaskStore(workspace)
    # 模拟钩子没跑成的崩溃:直接把 0 号的尝试写成 COMPLETED,不走 deliver。
    attempt = _only_attempt(workspace, children[0].id)
    store.save(attempt.evolve(state=TaskState.COMPLETED))
    assert store.attempts_of(children[1].id) == ()

    await orchestrator.recover()

    assert len(store.attempts_of(children[1].id)) == 1


# --- 搁置冻结派发 ------------------------------------------------------------


async def test_parking_the_parent_freezes_dispatch_until_resumed(
    workspace: Path, library: Path
) -> None:
    orchestrator, _, requirement_id = await _walk_to_confirmed(workspace, library)
    children = RequirementStore(workspace).children_of(requirement_id)
    store = TaskStore(workspace)

    revise(workspace, requirement_id, actor="alice", park="先停一停")
    _complete(orchestrator, _only_attempt(workspace, children[0].id).id)

    assert store.attempts_of(children[1].id) == (), "搁置的母需求不再自动开工新子需求"

    revise(workspace, requirement_id, actor="alice", resume=True)
    orchestrator.sweep_requirement_tree(requirement_id)

    assert len(store.attempts_of(children[1].id)) == 1, "恢复后派发继续"


# --- R4:CLI 只建不推,有测试钉住 --------------------------------------------


async def test_the_unlocked_sibling_is_created_but_not_driven(
    workspace: Path, library: Path
) -> None:
    """单步语义下解锁只**建**尝试:没有任何 Job 被派出,烧掉的 token 为零。

    这是记录在案的不对称(PRD 48 R4),不是缺陷——serve 的表面才承诺自动推进。
    这条红了的话,先去读 PRD 再动手:把它"修"成 CLI 里的常驻驱动循环恰恰是错的。
    """
    orchestrator, _, requirement_id = await _walk_to_confirmed(workspace, library)
    children = RequirementStore(workspace).children_of(requirement_id)

    _complete(orchestrator, _only_attempt(workspace, children[0].id).id)

    unlocked = TaskStore(workspace).attempts_of(children[1].id)[0]
    assert unlocked.state is TaskState.CREATED
    assert unlocked.tokens_used == 0
    jobs = [
        event
        for event in EventLog(workspace).events(unlocked.id)
        if event.kind is LogKind.JOB_STARTED
    ]
    assert jobs == [], "只建不推:不该有任何 Job 被派出"
