"""切片自裁与截断声明。

基因组切片此前作为一个整体排在裁剪顺序最后,于是它是第一个被**整块**砍掉的——结果只有两种
极端:知识全塞进去把需求和失败报告挤走,或者知识被整块砍掉、员工带着零认知上岗。没有中间。

改成切片内部先自裁:它以一个已经合规的体积参与整体组装,不再被整块砍。

**被截断这件事必须说出来。** 员工知道"这里还有东西没给我",跟员工以为"给我的就是全部",
是两种完全不同的处境。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentgenome import paths
from agentgenome.context import load_genome_slice
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.routing import RouteInputs
from agentgenome.genome.tree import MODULE_MAP, write_tree

BASE = {
    "version": 1,
    "project": {"name": "mall"},
    "modules": [{"id": "order-service", "path": "repos/order-service/"}],
}

#: 每张卡片都很长,这样预算一压就必须做取舍。
CARD = "---\nid: {id}\nsummary: {summary}\nhits: {hits}\n---\n" + ("正文。" * 200 + "\n")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / paths.KNOWLEDGE).mkdir(parents=True)
    (tmp_path / "repos/order-service" / "src").mkdir(parents=True)
    write_tree(tmp_path, ProjectMap.model_validate(BASE))
    target = tmp_path / paths.MODULES / "order-service" / MODULE_MAP
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["features"] = [
        {
            "id": f"f{index}",
            "summary": f"功能 {index}",
            "scope": ["repos/order-service/src/**"],
            "card": f"features/f{index}.md",
        }
        for index in range(4)
    ]
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    # 命中计数刻意做成 0/1/2/3,好看清"按有效性保留"到底保留了谁。
    for index in range(4):
        card = tmp_path / paths.MODULES / "order-service" / "features" / f"f{index}.md"
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text(
            CARD.format(id=f"f{index}", summary=f"功能 {index}", hits=index), encoding="utf-8"
        )
    return tmp_path


def _slice(root: Path, card_budget: int | None):
    return load_genome_slice(
        root,
        modules=("order-service",),
        route_inputs=RouteInputs(paths=("repos/order-service/src/a.py",)),
        card_budget_tokens=card_budget,
    )


def _titles(fragments) -> list[str]:
    return [item.title for item in fragments]


def _text(fragments) -> str:
    return "\n".join(f"{item.title}\n{item.body}" for item in fragments)


# --- 按命中计数保留 -----------------------------------------------------------


def test_without_a_budget_every_hit_card_comes_along(workspace: Path) -> None:
    text = _text(_slice(workspace, None))

    assert all(f"功能 f{index}" in text or f"f{index}" in text for index in range(4))


#: 一张卡片正文的开销。预算刚好装得下一张,才看得出"留下的是谁"。
ONE_CARD = 500


def test_the_most_proven_cards_survive_the_budget(workspace: Path) -> None:
    """命中计数反映的是**有效性**:被选中过、而且那些任务成功了。"""
    fragments = _slice(workspace, card_budget=ONE_CARD)

    kept = [title for title in _titles(fragments) if title.startswith("功能 ")]
    assert len(kept) == 1
    assert "f3" in kept[0], "留下的不是命中计数最高的那张"


def test_a_budget_too_small_for_even_one_card_still_gives_the_directory(
    workspace: Path,
) -> None:
    """**员工带着零认知上岗,比带一份目录更糟。** 装不下正文时全降级,目录照给。"""
    fragments = _slice(workspace, card_budget=1)

    assert not [title for title in _titles(fragments) if title.startswith("功能 ")]
    assert any("没被带进来" in title for title in _titles(fragments))


def test_a_degraded_card_still_has_its_directory_line(workspace: Path) -> None:
    """降级不等于消失。员工仍然要知道「这里有知识存在」。"""
    text = _text(_slice(workspace, card_budget=ONE_CARD))

    assert "f0" in text


# --- 截断声明 ----------------------------------------------------------------


def test_truncation_is_declared_in_the_bundle(workspace: Path) -> None:
    """员工知道「这里还有东西没给我」，跟员工以为「给我的就是全部」，是两种处境。"""
    text = _text(_slice(workspace, card_budget=ONE_CARD))

    assert "预算" in text or "截断" in text


def test_the_notice_names_what_was_dropped(workspace: Path) -> None:
    text = _text(_slice(workspace, card_budget=ONE_CARD))

    assert "f0" in text


def test_sitting_inside_the_budget_says_nothing(workspace: Path) -> None:
    """没截断就不该有声明——一条恒在的提示等于没有提示。"""
    text = _text(_slice(workspace, card_budget=100_000))

    assert "因预算截断" not in text


# --- 目录行不被裁 -------------------------------------------------------------


def test_directory_lines_are_never_trimmed(workspace: Path) -> None:
    """它们便宜,而且它们是「知道有什么存在」的唯一载体。"""
    text = _text(_slice(workspace, card_budget=1))

    for index in range(4):
        assert f"f{index}" in text


def test_the_slice_never_vanishes_entirely(workspace: Path) -> None:
    """**整块被砍掉是此前唯一的两种极端之一。** 员工带着零认知上岗比带一份目录更糟。"""
    fragments = _slice(workspace, card_budget=1)

    assert fragments


# --- 可复现 ------------------------------------------------------------------


def test_the_same_inputs_produce_the_same_slice(workspace: Path) -> None:
    once = _text(_slice(workspace, card_budget=ONE_CARD))
    twice = _text(_slice(workspace, card_budget=ONE_CARD))

    assert once == twice
