"""需求实体与仓储。

需求是承载"再试一次"的持久容器:研发任务刻意不可复用(同一个需求再试一次是一个新任务),
于是"这件事的连续性"需要一个自己的落点——需求 1:N 于它的历次尝试。

## 状态是算出来的,不是存着的

这张表**没有 status 列**。可推导的状态存一份副本,副本迟早与事实分叉——与「生效模块」
同一条纪律。唯一落库的是人的判断:`parked`(搁置原因,空串表示没搁置)。其余全部从
尝试链推导,规则见 `derive_state`。

## 任务上的 `requirement` 是快照,不是引用

任务创建时把需求的当前文本抄进 `Task.requirement`,此后两者各走各的:需求文本可改,
改动不回写任何已存在的任务——复盘时"任务当时读到什么"不该被后来的编辑改写。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from agentgenome.core.events import ActorKind, EventLog, LogKind
from agentgenome.core.states import TaskState
from agentgenome.core.store import SqliteStore
from agentgenome.core.task import Task


class RequirementNotFound(LookupError):
    """没有这个需求。"""


class RequirementState(StrEnum):
    """需求现在什么状态。**推导值,不落库**——见模块说明。"""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    PARKED = "parked"


def derive_state(
    requirement: Requirement,
    attempts: Sequence[Task],
    children_states: Sequence[RequirementState] = (),
) -> RequirementState:
    """从尝试链(与子需求,有的话)推导需求状态。

    优先序是刻意的:搁置压过一切(它是人的显式判断);已交付压过进行中(交付之后又发起
    跟进尝试是合法的,列表上它仍然是"已交付");ESCALATED 的尝试**不算进行中**——那次
    尝试停了,需求回到排队,等下一次。

    树规则压过平面规则(PRD 48 D5):拆分确认后母需求自己唯一的尝试是 CANCELLED 的
    提案任务,平面规则会推出"排队中"——而它的子需求可能正烧着 token。所以任何一个
    子需求进行中,母需求就是进行中;全部子需求停在升级人工时它**如实回到排队中**,
    与"ESCALATED 的尝试不算进行中"同一个道理。母需求的"已交付"仍然只认自己名下的
    COMPLETED 尝试——那是收口尝试的位置,"所有子需求都交付了"不偷换成"母需求交付了"。
    """
    if requirement.parked:
        return RequirementState.PARKED
    states = {attempt.state for attempt in attempts}
    if TaskState.COMPLETED in states:
        return RequirementState.DELIVERED
    if any(state not in TaskState.terminal() for state in states):
        return RequirementState.IN_PROGRESS
    if any(child is RequirementState.IN_PROGRESS for child in children_states):
        return RequirementState.IN_PROGRESS
    return RequirementState.QUEUED


def derive_forest(
    requirements: Sequence[Requirement],
    attempts_by_requirement: Mapping[str, Sequence[Task]],
) -> dict[str, RequirementState]:
    """整片需求的状态一次算完,树自底向上。

    **所有读状态的地方都该走这一条**(直接调 `derive_state` 的话拿不到子需求那一维,
    症状是某个列表里母需求显示"排队中",而它的子需求正在烧 token——PRD 48 R3)。
    平面需求(没有 children)与今天逐字节同一个结果。
    """
    children_of: dict[str, list[Requirement]] = {}
    for requirement in requirements:
        if requirement.parent_id:
            children_of.setdefault(requirement.parent_id, []).append(requirement)

    states: dict[str, RequirementState] = {}

    def resolve(requirement: Requirement) -> RequirementState:
        found = states.get(requirement.id)
        if found is not None:
            return found
        children = tuple(resolve(child) for child in children_of.get(requirement.id, ()))
        state = derive_state(
            requirement, attempts_by_requirement.get(requirement.id, ()), children
        )
        states[requirement.id] = state
        return state

    for requirement in requirements:
        resolve(requirement)
    return states


@dataclass(frozen=True)
class RequirementScene:
    """需求面的一次性读盘:全部实体、按需求分组且按时间排的尝试、整片状态、树结构。

    **读需求状态的每一处都该从这里拿**(PRD 48 R3):直接调 `derive_state` 拿不到子需求
    那一维,症状是母需求显示"排队中"而它的子需求正烧 token。server、CLI、编排器共用
    这一份——各自读盘的话,派发判据与展示口径迟早分叉,而分叉没有任何症状。
    """

    requirements: tuple[Requirement, ...]
    attempts_by: dict[str, tuple[Task, ...]]
    states: dict[str, RequirementState]

    @classmethod
    def load(cls, workspace_root: Path) -> RequirementScene:
        from agentgenome.core.task import TaskStore  # 延迟导入:task 不认识 requirement

        requirements = RequirementStore(workspace_root).all()
        grouped: dict[str, list[Task]] = {}
        for task in TaskStore(workspace_root).all_tasks():
            if task.requirement_id:
                grouped.setdefault(task.requirement_id, []).append(task)
        attempts_by = {
            key: tuple(sorted(chain, key=lambda item: (item.created_at, item.id)))
            for key, chain in grouped.items()
        }
        return cls(
            requirements=requirements,
            attempts_by=attempts_by,
            states=derive_forest(requirements, attempts_by),
        )

    def attempts(self, requirement_id: str) -> tuple[Task, ...]:
        return self.attempts_by.get(requirement_id, ())

    def children(self, parent_id: str) -> tuple[Requirement, ...]:
        """一个母需求名下的子需求,按创建顺序(即拆分提案的批内序)。"""
        return tuple(
            sorted(
                (item for item in self.requirements if item.parent_id == parent_id),
                key=lambda item: (item.created_at, item.id),
            )
        )

    def tree_tokens(self, requirement_id: str) -> int:
        """自己 + 全部后代的账。树级计量必须一眼可见(预算闸门按过渡政策后置)。"""
        own = sum(item.tokens_used for item in self.attempts(requirement_id))
        return own + sum(self.tree_tokens(child.id) for child in self.children(requirement_id))

    def ready_to_start(self, parent_id: str) -> tuple[Requirement, ...]:
        """条件齐备、该建首次尝试的子需求:没被搁置、没有任何尝试、前置全部已交付。

        **这是需求层唯一的派发判据**——编排器按它建尝试,CLI 按它报"待推进"。
        各写一遍的话,树页说"没有待推进"而派发规则却建出任务,谁都解释不了。
        """
        parent = next((item for item in self.requirements if item.id == parent_id), None)
        if parent is None or parent.parked:
            return ()
        return tuple(
            child
            for child in self.children(parent_id)
            if not child.parked
            and not self.attempts(child.id)
            and all(
                self.states.get(dependency) is RequirementState.DELIVERED
                for dependency in child.blocked_by
            )
        )


@dataclass(frozen=True)
class Requirement:
    """一个需求。不可变,改动走 `evolve` 生成新值再存——与 `Task` 同一条纪律。"""

    id: str
    title: str
    #: 当前文本。历史版本在事件面与各次尝试的快照里,不在这里。
    text: str
    priority: int = 5
    #: 搁置原因。**空串表示没搁置**——搁置必须带原因(空原因的搁置会与"恢复"分不开)。
    parked: str = ""
    #: 母需求 id。空串 = 不在任何树上(存量需求与普通需求都是这样,PRD 48 D2)。
    parent_id: str = ""
    #: 本批兄弟里的前置(需求 id)。**只准指兄弟**——跨树依赖先用文本写,等真实需要。
    blocked_by: tuple[str, ...] = ()
    #: 这个子需求是哪个提案任务落下来的。空串 = 不是拆分落的(人提交的普通需求)。
    #: **它是落树幂等的键**:同一个提案任务重放落树时,靠它认出"这批已经落过"——
    #: 与创建同一笔事务,不像事件面那样存在"建了但没记上"的缝。
    origin_task: str = ""
    created_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    updated_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)

    def evolve(self, **changes: Any) -> Requirement:
        return replace(self, **changes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "text": self.text,
            "priority": self.priority,
            "parked": self.parked,
            "parent_id": self.parent_id,
            "blocked_by": json.dumps(list(self.blocked_by), ensure_ascii=False),
            "origin_task": self.origin_task,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


_SCHEMA = """
create table if not exists requirements (
    id text primary key,
    title text not null,
    text text not null,
    priority integer not null,
    parked text not null default '',
    parent_id text not null default '',
    blocked_by text not null default '[]',
    origin_task text not null default '',
    created_at text not null,
    updated_at text not null
);
"""

_COLUMNS = (
    "id",
    "title",
    "text",
    "priority",
    "parked",
    "parent_id",
    "blocked_by",
    "origin_task",
    "created_at",
    "updated_at",
)


class RequirementStore(SqliteStore):
    """需求的持久化仓储。与任务同库(`tasks/agentgenome.db`),SQL 只出现在这里。"""

    schema = _SCHEMA
    # 老库补列:`create table if not exists` 对已存在的表什么都不做,新列两边都要写。
    added_columns = (
        ("requirements", "parent_id", "parent_id text not null default ''"),
        ("requirements", "blocked_by", "blocked_by text not null default '[]'"),
        ("requirements", "origin_task", "origin_task text not null default ''"),
    )

    def create(
        self,
        title: str,
        text: str,
        priority: int = 5,
        now: datetime | None = None,
    ) -> Requirement:
        """建一个新需求并分配编号。编号纪律与 `TaskStore.create` 相同:一次事务里分配并写入。"""
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        day = stamp.strftime("%Y%m%d")
        requirement = Requirement(
            id="", title=title, text=text, priority=priority, created_at=stamp, updated_at=stamp
        )
        with self._connect() as connection:
            connection.execute("begin immediate")
            taken = connection.execute(
                "select id from requirements where id like ? order by id desc limit 1",
                (f"req-{day}-%",),
            ).fetchone()
            sequence = int(taken[0].rsplit("-", 1)[1]) + 1 if taken else 1
            requirement = requirement.evolve(id=f"req-{day}-{sequence:03d}")
            placeholders = ", ".join("?" * len(_COLUMNS))
            connection.execute(
                f"insert into requirements ({', '.join(_COLUMNS)}) values ({placeholders})",
                self._to_row(requirement),
            )
        return requirement

    def save(self, requirement: Requirement, now: datetime | None = None) -> Requirement:
        updated = requirement.evolve(updated_at=(now or datetime.now(UTC)).astimezone(UTC))
        assignments = ", ".join(f"{name} = ?" for name in _COLUMNS[1:])
        row = self._to_row(updated)
        with self._connect() as connection:
            connection.execute(
                f"update requirements set {assignments} where id = ?", (*row[1:], updated.id)
            )
        return updated

    def get(self, requirement_id: str) -> Requirement:
        with self._connect() as connection:
            row = connection.execute(
                f"select {', '.join(_COLUMNS)} from requirements where id = ?", (requirement_id,)
            ).fetchone()
        if row is None:
            raise RequirementNotFound(f"没有这个需求: {requirement_id}")
        return self._from_row(row)

    def all(self) -> tuple[Requirement, ...]:
        """全部需求,新的在前——列表页问的是"最近提的那些怎么样了"。"""
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from requirements order by created_at desc, id desc"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _to_row(requirement: Requirement) -> tuple[Any, ...]:
        payload = requirement.as_dict()
        return tuple(payload[name] for name in _COLUMNS)

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Requirement:
        values = dict(zip(_COLUMNS, row, strict=True))
        return Requirement(
            id=values["id"],
            title=values["title"],
            text=values["text"],
            priority=values["priority"],
            parked=values["parked"] or "",
            parent_id=values["parent_id"] or "",
            blocked_by=tuple(json.loads(values["blocked_by"] or "[]")),
            origin_task=values["origin_task"] or "",
            created_at=datetime.fromisoformat(values["created_at"]),
            updated_at=datetime.fromisoformat(values["updated_at"]),
        )

    def children_of(self, parent_id: str) -> tuple[Requirement, ...]:
        """一个母需求名下的子需求,按创建顺序——这就是拆分提案里的批内序。"""
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from requirements "
                "where parent_id = ? order by created_at asc, id asc",
                (parent_id,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def depth_of(self, requirement_id: str) -> int:
        """这个需求在树上的深度:根 = 0,子 = 1,孙 = 2。沿 `parent_id` 链算出来。"""
        depth = 0
        current = self.get(requirement_id)
        while current.parent_id:
            depth += 1
            current = self.get(current.parent_id)
        return depth

    def create_children(
        self,
        parent_id: str,
        children: Sequence[tuple[str, str, Sequence[int]]],
        priority: int,
        origin_task: str = "",
        now: datetime | None = None,
    ) -> tuple[Requirement, ...]:
        """把一批 `(title, text, blocked_by 批内序号)` 原子地落成子需求。

        **一笔事务**:环、超限该在确认那一刻全被拒掉,走到这里只剩"要么全落、要么全不落"
        ——落一半的批次会让 `blocked_by` 指向不存在的兄弟,而那是一个查不出来的静默死锁。
        序号在这里换算成真实 id:先按批内顺序分配全部 id,再解引用。
        """
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        day = stamp.strftime("%Y%m%d")
        placeholders = ", ".join("?" * len(_COLUMNS))
        with self._connect() as connection:
            connection.execute("begin immediate")
            taken = connection.execute(
                "select id from requirements where id like ? order by id desc limit 1",
                (f"req-{day}-%",),
            ).fetchone()
            sequence = int(taken[0].rsplit("-", 1)[1]) if taken else 0
            ids = [f"req-{day}-{sequence + offset + 1:03d}" for offset in range(len(children))]
            landed = []
            for (title, text, blocked_by), child_id in zip(children, ids, strict=True):
                child = Requirement(
                    id=child_id,
                    title=title,
                    text=text,
                    priority=priority,
                    parent_id=parent_id,
                    blocked_by=tuple(ids[index] for index in blocked_by),
                    origin_task=origin_task,
                    created_at=stamp,
                    updated_at=stamp,
                )
                connection.execute(
                    f"insert into requirements ({', '.join(_COLUMNS)}) values ({placeholders})",
                    self._to_row(child),
                )
                landed.append(child)
        return tuple(landed)


def intake(
    workspace_root: Path,
    *,
    title: str,
    text: str,
    priority: int,
    actor: str,
    actor_kind: ActorKind | None = None,
    requirement_id: str | None = None,
) -> Requirement:
    """提交入口的需求侧:没给 id 就新建需求;给了就核对存在并把当前文本同步成这一版。

    表单、CLI、IM 三个入口共用这一个函数——各写一遍的话,第一次改事件 payload 就会分叉,
    而分叉之后没有任何测试会红。事件记在**需求 id 名下**,任务事件只带指针。

    "再试一次时改的措辞"就是需求的最新表述(PRD 43 D5):同步当前文本,但**不回写任何
    已存在的任务**——它们各自的快照就是复盘时的证据。给了 id 但没有这个需求时抛
    `RequirementNotFound`,由调用方翻译成各自入口的报错;报错正文因此逐字相同。
    """
    store = RequirementStore(workspace_root)
    log = EventLog(workspace_root)
    if requirement_id is None:
        requirement = store.create(title=title, text=text, priority=priority)
        log.append(
            requirement.id,
            actor=actor,
            actor_kind=actor_kind,
            kind=LogKind.REQUIREMENT_CREATED,
            payload={"title": requirement.title, "priority": requirement.priority},
        )
        return requirement
    requirement = store.get(requirement_id)
    if requirement.text != text:
        requirement = store.save(requirement.evolve(text=text))
        log.append(
            requirement.id,
            actor=actor,
            actor_kind=actor_kind,
            kind=LogKind.REQUIREMENT_CHANGED,
            payload={"action": "text", "via": "retry"},
        )
    return requirement


def revise(
    workspace_root: Path,
    requirement_id: str,
    *,
    actor: str,
    actor_kind: ActorKind | None = None,
    text: str | None = None,
    priority: int | None = None,
    park: str | None = None,
    resume: bool = False,
) -> Requirement:
    """改一个需求:文本、优先级、搁置、恢复。每个动作在需求 id 名下记一条事件。

    **搁置必须带原因,恢复是独立的动作**——用"空原因的搁置"表示恢复的话,一次手滑的
    空提交就会把搁置的需求悄悄放回队列,而两者是相反的动作。冲突(又搁置又恢复)与空
    原因都当场拒绝,抛 `ValueError`;REST 与 CLI 各自翻译,报错正文因此逐字相同。

    改文本**不回写任何已存在的任务**(快照语义,见模块说明)。搁置也不取消尝试:它是
    对需求的判断,不是对任务的;正在跑的任务照常推进。
    """
    if park is not None and resume:
        raise ValueError("搁置与恢复是相反的动作,不能同时要。")
    if park is not None and not park.strip():
        raise ValueError("搁置需要一句原因——空原因的搁置与恢复分不开。")

    store = RequirementStore(workspace_root)
    log = EventLog(workspace_root)
    requirement = store.get(requirement_id)

    actions: list[str] = []
    if text is not None and text != requirement.text:
        requirement = requirement.evolve(text=text)
        actions.append("text")
    if priority is not None and priority != requirement.priority:
        requirement = requirement.evolve(priority=priority)
        actions.append("priority")
    if park is not None:
        requirement = requirement.evolve(parked=park.strip())
        actions.append("park")
    if resume and requirement.parked:
        requirement = requirement.evolve(parked="")
        actions.append("resume")

    if not actions:
        return requirement
    requirement = store.save(requirement)
    for action in actions:
        log.append(
            requirement.id,
            actor=actor,
            actor_kind=actor_kind,
            kind=LogKind.REQUIREMENT_CHANGED,
            payload={"action": action},
        )
    return requirement


__all__ = [
    "Requirement",
    "RequirementNotFound",
    "RequirementState",
    "RequirementScene",
    "RequirementStore",
    "derive_forest",
    "derive_state",
    "intake",
    "revise",
]
