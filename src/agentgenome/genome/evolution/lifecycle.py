"""知识的自然选择:命中计数与淘汰。

## 知识只增不减是个慢性病

跑一百个任务攒两百张卡片,其中大半是一次性的、过时的、或者根本没用的。上下文里塞满噪声,
真正有用的那几条反而被淹没——而那正是 `ContextAssembler` 拼命在解决的问题。让知识库无限
增长,等于亲手把那一层的努力抵消掉。

## 只有任务成功才计数

曝光但任务失败**不加分**。这样计数反映的是**有效性**而不是频率:按频率算的话,一张写得很
宽泛、每次都被选中但从没帮上忙的卡片会排到最前面,然后越排越前——正反馈会把最没用的那张
推到顶。

## 归档是移动,不是删除

判断可能是错的,而**可逆的操作才敢自动做**。删除意味着每一次淘汰都要人来复核,那这条机制就
不会真的运转起来。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentgenome.genome.evolution.cards import LessonCard, load_cards

#: 曝光记录落在哪。与任务目录同级——它是跨任务的账,不属于任何一个任务。
EXPOSURE_FILE = Path("tasks") / "exposures.json"

#: 归档区。
ARCHIVED_DIR = "archived"

#: 知识库容量上限的缺省值。
DEFAULT_CAPACITY = 200


@dataclass
class Exposures:
    """哪些任务的上下文里出现过哪些卡片。

    落盘而不是内存:任务可能跑几十分钟、跨进程重启,而"这一轮用了哪几张卡片"要活到任务结束
    那一刻才被消费。
    """

    by_task: dict[str, list[str]] = field(default_factory=dict)

    def record(self, task_id: str, card_ids: list[str]) -> None:
        seen = self.by_task.setdefault(task_id, [])
        for card_id in card_ids:
            if card_id not in seen:
                seen.append(card_id)

    def take(self, task_id: str) -> list[str]:
        """取出并清掉。计数只该发生一次。"""
        return self.by_task.pop(task_id, [])

    def as_dict(self) -> dict[str, Any]:
        return {"by_task": self.by_task}


def load_exposures(root: Path) -> Exposures:
    path = Path(root) / EXPOSURE_FILE
    if not path.is_file():
        return Exposures()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return Exposures()
    return Exposures(by_task=dict(payload.get("by_task") or {}))


def save_exposures(root: Path, exposures: Exposures) -> Path:
    path = Path(root) / EXPOSURE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(exposures.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def record_exposure(root: Path, task_id: str, card_ids: list[str]) -> None:
    """上下文里用了这几张卡片。"""
    if not card_ids:
        return
    exposures = load_exposures(root)
    exposures.record(task_id, card_ids)
    save_exposures(root, exposures)


def credit_hits(lessons_dir: Path, root: Path, task_id: str, succeeded: bool) -> list[str]:
    """任务结束,给这一轮曝光过的卡片记账。返回加过分的卡片编号。

    **失败的任务照样要把曝光取出来清掉**,只是不加分——留着的话,同一个任务下次重跑会被
    重复计数。
    """
    exposures = load_exposures(root)
    used = exposures.take(task_id)
    save_exposures(root, exposures)
    if not succeeded or not used:
        return []

    credited = []
    for card in load_cards(lessons_dir):
        if card.id not in used:
            continue
        _write(lessons_dir, card.model_copy(update={"hits": card.hits + 1}))
        credited.append(card.id)
    return credited


@dataclass(frozen=True)
class Eviction:
    """一次淘汰的结果。"""

    archived: tuple[str, ...] = ()
    reason: str = ""


def evict(lessons_dir: Path, capacity: int = DEFAULT_CAPACITY) -> Eviction:
    """超容量时淘汰尾部。按 `hits` 升序 + 创建时间(编号)升序。

    编号当创建时间用:它单调递增且不复用,而文件 mtime 会被一次 checkout 全部刷新。
    """
    cards = [card for card in load_cards(lessons_dir) if not card.archived]
    if len(cards) <= capacity:
        return Eviction()

    ordered = sorted(cards, key=lambda card: (card.hits, card.id))
    doomed = ordered[: len(cards) - capacity]
    for card in doomed:
        archive(lessons_dir, card)
    return Eviction(
        archived=tuple(card.id for card in doomed),
        reason=f"知识库超过容量上限 {capacity},按命中数淘汰尾部 {len(doomed)} 张",
    )


def archive_unused(lessons_dir: Path, minimum_hits: int = 0) -> Eviction:
    """长期零命中的卡片移入归档区。"""
    doomed = [
        card for card in load_cards(lessons_dir) if not card.archived and card.hits <= minimum_hits
    ]
    for card in doomed:
        archive(lessons_dir, card)
    return Eviction(
        archived=tuple(card.id for card in doomed),
        reason=f"命中数不超过 {minimum_hits} 的卡片移入归档区",
    )


def archive(lessons_dir: Path, card: LessonCard) -> Path:
    """移动而非删除。判断可能是错的,而可逆的操作才敢自动做。"""
    source = Path(lessons_dir) / f"{card.id}.md"
    target = Path(lessons_dir) / ARCHIVED_DIR / f"{card.id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(card.model_copy(update={"archived": True}).render(), encoding="utf-8")
    if source.is_file():
        source.unlink()
    return target


class CardNotFound(LookupError):
    """归档区没有这张卡片。"""


def restore(lessons_dir: Path, card_id: str) -> Path:
    """把一张卡片从归档区挪回来。`archive` 的逆操作,同样是移动。"""
    source = Path(lessons_dir) / ARCHIVED_DIR / f"{card_id}.md"
    if not source.is_file():
        raise CardNotFound(f"归档区没有 {card_id}")
    card = load_cards(source.parent)
    matched = next((item for item in card if item.id == card_id), None)
    if matched is None:
        raise CardNotFound(f"归档区没有 {card_id}")
    target = Path(lessons_dir) / f"{card_id}.md"
    target.write_text(matched.model_copy(update={"archived": False}).render(), encoding="utf-8")
    source.unlink()
    return target


def add_manual(lessons_dir: Path, card: LessonCard) -> LessonCard:
    """人工添加一张卡片。**走同一套模型与同一个目录**——人工经验与自动蒸馏的经验没有区别,
    都要过同样的适用条件校验(不能全空),都参与同一套命中计数与淘汰。
    """
    _write(lessons_dir, card)
    return card


def _write(lessons_dir: Path, card: LessonCard) -> Path:
    target = Path(lessons_dir) / f"{card.id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(card.render(), encoding="utf-8")
    return target


__all__ = [
    "ARCHIVED_DIR",
    "DEFAULT_CAPACITY",
    "EXPOSURE_FILE",
    "CardNotFound",
    "Eviction",
    "Exposures",
    "add_manual",
    "archive",
    "archive_unused",
    "credit_hits",
    "evict",
    "load_exposures",
    "record_exposure",
    "restore",
    "save_exposures",
]
