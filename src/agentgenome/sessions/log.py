"""会话日志:块流的持久事实源。

## 为什么会话的 SSE 和 `/events/stream` 不是一回事

`bus.py` 那条通道刻意只报"有变化了"不带数据,因为页面数据随时可以重新拉。会话消息不适用
——**token 流本身就是交付物**,没有"重拉一遍就好了"这回事。

解法不是引入增量协议(那是分布式系统里最容易出错的一类代码),而是**让流成为持久日志的
tail**:块在生成的同时逐条落盘,SSE 只是这份日志的实时尾随,断线后带上最后收到的序号
走 REST 补齐。这与任务日志"历史走分页 REST、实时走 SSE tail"是同一套打法。

## 一份数据两个用途

这份日志同时就是**日志面**的记录载体(三平面模型,§12.2)。一份数据两个用途,不会发散
——如果为回放和为审计各存一份,两份迟早对不上,而对不上之后没有办法判断哪一份是真的。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agentgenome import paths
from agentgenome.sessions.blocks import Block

MESSAGES_FILE = "messages.jsonl"


def session_dir(workspace_root: Path, session_id: str) -> Path:
    return Path(workspace_root) / paths.SESSIONS / session_id


def append(workspace_root: Path, session_id: str, blocks: Iterable[Block]) -> int:
    """追加一批块,返回落盘后的最大序号。

    **边生成边落盘**,不是等一轮结束再写:用户中断或进程崩溃时,已经产生的内容要留得住。
    """
    directory = session_dir(workspace_root, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    last = 0
    with (directory / MESSAGES_FILE).open("a", encoding="utf-8") as sink:
        for block in blocks:
            sink.write(json.dumps(block.as_dict(), ensure_ascii=False) + "\n")
            sink.flush()  # 运行中要能被 tail
            last = max(last, block.seq)
    return last


def read(workspace_root: Path, session_id: str, after: int = 0) -> list[dict[str, Any]]:
    """读回块。`after` 之后的那些——断线补齐与回放共用这一条路。"""
    target = session_dir(workspace_root, session_id) / MESSAGES_FILE
    if not target.is_file():
        return []
    found: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            # 坏行跳过而不是让整次读取失败:一行坏数据不该让整段历史读不出来。
            continue
        if int(payload.get("seq", 0)) > after:
            found.append(payload)
    return found


def user_block(seq: int, text: str) -> Block:
    """用户发的那一条也进日志。

    只记员工的回答的话,日志面回答不了"他当时问的是什么"——而那是复盘一次对话时
    第一个要问的问题。
    """
    from agentgenome.sessions.blocks import BlockKind

    return Block(seq=seq, kind=BlockKind.TEXT, text=text, detail={"role": "user"})


def context_path(workspace_root: Path, session_id: str, message_index: int) -> Path:
    """某一轮的上下文包落盘位置。保证任意一轮可离线复现。"""
    return session_dir(workspace_root, session_id) / f"context-{message_index:03d}.md"


__all__ = [
    "MESSAGES_FILE",
    "append",
    "context_path",
    "read",
    "session_dir",
    "user_block",
]
