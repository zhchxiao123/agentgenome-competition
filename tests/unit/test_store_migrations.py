"""SQLite 轻量迁移在并发服务请求下也只能执行一次。"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from agentgenome import paths
from agentgenome.core.store import SqliteStore


class _MigratingStore(SqliteStore):
    schema = "create table if not exists records (id text primary key);"
    added_columns = (("records", "note", "note text not null default ''"),)


def test_concurrent_first_reads_migrate_an_old_database_once(tmp_path: Path) -> None:
    database = tmp_path / paths.DATABASE
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("create table records (id text primary key)")

    workers = 8
    ready = Barrier(workers)

    def open_store(_: int) -> None:
        ready.wait()
        _MigratingStore(tmp_path)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(open_store, range(workers)))

    with sqlite3.connect(database) as connection:
        columns = [row[1] for row in connection.execute("pragma table_info(records)")]
    assert columns.count("note") == 1
