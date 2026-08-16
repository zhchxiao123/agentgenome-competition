"""会话模型与仓储。

会话是绕过状态机的一条新通路,而系统所有的安全性质都挂在状态机上。所以这一层的测试
盯的不是"能不能存",是**那几条不变式**:读写权限不可变、只读集不从员工定义继承、
挂起要说明原因。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentgenome.sessions.model import (
    READONLY_TOOLS,
    Session,
    SessionState,
    SuspendReason,
    legacy_mode,
)
from agentgenome.sessions.store import SessionStore


class TestPermission:
    def test_a_read_only_session_gets_the_hardcoded_tool_set(self) -> None:
        assert Session(id="s", employee_id="a").tools == READONLY_TOOLS

    def test_a_writable_session_falls_back_to_the_employee_policy(self) -> None:
        """空元组的意思是"这一层不限制",由 `SessionSpec` 落到员工自己的 allow 上。"""
        assert Session(id="s", employee_id="a", writable=True).tools == ()

    def test_the_readonly_tool_set_carries_no_write_tools(self) -> None:
        """只读靠"拿不到写工具"保证,不靠员工自觉。"""
        assert set(READONLY_TOOLS) == {"Read", "Grep", "Glob"}


class TestLegacyMode:
    """派生的旧模式名。**只为兼容而存在**——存储的历史列与还没切过来的调用方读它。"""

    def test_writable_maps_to_pair(self) -> None:
        assert legacy_mode(True, None) == "pair"
        assert legacy_mode(True, "ag-1") == "pair"

    def test_a_read_only_session_with_a_task_looks_like_an_inquiry(self) -> None:
        assert legacy_mode(False, "ag-1") == "inquiry"

    def test_a_bare_read_only_session_looks_like_a_consult(self) -> None:
        assert legacy_mode(False, None) == "consult"


class TestSession:
    def _session(self, **changes: object) -> Session:
        base = Session(
            id="sess-20260810-001",
            employee_id="architect",
            created_at=datetime(2026, 8, 10, tzinfo=UTC),
            updated_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
        return base.evolve(**changes) if changes else base

    def test_a_new_session_is_active(self) -> None:
        assert self._session().state is SessionState.ACTIVE

    def test_evolving_cannot_change_the_permission(self) -> None:
        """读写权限不可变是权限边界:中途切换等于一次没有闸门的提权。

        而且这条路径长得像一个普通的界面操作,不会有人觉得它需要审批。
        """
        with pytest.raises(ValueError, match="读写权限"):
            self._session().evolve(writable=True)

    def test_evolving_may_restate_the_same_permission(self) -> None:
        """拒的是**改**,不是"提到了它"。改别的字段时顺手带上同值的 `writable` 很常见,
        拦下来只会逼调用方去记住哪些字段不能出现在同一次 `evolve` 里。
        """
        assert self._session().evolve(writable=False, title="x").title == "x"

    def test_suspending_records_why(self) -> None:
        """只显示「已挂起」不说原因,用户的第一反应是「它坏了」而不是「它按规则停了」。"""
        session = self._session().suspend(SuspendReason.IDLE)

        assert session.state is SessionState.SUSPENDED
        assert session.suspend_reason is SuspendReason.IDLE

    def test_a_suspended_session_can_be_resumed(self) -> None:
        """实测:`--resume` 没有短期 TTL,恢复不需要任何额外机制。"""
        session = self._session().suspend(SuspendReason.BUDGET).resume()

        assert session.state is SessionState.ACTIVE
        assert session.suspend_reason is None

    def test_an_ended_session_cannot_be_resumed(self) -> None:
        """已结束才是不可恢复的那一档。"""
        with pytest.raises(ValueError, match="已结束"):
            self._session().end().resume()

    def test_an_ended_session_takes_no_more_messages(self) -> None:
        assert not self._session().end().accepts_messages

    def test_a_suspended_session_takes_no_more_messages_until_resumed(self) -> None:
        suspended = self._session().suspend(SuspendReason.IDLE)

        assert not suspended.accepts_messages
        assert suspended.resume().accepts_messages

    def test_the_state_vocabulary_avoids_the_word_archived(self) -> None:
        """`CONTEXT.md` 的「已了结」词条把「归档」列为 Avoid,而归档在本系统里另有所指
        (审计包)。
        """
        assert {item.value for item in SessionState} == {"active", "suspended", "ended"}


class TestBudgetAndIdle:
    def _session(self, **changes: object) -> Session:
        return Session(
            id="s-1",
            employee_id="dev",
            max_tokens=1000,
            idle_timeout_s=1800,
            created_at=datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
            updated_at=datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
        ).evolve(**changes)

    def test_a_session_within_budget_keeps_going(self) -> None:
        assert self._session(tokens_used=900).over_budget is False

    def test_hitting_the_budget_is_over(self) -> None:
        """超限自动挂起并写事件——**不静默截断**。"""
        assert self._session(tokens_used=1000).over_budget is True

    def test_idle_is_measured_from_the_last_update(self) -> None:
        session = self._session()
        later = datetime(2026, 8, 10, 10, 31, tzinfo=UTC)

        assert session.idle_expired(later) is True

    def test_a_session_touched_recently_is_not_idle(self) -> None:
        session = self._session()
        later = datetime(2026, 8, 10, 10, 29, tzinfo=UTC)

        assert session.idle_expired(later) is False


class TestStore:
    def test_a_saved_session_reads_back_the_same(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        session = store.create(employee_id="architect")

        assert store.get(session.id) == session

    def test_ids_are_stable_and_sortable(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)

        first = store.create(employee_id="a")
        second = store.create(employee_id="a")

        assert first.id < second.id

    def test_listing_filters_by_permission_and_state(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        readonly = store.create(employee_id="a")
        writable = store.create(employee_id="a", writable=True, task_id="ag-1")
        store.save(readonly.end())

        # **`False` 与"不筛"是两件事。** 存的是 0/1,把 bool 直接当"有没有给"用的话,
        # 筛只读会被当成没筛,结果里混进可写的那些。
        assert [s.id for s in store.list(writable=False)] == [readonly.id]
        assert [s.id for s in store.list(writable=True)] == [writable.id]
        assert len(store.list()) == 2
        assert [s.id for s in store.list(state=SessionState.ENDED)] == [readonly.id]

    def test_an_old_database_without_the_column_still_reads(self, tmp_path: Path) -> None:
        """升级前建的会话必须读得出来,而且**结对那些要读成可写**。

        `added_columns` 只加列、只给默认值,不回填。少了那一步,升级前的每一个结对会话都会
        被读成只读——而它的 `workdir` 还指着任务 worktree,员工照旧坐在可写的目录里,只是
        拿不到写工具了。症状是"昨天还能改代码的会话今天改不动了",而从系统的角度看它一直
        就是只读的,没有一条错误信息解释得了。
        """
        import sqlite3

        from agentgenome import paths

        database = tmp_path / paths.DATABASE
        database.parent.mkdir(parents=True, exist_ok=True)
        # 老库:有 `mode`,没有 `writable`。
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                create table sessions (
                    id text primary key, employee_id text not null,
                    runtime text not null default '', mode text not null, state text not null,
                    title text not null default '', task_id text, native_session_id text,
                    started integer not null default 0, workdir text not null default '',
                    tokens_used integer not null default 0, max_tokens integer not null,
                    idle_timeout_s integer not null, suspend_reason text,
                    last_seq integer not null default 0,
                    context_items text not null default '[]', pinned text not null default '[]',
                    created_at text not null, updated_at text not null
                );
                """
            )
            for session_id, mode in (("old-1", "consult"), ("old-2", "inquiry"), ("old-3", "pair")):
                connection.execute(
                    "insert into sessions (id, employee_id, mode, state, max_tokens,"
                    " idle_timeout_s, created_at, updated_at) values (?,?,?,?,?,?,?,?)",
                    (session_id, "dev", mode, "active", 50000, 1800, "2026-08-10", "2026-08-10"),
                )

        store = SessionStore(tmp_path)

        assert store.get("old-1").writable is False
        assert store.get("old-2").writable is False
        assert store.get("old-3").writable is True

    def test_an_unknown_session_is_reported_not_guessed(self, tmp_path: Path) -> None:
        with pytest.raises(LookupError):
            SessionStore(tmp_path).get("nope")
