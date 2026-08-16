"""任务模型与仓储。

**状态即事实。** 编排器不在内存里保存真相——跑到一半机器重启,任务得能从上次断的地方继续,
而这要求进度是持久化的事实,不是内存里的一个变量。这个模块是那个"事实"住的地方。

## 为什么是裸 sqlite3

只有两张表,而 ORM 会拖进 SQLAlchemy 的 session 生命周期——在 asyncio 里那一层要额外想清楚
何时开何时关,收益抵不过成本。仓储层把 SQL 关在里面,上层只见领域对象;后面接 REST 时两条路
走的是同一个仓储。

## task.json 不是真相来源

它是容灾与人眼可读:数据库损坏时我至少还能打开任务目录看看这个任务是谁、卡在哪。反过来用它
当真相就会有两份会分叉的状态,而分叉之后没有任何办法判断哪一份是对的。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from agentgenome.core.states import TaskState
from agentgenome.core.store import SqliteStore

#: 任务目录里那份人眼可读的快照。
SNAPSHOT = "task.json"


class TaskNotFound(LookupError):
    """没有这个任务。"""


class AttemptConflict(ValueError):
    """当前需求不能创建新的研发尝试。"""


class ItestNeed(StrEnum):
    """要不要跑集成测试。

    **三态而非布尔。** "还没判断"与"判断为否"是两件事:合成一个布尔的话,判定环节压根没跑
    和判定结果为否长得一模一样,而前者是 bug、后者是正常流程。
    """

    UNDECIDED = "undecided"
    YES = "yes"
    NO = "no"


class ItestOverride(StrEnum):
    """人对这个任务的集成测试有没有发话。

    **与 `ItestNeed` 分开存。** 合成一个字段的话,"人说了必须跑"与"规则算出来要跑"长得
    一模一样,而后者会被下一次重新判定覆盖掉,前者不该被。
    """

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class TaskMode(StrEnum):
    """这个任务的 DEVELOPING 态怎么执行。

    **状态机、迁移表、终态判定全部不动**——唯一的差别是 DEVELOPING 态的 Handler 看到
    `INTERACTIVE` 时不派发自主 Job,而是创建一个结对会话并等人结束它。会话结束等价于
    `dev_done`,后续照原路走:门禁 → 集测判定 → 安全提交 → 审批。

    **人参与不等于少走流程。** 为交互式任务新增状态或跳过某一关,等于给"人在场"开了一个
    绕过质量闸的口子,而那正是这条设计要防的事。
    """

    AUTONOMOUS = "autonomous"
    INTERACTIVE = "interactive"


class TaskRunStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    INTERRUPTED = "interrupted"
    FINISHED = "finished"
    #: CREATED 已有解析失败，但仍有自动重试机会。这是 API 投影，通常不持久化。
    RETRY_PENDING = "retry_pending"


@dataclass(frozen=True)
class Task:
    """一个任务的全部状态。

    **不可变。** 改状态走 `evolve` 生成新值再存——就地改字段的话,"这个对象现在是库里的样子
    还是改了一半的样子"没人说得清。
    """

    id: str
    title: str
    requirement: str
    #: 所属需求。**None 而不是空串**:存量任务没有需求,"没有"与"查不到"要分得开,
    #: 也不回填——回填靠标题猜,猜错一次就是一条假链,而假链比没有链更糟(有人会信它)。
    #: `requirement` 字段是这次尝试开工那一刻的**快照**,此后与需求实体各走各的。
    requirement_id: str | None = None
    state: TaskState = TaskState.CREATED
    branch: str | None = None
    priority: int = 5
    fix_rounds: int = 0
    #: 需求解析重试了几次。与 `fix_rounds` 分开:前者上限 1(需求说不清,多试没用),
    #: 后者上限 3。合成一个计数器的话两条不同的上限就没法各管各的。
    plan_retries: int = 0
    needs_itest: ItestNeed = ItestNeed.UNDECIDED
    #: 人对这个任务的集成测试发过的话。优先级高于规则与 AI 兜底。
    itest_override: ItestOverride = ItestOverride.AUTO
    #: 自主执行还是交互式(结对)。默认自主——既有任务行为一个字不变。
    mode: TaskMode = TaskMode.AUTONOMOUS
    #: 这个任务正在等哪张待办。空表示没人被等着。
    #:
    #: **它是"能不能推"的判据**:挂着待办的任务不该被机器推——推了也只会再投一张一模一样
    #: 的待办,而两张都"合法"。与"已升级人工"分得很清:这是**待确认**,任务是健康的。
    pending_todo_id: str = ""
    #: 这个任务的状态内部跑哪张图。空表示按根配置走(`topology.default`)。
    #:
    #: **存空串而不是把根配置的值抄进来**:抄进来的话,改了根配置之后已建的任务仍按旧值跑,
    #: 而没有任何地方能看出它们为什么不一样。
    topology: str = ""
    risk_level: str | None = None
    budget_tokens: int | None = None
    tokens_used: int = 0
    #: 生命周期之外的持久执行投影。服务重启后 RUNNING 会被解释为 INTERRUPTED。
    run_status: TaskRunStatus = TaskRunStatus.IDLE
    escalate_reason: str | None = None
    #: 人工介入是否已经处置完。**不改写 ESCALATED 状态**:失败事实留在状态机里，
    #: 这里只回答「这张人工待办还要不要继续占着任务中心」。
    intervention_resolved_at: datetime | None = None
    #: 通过「修改需求并再试一次」处置时创建的后继任务。它让请求重放可以返回同一个
    #: 任务，而不是重复创建；普通「标记已处理」保持为空。
    intervention_successor_task_id: str | None = None
    created_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    updated_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)

    def evolve(self, **changes: Any) -> Task:
        return replace(self, **changes)

    @property
    def is_terminal(self) -> bool:
        return self.state in TaskState.terminal()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "requirement": self.requirement,
            "requirement_id": self.requirement_id,
            "state": self.state.value,
            "branch": self.branch,
            "priority": self.priority,
            "fix_rounds": self.fix_rounds,
            "plan_retries": self.plan_retries,
            "needs_itest": self.needs_itest.value,
            "itest_override": self.itest_override.value,
            "mode": self.mode.value,
            "pending_todo_id": self.pending_todo_id,
            "topology": self.topology,
            "risk_level": self.risk_level,
            "budget_tokens": self.budget_tokens,
            "tokens_used": self.tokens_used,
            "run_status": self.run_status.value,
            "escalate_reason": self.escalate_reason,
            "intervention_resolved_at": (
                self.intervention_resolved_at.isoformat()
                if self.intervention_resolved_at is not None
                else None
            ),
            "intervention_successor_task_id": self.intervention_successor_task_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


_SCHEMA = """
create table if not exists tasks (
    id text primary key,
    title text not null,
    requirement text not null,
    requirement_id text,
    state text not null,
    branch text,
    priority integer not null,
    fix_rounds integer not null,
    plan_retries integer not null,
    needs_itest text not null,
    itest_override text not null default 'auto',
    mode text not null default 'autonomous',
    pending_todo_id text not null default '',
    topology text not null default '',
    risk_level text,
    budget_tokens integer,
    tokens_used integer not null,
    run_status text not null default 'idle',
    escalate_reason text,
    intervention_resolved_at text,
    intervention_successor_task_id text,
    created_at text not null,
    updated_at text not null
);
create index if not exists tasks_by_state on tasks (state);
"""

_COLUMNS = (
    "id",
    "title",
    "requirement",
    "requirement_id",
    "state",
    "branch",
    "priority",
    "fix_rounds",
    "plan_retries",
    "needs_itest",
    "itest_override",
    "mode",
    "pending_todo_id",
    "topology",
    "risk_level",
    "budget_tokens",
    "tokens_used",
    "run_status",
    "escalate_reason",
    "intervention_resolved_at",
    "intervention_successor_task_id",
    "created_at",
    "updated_at",
)


class TaskStore(SqliteStore):
    """任务的持久化仓储。SQL 只出现在这里。"""

    schema = _SCHEMA
    added_columns = (
        ("tasks", "itest_override", "itest_override text not null default 'auto'"),
        ("tasks", "mode", "mode text not null default 'autonomous'"),
        ("tasks", "topology", "topology text not null default ''"),
        ("tasks", "pending_todo_id", "pending_todo_id text not null default ''"),
        # 可空、无默认:老库里的存量任务就该是 NULL(见 `Task.requirement_id`)。
        ("tasks", "requirement_id", "requirement_id text"),
        ("tasks", "intervention_resolved_at", "intervention_resolved_at text"),
        (
            "tasks",
            "intervention_successor_task_id",
            "intervention_successor_task_id text",
        ),
        ("tasks", "run_status", "run_status text not null default 'idle'"),
    )

    # --- 写 ------------------------------------------------------------------

    def create(
        self,
        title: str,
        requirement: str,
        priority: int = 5,
        budget_tokens: int | None = None,
        itest_override: ItestOverride = ItestOverride.AUTO,
        mode: TaskMode = TaskMode.AUTONOMOUS,
        topology: str = "",
        requirement_id: str | None = None,
        now: datetime | None = None,
    ) -> Task:
        """建一个新任务并分配编号。

        编号在**一次事务里**分配并写入:先查最大值再插入的话,两个进程同时提交会拿到同一个号,
        而重复的编号会让后来的那个任务把前一个覆盖掉。这里靠主键唯一约束兜底,撞了就重试。
        """
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        day = stamp.strftime("%Y%m%d")
        task = Task(
            id="",
            title=title,
            requirement=requirement,
            priority=priority,
            budget_tokens=budget_tokens,
            itest_override=itest_override,
            mode=mode,
            topology=topology,
            requirement_id=requirement_id,
            created_at=stamp,
            updated_at=stamp,
        )
        with self._connect() as connection:
            # BEGIN IMMEDIATE:读到的最大序号在插入前不会被别的写者改掉。
            connection.execute("begin immediate")
            taken = connection.execute(
                "select id from tasks where id like ? order by id desc limit 1", (f"ag-{day}-%",)
            ).fetchone()
            sequence = int(taken[0].rsplit("-", 1)[1]) + 1 if taken else 1
            task = task.evolve(id=f"ag-{day}-{sequence:03d}")
            self._insert(connection, task)
        self._write_snapshot(task)
        return task

    def save(self, task: Task, now: datetime | None = None) -> Task:
        """覆写一个已有任务,并刷新它的快照。"""
        updated = task.evolve(updated_at=(now or datetime.now(UTC)).astimezone(UTC))
        assignments = ", ".join(f"{name} = ?" for name in _COLUMNS[1:])
        row = self._to_row(updated)
        with self._connect() as connection:
            connection.execute(
                f"update tasks set {assignments} where id = ?", (*row[1:], updated.id)
            )
        self._write_snapshot(updated)
        return updated

    def create_retry(
        self,
        *,
        requirement_id: str,
        title: str,
        requirement: str,
        priority: int,
        budget_tokens: int | None,
        itest_override: ItestOverride,
        mode: TaskMode,
        topology: str,
        source_task_id: str | None = None,
        now: datetime | None = None,
    ) -> RetryAttempt:
        """原子地更新需求文本并创建一次后继尝试。

        `source_task_id` 存在时，这也是一次人工介入处置：旧任务保持 ``ESCALATED``，
        但与新任务在同一事务里记下处置时间和后继指针。后继指针同时提供请求幂等性。
        """
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        source: Task | None = None
        requirement_changed = False
        created = True
        with self._connect() as connection:
            connection.execute("begin immediate")
            requirement_row = connection.execute(
                "select text, parked from requirements where id = ?", (requirement_id,)
            ).fetchone()
            if requirement_row is None:
                raise AttemptConflict(f"没有这个需求: {requirement_id}")
            if requirement_row[1]:
                raise AttemptConflict(f"需求 {requirement_id} 已搁置，请先恢复再发起新尝试")

            if source_task_id is not None:
                source_row = connection.execute(
                    f"select {', '.join(_COLUMNS)} from tasks where id = ?", (source_task_id,)
                ).fetchone()
                if source_row is None:
                    raise TaskNotFound(f"没有这个任务: {source_task_id}")
                source = self._from_row(source_row)
                if source.requirement_id != requirement_id:
                    raise AttemptConflict(f"{source.id} 不属于需求 {requirement_id}")
                if source.state is not TaskState.ESCALATED:
                    raise AttemptConflict(f"{source.id} 当前不需要人工介入")
                if source.intervention_resolved_at is not None:
                    successor_id = source.intervention_successor_task_id
                    if successor_id is None:
                        raise AttemptConflict(f"{source.id} 的人工介入已经处理完")
                    successor_row = connection.execute(
                        f"select {', '.join(_COLUMNS)} from tasks where id = ?", (successor_id,)
                    ).fetchone()
                    if successor_row is None:
                        raise AttemptConflict(f"{source.id} 记录的后继任务 {successor_id} 不存在")
                    return RetryAttempt(
                        task=self._from_row(successor_row),
                        source=source,
                        requirement_changed=False,
                        created=False,
                    )

            terminal = tuple(state.value for state in sorted(TaskState.terminal()))
            placeholders = ", ".join("?" * len(terminal))
            active = connection.execute(
                "select id from tasks where requirement_id = ? "
                f"and state not in ({placeholders}) order by created_at asc limit 1",
                (requirement_id, *terminal),
            ).fetchone()
            if active is not None:
                raise AttemptConflict(
                    f"需求 {requirement_id} 已有进行中的尝试 {active[0]}，不能重复发起"
                )

            requirement_changed = requirement_row[0] != requirement
            if requirement_changed:
                connection.execute(
                    "update requirements set text = ?, updated_at = ? where id = ?",
                    (requirement, stamp.isoformat(), requirement_id),
                )

            day = stamp.strftime("%Y%m%d")
            taken = connection.execute(
                "select id from tasks where id like ? order by id desc limit 1", (f"ag-{day}-%",)
            ).fetchone()
            sequence = int(taken[0].rsplit("-", 1)[1]) + 1 if taken else 1
            task = Task(
                id=f"ag-{day}-{sequence:03d}",
                title=title,
                requirement=requirement,
                requirement_id=requirement_id,
                priority=priority,
                budget_tokens=budget_tokens,
                itest_override=itest_override,
                mode=mode,
                topology=topology,
                created_at=stamp,
                updated_at=stamp,
            )
            self._insert(connection, task)

            if source is not None:
                source = source.evolve(
                    intervention_resolved_at=stamp,
                    intervention_successor_task_id=task.id,
                    updated_at=stamp,
                )
                connection.execute(
                    "update tasks set intervention_resolved_at = ?, "
                    "intervention_successor_task_id = ?, updated_at = ? where id = ?",
                    (stamp.isoformat(), task.id, stamp.isoformat(), source.id),
                )

        self._write_snapshot(task)
        if source is not None:
            self._write_snapshot(source)
        return RetryAttempt(
            task=task,
            source=source,
            requirement_changed=requirement_changed,
            created=created,
        )

    # --- 读 ------------------------------------------------------------------

    def get(self, task_id: str) -> Task:
        with self._connect() as connection:
            row = connection.execute(
                f"select {', '.join(_COLUMNS)} from tasks where id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFound(f"没有这个任务: {task_id}")
        return self._from_row(row)

    def open_tasks(self) -> tuple[Task, ...]:
        """全部未终结的任务,按优先级降序、创建时间升序。

        **调度队列从这里派生,不在内存里另存一份。** 内存队列崩溃后就没了,而任务还在库里
        等着——表现为"重启之后有些任务再也不动了"。
        """
        terminal = tuple(state.value for state in sorted(TaskState.terminal()))
        placeholders = ", ".join("?" * len(terminal))
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from tasks "
                f"where state not in ({placeholders}) "
                "order by priority desc, created_at asc, id asc",
                terminal,
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def unsettled_tasks(self) -> tuple[Task, ...]:
        """人还需要看见的任务,排序同 `open_tasks`。

        **和 `open_tasks` 差一个 `ESCALATED`,而这个差别就是它存在的全部理由。**
        `open_tasks` 派生的是调度队列,升级人工的任务当然不该在里面;但界面与 `agctl
        task list` 问的是另一个问题——"有什么要我管的"。此前两边共用 `open_tasks`,
        症状是提交完任务、它一路走到升级人工,然后从列表里彻底消失,看起来像从没提交过。
        """
        settled = tuple(state.value for state in sorted(TaskState.settled()))
        placeholders = ", ".join("?" * len(settled))
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from tasks "
                f"where state not in ({placeholders}) and intervention_resolved_at is null "
                "order by priority desc, created_at asc, id asc",
                settled,
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def attempts_of(self, requirement_id: str) -> tuple[Task, ...]:
        """一个需求名下的历次尝试,按发起时间排——链就是这个顺序本身。"""
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from tasks "
                "where requirement_id = ? order by created_at asc, id asc",
                (requirement_id,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def all_tasks(self) -> tuple[Task, ...]:
        """全部任务,不分终态与否。成本看板与审计检索要看到已完结的任务。"""
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from tasks order by created_at desc, id desc"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    # --- 内部 ----------------------------------------------------------------

    @staticmethod
    def _insert(connection: sqlite3.Connection, task: Task) -> None:
        placeholders = ", ".join("?" * len(_COLUMNS))
        connection.execute(
            f"insert into tasks ({', '.join(_COLUMNS)}) values ({placeholders})",
            TaskStore._to_row(task),
        )

    @staticmethod
    def _to_row(task: Task) -> tuple[Any, ...]:
        payload = task.as_dict()
        return tuple(payload[name] for name in _COLUMNS)

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Task:
        values = dict(zip(_COLUMNS, row, strict=True))
        return Task(
            id=values["id"],
            title=values["title"],
            requirement=values["requirement"],
            requirement_id=values["requirement_id"],
            state=TaskState(values["state"]),
            branch=values["branch"],
            priority=values["priority"],
            fix_rounds=values["fix_rounds"],
            plan_retries=values["plan_retries"],
            needs_itest=ItestNeed(values["needs_itest"]),
            itest_override=ItestOverride(values["itest_override"] or "auto"),
            mode=TaskMode(values["mode"] or "autonomous"),
            pending_todo_id=values["pending_todo_id"] or "",
            topology=values["topology"] or "",
            risk_level=values["risk_level"],
            budget_tokens=values["budget_tokens"],
            tokens_used=values["tokens_used"],
            run_status=TaskRunStatus(values["run_status"] or "idle"),
            escalate_reason=values["escalate_reason"],
            intervention_resolved_at=(
                datetime.fromisoformat(values["intervention_resolved_at"])
                if values["intervention_resolved_at"]
                else None
            ),
            intervention_successor_task_id=values["intervention_successor_task_id"],
            created_at=datetime.fromisoformat(values["created_at"]),
            updated_at=datetime.fromisoformat(values["updated_at"]),
        )

    def _write_snapshot(self, task: Task) -> None:
        directory = self.task_dir(task.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / SNAPSHOT).write_text(
            json.dumps(task.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


@dataclass(frozen=True)
class RetryAttempt:
    task: Task
    source: Task | None
    requirement_changed: bool
    created: bool


__all__ = [
    "SNAPSHOT",
    "AttemptConflict",
    "ItestNeed",
    "ItestOverride",
    "RetryAttempt",
    "Task",
    "TaskNotFound",
    "TaskStore",
]
