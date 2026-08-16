"""SSE 实时流,以及 CLI 与 REST 的一致性。

一致性那一组是本 PRD 最该投入的测试:CLI 直连服务层而不是走 REST(见 issues/01 记的偏离),
防分叉的责任全压在这里。分叉一旦形成就很难收回——网页能做的命令行做不了,同一个操作两处
校验逻辑不同步,然后没人敢动其中任何一处。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from agentgenome.core.events import EventLog
from agentgenome.core.task import TaskStore
from agentgenome.server.app import create_app, stream_notices
from agentgenome.server.bus import EventBus
from tests.fixtures.git import commit_all
from tests.fixtures.mall import materialize_mall

runner = CliRunner()
APPROVER = "alice"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        cli_app,
        [
            "init",
            "--local-only",
            str(root),
            "--name",
            "mall",
            "--repo",
            mall["order-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    config = root / "agentgenome.yaml"
    config.write_text(
        config.read_text(encoding="utf-8") + f"\napproval:\n  approvers: [{APPROVER}]\n",
        encoding="utf-8",
    )
    commit_all(root, "chore: 审批人")
    return root


# --- SSE --------------------------------------------------------------------


async def test_a_subscriber_receives_notices() -> None:
    bus = EventBus()
    stream = stream_notices(bus, "ag-1", None)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)  # 让生成器跑到订阅那一步

    bus.publish("ag-1", "task_created")

    chunk = await asyncio.wait_for(pending, timeout=2)
    assert chunk.startswith("id: 1\n")
    assert "task_created" in chunk
    # 回归测试:一旦这一帧带上了 `event: task_created`,浏览器 `EventSource` 会把它当成
    # 具名事件派发,只有专门 `addEventListener("task_created", ...)` 的监听者才收得到——
    # 而前端 `subscribe()` 一直是 `source.onmessage`,只认没有 `event:` 字段的默认事件。
    # 这条一直没被断言过,真实后果是**所有**推送都发不到浏览器,直到"启动"按钮把
    # "页面自己会不会刷新"变成用户能直接看见的卡死状态才第一次被发现。见 `bus.py`
    # `Notice.as_sse` 的说明。
    assert "event:" not in chunk
    await stream.aclose()


async def test_a_quiet_stream_sends_a_heartbeat() -> None:
    """中间的代理常在 30~60 秒无数据时掐断连接。心跳是让长连接活下去的唯一办法。"""
    stream = stream_notices(EventBus(), None, None, heartbeat_s=0.01)

    chunk = await asyncio.wait_for(anext(stream), timeout=2)

    assert chunk.startswith(":")
    await stream.aclose()


async def test_reconnecting_over_the_stream_replays_first() -> None:
    bus = EventBus()
    first = bus.publish("ag-1", "one")
    bus.publish("ag-1", "two")
    stream = stream_notices(bus, "ag-1", first.seq)

    chunk = await asyncio.wait_for(anext(stream), timeout=2)

    assert "two" in chunk
    await stream.aclose()


def test_the_payload_carries_no_business_data(workspace: Path) -> None:
    """推送只承载"有变化了"。做成数据通道的话,断线那一刻页面就永久停在旧状态。"""
    bus = EventBus()
    notice = bus.publish("ag-1", "task_created")

    body = notice.as_sse()

    assert "ag-1" in body
    assert set(json.loads(body.split("data: ", 1)[1])) == {"task_id", "kind"}


def test_a_filtered_subscriber_does_not_see_other_tasks(workspace: Path) -> None:
    bus = EventBus()
    subscription = bus.subscribe("ag-1")

    assert not subscription.wants(bus.publish("ag-2", "task_created"))
    assert subscription.wants(bus.publish("ag-1", "task_created"))


def test_reconnecting_replays_what_was_missed(workspace: Path) -> None:
    bus = EventBus()
    first = bus.publish("ag-1", "one")
    bus.publish("ag-1", "two")

    missed = bus.since(first.seq, "ag-1")

    assert [notice.kind for notice in missed] == ["two"]


def test_a_slow_subscriber_never_blocks_the_orchestrator(workspace: Path) -> None:
    """一个卡住的浏览器标签页不该让整个编排器停下来等它。

    背压传导到生产者是最容易被写出来的默认行为,而它的症状是"某个任务莫名其妙不动了"。
    """
    from agentgenome.server.bus import QUEUE_SIZE

    bus = EventBus()
    subscription = bus.subscribe("ag-1")
    bus.attach(subscription)

    for _ in range(QUEUE_SIZE * 2):
        bus.publish("ag-1", "flood")

    assert bus.subscriber_count == 1


# --- CLI 与 REST 的一致性 ---------------------------------------------------


def _rest(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace))


def test_submitting_produces_the_same_task_either_way(workspace: Path) -> None:
    via_cli = runner.invoke(
        cli_app,
        ["task", "submit", "--requirement", "同一件事", "--workspace", str(workspace), "--json"],
    )
    assert via_cli.exit_code == 0, via_cli.output
    cli_task = json.loads(via_cli.output)

    rest_task = _rest(workspace).post("/tasks", json={"requirement": "同一件事"}).json()

    store = TaskStore(workspace)
    left, right = store.get(cli_task["id"]), store.get(rest_task["id"])
    assert left.state is right.state
    assert left.requirement == right.requirement
    assert left.priority == right.priority
    assert left.itest_override is right.itest_override


def test_cancelling_produces_the_same_state_and_events_either_way(workspace: Path) -> None:
    client = _rest(workspace)
    left = client.post("/tasks", json={"requirement": "取消我"}).json()
    right = client.post("/tasks", json={"requirement": "也取消我"}).json()

    runner.invoke(cli_app, ["task", "cancel", left["id"], "--workspace", str(workspace)])
    client.post(f"/tasks/{right['id']}/cancel")

    store, log = TaskStore(workspace), EventLog(workspace)
    assert store.get(left["id"]).state is store.get(right["id"]).state
    assert [event.kind for event in log.events(left["id"])] == [
        event.kind for event in log.events(right["id"])
    ]


def test_an_unknown_approver_is_refused_either_way(workspace: Path) -> None:
    """两条路的校验逻辑不同步,是这套系统最容易出的一类安全洞。"""
    client = _rest(workspace)
    task = client.post("/tasks", json={"requirement": "审我"}).json()

    via_cli = runner.invoke(
        cli_app,
        ["task", "approve", task["id"], "--as", "mallory", "--workspace", str(workspace)],
    )
    via_rest = client.post(
        f"/tasks/{task['id']}/approval", json={"actor": "mallory", "approved": True}
    )

    assert via_cli.exit_code != 0
    assert via_rest.status_code == 403
    assert TaskStore(workspace).get(task["id"]).state.value == "CREATED"


def test_an_empty_requirement_is_refused_either_way(workspace: Path) -> None:
    via_cli = runner.invoke(
        cli_app, ["task", "submit", "--requirement", "", "--workspace", str(workspace)]
    )
    via_rest = _rest(workspace).post("/tasks", json={"requirement": ""})

    assert via_cli.exit_code != 0
    assert via_rest.status_code >= 400
