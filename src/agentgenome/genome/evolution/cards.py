"""经验卡片:员工会长记性的那一部分。

## 没有证据链接的一律拒收

这是防污染的第一道、也是最有效的一道。**知识污染比没有知识更糟**——没有知识只是效率低,
错误知识会主动误导,而且它会被后续每一个任务反复消费,越错越深。

过滤由脚本做,不依赖员工自觉:一个把偶然现象写成普遍规律的员工,不会觉得自己在编造。

## 过滤这件事本身要留痕

静默丢弃等于看不见污染尝试。哪天某个员工开始大批量产出无证据卡片,那是它跑偏了的早期信号,
而唯一能看到这个信号的地方就是"被过滤了几张"。

## 去重不引入向量检索

标题 + 适用条件的简单匹配就够。同一个坑被踩第二次,**证明它是规律而不是偶然**——所以命中
已有卡片时补证据、提置信度,而不是新建一张。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.yamlio import split_front_matter

_STRICT = ConfigDict(extra="forbid")

#: 卡片编号的形状。稳定且不复用——引用它的证据链接要能一直指得住。
CARD_ID = re.compile(r"^L-\d{4}$")


class Level(StrEnum):
    """这条经验该往哪儿去。本期只有 L1 走完整闭环。"""

    #: 模块认知修正、踩坑记录、依赖事实。过 lint 后自动合并。
    L1 = "L1"
    #: 新的边界、规范、影响规则。必须人工审批(PRD 13)。
    L2 = "L2"
    #: **工序**改动:`procedure.yaml` 与 `prompt.md`。接口级,影响编排与审计,
    #: 必须人工审批 —— 慢通道。
    L3A = "L3a"
    #: **手艺**改动:`craft/` 下的内容。只影响干得好不好,回放回归验证过了即可合并
    #: —— 快通道。
    #:
    #: 分成两档是因为它们的**可读性**不同:工序契约的影响人读得出来,所以由人把关;
    #: 手艺内容的影响读不出来,只能测出来,所以由回归把关。共用一道闸的结果只有两种
    #: ——要么内容改动被审批排期拖死,要么接口改动被快通道放过去。
    L3B = "L3b"
    #: 与项目无关的通用经验。全局基因库(PRD 17)。
    L4 = "L4"


class Applicability(BaseModel):
    """这条经验什么时候适用。

    三样都可以为空,但**不能全空**:一张"到处都适用"的卡片会被每次切片选中,然后把真正
    相关的几行淹掉——那是这套知识库最典型的退化方式。
    """

    model_config = _STRICT

    modules: list[str] = Field(default_factory=list)
    path_globs: list[str] = Field(default_factory=list)
    scenario: str = ""

    def is_empty(self) -> bool:
        return not (self.modules or self.path_globs or self.scenario.strip())

    def key(self) -> str:
        return "|".join(
            [
                ",".join(sorted(self.modules)),
                ",".join(sorted(self.path_globs)),
                self.scenario.strip(),
            ]
        )


class Evidence(BaseModel):
    """一条证据。指向任务的某个产物或事件。"""

    model_config = _STRICT

    task_id: str
    #: 相对任务目录的路径。可达性由 lint 门禁检查——指向不存在产物的"证据"就是编造。
    path: str
    note: str = ""


class LessonCard(BaseModel):
    """一张经验卡片。"""

    model_config = _STRICT

    id: str
    title: str
    applies_to: Applicability
    conclusion: str
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    level: Level = Level.L1
    hits: int = Field(default=0, ge=0)
    created_from: str = ""
    archived: bool = False

    def key(self) -> str:
        """去重键:标题 + 适用条件。"""
        return f"{self.title.strip()}#{self.applies_to.key()}"

    def reinforced_by(self, other: LessonCard) -> LessonCard:
        """同一个坑被踩第二次。补证据、提置信度,不新建。"""
        seen = {(item.task_id, item.path) for item in self.evidence}
        extra = [item for item in other.evidence if (item.task_id, item.path) not in seen]
        return self.model_copy(
            update={
                "evidence": [*self.evidence, *extra],
                "confidence": min(1.0, self.confidence + 0.1),
            }
        )

    def render(self) -> str:
        """YAML front matter + 正文。给人读的是正文,给机器读的是 front matter。"""
        front = self.model_dump(mode="json", exclude={"conclusion"})
        return (
            "---\n"
            + yaml.safe_dump(front, allow_unicode=True, sort_keys=True)
            + "---\n\n"
            + self.conclusion.strip()
            + "\n"
        )


@dataclass(frozen=True)
class Intake:
    """一次入库的结果。**被拒的也要带出来**——看不见的过滤等于看不见污染。"""

    accepted: tuple[LessonCard, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    reinforced: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": [card.id for card in self.accepted],
            "reinforced": list(self.reinforced),
            "rejected": [{"title": title, "reason": reason} for title, reason in self.rejected],
        }


def parse_cards(payload: Any, created_from: str, next_number: int) -> Intake:
    """把蒸馏产物变成卡片。**在这里就把没有证据的丢掉。**"""
    accepted: list[LessonCard] = []
    rejected: list[tuple[str, str]] = []
    number = next_number
    for raw in payload if isinstance(payload, list) else []:
        if not isinstance(raw, dict):
            rejected.append(("(不是一个映射)", "卡片必须是对象"))
            continue
        title = str(raw.get("title") or "(无标题)")
        candidate = dict(raw)
        candidate.setdefault("created_from", created_from)
        candidate["id"] = f"L-{number:04d}"
        try:
            card = LessonCard.model_validate(candidate)
        except ValidationError as error:
            rejected.append((title, _first_error(error)))
            continue
        if not card.evidence:
            # 防污染的第一道。不依赖员工自觉——它不会觉得自己在编造。
            rejected.append((title, "没有证据链接"))
            continue
        if card.applies_to.is_empty():
            rejected.append((title, "没有适用条件,会被每次切片选中并淹掉真正相关的内容"))
            continue
        accepted.append(card)
        number += 1
    return Intake(accepted=tuple(accepted), rejected=tuple(rejected))


def merge(existing: list[LessonCard], incoming: Intake) -> Intake:
    """与已有卡片去重。命中的补证据,没命中的作为新卡片。"""
    index = {card.key(): card for card in existing}
    fresh: list[LessonCard] = []
    reinforced: list[str] = []
    for card in incoming.accepted:
        found = index.get(card.key())
        if found is None:
            fresh.append(card)
            continue
        index[card.key()] = found.reinforced_by(card)
        reinforced.append(found.id)
    return Intake(accepted=tuple(fresh), rejected=incoming.rejected, reinforced=tuple(reinforced))


def load_cards(directory: Path) -> list[LessonCard]:
    """读回一个目录里的全部卡片,按编号升序。坏掉的那张不该让整库读不出来。"""
    found: list[LessonCard] = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("L-*.md")):
        try:
            found.append(parse_card(path.read_text(encoding="utf-8")))
        except GenomeValidationError:
            continue
    return sorted(found, key=lambda card: card.id)


def parse_card(text: str) -> LessonCard:
    front, conclusion = split_front_matter(text, "(卡片)")
    front["conclusion"] = conclusion
    try:
        return LessonCard.model_validate(front)
    except ValidationError as error:
        raise GenomeValidationError(
            [ValidationIssue(str(front.get("id", "(卡片)")), _first_error(error))]
        ) from error


def next_number(existing: list[LessonCard]) -> int:
    """下一个可用编号。**只增不复用**——引用它的证据链接要能一直指得住。"""
    used = [int(card.id.split("-", 1)[1]) for card in existing if CARD_ID.match(card.id)]
    return max(used, default=0) + 1


def _first_error(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(item) for item in first["loc"]) or "(顶层)"
    return f"{where}: {first['msg']}"


__all__ = [
    "CARD_ID",
    "Applicability",
    "Evidence",
    "Intake",
    "LessonCard",
    "Level",
    "load_cards",
    "merge",
    "next_number",
    "parse_card",
    "parse_cards",
]
