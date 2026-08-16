"""人工闸门:草案落盘、答复写入、重启恢复。

全异步那条承诺的落地点。人敲完命令之后不该被迫守着终端——尤其是这个闸门前面还有一段几十
分钟的扫描。

**草案与答复分开存。** 合成一份的话,人一改就把系统原本建议了什么覆盖掉了,而"哪里是人改的"
恰恰是这份产物最有价值的部分。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome.core.events import EventLog
from agentgenome.core.genome_driver import GenomeDriver, NotWaiting
from agentgenome.core.genome_gate import (
    AnswerInvalid,
    NoDraft,
    module_boundary_answer,
    read_answer,
    read_draft,
    write_answer,
    write_draft,
)
from agentgenome.core.genome_task import (
    GenomeTaskKind,
    GenomeTaskState,
    GenomeTaskStore,
    Origin,
)
from agentgenome.core.genome_transitions import GenomeEvent

DRAFT = {
    "modules": [
        {"id": "order-service", "path": "repos/order-service/", "rationale": "独立的构建文件"},
        {
            "id": "inventory-service",
            "path": "repos/inventory-service/",
            "rationale": "独立的构建文件",
        },
    ]
}

ANSWER = {
    "modules": [{"id": "orders", "path": "repos/order-service/"}],
    "note": "把两个合成一个,它们共用一套迁移",
}


@pytest.fixture
def store(tmp_path: Path) -> GenomeTaskStore:
    return GenomeTaskStore(tmp_path)


@pytest.fixture
def task_id(store: GenomeTaskStore) -> str:
    return store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN).id


# --- 草案 --------------------------------------------------------------------


def test_a_draft_lands_where_a_human_can_read_it(tmp_path: Path, task_id: str) -> None:
    write_draft(tmp_path, task_id, DRAFT)

    assert read_draft(tmp_path, task_id) == DRAFT


def test_reading_a_draft_that_was_never_written_says_so(tmp_path: Path, task_id: str) -> None:
    with pytest.raises(NoDraft):
        read_draft(tmp_path, task_id)


# --- 答复 --------------------------------------------------------------------


def test_an_answer_can_be_written_and_read_back(tmp_path: Path, task_id: str) -> None:
    write_draft(tmp_path, task_id, DRAFT)

    write_answer(tmp_path, task_id, ANSWER)

    assert read_answer(tmp_path, task_id) == ANSWER


def test_the_draft_survives_the_answer(tmp_path: Path, task_id: str) -> None:
    """「系统建议了什么」与「人改成了什么」永远都能各自读出来。"""
    write_draft(tmp_path, task_id, DRAFT)

    write_answer(tmp_path, task_id, ANSWER)

    assert read_draft(tmp_path, task_id) == DRAFT


def test_an_answer_without_a_draft_is_refused(tmp_path: Path, task_id: str) -> None:
    """没有草案就回答,答的是什么无从谈起。"""
    with pytest.raises(NoDraft):
        write_answer(tmp_path, task_id, ANSWER)


def test_an_answer_that_is_not_a_mapping_is_refused(tmp_path: Path, task_id: str) -> None:
    write_draft(tmp_path, task_id, DRAFT)

    with pytest.raises(AnswerInvalid):
        write_answer(tmp_path, task_id, ["不是一个映射"], validator=module_boundary_answer)


def test_an_answer_with_no_modules_is_refused(tmp_path: Path, task_id: str) -> None:
    """空答复与「还没回答」长得一模一样,而前者是错、后者是正常流程。"""
    write_draft(tmp_path, task_id, DRAFT)

    with pytest.raises(AnswerInvalid) as caught:
        write_answer(tmp_path, task_id, {"modules": []}, validator=module_boundary_answer)

    assert "modules" in str(caught.value)


def test_a_module_without_an_id_is_refused(tmp_path: Path, task_id: str) -> None:
    write_draft(tmp_path, task_id, DRAFT)

    with pytest.raises(AnswerInvalid):
        write_answer(
            tmp_path,
            task_id,
            {"modules": [{"path": "repos/order-service/"}]},
            validator=module_boundary_answer,
        )


def test_a_second_answer_replaces_the_first(tmp_path: Path, task_id: str) -> None:
    """人改主意是常态。留两份的话「哪一份算数」没人答得上来。"""
    write_draft(tmp_path, task_id, DRAFT)
    write_answer(tmp_path, task_id, ANSWER)

    write_answer(tmp_path, task_id, {"modules": [{"id": "final", "path": "repos/order-service/"}]})

    assert read_answer(tmp_path, task_id)["modules"][0]["id"] == "final"


# --- 与推进器的接线 -----------------------------------------------------------


def test_answering_moves_the_task_on(tmp_path: Path, store: GenomeTaskStore) -> None:
    driver = GenomeDriver(store, EventLog(tmp_path))
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    write_draft(tmp_path, created.id, DRAFT)
    driver.deliver(created.id, GenomeEvent.DRAFT_READY)

    driver.confirm(created.id, ANSWER)

    assert store.get(created.id).state is GenomeTaskState.DEEP_READ


def test_a_bad_answer_leaves_the_task_waiting(tmp_path: Path, store: GenomeTaskStore) -> None:
    driver = GenomeDriver(store, EventLog(tmp_path))
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    write_draft(tmp_path, created.id, DRAFT)
    driver.deliver(created.id, GenomeEvent.DRAFT_READY)

    with pytest.raises(AnswerInvalid):
        driver.confirm(created.id, {"modules": []})

    assert store.get(created.id).state is GenomeTaskState.AWAITING_CONFIRMATION
    assert read_answer(tmp_path, created.id) is None, "不合契约的答复不该落盘"


# --- 恢复:靠读产物,不靠内存 --------------------------------------------------


def test_a_restart_finds_the_task_still_waiting(tmp_path: Path, store: GenomeTaskStore) -> None:
    driver = GenomeDriver(store, EventLog(tmp_path))
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    write_draft(tmp_path, created.id, DRAFT)
    driver.deliver(created.id, GenomeEvent.DRAFT_READY)

    reopened = GenomeTaskStore(tmp_path)

    assert [item.id for item in reopened.awaiting_confirmation()] == [created.id]


def test_an_answer_written_before_a_restart_still_moves_the_task(
    tmp_path: Path, store: GenomeTaskStore
) -> None:
    """**恢复逻辑就是这一条**:看当前状态、读上一阶段的产物、继续。不需要额外的恢复分支。"""
    driver = GenomeDriver(store, EventLog(tmp_path))
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    write_draft(tmp_path, created.id, DRAFT)
    driver.deliver(created.id, GenomeEvent.DRAFT_READY)
    write_answer(tmp_path, created.id, ANSWER)

    # 编排器重启:新的仓储、新的推进器,内存里什么都没有。
    resumed = GenomeDriver(GenomeTaskStore(tmp_path), EventLog(tmp_path))
    applied = resumed.resume(created.id)

    assert applied is not None
    assert applied.moved
    assert GenomeTaskStore(tmp_path).get(created.id).state is GenomeTaskState.DEEP_READ


def test_resuming_a_task_nobody_answered_yet_does_nothing(
    tmp_path: Path, store: GenomeTaskStore
) -> None:
    driver = GenomeDriver(store, EventLog(tmp_path))
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    write_draft(tmp_path, created.id, DRAFT)
    driver.deliver(created.id, GenomeEvent.DRAFT_READY)

    assert driver.resume(created.id) is None
    assert store.get(created.id).state is GenomeTaskState.AWAITING_CONFIRMATION


# --- 评审抓到的:闸门可被预答绕过、拒绝无留痕、非原子写 -------------------------


def test_an_answer_written_before_the_gate_opens_is_refused(
    tmp_path: Path, store: GenomeTaskStore
) -> None:
    """**闸门形同虚设的那条路。** 趁任务还在扫描时先写一份答复，等草案一就绪，下一次恢复
    就把闸门自动推过去——人从没看过草案，而这个闸门存在的全部理由就是让人看一眼。"""
    driver = GenomeDriver(store, EventLog(tmp_path))
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    write_draft(tmp_path, created.id, DRAFT)

    with pytest.raises(NotWaiting):
        driver.confirm(created.id, ANSWER)

    assert read_answer(tmp_path, created.id) is None
    driver.deliver(created.id, GenomeEvent.DRAFT_READY)
    assert driver.resume(created.id) is None, "预先写下的答复把闸门推过去了"


def test_a_rejected_answer_leaves_a_trail(tmp_path: Path, store: GenomeTaskStore) -> None:
    """不记的话人看到的是「我明明回答了，它却没往下走」，而查无可查。"""
    from agentgenome.core.events import LogKind

    driver = GenomeDriver(store, EventLog(tmp_path))
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)
    write_draft(tmp_path, created.id, DRAFT)
    driver.deliver(created.id, GenomeEvent.DRAFT_READY)

    with pytest.raises(AnswerInvalid):
        driver.confirm(created.id, {"modules": []})

    refusals = [
        event
        for event in EventLog(tmp_path).events(created.id)
        if event.kind is LogKind.TRANSITION_REFUSED
    ]
    assert refusals, "被拒的确认没有留下任何痕迹"


def test_a_half_written_answer_does_not_wedge_the_task(tmp_path: Path, task_id: str) -> None:
    """崩在写到一半会留下半截 JSON。读它就抛的话，之后**每一次**恢复都会炸——
    一次写到一半，任务永久卡住。"""
    write_draft(tmp_path, task_id, DRAFT)
    write_answer(tmp_path, task_id, ANSWER)
    from agentgenome.core.genome_gate import gate_dir

    (gate_dir(tmp_path, task_id) / "answer.json").write_text("{半截", encoding="utf-8")

    assert read_answer(tmp_path, task_id) is None


def test_entering_the_gate_without_a_draft_is_refused(
    tmp_path: Path, store: GenomeTaskStore
) -> None:
    """没有草案的待确认是个死局：两条入口都会拒绝回答，而任务永远等不到答复。"""
    driver = GenomeDriver(store, EventLog(tmp_path))
    created = store.create(title="init", kind=GenomeTaskKind.INIT, origin=Origin.HUMAN)

    applied = driver.deliver(created.id, GenomeEvent.DRAFT_READY, _no_draft())

    assert not applied.moved
    assert store.get(created.id).state is GenomeTaskState.SCANNING


def _no_draft():
    from agentgenome.core.genome_transitions import GenomeFacts

    return GenomeFacts(draft_exists=False)
