"""消息块:会话回包的最小单位。

## 后端不为前端造格式

块协议是**归一化事件的映射**,映射表住在这里。前端只认块,不认某个 CLI 的原生格式——
换一个运行时时,要改的是那个运行时的解析器,不是前端的渲染器。

## 为什么工具调用要成为一种块

**工具调用过程可视化是信任的来源。** 员工说"我查过了,这个补偿逻辑有超时保护",用户凭
什么信?看得见"正在读 src/order/reserve/timeout.py"的话,信任有依据;看不见,那句话就
只是一段可能对也可能不对的话——而这种态度下的对话没有使用价值。

所以 `tool-step` 必须带上**具体读了什么文件、跑了什么命令**。做成笼统的"正在思考…"
这个块就白做了。

## 序号

每个块带单调递增序号,断线重连后靠它补齐。序号由会话累计而不是每条消息重置——
跨消息的连续序号才能让"我收到第 42 号了"成为一个明确的断点。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agentgenome.agents.events import EventKind, NormalizedEvent


class BlockKind(StrEnum):
    """块类型。**这是对外契约**——前端按它注册渲染器。

    加一种块类型前端会降级成纯文本而不是白屏(见 PRD 28),所以后端可以先上线。
    """

    TEXT = "text"
    #: 员工的推理过程。**与 `TEXT` 是两种块,不是同一种块的两种样式。**
    #:
    #: 用户要读的是结论,而推理过程是**过程**——它与 `TOOL_STEP` 属于同一档:看得见才有
    #: 信任,但不该淹没结论。合成一种块的话,前端连"要不要默认折叠"这个问题都问不出来,
    #: 因为它拿不到"这一段是盘算还是答复"。
    THINKING = "thinking"
    CODE = "code"
    CARD_REF = "card-ref"
    FILE_REF = "file-ref"
    DIFF = "diff"
    TOOL_STEP = "tool-step"
    TASK_CARD = "task-card"
    GATE_REPORT = "gate-report"
    ACTION = "action"
    #: 错误内联呈现,不弹全局 toast 打断心流。
    ERROR = "error"


@dataclass(frozen=True)
class Block:
    """一个块。"""

    seq: int
    kind: BlockKind
    text: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"seq": self.seq, "kind": self.kind.value}
        if self.text:
            payload["text"] = self.text
        if self.detail:
            payload["detail"] = self.detail
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Block:
        return cls(
            seq=int(payload["seq"]),
            kind=BlockKind(payload["kind"]),
            text=payload.get("text", ""),
            detail=payload.get("detail", {}),
        )


#: 归一化事件到块的映射。**只此一处**——两处各写一遍迟早会漂移。
_KIND = {
    EventKind.TEXT: BlockKind.TEXT,
    EventKind.THINKING: BlockKind.THINKING,
    EventKind.TOOL_USE: BlockKind.TOOL_STEP,
    EventKind.TOOL_RESULT: BlockKind.TOOL_STEP,
    EventKind.ERROR: BlockKind.ERROR,
}

#: 从事件 detail 原样带到块 detail 的键。
#:
#: **"谁产出的"必须过得来。** `parent_tool_use_id` 非空就表示这一块是子 agent 干的,而
#: 那是与块类型正交的一根轴——子 agent 同样会思考、会调工具、会说话。丢在这一层的话,
#: 前端就只能把子 agent 的过程与主线的混在一条流里,而它们的可信度与相关性完全不同。
_CARRIED = ("tool_use_id", "parent_tool_use_id")


def blocks_from(
    events: Iterable[NormalizedEvent], start_seq: int, elapsed_s: float | None = None
) -> list[Block]:
    """把一串归一化事件映射成块。

    `USAGE` 不产块——用量是记账,不是给用户看的内容。空文本也不产块:渲染成一个空块
    只会让界面出现莫名其妙的空行。

    `elapsed_s` 是**这一轮开始到现在**的秒数,只落在工具块上。界面用它显示「用时 4.2s」
    ——那句话必须是真的量出来的:编一个数出来,用户会据此判断"它是不是卡住了",而这个
    判断会一直是错的。给不出来时就不写,界面少显示一项比显示一个假数好。
    """
    found: list[Block] = []
    seq = start_seq
    for event in events:
        kind = _KIND.get(event.kind)
        if kind is None:
            continue
        text = event.text.strip()
        name = str(event.detail.get("name", "")) if event.detail else ""
        if not text and not name:
            continue
        seq += 1
        detail: dict[str, Any] = {"name": name} if name else {}
        for key in _CARRIED:
            value = event.detail.get(key) if event.detail else None
            if value:
                detail[key] = value
        if kind is BlockKind.TOOL_STEP and elapsed_s is not None:
            detail["elapsed_s"] = round(elapsed_s, 1)
        found.append(
            Block(
                seq=seq,
                kind=kind,
                # 工具块的正文要说清"对什么做了什么",而不只是工具名。
                text=f"{name} {text}".strip() if kind is BlockKind.TOOL_STEP else text,
                detail=detail,
            )
        )
    return found


__all__ = ["Block", "BlockKind", "blocks_from"]
