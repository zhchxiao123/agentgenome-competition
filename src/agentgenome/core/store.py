"""两个仓储共用的 sqlite 底座。

任务表与事件表住在同一个库里,连接方式与建表时机也一样。各写一遍的话,`isolation_level=None`
这类"为什么这么连"的理由会有两份,而改动其中一份时另一份不会跟着改。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agentgenome import paths

#: 并发写者排队而不是立刻抛 database is locked。
_TIMEOUT_S = 30


def task_dir(workspace_root: Path, task_id: str) -> Path:
    """一个任务的运行态目录。

    独立函数而不是只挂在仓储上:只想算一条路径的调用方不该被迫开一个数据库连接。
    """
    return Path(workspace_root) / paths.TASKS / task_id


class SqliteStore:
    """一个开在 Workspace 里的 sqlite 库。"""

    #: 子类给出自己的建表语句。空数据库首次使用时自动执行——不该要求先手工跑迁移脚本。
    schema: str = ""

    #: 建表之后补上的列,每项是 `(表名, 列名, 列定义)`。
    #:
    #: **`create table if not exists` 对已经存在的表什么都不做**,于是往 `schema` 里加一列
    #: 对老库完全无效——症状是升级之后读任何一个老任务都报"没有这一列",而建表语句里明明
    #: 写着。新库走 `schema`、老库走这里,加列时两边都要写。
    added_columns: tuple[tuple[str, str, str], ...] = ()

    def __init__(self, workspace_root: Path) -> None:
        self.root = Path(workspace_root)
        self.database = self.root / paths.DATABASE
        self.database.parent.mkdir(parents=True, exist_ok=True)
        if self.schema:
            with self._connect() as connection:
                connection.executescript(self.schema)
                self._backfill_columns(connection)

    def task_dir(self, task_id: str) -> Path:
        return task_dir(self.root, task_id)

    def _backfill_columns(self, connection: sqlite3.Connection) -> None:
        missing = [
            item
            for item in self.added_columns
            if item[1]
            not in {
                row[1]
                for row in connection.execute(f"pragma table_info({item[0]})").fetchall()
            }
        ]
        if not missing:
            return
        # 服务启动后多个请求可能同时第一次打开老库。检查列与 ALTER 必须在同一把
        # 写锁里；否则两个连接都会看到「没有」，后执行的那个报 duplicate column。
        # 快路径不取写锁：列都在时，普通读请求不该被一次迁移检查串行化。
        connection.execute("begin immediate")
        for table, column, ddl in missing:
            existing = {
                row[1] for row in connection.execute(f"pragma table_info({table})").fetchall()
            }
            if column not in existing:
                connection.execute(f"alter table {table} add column {ddl}")
        connection.execute("commit")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """开一条连接,用完提交并**关掉**。

        **`with sqlite3.connect(...)` 不关连接**——它的上下文管理器只管提交与回滚,退出时
        连接还开着,等 GC 才释放。而这里每次读写都开一条新的,于是一个长跑的服务端(指标接口
        被反复抓取)会攒下几十上百个指向同一个库文件的描述符,最终撞上进程的 fd 上限;症状是
        跑了很久之后突然什么都打不开,而代码看着完全正常。

        里面那层 `with connection` 是**故意保留**的:`create()` 自己发 `begin immediate`,
        靠它在成功时提交、异常时回滚。只在外面补 `finally: close()`,语义一个字不变。
        """
        # `isolation_level=None` 关掉 sqlite3 模块自作主张的事务管理,由调用方显式控制。
        connection = sqlite3.connect(self.database, timeout=_TIMEOUT_S, isolation_level=None)
        try:
            with connection:
                yield connection
        finally:
            connection.close()


__all__ = ["SqliteStore", "task_dir"]
