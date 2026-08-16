"""事件总线:告诉订阅者"有变化了"。

## 推送只承载通知,不承载数据

页面数据始终以主动拉取为准。做成数据通道的话,断线那一刻页面就永久停在旧状态,而"重连补齐"
会变成一套需要自己维护正确性的增量协议——那是分布式系统里最容易出错的一类代码。

现在的形态下,推送挂了最坏结果只是"更新慢一点"。这个降级是可接受的,而且**不需要任何额外
代码**去保证。

## 为什么留成一个接口

本期只有进程内实现。形态刻意与 Redis Streams 对齐(单调递增的序号 + 从某个序号之后重放),
这样 PRD 18 换成多实例部署时,换的是实现而不是调用方。

## 订阅者断开不能影响编排器

每个订阅者一个独立队列,满了就丢最旧的。一个卡住的浏览器标签页不该让整个编排器停下来等它
——而"背压传导到生产者"正是最容易被写出来的默认行为。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any

#: 每个订阅者的队列长度。超了丢最旧的——订阅者的死活不该反压到编排器。
QUEUE_SIZE = 256

#: 总线自己保留多少条历史,供 `Last-Event-ID` 补发。
HISTORY_SIZE = 1024


@dataclass(frozen=True)
class Notice:
    """一条变化通知。**没有完整业务数据**——见模块说明。"""

    seq: int
    task_id: str
    kind: str
    #: 这条通知属于哪个项目(工作区名)。空串表示发布方没说——单项目部署的历史形态。
    #: 通知只带 task_id 与 kind,但 **task_id 本身就是数据**:多项目实例上把 a 的任务号
    #: 推给 b 的订阅者,是一次没有症状的越界。
    workspace: str = ""

    def as_sse(self) -> str:
        import json

        body = json.dumps({"task_id": self.task_id, "kind": self.kind}, ensure_ascii=False)
        # **不带 `event:` 字段。** 带上的话,浏览器 `EventSource` 会把这一帧当成"名字是
        # `self.kind` 的具名事件"派发,只有专门 `addEventListener(kind, ...)` 的监听者才
        # 收得到——而前端 `subscribe()` 一直是 `source.onmessage`,只认没有 `event:` 字段
        # (或 `event: message`)的默认事件。两边一直没对上:每一条通知都以"具名事件"发出,
        # `onmessage` 因此永远收不到任何一条。降级设计("推送挂了最多更新慢一点")把这个
        # bug 完全盖住了——直到"启动"按钮把"页面还能不能自己刷新"变成用户能直接看见的
        # 卡死状态,才第一次露出来。`kind` 本来就在 `data` 里,SSE 帧级别的 `event:` 是
        # 多余信息,删掉即可,不必反过来去改前端认哪些具名事件。
        return f"id: {self.seq}\ndata: {body}\n\n"


class Subscription:
    """一个订阅者。用作异步上下文管理器,退出时自动摘掉。"""

    def __init__(
        self, bus: EventBus, task_id: str | None, workspace: str | None = None
    ) -> None:
        self._bus = bus
        self._task_id = task_id
        self._workspace = workspace
        self._queue: asyncio.Queue[Notice] = asyncio.Queue(maxsize=QUEUE_SIZE)

    def wants(self, notice: Notice) -> bool:
        if self._task_id is not None and notice.task_id != self._task_id:
            return False
        # 订阅方说了要哪个项目,就只给那个项目的;发布方没标项目的通知照发——单项目
        # 部署里它是全部流量,拦掉等于把推送整个关了。
        if self._workspace is None or not notice.workspace:
            return True
        return notice.workspace == self._workspace

    def offer(self, notice: Notice) -> None:
        """投递一条。**队列满了丢最旧的,不阻塞。**"""
        if self._queue.full():
            with contextlib_suppress():
                self._queue.get_nowait()
        with contextlib_suppress():
            self._queue.put_nowait(notice)

    async def get(self) -> Notice:
        return await self._queue.get()

    async def __aenter__(self) -> Subscription:
        self._bus.attach(self)
        return self

    async def __aexit__(self, *_: Any) -> None:
        self._bus.detach(self)


class EventBus:
    """进程内实现。"""

    def __init__(self) -> None:
        self._seq = 0
        self._history: deque[Notice] = deque(maxlen=HISTORY_SIZE)
        self._subscribers: list[Subscription] = []

    def publish(self, task_id: str, kind: str, workspace: str = "") -> Notice:
        self._seq += 1
        notice = Notice(seq=self._seq, task_id=task_id, kind=kind, workspace=workspace)
        self._history.append(notice)
        for subscriber in list(self._subscribers):
            if subscriber.wants(notice):
                subscriber.offer(notice)
        return notice

    def since(
        self, last_id: int | None, task_id: str | None, workspace: str | None = None
    ) -> list[Notice]:
        """`Last-Event-ID` 之后的历史。

        补发失败不致命——前端的拉取策略本来就能自愈,这是同一个设计决定的两面。所以历史
        是有限的 `deque`,不是一份必须完整的日志。
        """
        if last_id is None:
            return []
        return [
            notice
            for notice in self._history
            if notice.seq > last_id
            and (task_id is None or notice.task_id == task_id)
            and (workspace is None or not notice.workspace or notice.workspace == workspace)
        ]

    def subscribe(
        self, task_id: str | None = None, workspace: str | None = None
    ) -> Subscription:
        return Subscription(self, task_id, workspace)

    def attach(self, subscription: Subscription) -> None:
        self._subscribers.append(subscription)

    def detach(self, subscription: Subscription) -> None:
        if subscription in self._subscribers:
            self._subscribers.remove(subscription)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class contextlib_suppress:
    """`asyncio.QueueEmpty` / `QueueFull` 都不该让投递失败。

    自己写而不是 `contextlib.suppress`:这里要压的是"投递永远不抛",而不是某几个具体异常——
    漏掉一个的表现是编排器被一个卡住的订阅者拖死。
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, asyncio.QueueEmpty | asyncio.QueueFull)


__all__ = ["HISTORY_SIZE", "QUEUE_SIZE", "EventBus", "Notice", "Subscription"]
