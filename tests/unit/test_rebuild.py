"""⑤ 按模块重建：先 diff 再落盘，人手改过的一个都不碰。

「我改过的东西下次扫描就没了」会让人**彻底放弃维护知识**——那时知识树就只剩机器写的那一半，
而机器写的那一半正是最需要人复核的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome.genome.rebuild import (
    apply_rebuild,
    plan_rebuild,
    render_rebuild_report,
)
from agentgenome.genome.staging import HUMAN_EDITED_MARKER

CARD = "genome/knowledge/modules/order-service/features/reserve-flow.md"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    target = tmp_path / CARD
    target.parent.mkdir(parents=True)
    target.write_text("旧的认知。\n", encoding="utf-8")
    return tmp_path


def test_a_changed_card_shows_up_in_the_plan(workspace: Path) -> None:
    plan = plan_rebuild(workspace, "order-service", {CARD: "新的认知。\n"})

    assert plan.changed == [CARD]


def test_an_unchanged_card_is_not_touched(workspace: Path) -> None:
    """重建产出与现状一致时不该制造一次假变更——知识 PR 的 diff 要能被读完。"""
    plan = plan_rebuild(workspace, "order-service", {CARD: "旧的认知。\n"})

    assert plan.changed == []


def test_a_new_card_counts_as_a_change(workspace: Path) -> None:
    new = "genome/knowledge/modules/order-service/features/refund-flow.md"

    plan = plan_rebuild(workspace, "order-service", {new: "新功能点。\n"})

    assert plan.changed == [new]


# --- 人工编辑保护 -------------------------------------------------------------


def test_a_hand_edited_card_is_protected(workspace: Path) -> None:
    (workspace / CARD).write_text(f"{HUMAN_EDITED_MARKER}\n我手写的。\n", encoding="utf-8")

    plan = plan_rebuild(workspace, "order-service", {CARD: "机器想覆盖的内容。\n"})

    assert plan.protected == [CARD]
    assert plan.changed == []


def test_applying_never_writes_a_protected_card(workspace: Path) -> None:
    """**哪怕内容确实变了。** 那正是这条保护存在的意义。"""
    (workspace / CARD).write_text(f"{HUMAN_EDITED_MARKER}\n我手写的。\n", encoding="utf-8")
    plan = plan_rebuild(workspace, "order-service", {CARD: "机器想覆盖的内容。\n"})

    written = apply_rebuild(workspace, plan)

    assert written == []
    assert "我手写的" in (workspace / CARD).read_text(encoding="utf-8")


def test_protection_is_judged_by_the_existing_file_not_the_output(workspace: Path) -> None:
    """产出里当然不会有那个标记——拿产出去判等于永远判不出来。"""
    (workspace / CARD).write_text(f"{HUMAN_EDITED_MARKER}\n我手写的。\n", encoding="utf-8")

    plan = plan_rebuild(workspace, "order-service", {CARD: "没有标记的新内容。\n"})

    assert plan.protected == [CARD]


# --- 落盘 --------------------------------------------------------------------


def test_applying_writes_the_changed_ones(workspace: Path) -> None:
    plan = plan_rebuild(workspace, "order-service", {CARD: "新的认知。\n"})

    assert apply_rebuild(workspace, plan) == [CARD]
    assert (workspace / CARD).read_text(encoding="utf-8") == "新的认知。\n"


def test_the_plan_comes_before_the_write(workspace: Path) -> None:
    """先算出来给人看，再落盘——这样人在 PR 上比对的和实际会发生的是同一件事。"""
    plan = plan_rebuild(workspace, "order-service", {CARD: "新的认知。\n"})

    assert (workspace / CARD).read_text(encoding="utf-8") == "旧的认知。\n"
    apply_rebuild(workspace, plan)
    assert (workspace / CARD).read_text(encoding="utf-8") == "新的认知。\n"


def test_the_report_tells_you_what_was_left_alone(workspace: Path) -> None:
    (workspace / CARD).write_text(f"{HUMAN_EDITED_MARKER}\n我手写的。\n", encoding="utf-8")
    plan = plan_rebuild(workspace, "order-service", {CARD: "机器想覆盖的内容。\n"})

    text = render_rebuild_report(plan)

    assert "一个都没动" in text
    assert CARD in text
