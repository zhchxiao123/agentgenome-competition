"""基因组任务:改的是基因组而不是业务代码的那一类任务。

知识初始化、按模块重建、经验蒸馏、卡片补写——这些事系统一直在做,但它们没有身份:成功时留
一个结果,失败时什么都不留,进行中时人完全看不见。接一个五十个模块的老项目,初始化跑几小时,
终端上只有一行光标。

## 为什么不是给 `Task` 加一个「种类」字段

`Task` 十七个字段里这类任务用得上的约八个,其余六个(分支、修复轮次、需求解析重试、集成测试
判定、人工发话、风险评级)对它**永远不成立**。一个永远为空的字段,读的人无从判断是"还没填"
还是"不适用"——这与 `ItestNeed` 用三态而非布尔是同一条理由。

于是「任务」升格成父类型,只拥有两类真正共有的四条:有 id、有事件流与日志、有终态、烧 token。
原来那个含义——"一次从需求到合入的完整尝试"——让给**研发任务**(`core.task.Task`)。

## id 空间共享,存储分表

事件表以任务 id 为键,两类必须能进同一条流,否则"一次检索覆盖系统的全部行为"就不成立。所以
id 前缀区分种类(`gn-` / `ag-`)、值不冲突。但表分开:一类的 schema 变更不该波及另一类。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from agentgenome.core.store import SqliteStore
from agentgenome.core.task import SNAPSHOT, TaskNotFound
from agentgenome.core.task_ids import GENOME_PREFIX

#: 基因组任务 id 的前缀。与研发任务的 `ag-` 并列且不冲突。见 `core.task_ids`。
ID_PREFIX = GENOME_PREFIX


class ModuleBusy(RuntimeError):
    """这个模块上已经有一个基因组任务在跑。"""


class GenomeTaskKind(StrEnum):
    """这个基因组任务在改基因组的哪一部分。

    有限集合而不是自由文本:按种类筛选才有意义,而自由文本的第一个后果是同一件事出现三种
    写法。
    """

    #: 第一次读懂一个存量项目。**这是冷启动,不是进化**——此刻还没有任何经验可回流。
    INIT = "init"
    #: 把业务仓 clone 进 `repos/`。**接入的一部分,与知识初始化同属冷启动**:它改的是
    #: 协作仓的结构而不是知识,归进基因组任务买的是整套"人发起、有人在等、失败进异常
    #: 队列"的机制(发起方词条的全部语义),不为挂载再造第二套异步通道。
    MOUNT = "mount"
    #: 按模块重建。代码大改之后刷新那一块认知。
    REINIT = "reinit"
    #: 把一个任务的经验蒸馏成卡片。
    DISTILL = "distill"
    #: 补写代码变更之后跟不上的卡片。
    BACKFILL = "backfill"
    #: 增厚存量薄卡。骨架没变,内容纪律变了之后按热区逐张补深(PRD 41)。
    DEEPEN = "deepen"
    #: 为没有标准入口的模块调查验证命令，只产出候选，等待人确认后才生效。
    VERIFICATION = "verification"


class Origin(StrEnum):
    """谁发起的。

    **「失败该不该惊动人」由它决定,不由失败本身决定。** 人敲了命令正在等结果的初始化失败了,
    该进异常队列;系统自己排的蒸馏失败了,只留一条事件——同样是失败,响应完全不同。
    """

    HUMAN = "human"
    SYSTEM = "system"


class GenomeTaskState(StrEnum):
    """基因组任务的生命周期。**不与研发任务共用枚举。**

    它没有开发、没有单元测试、没有风险评级、没有分支。共用一套枚举只会让读状态机的人看到
    一堆对某一类永远不成立的迁移,而「迁移即文档」这条实现原则当场失效。
    """

    SCANNING = "SCANNING"
    #: **计划内的闸门,任务是健康的。** 人做的是确认或调整,不是诊断。它不是终态:
    #: 人一答复机器自己往下走。与已升级人工的区别见 `CONTEXT.md`。
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    DEEP_READ = "DEEP_READ"
    SUMMARISING = "SUMMARISING"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @classmethod
    def terminal(cls) -> frozenset[GenomeTaskState]:
        """状态机不再自动迁移的状态。**待确认不在里面**——它等的是人,不是终结。"""
        return frozenset({cls.SUBMITTED, cls.FAILED, cls.CANCELLED})


@dataclass(frozen=True)
class GenomeTask:
    """一个基因组任务的全部状态。不可变,改状态走 `evolve` 生成新值再存。"""

    id: str
    title: str
    kind: GenomeTaskKind
    origin: Origin
    state: GenomeTaskState = GenomeTaskState.SCANNING
    #: 作用在哪个模块上。按模块重建时有值,全量初始化时没有。
    subject: str | None = None
    #: 由哪个研发任务触发。系统自发的蒸馏有值,人发起的初始化没有。
    source_task_id: str | None = None
    #: 为什么失败,用「下一步该从哪查」的口径写。
    failure_reason: str | None = None
    budget_tokens: int | None = None
    tokens_used: int = 0
    #: 与研发任务同义:保留 FAILED 事实，只结束人工待办。
    intervention_resolved_at: datetime | None = None
    created_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    updated_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)

    def evolve(self, **changes: Any) -> GenomeTask:
        return replace(self, **changes)

    @property
    def is_terminal(self) -> bool:
        return self.state in GenomeTaskState.terminal()

    @property
    def is_settled(self) -> bool:
        """人还用不用管它。

        **失败时看发起方。** 人发起的失败是终态但未了结(有人在等结果);系统自发的失败算
        已了结,只留一条事件——一次蒸馏失败不该变成一条需要处理的告警。

        这条判定只住在这里。散到各个查询里各写一遍的话,第一个忘了排除系统自发失败的列表
        就会让蒸馏失败开始刷屏,而这恰恰是这条区分要防的事情的镜像。
        """
        if self.intervention_resolved_at is not None:
            return True
        if not self.is_terminal:
            return False
        if self.state is GenomeTaskState.FAILED:
            return self.origin is Origin.SYSTEM
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind.value,
            "origin": self.origin.value,
            "state": self.state.value,
            "subject": self.subject,
            "source_task_id": self.source_task_id,
            "failure_reason": self.failure_reason,
            "budget_tokens": self.budget_tokens,
            "tokens_used": self.tokens_used,
            "intervention_resolved_at": (
                self.intervention_resolved_at.isoformat()
                if self.intervention_resolved_at is not None
                else None
            ),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


_SCHEMA = """
create table if not exists genome_tasks (
    id text primary key,
    title text not null,
    kind text not null,
    origin text not null,
    state text not null,
    subject text,
    source_task_id text,
    failure_reason text,
    budget_tokens integer,
    tokens_used integer not null,
    intervention_resolved_at text,
    created_at text not null,
    updated_at text not null
);
create index if not exists genome_tasks_by_state on genome_tasks (state);
create index if not exists genome_tasks_by_subject on genome_tasks (subject);
"""

_COLUMNS = (
    "id",
    "title",
    "kind",
    "origin",
    "state",
    "subject",
    "source_task_id",
    "failure_reason",
    "budget_tokens",
    "tokens_used",
    "intervention_resolved_at",
    "created_at",
    "updated_at",
)


class GenomeTaskStore(SqliteStore):
    """基因组任务的持久化仓储。SQL 只出现在这里。"""

    schema = _SCHEMA
    added_columns = (
        (
            "genome_tasks",
            "intervention_resolved_at",
            "intervention_resolved_at text",
        ),
    )

    # --- 写 ------------------------------------------------------------------

    def create(
        self,
        title: str,
        kind: GenomeTaskKind,
        origin: Origin,
        subject: str | None = None,
        source_task_id: str | None = None,
        budget_tokens: int | None = None,
        now: datetime | None = None,
    ) -> GenomeTask:
        """建一个新基因组任务并分配编号。

        编号在**一次事务里**分配并写入,与研发任务同一条理由:先查最大值再插入的话,两个
        进程同时提交会拿到同一个号,而重复的编号会让后来的那个把前一个覆盖掉。
        """
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        day = stamp.strftime("%Y%m%d")
        task = GenomeTask(
            id="",
            title=title,
            kind=kind,
            origin=origin,
            subject=subject,
            source_task_id=source_task_id,
            budget_tokens=budget_tokens,
            created_at=stamp,
            updated_at=stamp,
        )
        with self._connect() as connection:
            connection.execute("begin immediate")
            # **同模块互斥要在事务里查。** 放在事务外面的话,两个进程会同时看到"没人在跑"
            # 然后双双插入——正是下面那段编号分配小心避开的那种竞态,换了个地方重演。
            if subject and self._open_for_subject(connection, subject):
                raise ModuleBusy(f"{subject} 上已经有一个基因组任务在跑,等它结束再来")
            taken = connection.execute(
                "select id from genome_tasks where id like ? order by id desc limit 1",
                (f"{ID_PREFIX}-{day}-%",),
            ).fetchone()
            sequence = int(taken[0].rsplit("-", 1)[1]) + 1 if taken else 1
            task = task.evolve(id=f"{ID_PREFIX}-{day}-{sequence:03d}")
            self._insert(connection, task)
        self._write_snapshot(task)
        return task

    def save(self, task: GenomeTask, now: datetime | None = None) -> GenomeTask:
        updated = task.evolve(updated_at=(now or datetime.now(UTC)).astimezone(UTC))
        assignments = ", ".join(f"{name} = ?" for name in _COLUMNS[1:])
        row = self._to_row(updated)
        with self._connect() as connection:
            connection.execute(
                f"update genome_tasks set {assignments} where id = ?", (*row[1:], updated.id)
            )
        self._write_snapshot(updated)
        return updated

    # --- 读 ------------------------------------------------------------------

    def get(self, task_id: str) -> GenomeTask:
        with self._connect() as connection:
            row = connection.execute(
                f"select {', '.join(_COLUMNS)} from genome_tasks where id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFound(f"没有这个基因组任务: {task_id}")
        return self._from_row(row)

    def open_tasks(self) -> tuple[GenomeTask, ...]:
        """全部未终结的基因组任务。**崩溃恢复从这里派生。**"""
        terminal = tuple(state.value for state in sorted(GenomeTaskState.terminal()))
        placeholders = ", ".join("?" * len(terminal))
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from genome_tasks "
                f"where state not in ({placeholders}) "
                "order by created_at asc, id asc",
                terminal,
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def unsettled_tasks(self) -> tuple[GenomeTask, ...]:
        """人还需要看见的基因组任务。

        与 `open_tasks` 差的那部分正是它存在的理由:**人发起的失败要留在列表里等人处理,
        系统自发的失败不该出现**。判定在 `GenomeTask.is_settled` 一处,这里只做过滤。
        """
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from genome_tasks order by created_at asc, id asc"
            ).fetchall()
        return tuple(task for task in map(self._from_row, rows) if not task.is_settled)

    def _open_for_subject(
        self, connection: sqlite3.Connection, subject: str
    ) -> tuple[GenomeTask, ...]:
        terminal = tuple(state.value for state in sorted(GenomeTaskState.terminal()))
        placeholders = ", ".join("?" * len(terminal))
        rows = connection.execute(
            f"select {', '.join(_COLUMNS)} from genome_tasks "
            f"where subject = ? and state not in ({placeholders})",
            (subject, *terminal),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def open_for_subject(self, subject: str) -> tuple[GenomeTask, ...]:
        """这个模块上还有没跑完的基因组任务吗。

        **`subject` 为空的任务不参与互斥**:全量初始化与蒸馏都不作用在某一个模块上,
        拿空值当键会让它们互相排斥。
        """
        if not subject:
            return ()
        with self._connect() as connection:
            return self._open_for_subject(connection, subject)

    def awaiting_confirmation(self) -> tuple[GenomeTask, ...]:
        """等人确认的任务。**待办队列,不是异常队列。**

        与 `needs_attention` 分成两个查询,而不是一个"未了结"里都装着:合成一个的话,
        一批健康的、只是在等人点一下的任务会混进异常列表,把真正出事的那个淹没——而这
        正是「待确认」与「已升级人工」分开存在的全部理由,只是方向相反。
        """
        return tuple(
            task
            for task in self.open_tasks()
            if task.state is GenomeTaskState.AWAITING_CONFIRMATION
        )

    def needs_attention(self) -> tuple[GenomeTask, ...]:
        """出事了、等人诊断的任务。**异常队列。**

        只含人发起的失败:系统自发的失败算已了结(见 `GenomeTask.is_settled`),一次蒸馏
        失败不该变成一条需要处理的告警。
        """
        return tuple(
            task
            for task in self.all_tasks()
            if task.state is GenomeTaskState.FAILED and not task.is_settled
        )

    def all_tasks(
        self, kind: GenomeTaskKind | None = None, subject: str | None = None
    ) -> tuple[GenomeTask, ...]:
        clauses = []
        params: list[str] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        where = f"where {' and '.join(clauses)} " if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from genome_tasks {where}"
                "order by created_at desc, id desc",
                params,
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    # --- 内部 ----------------------------------------------------------------

    @staticmethod
    def _to_row(task: GenomeTask) -> tuple[Any, ...]:
        return (
            task.id,
            task.title,
            task.kind.value,
            task.origin.value,
            task.state.value,
            task.subject,
            task.source_task_id,
            task.failure_reason,
            task.budget_tokens,
            task.tokens_used,
            task.intervention_resolved_at.isoformat()
            if task.intervention_resolved_at is not None
            else None,
            task.created_at.isoformat(),
            task.updated_at.isoformat(),
        )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> GenomeTask:
        return GenomeTask(
            id=row[0],
            title=row[1],
            kind=GenomeTaskKind(row[2]),
            origin=Origin(row[3]),
            state=GenomeTaskState(row[4]),
            subject=row[5],
            source_task_id=row[6],
            failure_reason=row[7],
            budget_tokens=row[8],
            tokens_used=row[9],
            intervention_resolved_at=datetime.fromisoformat(row[10]) if row[10] else None,
            created_at=datetime.fromisoformat(row[11]),
            updated_at=datetime.fromisoformat(row[12]),
        )

    def _insert(self, connection: sqlite3.Connection, task: GenomeTask) -> None:
        placeholders = ", ".join("?" * len(_COLUMNS))
        connection.execute(
            f"insert into genome_tasks ({', '.join(_COLUMNS)}) values ({placeholders})",
            self._to_row(task),
        )

    def _write_snapshot(self, task: GenomeTask) -> None:
        """任务目录里那份人眼可读的快照。与研发任务同一条:**它不是真相来源**,
        是容灾与人眼可读。"""
        target = self.task_dir(task.id)
        target.mkdir(parents=True, exist_ok=True)
        (target / SNAPSHOT).write_text(
            json.dumps(task.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


def overdue_confirmations(tasks: Iterable[GenomeTask], cutoff: datetime) -> tuple[GenomeTask, ...]:
    """哪些闸门等太久了。

    **判定是纯函数,收截止时刻。** 与日志保留期同一条:注入时钟只是为了一个时间比较,
    而那会在系统里开一道缝;测试直接传截止时刻就行。

    **它产出的是提醒,不是判死。** 一个等人的健康任务不该因为人休假而失败。
    """
    return tuple(
        task
        for task in tasks
        if task.state is GenomeTaskState.AWAITING_CONFIRMATION and task.updated_at <= cutoff
    )


def genome_task_dir(workspace_root: Path, task_id: str) -> Path:
    from agentgenome.core.store import task_dir

    return task_dir(workspace_root, task_id)


__all__ = [
    "ID_PREFIX",
    "GenomeTask",
    "GenomeTaskKind",
    "GenomeTaskState",
    "GenomeTaskStore",
    "ModuleBusy",
    "Origin",
    "genome_task_dir",
    "overdue_confirmations",
]
