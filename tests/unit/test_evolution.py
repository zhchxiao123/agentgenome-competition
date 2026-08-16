"""L1 知识回流:卡片、素材、管道与自然选择。

这条管道的核心风险不是"蒸馏得不够好",是**污染**——一张把偶然现象写成普遍规律的卡片会被
后续每一个任务反复消费,越错越深。所以这里最密集的测试是那两道过滤。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome.core.states import TaskState
from agentgenome.core.task import Task
from agentgenome.genome.evolution.cards import (
    Applicability,
    Evidence,
    LessonCard,
    Level,
    load_cards,
    merge,
    next_number,
    parse_card,
    parse_cards,
)
from agentgenome.genome.evolution.lifecycle import (
    archive_unused,
    credit_hits,
    evict,
    record_exposure,
)
from agentgenome.genome.evolution.pipeline import distill, land_cards, lint


def _raw(title: str = "订单服务的测试需要本地 Redis", **overrides) -> dict:
    payload = {
        "title": title,
        "applies_to": {"modules": ["order-service"]},
        "conclusion": "跑 order-service 的测试之前要先起一个本地 Redis。",
        "evidence": [{"task_id": "ag-1", "path": "artifacts/03-unit-gate/gate-report.json"}],
        "level": "L1",
    }
    payload.update(overrides)
    return payload


def _card(**overrides) -> LessonCard:
    base = LessonCard(
        id="L-0001",
        title="订单服务的测试需要本地 Redis",
        applies_to=Applicability(modules=["order-service"]),
        conclusion="先起 Redis。",
        evidence=[Evidence(task_id="ag-1", path="artifacts/x.json")],
    )
    return base.model_copy(update=overrides)


# --- 防污染 -----------------------------------------------------------------


def test_a_card_without_evidence_is_rejected() -> None:
    """防污染的第一道。不依赖员工自觉——它不会觉得自己在编造。"""
    intake = parse_cards([_raw(evidence=[])], "ag-1", 1)

    assert intake.accepted == ()
    assert intake.rejected[0][1] == "没有证据链接"


def test_rejection_is_visible_not_silent() -> None:
    """静默丢弃等于看不见污染尝试。大批量无证据卡片是员工跑偏了的早期信号。"""
    intake = parse_cards([_raw(evidence=[]), _raw(title="另一条", evidence=[])], "ag-1", 1)

    assert len(intake.rejected) == 2


def test_a_card_that_applies_to_everything_is_rejected() -> None:
    """一张"到处都适用"的卡片会被每次切片选中,然后把真正相关的几行淹掉。"""
    intake = parse_cards([_raw(applies_to={})], "ag-1", 1)

    assert intake.accepted == ()


def test_evidence_must_point_at_something_that_exists(tmp_path: Path) -> None:
    """防污染的第二道:指向不存在产物的"证据"就是编造,而且比没有证据更难被发现。"""
    problems = lint(_card(), tmp_path)

    assert problems and "不存在" in problems[0]


def test_evidence_that_resolves_passes_lint(tmp_path: Path) -> None:
    target = tmp_path / "tasks" / "ag-1" / "artifacts"
    target.mkdir(parents=True)
    (target / "x.json").write_text("{}", encoding="utf-8")

    assert lint(_card(), tmp_path) == []


# --- 编号与去重 -------------------------------------------------------------


def test_numbers_are_never_reused() -> None:
    """引用卡片的证据链接要能一直指得住。"""
    assert next_number([_card(id="L-0001"), _card(id="L-0007")]) == 8


def test_the_same_lesson_twice_reinforces_instead_of_duplicating() -> None:
    """同一个坑被踩第二次,证明它是规律而不是偶然——那恰恰该加分。"""
    existing = [_card(confidence=0.5)]
    incoming = parse_cards([_raw()], "ag-2", 2)

    result = merge(existing, incoming)

    assert result.accepted == ()
    assert result.reinforced == ("L-0001",)


def test_a_different_lesson_is_a_new_card() -> None:
    result = merge([_card()], parse_cards([_raw(title="完全不同的一条")], "ag-2", 2))

    assert len(result.accepted) == 1


def test_a_card_survives_a_round_trip_through_disk(tmp_path: Path) -> None:
    card = _card()
    (tmp_path / "L-0001.md").write_text(card.render(), encoding="utf-8")

    assert parse_card(card.render()).conclusion == card.conclusion
    assert load_cards(tmp_path)[0].id == "L-0001"


def test_a_broken_card_does_not_make_the_whole_library_unreadable(tmp_path: Path) -> None:
    (tmp_path / "L-0001.md").write_text(_card().render(), encoding="utf-8")
    (tmp_path / "L-0002.md").write_text("这不是一张卡片", encoding="utf-8")

    assert [card.id for card in load_cards(tmp_path)] == ["L-0001"]


# --- 分级入库 ---------------------------------------------------------------


def test_only_l1_lands_in_lessons(tmp_path: Path) -> None:
    """本期只有 L1 走完整闭环。其余存档待后续消费——丢掉的话,那几级的素材要重新蒸馏,
    而那时任务现场早就没了。"""
    target = tmp_path / "tasks" / "ag-1" / "artifacts"
    target.mkdir(parents=True)
    (target / "x.json").write_text("{}", encoding="utf-8")
    intake = parse_cards(
        [_raw(evidence=[{"task_id": "ag-1", "path": "artifacts/x.json"}], level="L2")], "ag-1", 1
    )

    written, deferred, _ = land_cards(tmp_path, intake)

    assert written == []
    assert deferred == ["L-0001"]


# --- 管道的异常隔离 ---------------------------------------------------------


def _task(state: TaskState = TaskState.COMPLETED) -> Task:
    return Task(id="ag-1", title="t", requirement="r", state=state)


def test_a_broken_distiller_never_fails_the_task(tmp_path: Path) -> None:
    """**这条管道能造成的最大伤害**就是让一个蒸馏 bug 把所有任务标成失败。"""
    directory = tmp_path / "tasks" / "ag-1" / "failures"
    directory.mkdir(parents=True)
    (directory / "round-1.md").write_text("# 第 1 轮失败\n", encoding="utf-8")

    def explode(_material):
        raise RuntimeError("蒸馏器自己炸了")

    result = distill(tmp_path, _task(), explode)

    assert result.ok is False
    assert "蒸馏器自己炸了" in result.error


def test_a_task_with_nothing_to_learn_from_is_skipped(tmp_path: Path) -> None:
    """直通且没有审批意见的任务没什么可蒸馏的。为它烧一次 Agent 是纯浪费。"""
    called = []

    result = distill(tmp_path, _task(), lambda material: called.append(material) or [])

    assert result.skipped
    assert called == []


def test_an_exhausted_budget_skips_instead_of_spending(tmp_path: Path) -> None:
    def explode(_material):
        raise AssertionError("预算用尽了还去派发")

    result = distill(tmp_path, _task(), explode, budget_tokens=1000, spent_tokens=1000)

    assert "预算" in result.skipped


# --- 自然选择 ---------------------------------------------------------------


def _library(tmp_path: Path, cards: list[LessonCard]) -> Path:
    lessons = tmp_path / "lessons"
    lessons.mkdir(parents=True, exist_ok=True)
    for card in cards:
        (lessons / f"{card.id}.md").write_text(card.render(), encoding="utf-8")
    return lessons


def test_only_a_successful_task_credits_a_hit(tmp_path: Path) -> None:
    """计数要反映**有效性**而不是频率。

    按频率算的话,一张写得很宽泛、每次都被选中但从没帮上忙的卡片会排到最前面——正反馈会把
    最没用的那张推到顶。
    """
    lessons = _library(tmp_path, [_card(id="L-0001")])
    record_exposure(tmp_path, "ag-1", ["L-0001"])

    credit_hits(lessons, tmp_path, "ag-1", succeeded=False)

    assert load_cards(lessons)[0].hits == 0


def test_a_successful_task_credits_the_cards_it_used(tmp_path: Path) -> None:
    lessons = _library(tmp_path, [_card(id="L-0001")])
    record_exposure(tmp_path, "ag-1", ["L-0001"])

    credited = credit_hits(lessons, tmp_path, "ag-1", succeeded=True)

    assert credited == ["L-0001"]
    assert load_cards(lessons)[0].hits == 1


def test_a_failed_task_still_clears_its_exposures(tmp_path: Path) -> None:
    """不清的话,同一个任务下次重跑会被重复计数。"""
    lessons = _library(tmp_path, [_card(id="L-0001")])
    record_exposure(tmp_path, "ag-1", ["L-0001"])
    credit_hits(lessons, tmp_path, "ag-1", succeeded=False)

    credit_hits(lessons, tmp_path, "ag-1", succeeded=True)

    assert load_cards(lessons)[0].hits == 0


def test_eviction_drops_the_least_useful_first(tmp_path: Path) -> None:
    lessons = _library(
        tmp_path,
        [
            _card(id="L-0001", hits=5, title="有用"),
            _card(id="L-0002", hits=0, title="没用"),
            _card(id="L-0003", hits=2, title="还行"),
        ],
    )

    result = evict(lessons, capacity=2)

    assert result.archived == ("L-0002",)
    assert {card.id for card in load_cards(lessons)} == {"L-0001", "L-0003"}


def test_archiving_moves_rather_than_deletes(tmp_path: Path) -> None:
    """判断可能是错的,而可逆的操作才敢自动做。"""
    lessons = _library(tmp_path, [_card(id="L-0001", hits=0)])

    archive_unused(lessons)

    assert (lessons / "archived" / "L-0001.md").is_file()
    assert not (lessons / "L-0001.md").exists()


def test_an_archived_card_is_not_loaded_as_active(tmp_path: Path) -> None:
    lessons = _library(tmp_path, [_card(id="L-0001", hits=0)])
    archive_unused(lessons)

    assert load_cards(lessons) == []


@pytest.mark.parametrize("level", [Level.L1, Level.L2, Level.L3A, Level.L3B, Level.L4])
def test_every_level_is_representable(level: Level) -> None:
    assert LessonCard.model_validate(_raw(level=level.value) | {"id": "L-0001"}).level is level


# --- 卡片进上下文并被记账 ---------------------------------------------------


def test_a_lesson_card_reaches_the_context_slice(tmp_path: Path) -> None:
    """**这是自进化闭环真正闭上的那一环。**

    卡片产出得再好,不进上下文就等于没有。
    """
    from agentgenome.context import load_genome_slice
    from tests.fixtures.mini_map import write_workspace

    write_workspace(tmp_path)
    lessons = tmp_path / "genome" / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "L-0001.md").write_text(
        _card(applies_to=Applicability(modules=["order-service"])).render(), encoding="utf-8"
    )

    titles = [item.title for item in load_genome_slice(tmp_path, ["order-service"])]

    assert any(item.startswith("经验 L-0001") for item in titles)


def test_a_card_for_another_module_stays_out(tmp_path: Path) -> None:
    """塞进去只会把真正相关的三行淹掉——那正是切片这一层要解决的问题。"""
    from agentgenome.context import load_genome_slice
    from tests.fixtures.mini_map import write_workspace

    write_workspace(tmp_path)
    lessons = tmp_path / "genome" / "knowledge" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "L-0001.md").write_text(
        _card(applies_to=Applicability(modules=["inventory-service"])).render(), encoding="utf-8"
    )

    titles = [item.title for item in load_genome_slice(tmp_path, ["order-service"])]

    assert not any(item.startswith("经验 L-0001") for item in titles)


def test_only_the_cards_that_survived_the_budget_count_as_exposed() -> None:
    """被预算砍掉的卡片没有进上下文,给它记一次曝光等于凭空给它加分。"""
    from agentgenome.context import Fragment, lesson_ids

    kept = [
        Fragment("经验 L-0001:先起 Redis", "…"),
        Fragment("order-service", "…"),
    ]

    assert lesson_ids(kept) == ["L-0001"]


# --- 蒸馏的产出通道:staged lessons(PRD 34) -----------------------------------


LESSON_FILE = """\
---
title: 迁移目录改动要走人工审批
level: L1
applies_to:
  modules: [order-service]
evidence:
  - task_id: ag-1
    path: artifacts/x.json
---

迁移目录的改动应该强制走人工审批。
"""


async def test_the_distill_dispatch_reads_cards_from_staged_lessons(tmp_path: Path) -> None:
    """`_dispatch` 装载的是产物目录 `staging/lessons/` 下已验证的文件,不是解包 JSON。

    这条钉住的是搬运源:全库不允许残留"卡片进信封"的读法——回归的话,蒸馏会安静地
    读到零张卡片,而"这次没提炼出东西"恰恰是它的正常结果之一,没人会起疑。
    """
    from types import SimpleNamespace

    from agentgenome.genome.evolution.pipeline import _dispatch
    from agentgenome.jobs.artifacts import Slot

    class _Bus:
        def allocate(self, stage: str) -> Slot:
            path = tmp_path / f"01-{stage}"
            path.mkdir(parents=True, exist_ok=True)
            return Slot(path=path, stage=stage, number=1)

    class _Orchestrator:
        def bus(self, task: object) -> _Bus:
            return _Bus()

        async def dispatch(self, task, procedure_id, employee_id, slot, state, inputs=None):
            target = slot.path / "staging" / "lessons" / "migration-review.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(LESSON_FILE, encoding="utf-8")
            return SimpleNamespace(ok=True)

    cards = await _dispatch(
        _Orchestrator(), SimpleNamespace(id="ag-1"), SimpleNamespace(as_dict=dict)
    )

    assert [item["title"] for item in cards] == ["迁移目录改动要走人工审批"]
    assert cards[0]["conclusion"].startswith("迁移目录的改动")
