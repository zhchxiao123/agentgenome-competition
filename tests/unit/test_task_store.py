"""任务模型与仓储:写进去的能原样读回来,并发下编号不撞车。

**状态即事实。** 这一层是那个"事实"住的地方,所以测试盯的是持久化本身:字段不丢、
时区不丢、编号唯一、快照与库一致。
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentgenome.core import store as store_module
from agentgenome.core.states import TaskState
from agentgenome.core.task import ItestNeed, ItestOverride, Task, TaskStore


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path)


def _create(store: TaskStore, **kwargs: object) -> Task:
    payload: dict = {"title": "下单预占库存", "requirement": "下单时要预占库存"} | kwargs
    return store.create(**payload)


# --- 编号 -------------------------------------------------------------------


def test_the_id_carries_the_date_and_a_sequence(store: TaskStore) -> None:
    """编号是给人用的:我要能在对话里说"ag-20260901-003 怎么样了"。"""
    task = _create(store, now=datetime(2026, 9, 1, 10, 0, tzinfo=UTC))

    assert task.id == "ag-20260901-001"


def test_the_sequence_increments_within_a_day(store: TaskStore) -> None:
    day = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

    ids = [_create(store, now=day).id for _ in range(3)]

    assert ids == ["ag-20260901-001", "ag-20260901-002", "ag-20260901-003"]


def test_the_sequence_restarts_on_a_new_day(store: TaskStore) -> None:
    _create(store, now=datetime(2026, 9, 1, 23, 59, tzinfo=UTC))

    task = _create(store, now=datetime(2026, 9, 2, 0, 1, tzinfo=UTC))

    assert task.id == "ag-20260902-001"


def test_concurrent_submissions_never_collide(store: TaskStore) -> None:
    """ "查最大值再加一"在两个进程同时提交时会给出同一个号。

    用真并发跑,不是串行调用几次就算数——串行的话这个 bug 永远不会出现。
    """
    day = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = [task.id for task in pool.map(lambda _: _create(store, now=day), range(40))]

    assert len(set(ids)) == 40, "分配出了重复编号"


# --- 往返 -------------------------------------------------------------------


def test_a_task_round_trips_through_the_store(store: TaskStore) -> None:
    created = _create(store, priority=7, budget_tokens=123_456)

    loaded = store.get(created.id)

    assert loaded == created


def test_timestamps_keep_their_timezone(store: TaskStore) -> None:
    """丢了时区之后,"这个任务跑了多久"就再也算不准了。"""
    created = _create(store, now=datetime(2026, 9, 1, 10, 0, tzinfo=UTC))

    loaded = store.get(created.id)

    assert loaded.created_at.tzinfo is not None
    assert loaded.created_at == created.created_at


def test_updates_are_persisted(store: TaskStore) -> None:
    task = _create(store)

    store.save(task.evolve(state=TaskState.DEVELOPING, fix_rounds=2, branch="task/x"))

    loaded = store.get(task.id)
    assert loaded.state is TaskState.DEVELOPING
    assert loaded.fix_rounds == 2
    assert loaded.branch == "task/x"


def test_needs_itest_starts_undecided(store: TaskStore) -> None:
    """ "还没判断"与"判断为否"是两件事。合成一个布尔的话,没跑判定和判定为否长得一样。"""
    task = _create(store)

    assert task.needs_itest is ItestNeed.UNDECIDED
    assert store.get(task.id).needs_itest is ItestNeed.UNDECIDED


def test_needs_itest_survives_a_round_trip(store: TaskStore) -> None:
    task = _create(store)

    store.save(task.evolve(needs_itest=ItestNeed.NO))

    assert store.get(task.id).needs_itest is ItestNeed.NO


def test_an_unknown_id_is_reported_not_returned_as_none(store: TaskStore) -> None:
    with pytest.raises(LookupError) as excinfo:
        store.get("ag-20260901-999")

    assert "ag-20260901-999" in str(excinfo.value)


# --- 快照 -------------------------------------------------------------------


def test_a_snapshot_lands_in_the_task_directory(tmp_path: Path, store: TaskStore) -> None:
    """数据库损坏时我至少还能打开任务目录看看这个任务是谁、卡在哪。"""
    task = _create(store)

    payload = json.loads((tmp_path / "tasks" / task.id / "task.json").read_text(encoding="utf-8"))

    assert payload["id"] == task.id
    assert payload["state"] == "CREATED"
    assert payload["requirement"] == "下单时要预占库存"


def test_the_snapshot_is_rewritten_on_every_save(tmp_path: Path, store: TaskStore) -> None:
    task = _create(store)

    store.save(task.evolve(state=TaskState.DEVELOPING))

    payload = json.loads((tmp_path / "tasks" / task.id / "task.json").read_text(encoding="utf-8"))
    assert payload["state"] == "DEVELOPING"


def test_a_broken_snapshot_does_not_affect_reads(tmp_path: Path, store: TaskStore) -> None:
    """快照不是真相来源。反过来用它当真相就会有两份会分叉的状态。"""
    task = _create(store)
    (tmp_path / "tasks" / task.id / "task.json").write_text("{ 这不是 JSON", encoding="utf-8")

    assert store.get(task.id).id == task.id


def test_a_missing_snapshot_does_not_affect_reads(tmp_path: Path, store: TaskStore) -> None:
    task = _create(store)
    (tmp_path / "tasks" / task.id / "task.json").unlink()

    assert store.get(task.id).state is TaskState.CREATED


# --- 查询 -------------------------------------------------------------------


def test_open_tasks_exclude_terminal_ones(store: TaskStore) -> None:
    alive = _create(store)
    done = _create(store)
    store.save(done.evolve(state=TaskState.COMPLETED))

    assert [task.id for task in store.open_tasks()] == [alive.id]


def test_open_tasks_exclude_escalated_ones_because_the_scheduler_must_not_pick_them_up(
    store: TaskStore,
) -> None:
    """调度队列从这里派生。升级人工的任务再被派活,等于把"停手"这个决定作废了。"""
    alive = _create(store)
    escalated = _create(store)
    store.save(escalated.evolve(state=TaskState.ESCALATED))

    assert [task.id for task in store.open_tasks()] == [alive.id]


def test_unsettled_tasks_keep_escalated_ones_visible(store: TaskStore) -> None:
    """回归:此前列表接口复用 `open_tasks`,升级人工的任务从界面和 `agctl task list`
    里一起消失——表现成"任务根本没提交成功",而它其实正等着人处理。"""
    alive = _create(store)
    escalated = _create(store)
    store.save(escalated.evolve(state=TaskState.ESCALATED))

    assert {task.id for task in store.unsettled_tasks()} == {alive.id, escalated.id}


def test_a_resolved_escalation_leaves_the_attention_queue(store: TaskStore) -> None:
    """状态仍保留 ESCALATED 供审计，但人工已处理后不该继续占着「需我处理」。"""
    escalated = _create(store)
    store.save(
        escalated.evolve(
            state=TaskState.ESCALATED,
            intervention_resolved_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        )
    )

    assert store.unsettled_tasks() == ()
    loaded = store.get(escalated.id)
    assert loaded.state is TaskState.ESCALATED


@pytest.mark.parametrize("state", sorted(TaskState.settled()))
def test_unsettled_tasks_still_hide_the_ones_nobody_needs_to_look_at(
    store: TaskStore, state: TaskState
) -> None:
    """`ESCALATED` 要露出来,不代表已完成和已取消也要——那样列表会被历史任务淹掉。"""
    alive = _create(store)
    done = _create(store)
    store.save(done.evolve(state=state))

    assert [task.id for task in store.unsettled_tasks()] == [alive.id]


def test_unsettled_tasks_are_ordered_like_open_tasks(store: TaskStore) -> None:
    """两个列表并排看时顺序不一致的话,人会以为自己看的是两批不同的任务。"""
    day = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    low = _create(store, priority=1, now=day)
    high = _create(store, priority=9, now=day)

    assert [task.id for task in store.unsettled_tasks()] == [high.id, low.id]


def test_open_tasks_are_ordered_by_priority_then_age(store: TaskStore) -> None:
    """并发受限时重要任务先跑。同优先级按先来后到,不然插队没有规律。"""
    day = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    low = _create(store, priority=1, now=day)
    high = _create(store, priority=9, now=day)
    also_high = _create(store, priority=9, now=day)

    assert [task.id for task in store.open_tasks()] == [high.id, also_high.id, low.id]


# --- 连接 -------------------------------------------------------------------


def test_every_connection_is_closed_and_not_merely_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归:`with sqlite3.connect(...)` 只提交,**不关连接**。

    每次读写都开一条新连接,不关的话要等 GC 才释放。长跑的服务端(指标接口被反复抓取)会攒下
    上百个指向同一个库文件的描述符,最后撞上进程 fd 上限——症状是跑了很久之后突然什么都打不开,
    而代码看着完全正常,也没有任何一个测试会红。
    """
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(connection)
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", spy)
    store = TaskStore(tmp_path)
    task = store.create(title="下单预占库存", requirement="下单时要预占库存")
    store.save(task)
    store.get(task.id)
    store.unsettled_tasks()
    store.open_tasks()
    store.all_tasks()

    assert opened, "一条连接都没开,这个测试没测到东西"
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("select 1")


def test_a_failed_transaction_still_rolls_back(store: TaskStore) -> None:
    """关连接不能顺手把提交/回滚弄丢。

    `create` 自己发 `begin immediate`,靠上下文管理器在异常时回滚。如果 `_connect` 被改成
    只 `close()` 不回滚,这里删掉的行会**留在库里**——一次失败的写入污染掉整张表。
    """
    task = _create(store)

    with pytest.raises(RuntimeError), store._connect() as connection:
        connection.execute("begin immediate")
        connection.execute("delete from tasks")
        raise RuntimeError("写到一半炸了")

    assert [row.id for row in store.all_tasks()] == [task.id]


def test_a_successful_transaction_still_commits(store: TaskStore) -> None:
    """回滚的另一半:正常退出时那个显式事务必须真的落盘。"""
    task = _create(store)

    with store._connect() as connection:
        connection.execute("begin immediate")
        connection.execute("update tasks set title = ? where id = ?", ("改过的标题", task.id))

    assert TaskStore(store.root).get(task.id).title == "改过的标题"


def test_an_empty_database_is_initialised_on_first_use(tmp_path: Path) -> None:
    """空数据库要能自动建表——不该要求先手工跑一个迁移脚本。"""
    store = TaskStore(tmp_path)

    assert store.open_tasks() == ()


def test_a_second_store_sees_what_the_first_wrote(tmp_path: Path) -> None:
    """进程重启后进度不丢——整套崩溃恢复都压在这条上。"""
    task = _create(TaskStore(tmp_path))

    assert TaskStore(tmp_path).get(task.id).id == task.id


def test_the_schema_is_created_only_once(tmp_path: Path) -> None:
    """重复初始化不该报错,也不该把已有数据清掉。"""
    task = _create(TaskStore(tmp_path))

    TaskStore(tmp_path)
    TaskStore(tmp_path)

    assert TaskStore(tmp_path).get(task.id).id == task.id


def test_the_database_lives_in_the_workspace(tmp_path: Path, store: TaskStore) -> None:
    _create(store)

    assert store.database.is_file()
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("select count(*) from tasks").fetchone()[0] == 1


def test_a_database_created_before_the_column_existed_still_opens(tmp_path: Path) -> None:
    """老库要能被升级后的代码读。

    `create table if not exists` 对已经存在的表什么都不做,于是往建表语句里加一列对老库
    完全无效——症状是升级之后读任何一个老任务都报"没有这一列",而建表语句里明明写着。
    """
    store = TaskStore(tmp_path)
    task = _create(store)
    with sqlite3.connect(store.database) as connection:
        connection.execute("alter table tasks drop column itest_override")

    reopened = TaskStore(tmp_path)

    assert reopened.get(task.id).itest_override is ItestOverride.AUTO
