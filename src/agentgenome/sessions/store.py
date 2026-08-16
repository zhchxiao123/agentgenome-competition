"""会话仓储。与任务同库——同一个 Workspace 的运行态住在一起,备份与清理只有一套规则。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentgenome.core.store import SqliteStore
from agentgenome.sessions.model import (
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_MAX_TOKENS,
    Session,
    SessionState,
    SuspendReason,
    legacy_mode,
)

_SCHEMA = """
create table if not exists sessions (
    id text primary key,
    employee_id text not null,
    runtime text not null default '',
    -- **历史列。真相在 `writable` 与 `task_id`。**
    -- 旧的三选一模式(consult/inquiry/pair)在 PRD 45 里下线了,这一列留着只为让老库、
    -- 老备份、以及任何还在读它的东西不炸;新行写的是由那两个字段派生出来的值。
    -- 不要拿它做判断——派生值与真相分叉的那一天,读它的地方会静默地按旧世界行事。
    mode text not null,
    writable integer not null default 0,
    state text not null,
    title text not null default '',
    task_id text,
    native_session_id text,
    started integer not null default 0,
    workdir text not null default '',
    tokens_used integer not null default 0,
    max_tokens integer not null,
    idle_timeout_s integer not null,
    suspend_reason text,
    last_seq integer not null default 0,
    context_items text not null default '[]',
    pinned text not null default '[]',
    created_at text not null,
    updated_at text not null
);
create index if not exists sessions_by_task on sessions(task_id);
"""


class SessionNotFound(LookupError):
    """没有这个会话。"""


class SessionStore(SqliteStore):
    """会话的持久化。"""

    schema = _SCHEMA
    added_columns = (("sessions", "writable", "writable integer not null default 0"),)

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self._backfill_writable()

    def _backfill_writable(self) -> None:
        """老库里的结对会话补上 `writable = 1`。

        `added_columns` 只加列、只给默认值,**不回填**。少了这一步,升级前建的每一个结对
        会话都会被读成只读——而它的 `workdir` 还指着任务 worktree,员工照旧坐在可写的目录里,
        只是拿不到写工具了。症状是"昨天还能改代码的会话今天改不动了",没有一条错误信息
        解释得了,因为从系统的角度看它一直就是只读的。

        判据只能是 `mode`:那是升级前唯一记录过读写权限的地方。

        **先查再写。** 这个类每次请求都会新建一个实例,无条件发一次 `update` 等于给每一次
        列表请求都搭上一次写事务;而需要回填的行只在升级后的第一次出现过一次。
        """
        with self._connect() as connection:
            stale = connection.execute(
                "select 1 from sessions where mode = 'pair' and writable = 0 limit 1"
            ).fetchone()
            if stale is not None:
                connection.execute("update sessions set writable = 1 where mode = 'pair'")

    def create(
        self,
        employee_id: str,
        writable: bool = False,
        runtime: str = "",
        title: str = "",
        task_id: str | None = None,
        workdir: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S,
        now: datetime | None = None,
    ) -> Session:
        """建一个会话并分配编号。

        编号在一次事务里分配并写入,理由同 `TaskStore.create`:先查最大值再插入的话,
        两个进程同时创建会拿到同一个号。
        """
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        day = stamp.strftime("%Y%m%d")
        with self._connect() as connection:
            for _ in range(64):
                serial = self._next_serial(connection, day)
                session = Session(
                    id=f"sess-{day}-{serial:03d}",
                    employee_id=employee_id,
                    runtime=runtime,
                    writable=writable,
                    title=title,
                    task_id=task_id,
                    workdir=workdir,
                    max_tokens=max_tokens,
                    idle_timeout_s=idle_timeout_s,
                    created_at=stamp,
                    updated_at=stamp,
                )
                try:
                    connection.execute(_INSERT, _row(session))
                except sqlite3.IntegrityError:
                    continue  # 撞号了,再要一个
                return session
        raise RuntimeError("连续 64 次分配会话编号都撞号,不再重试")

    def save(self, session: Session) -> Session:
        with self._connect() as connection:
            connection.execute(_UPDATE, _row(session))
        return session

    def get(self, session_id: str) -> Session:
        with self._connect() as connection:
            row = connection.execute(
                f"select {', '.join(_COLUMNS)} from sessions where id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise SessionNotFound(f"没有这个会话: {session_id}")
        return _session(row)

    def list(
        self,
        writable: bool | None = None,
        state: SessionState | None = None,
        employee_id: str | None = None,
        task_id: str | None = None,
    ) -> list[Session]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            # `writable` 存 0/1,而 `None`(不筛)与 `False`(筛只读)是两件事——直接把
            # bool 传下去的话 `False` 会被下面那句 `is not None` 放行,但写成 `int` 才
            # 与列里存的值可比。
            ("writable", None if writable is None else int(writable)),
            ("state", state.value if state else None),
            ("employee_id", employee_id),
            ("task_id", task_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f" where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from sessions{where} order by id", values
            ).fetchall()
        return [_session(row) for row in rows]

    @staticmethod
    def _next_serial(connection: sqlite3.Connection, day: str) -> int:
        row = connection.execute(
            "select id from sessions where id like ? order by id desc limit 1",
            (f"sess-{day}-%",),
        ).fetchone()
        return 1 if row is None else int(str(row[0]).rsplit("-", 1)[1]) + 1


_COLUMNS = (
    "id",
    "employee_id",
    "runtime",
    "mode",
    "writable",
    "state",
    "title",
    "task_id",
    "native_session_id",
    "started",
    "workdir",
    "tokens_used",
    "max_tokens",
    "idle_timeout_s",
    "suspend_reason",
    "last_seq",
    "context_items",
    "pinned",
    "created_at",
    "updated_at",
)

_INSERT = (
    f"insert into sessions ({', '.join(_COLUMNS)}) "
    f"values ({', '.join(':' + name for name in _COLUMNS)})"
)

_UPDATE = (
    "update sessions set "
    + ", ".join(f"{name} = :{name}" for name in _COLUMNS if name != "id")
    + " where id = :id"
)


def _row(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "employee_id": session.employee_id,
        "runtime": session.runtime,
        # 历史列,写的是派生值——见 `_SCHEMA` 上那段注释。
        "mode": legacy_mode(session.writable, session.task_id),
        # sqlite 没有布尔类型,存 0/1。
        "writable": int(session.writable),
        "state": session.state.value,
        "title": session.title,
        "task_id": session.task_id,
        "native_session_id": session.native_session_id,
        # sqlite 没有布尔类型,存 0/1。
        "started": int(session.started),
        "workdir": session.workdir,
        "tokens_used": session.tokens_used,
        "max_tokens": session.max_tokens,
        "idle_timeout_s": session.idle_timeout_s,
        "suspend_reason": session.suspend_reason.value if session.suspend_reason else None,
        # 列表存成 JSON 串:sqlite 没有数组类型,而另开一张关联表对"一次会话装了
        # 什么"这种只随会话整体读写的数据是过度设计。
        "context_items": json.dumps(list(session.context_items), ensure_ascii=False),
        "pinned": json.dumps(list(session.pinned), ensure_ascii=False),
        "last_seq": session.last_seq,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }


def _session(row: tuple[Any, ...]) -> Session:
    """按 `_COLUMNS` 的列序读。

    与 `TaskStore` 同一种读法(显式列序而不是 `sqlite3.Row`)——两种读法并存的话,
    加一列时只改了其中一处,而另一处会静默错位。
    """
    values = dict(zip(_COLUMNS, row, strict=True))
    return Session(
        id=values["id"],
        employee_id=values["employee_id"],
        runtime=values["runtime"],
        writable=bool(values["writable"]),
        state=SessionState(values["state"]),
        title=values["title"],
        task_id=values["task_id"],
        native_session_id=values["native_session_id"],
        started=bool(values["started"]),
        workdir=values["workdir"],
        tokens_used=values["tokens_used"],
        max_tokens=values["max_tokens"],
        idle_timeout_s=values["idle_timeout_s"],
        suspend_reason=SuspendReason(values["suspend_reason"])
        if values["suspend_reason"]
        else None,
        context_items=tuple(json.loads(values["context_items"] or "[]")),
        pinned=tuple(json.loads(values["pinned"] or "[]")),
        last_seq=values["last_seq"],
        created_at=datetime.fromisoformat(values["created_at"]),
        updated_at=datetime.fromisoformat(values["updated_at"]),
    )


__all__ = ["SessionNotFound", "SessionStore"]
