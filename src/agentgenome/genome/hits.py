"""功能卡片的命中计数:知识也要接受自然选择。

一张卡片被选进上下文、**并且那个任务最终成功**,计数才加一。

## 计数反映有效性,不是曝光度

一张总被选中但任务总失败的卡片,它的高曝光恰恰是个坏信号。按曝光计数的话,最没用的那批卡片
会排在最前面,然后在预算裁剪(见 `context._feature_fragments`)时优先活下来——自然选择会
选出最差的那批。

## 编排器不写知识树

计数更新是对知识树的**写入**,所以它必须走既有的知识写入路径(架构员工的知识更新 Procedure →
知识 PR)。编排器只记"这次命中了哪些、任务成没成功",落盘由知识更新那条路径汇总。

**这条约束比计数本身重要。** 开了"编排器顺手改一下卡片文件"这个口子,下一个需求会走同一个
口子,几轮之后编排器就在随便改知识了——而知识树只由架构员工写入,是 ADR-0005 与整棵树的
校验体系共同依赖的前提。计数晚几天更新没有任何伤害。

## 两本账,不是一本

**曝光账**(`PENDING_FILE`)记"这个任务的上下文里出现过哪几张卡片",任务一进终态就被取走清掉。
取走时如果任务成功,那几张卡片进**待记账**(`CREDITS_FILE`)——它等的是知识写入路径来消费。

分成两本是因为两者的生命周期完全不同:曝光按任务清,待记账按"上一次记账到现在"攒。合成一本
的话,一次记账要么把还在跑的任务的曝光也算进去,要么就得在同一份文件里维护两种状态——而那种
文件迟早会被某一侧写坏。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from agentgenome.genome.features import KnowledgeTree
from agentgenome.genome.tree import module_dir
from agentgenome.genome.yamlio import split_front_matter

#: 曝光记录。**住在运行态目录,不在 genome 里**——它是账本,不是知识。
PENDING_FILE = Path("tasks") / "card-hits.json"

#: 已结算、等着写进卡片的命中。同上,它也是账本。
CREDITS_FILE = Path("tasks") / "card-credits.json"


@dataclass(frozen=True)
class CardRef:
    """一张卡片。功能点 id 只在模块内唯一,所以引用它必须带上模块。"""

    module_id: str
    feature_id: str

    @property
    def key(self) -> str:
        return f"{self.module_id}/{self.feature_id}"

    @classmethod
    def parse(cls, key: str) -> CardRef:
        module_id, _, feature_id = key.partition("/")
        return cls(module_id, feature_id)


@dataclass
class Pending:
    """哪些任务的上下文里出现过哪些卡片。

    落盘而不是内存:任务可能跑几十分钟、跨进程重启,而"这一轮用了哪几张卡片"要活到任务结束
    那一刻才被消费。
    """

    by_task: dict[str, list[str]] = field(default_factory=dict)


def _load(root: Path) -> Pending:
    path = Path(root) / PENDING_FILE
    if not path.is_file():
        return Pending()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Pending()
    return Pending(by_task=dict(payload.get("by_task") or {}))


def _save(root: Path, pending: Pending) -> None:
    path = Path(root) / PENDING_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"by_task": pending.by_task}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def record_exposure(root: Path, task_id: str, refs: list[CardRef]) -> None:
    """这次上下文里带上了这几张卡片。"""
    if not refs:
        return
    pending = _load(root)
    seen = dict.fromkeys(pending.by_task.get(task_id, []))
    for ref in refs:
        seen.setdefault(ref.key, None)
    pending.by_task[task_id] = list(seen)
    _save(root, pending)


def take_exposures(root: Path, task_id: str) -> tuple[CardRef, ...]:
    """取出并清掉这个任务的曝光记录。

    **失败的任务也要取出来清掉**,只是不记账——留着的话,同一个任务重跑会被重复计数,
    而那让计数从"有效性"退化成"被重跑过几次"。
    """
    pending = _load(root)
    keys = pending.by_task.pop(task_id, [])
    if keys:
        _save(root, pending)
    return tuple(CardRef.parse(key) for key in keys)


def record_credits(root: Path, refs: tuple[CardRef, ...]) -> None:
    """把结算好的命中记进待记账。

    **编排器写的是账本,不是知识树。** 它记"这几张卡片这次算数了",至于什么时候写进卡片、
    由谁写,是知识写入路径的事——见模块说明里那条比计数本身更重要的约束。

    同一张卡片可以出现多次:两个任务各命中一次就该加两次。
    """
    if not refs:
        return
    path = Path(root) / CREDITS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = _load_credits(path)
    pending.extend(ref.key for ref in refs)
    _write_credits(path, pending)


def pending_credits(root: Path) -> tuple[CardRef, ...]:
    """看一眼待记账,**不清掉**。

    单独一个只读的入口是给"只报告不写"那条路用的:让它调 `take_credits` 的话,一次
    `--dry-run` 会把这批命中清空,而它们再也回不来——一个把数据吃掉的预演比没有预演更糟。
    """
    return tuple(CardRef.parse(key) for key in _load_credits(Path(root) / CREDITS_FILE))


def take_credits(root: Path) -> tuple[CardRef, ...]:
    """取出并清掉待记账。

    **先清账再写卡片。** 反过来的话,写到一半崩溃会让这一批命中被记两次——而计数一旦虚高,
    "哪些知识真正被用上了"这个问题就再也答不准了。宁可少记一次:少记的下一轮还会再来,
    多记的没有任何办法回退。
    """
    path = Path(root) / CREDITS_FILE
    pending = _load_credits(path)
    if pending:
        _write_credits(path, [])
    return tuple(CardRef.parse(key) for key in pending)


def _write_credits(path: Path, credits: list[str]) -> None:
    """先写临时文件再改名。

    **半份 JSON 会被下一次读当成"账本坏了"而整批丢掉**——而这个账本里的每一条都对应一次
    真实发生过的命中,丢了就再也补不回来。改名是原子的,读到的要么是旧的整份、要么是新的整份。
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"credits": credits}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class LedgerUnreadable(RuntimeError):
    """账本读不出来。"""


def _load_credits(path: Path) -> list[str]:
    """读账本。**读不出来就抛,不当成空。**

    当成空的话,一次损坏的读会让下一次写把整批待记账覆盖掉——而里面每一条都对应一次真实
    发生过的命中,丢了再也补不回来。宁可让调用方看到一次报错:账本坏了是要人看一眼的事。
    """
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LedgerUnreadable(f"命中账本读不出来({path}): {error}") from error
    return [str(item) for item in payload.get("credits") or []]


def apply_credits(root: Path, tree: KnowledgeTree, refs: tuple[CardRef, ...]) -> list[str]:
    """把计数写进卡片。**这是知识写入路径的工具,不是编排器的。**

    调用它的应该是架构员工的知识更新 Procedure(它本来就要动 `genome/**` 并走知识 PR),
    而不是任务终态的副作用——见模块说明。
    """
    updated: list[str] = []
    for ref in refs:
        card = tree.card(ref.module_id, ref.feature_id)
        feature = next(
            (item for item in tree.features(ref.module_id) if item.id == ref.feature_id), None
        )
        if card is None or feature is None or feature.card is None:
            continue
        path = module_dir(root, ref.module_id) / feature.card
        if not path.is_file():
            continue
        front, body = split_front_matter(path.read_text(encoding="utf-8"), str(path))
        front["hits"] = int(front.get("hits") or 0) + 1
        path.write_text(_render(front, body), encoding="utf-8")
        updated.append(ref.key)
    return updated


def _render(front: dict[str, object], body: str) -> str:
    import yaml

    matter = yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
    return f"---\n{matter}---\n{body}\n"


__all__ = [
    "CREDITS_FILE",
    "PENDING_FILE",
    "CardRef",
    "LedgerUnreadable",
    "apply_credits",
    "pending_credits",
    "record_credits",
    "record_exposure",
    "take_credits",
    "take_exposures",
]
