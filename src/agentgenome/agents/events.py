"""归一化事件与 token 计量。

各运行时的原生输出格式不同(PRD 16 会接第二个),但日志查看器与成本统计不该为此
分叉。所以每个运行时把自己的输出解析成这里的归一化事件,下游只认这一种形状。

回放运行时也工作在这一层——这不只是整洁,它是"同一份录制能在任何运行时配置下回放"
的前提。录制一旦与某个 CLI 的原生格式耦合,接第二个运行时时整套测试策略就要重写。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: 参与计量的 token 字段。
#:
#: 缓存读写也算——它们是真实花掉的量,漏掉会让预算失真。字段名照真实 stream-json。
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def sum_tokens(usage: dict[str, Any]) -> int:
    """一笔用量折算成 token 数。只此一处——两处各写一遍迟早会漂移。"""
    return sum(int(usage.get(key, 0) or 0) for key in TOKEN_FIELDS)


class EventKind(StrEnum):
    TEXT = "text"
    #: 员工的推理过程。**与 `TEXT` 分开是这一层的责任,不是渲染偏好。**
    #:
    #: 早先它被映射成 `TEXT`,于是"它在盘算什么"与"它的答复是什么"在归一化之后就再也
    #: 分不开了——下游想区分也无从下手,只能把一整段内心独白当正文渲染给用户看。
    #: 原生输出里这两者本来就是两种 content block,合并是我们自己丢的信息。
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    ERROR = "error"
    #: 外部运行时的原生终态。保留它才能区分“模型没产出”与“解析器没接住”。
    RUNTIME_RESULT = "runtime_result"
    #: 运行时原生的结构化终态。它由编排器落成 result.json，员工不直接寻找产物目录。
    STRUCTURED_OUTPUT = "structured_output"


@dataclass
class NormalizedEvent:
    """一条归一化事件。"""

    kind: EventKind
    text: str = ""
    #: 产生这条事件的 API 响应标识。用量去重靠它——见 `UsageAccumulator`。
    message_id: str | None = None
    usage: dict[str, Any] | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind.value}
        if self.text:
            payload["text"] = self.text
        if self.message_id:
            payload["message_id"] = self.message_id
        if self.usage is not None:
            payload["usage"] = self.usage
        if self.detail:
            payload["detail"] = self.detail
        return payload

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False)


class UsageAccumulator:
    """按 API 响应去重的 token 累加器。

    **不能逐行累加。** 同一次 API 响应会被拆成多行发出(thinking 一行、tool_use 一行),
    每行都带相同的 `message.id` 与**相同的** usage——那是这次请求的快照,不是增量。
    逐行加会翻倍多算。

    这不是推测:采集真实样本时验证过,按 message.id 去重后的和与终态权威总数逐字相等
    (见 `tests/fixtures/golden/claude-code/README.md`)。
    """

    def __init__(self) -> None:
        self._by_message: dict[str, int] = {}
        self._anonymous = 0
        self._authoritative: int | None = None
        self.seen_any = False

    def observe(
        self, message_id: str | None, usage: dict[str, Any], authoritative: bool = False
    ) -> None:
        """记一笔用量。

        `authoritative=True` 表示这是终态行报出的**总量**,不是又一笔花销——
        它替换运行中的估算而不是加上去。把它当增量加会让总数直接翻倍。
        """
        amount = sum_tokens(usage)
        self.seen_any = True
        if authoritative:
            self._authoritative = amount
        elif message_id is None:
            self._anonymous += amount
        else:
            # 同一 message 反复出现时取最新快照,不叠加。
            self._by_message[message_id] = amount

    def total(self) -> int:
        """当前用量。终态权威值一旦出现就以它为准。

        **运行中的值是下界。** 流里的 `output_tokens` 是流式过程中的部分值
        (实测 27 vs 真实 519),所以中途掐断会比设定的上限多花一些。结束之前
        这个数据根本不存在,只能这样。任务级预算用的是终态权威值,是准的。
        """
        if self._authoritative is not None:
            return self._authoritative
        return sum(self._by_message.values()) + self._anonymous

    def observe_event(self, event: NormalizedEvent) -> None:
        """吃一条归一化事件。真实运行与回放共用它,免得去重规则只在一边生效。"""
        if event.kind is not EventKind.USAGE or event.usage is None:
            return
        self.observe(
            event.message_id, event.usage, authoritative=bool(event.detail.get("authoritative"))
        )


__all__ = [
    "TOKEN_FIELDS",
    "EventKind",
    "NormalizedEvent",
    "UsageAccumulator",
    "sum_tokens",
]
