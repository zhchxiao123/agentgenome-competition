"""待办:派给人的那些 Job。

## 为什么它是一张表而不是内存里的队列

一张待办的寿命以**工作日**计。进程重启、部署、机器换一台,它都得还在——内存队列在这几件
事上一件都撑不住,而症状是"我的待办不见了",发生在人正准备去干活的那一刻。

## 幂等键:一次派发对应一张待办

`(任务, 阶段, 节点, 轮次)` 唯一。崩溃恢复会把非终态任务各推一步,那一步会重新走到派发
——不去重的话,人的列表里会长出第二张一模一样的待办,而两张都"合法",谁也说不清该做哪张。

处理器文件里那份「不幂等的副作用清单」因此多了一条:**投递待办**。

## 状态只有三个,而且没有第三态之外的第三态

`pending`(等人干)→ `done`(交了)或 `escalated`(三段走完还没人管)。改派不是新状态:
它换的是人,不是这张待办的性质——**待确认与已升级人工的区别不许合并**,而改派仍然是待确认。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from agentgenome.core.store import SqliteStore

#: 等人干。
PENDING = "pending"
#: 人交了活,契约也过了。
DONE = "done"
#: 三段走完还是没人管,任务已升级人工。**它是待办的终点,不是任务的**。
ESCALATED = "escalated"

#: 网页上交产物的待办(确认、评审、判定)。
ARTIFACT = "artifact"
#: 在任务工作树里改代码的待办(开发)。
WORKTREE = "worktree"
#: 确认一份拆分提案的待办(PRD 48)。交上来的不是产物而是裁决:approved + 反馈。
SPLIT = "split"


@dataclass(frozen=True)
class Todo:
    """一张待办。

    它带着**这次派发的全部现场**:产物往哪写、上下文包在哪、要过哪份契约。少任何一样,
    "人交回来的东西怎么接回流水线"就得靠别处再查一遍,而那一遍迟早与派发那一刻不一致。
    """

    id: str
    task_id: str
    stage: str
    #: 图里的哪个节点。`single` 下是空串。
    node: str
    #: 这个节点的第几轮。与产物槽的轮次同一个数。
    attempt: int
    assignee: str
    employee_id: str
    procedure_id: str
    #: 产物目录,相对 Workspace 根。**人交回来的东西写进这里**——它就是当初分配给这次
    #: 派发的那个槽,于是"交完 = 产物已存在 = 崩溃恢复那条重放路"。
    output_dir: str
    #: 上下文包,相对 Workspace 根。人要看的"这活是什么"在里面。
    context_file: str
    #: 干活的地方,相对 Workspace 根。工作树类待办靠它告诉人去哪改代码。
    workdir: str = ""
    kind: str = ARTIFACT
    state: str = PENDING
    #: 提醒发过没有。**幂等扫描靠它**:同一分钟连跑三次,提醒只发一次。
    reminded: bool = False
    #: 改派过几次。0 表示还在第一个人手里。
    reassignments: int = 0
    created_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    updated_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    #: 这张待办前后经手过谁。改派要留痕——"现在卡在谁那儿、之前卡过谁"是同一个问题的两半。
    history: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        """幂等键。一次派发一张待办。"""
        return f"{self.task_id}|{self.stage}|{self.node}|{self.attempt}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "stage": self.stage,
            "node": self.node,
            "attempt": self.attempt,
            "assignee": self.assignee,
            "employee_id": self.employee_id,
            "procedure_id": self.procedure_id,
            "output_dir": self.output_dir,
            "context_file": self.context_file,
            "workdir": self.workdir,
            "kind": self.kind,
            "state": self.state,
            "reminded": self.reminded,
            "reassignments": self.reassignments,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "history": list(self.history),
        }


_SCHEMA = """
create table if not exists todos (
    id text primary key,
    task_id text not null,
    stage text not null,
    node text not null,
    attempt integer not null,
    assignee text not null,
    employee_id text not null,
    procedure_id text not null,
    output_dir text not null,
    context_file text not null,
    workdir text not null default '',
    kind text not null default 'artifact',
    state text not null,
    reminded integer not null default 0,
    reassignments integer not null default 0,
    created_at text not null,
    updated_at text not null,
    history text not null default '[]',
    unique (task_id, stage, node, attempt)
);
create index if not exists todos_by_assignee on todos (assignee, state);
"""

_COLUMNS = (
    "id",
    "task_id",
    "stage",
    "node",
    "attempt",
    "assignee",
    "employee_id",
    "procedure_id",
    "output_dir",
    "context_file",
    "workdir",
    "kind",
    "state",
    "reminded",
    "reassignments",
    "created_at",
    "updated_at",
    "history",
)


class TodoNotFound(LookupError):
    """没有这张待办。"""


class TodoStore(SqliteStore):
    """待办的持久化仓储。SQL 只出现在这里。"""

    schema = _SCHEMA

    def deliver(self, todo: Todo, now: datetime | None = None) -> Todo:
        """投一张待办。**已经有同键的就原样返回它,不新开一张。**

        这条是崩溃恢复的必需品:恢复会把任务各推一步,那一步会重新走到派发。不去重的话,
        人的列表里会长出第二张一模一样的待办,而两张都"合法"。
        """
        existing = self.by_key(todo.task_id, todo.stage, todo.node, todo.attempt)
        if existing is not None:
            return existing
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        landed = replace(
            todo, created_at=stamp, updated_at=stamp, history=(todo.assignee,)
        )
        with self._connect() as connection:
            connection.execute(
                f"insert into todos ({', '.join(_COLUMNS)}) "
                f"values ({', '.join('?' * len(_COLUMNS))})",
                self._to_row(landed),
            )
        return landed

    def save(self, todo: Todo, now: datetime | None = None) -> Todo:
        updated = replace(todo, updated_at=(now or datetime.now(UTC)).astimezone(UTC))
        assignments = ", ".join(f"{name} = ?" for name in _COLUMNS[1:])
        row = self._to_row(updated)
        with self._connect() as connection:
            connection.execute(
                f"update todos set {assignments} where id = ?", (*row[1:], updated.id)
            )
        return updated

    def get(self, todo_id: str) -> Todo:
        with self._connect() as connection:
            row = connection.execute(
                f"select {', '.join(_COLUMNS)} from todos where id = ?", (todo_id,)
            ).fetchone()
        if row is None:
            raise TodoNotFound(f"没有这张待办: {todo_id}")
        return self._from_row(row)

    def by_key(self, task_id: str, stage: str, node: str, attempt: int) -> Todo | None:
        with self._connect() as connection:
            row = connection.execute(
                f"select {', '.join(_COLUMNS)} from todos "
                "where task_id = ? and stage = ? and node = ? and attempt = ?",
                (task_id, stage, node, attempt),
            ).fetchone()
        return self._from_row(row) if row else None

    def open_todos(self, assignee: str = "", task_id: str = "") -> tuple[Todo, ...]:
        """还等着人干的那些,按创建时间升序。

        **不给"全部状态"这个缺省**:待办列表是给人看的工作面板,把已经交掉的混进来,
        真正等他的那几张会被淹掉。要看历史走 `all_of`。
        """
        clauses = ["state = ?"]
        values: list[Any] = [PENDING]
        if assignee:
            clauses.append("assignee = ?")
            values.append(assignee)
        if task_id:
            clauses.append("task_id = ?")
            values.append(task_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from todos "
                f"where {' and '.join(clauses)} order by created_at asc, id asc",
                values,
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def all_of(self, task_id: str) -> tuple[Todo, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"select {', '.join(_COLUMNS)} from todos where task_id = ? "
                "order by created_at asc, id asc",
                (task_id,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _to_row(todo: Todo) -> tuple[Any, ...]:
        payload = todo.as_dict()
        payload["reminded"] = int(todo.reminded)
        payload["history"] = json.dumps(list(todo.history), ensure_ascii=False)
        return tuple(payload[name] for name in _COLUMNS)

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> Todo:
        values = dict(zip(_COLUMNS, row, strict=True))
        return Todo(
            id=values["id"],
            task_id=values["task_id"],
            stage=values["stage"],
            node=values["node"],
            attempt=values["attempt"],
            assignee=values["assignee"],
            employee_id=values["employee_id"],
            procedure_id=values["procedure_id"],
            output_dir=values["output_dir"],
            context_file=values["context_file"],
            workdir=values["workdir"] or "",
            kind=values["kind"] or ARTIFACT,
            state=values["state"],
            reminded=bool(values["reminded"]),
            reassignments=values["reassignments"],
            created_at=datetime.fromisoformat(values["created_at"]),
            updated_at=datetime.fromisoformat(values["updated_at"]),
            history=tuple(json.loads(values["history"] or "[]")),
        )


__all__ = [
    "ARTIFACT",
    "DONE",
    "ESCALATED",
    "PENDING",
    "SPLIT",
    "WORKTREE",
    "Todo",
    "TodoNotFound",
    "TodoStore",
]
