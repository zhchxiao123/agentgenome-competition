"""可疑账:知识过期的信号,**软信号,不是门禁**(PRD 41)。

卡片自带 `scope`,任务自带变更清单——此前两者只在 `routing` 里相遇("这次任务该带哪些
知识"),从没反向用来质疑知识("代码改了,卡片跟上了吗")。这里补上反方向:任务终结时,
变更命中了某卡的覆盖范围而本轮没有对应的知识更新,就记一条**可疑过期**;fix 类任务结项
却没产出经验卡片,记一条**教训蒸发**。

## 为什么是信号,不是门禁

硬门禁会逼出敷衍的卡片编辑——为了让任务过关而随手改两个字的"更新",是另一种腐烂,
而且比薄卡更难发现。所以可疑账非空不阻塞任何状态迁移、不阻塞提交;它只让"这张卡可能
在撒谎"从看不见变成看得见,由知识更新工序消费:改卡,或声明"核对过,无需更新"——
**两种都算响应**,没有响应的记录持续累积,在 `agctl knowledge status` 里可见。

## 编排器只记账

与 `hits` 的两本账同一条约束:计账是对运行态目录(`tasks/`)的写入,知识树
(`genome/`)一个字节不碰。落卡由知识写入路径(staging → 原子应用)做,清账由消费方做。

## 重放安全

崩溃恢复会重放终态收尾,同一条可疑按 `(kind, task, card)` 去重——不去重的话账面余额
是"被重放过几次",而余额恰恰是 status 要给人看的数字。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agentgenome.core.scope import compile_glob
from agentgenome.genome.features import KnowledgeTree
from agentgenome.genome.hits import LedgerUnreadable
from agentgenome.genome.tree import module_dir

#: 可疑账。**住运行态目录,不在 genome 里**——它是账本,不是知识。
SUSPECTS_FILE = Path("tasks") / "card-suspects.json"

#: 已响应的可疑,append-only 的留痕。"核对过,无需更新"也要查得到是谁、凭哪个任务
#: 核对的——不留痕的清账和静默丢弃在事后没有任何区别。
RESOLUTIONS_FILE = Path("tasks") / "card-suspects-resolved.json"


class SuspectKind(StrEnum):
    """一条可疑是哪种信号。"""

    #: 变更命中了卡的覆盖范围,知识没动——卡片可能过期。
    STALE = "stale_card"
    #: fix 类任务结项却没有经验卡片——一条教训正在蒸发。
    EVAPORATED = "evaporated_lesson"


class ResolutionAction(StrEnum):
    """对一条可疑的响应,二选一,**都算清账**(ADR-0012)。"""

    #: 改卡:深化/更新已经应用,知识跟上了。
    UPDATED = "updated"
    #: 核对过,无需更新——声明,不产生卡片 diff。
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class Suspect:
    """一条可疑记录。"""

    kind: SuspectKind
    task_id: str
    #: `模块/功能点`。蒸发信号没有卡,留空。
    card: str = ""
    #: 命中该卡覆盖范围的变更文件(Workspace 相对)。
    changed: tuple[str, ...] = ()
    round: int = 0

    @property
    def key(self) -> str:
        """去重与消费用的身份。`changed` 不参与——同一任务对同一卡只算一条可疑。"""
        return f"{self.kind.value}|{self.task_id}|{self.card}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "task_id": self.task_id,
            "card": self.card,
            "changed": list(self.changed),
            "round": self.round,
        }

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> Suspect:
        return cls(
            kind=SuspectKind(str(payload.get("kind"))),
            task_id=str(payload.get("task_id") or ""),
            card=str(payload.get("card") or ""),
            changed=tuple(str(item) for item in payload.get("changed") or []),
            round=int(payload.get("round") or 0),
        )


def stale_suspects(
    tree: KnowledgeTree, changed: Sequence[str], task_id: str, round_: int
) -> tuple[Suspect, ...]:
    """这次任务的变更让哪些卡片变得可疑。**纯函数**:输入是树与清单,不做 I/O。

    - 只看有卡片的功能点:`no_card` 声明过"这里不需要知识",没有可过期的东西。
    - 变更清单里带着卡片文件本身的不算:知识在同一个任务里跟上了,这正是我们想要的
      结局,不是可疑。
    """
    normalized = [str(path).replace("\\", "/") for path in changed]
    found: list[Suspect] = []
    for module_id, features in tree.feature_index.items():
        for feature in features:
            if feature.card is None:
                continue
            card_path = str(module_dir(Path(), module_id) / feature.card)
            if card_path in normalized:
                continue
            matchers = [compile_glob(scope) for scope in feature.scope]
            hits = tuple(
                path for path in normalized if any(matcher.match(path) for matcher in matchers)
            )
            if hits:
                found.append(
                    Suspect(
                        kind=SuspectKind.STALE,
                        task_id=task_id,
                        card=f"{module_id}/{feature.id}",
                        changed=hits,
                        round=round_,
                    )
                )
    return tuple(found)


def record_suspects(root: Path, suspects: Sequence[Suspect]) -> None:
    """把一批可疑记进账本。按 `(kind, task, card)` 去重——终态收尾会被崩溃恢复重放。"""
    if not suspects:
        return
    path = Path(root) / SUSPECTS_FILE
    pending = _load(path)
    seen = {item.key for item in pending}
    for suspect in suspects:
        if suspect.key not in seen:
            pending.append(suspect)
            seen.add(suspect.key)
    _write(path, pending)


def pending_suspects(root: Path) -> tuple[Suspect, ...]:
    """看一眼账本,**不清掉**。只读入口给 status 与预演用——把数据吃掉的预演比没有更糟。"""
    return tuple(_load(Path(root) / SUSPECTS_FILE))


def resolve_suspects(
    root: Path,
    *,
    card: str = "",
    task_id: str = "",
    action: ResolutionAction = ResolutionAction.UNCHANGED,
    note: str = "",
) -> tuple[Suspect, ...]:
    """消费可疑:清账并留痕。**"改卡"与"核对过,无需更新"都是合法响应。**

    - `card=`:清掉这张卡的全部可疑过期。核对是对着卡与当前代码做的,几个任务flag的
      是同一张卡,一次核对一起清。
    - `task_id=`:清掉这个任务的蒸发信号(它没有卡)。

    **先留痕,再清账。** 反过来的话,清完崩溃会让一次响应变成静默丢弃——重复的留痕
    可以核对,消失的留痕没法找回。声明无变化不碰 `genome/` 一个字节。
    """
    if not card and not task_id:
        return ()
    path = Path(root) / SUSPECTS_FILE
    pending = _load(path)
    hit = tuple(
        item
        for item in pending
        if (card and item.card == card) or (task_id and not item.card and item.task_id == task_id)
    )
    if not hit:
        return ()
    _append_resolutions(Path(root), hit, action, note)
    _write(path, [item for item in pending if item not in hit])
    return hit


def resolutions(root: Path) -> tuple[dict[str, Any], ...]:
    """已响应的可疑,append-only 留痕。只读。"""
    path = Path(root) / RESOLUTIONS_FILE
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return tuple(dict(item) for item in payload.get("resolutions") or [])
    except (OSError, ValueError) as error:
        raise LedgerUnreadable(f"留痕读不出来({path}): {error}") from error


def _append_resolutions(
    root: Path, resolved: Sequence[Suspect], action: ResolutionAction, note: str
) -> None:
    from datetime import UTC, datetime

    path = root / RESOLUTIONS_FILE
    existing = list(resolutions(root))
    stamp = datetime.now(UTC).isoformat()
    for suspect in resolved:
        existing.append(
            {
                "key": suspect.key,
                "kind": suspect.kind.value,
                "card": suspect.card,
                "task_id": suspect.task_id,
                "action": action.value,
                "note": note,
                "resolved_at": stamp,
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"resolutions": existing}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)


def _load(path: Path) -> list[Suspect]:
    """读账本。**读不出来就抛,不当成空**——当成空的话,下一次写会把整本账覆盖掉,
    而每一条都对应一次真实发生过的信号(与 `hits._load_credits` 同一条理由)。"""
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [Suspect.parse(item) for item in payload.get("suspects") or []]
    except (OSError, ValueError, KeyError) as error:
        raise LedgerUnreadable(f"可疑账读不出来({path}): {error}") from error


def _write(path: Path, suspects: list[Suspect]) -> None:
    """先写临时文件再改名——读到的要么是旧的整份、要么是新的整份。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"suspects": [item.as_dict() for item in suspects]}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


__all__ = [
    "RESOLUTIONS_FILE",
    "ResolutionAction",
    "SUSPECTS_FILE",
    "Suspect",
    "SuspectKind",
    "pending_suspects",
    "record_suspects",
    "resolutions",
    "resolve_suspects",
    "stale_suspects",
]
