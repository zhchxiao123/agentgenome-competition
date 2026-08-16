"""每个人关心哪些任务的哪些事件。

**按用户 + 事件类型配置,不是全开或全关。** 任务状态变化很频繁,全量推送几轮之后就会被
当成噪声关掉——真正有用的是"只推我关心的那几种"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentgenome.approval import notify as approval_notify

#: 偏好落盘的位置。与设置变更审计同放 `tasks/` 下——它们都是跨任务的账。
PREFS_FILE = Path("tasks") / "notification-preferences.json"

#: 任务生命周期里能订阅的事件。与 `_publish` 调用点使用的 `kind` 字符串保持一致。
KNOWN_EVENTS = frozenset(
    {"task_created", "cancelled", "approved", "rejected", "escalated", "completed"}
)


@dataclass
class Preference:
    actor: str
    events: list[str] = field(default_factory=list)
    webhook_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"actor": self.actor, "events": self.events, "webhook_url": self.webhook_url}

    def wants(self, event: str) -> bool:
        return event in self.events


def load_all(workspace_root: Path) -> list[Preference]:
    path = Path(workspace_root) / PREFS_FILE
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Preference(**item) for item in payload]


def for_actor(workspace_root: Path, actor: str) -> Preference:
    for pref in load_all(workspace_root):
        if pref.actor == actor:
            return pref
    return Preference(actor=actor)


def save(workspace_root: Path, preference: Preference) -> Preference:
    """按 `actor` upsert。**不允许订阅未知事件**——拼错的事件名会让人以为自己订阅成功了,
    实际上永远收不到推送。
    """
    unknown = set(preference.events) - KNOWN_EVENTS
    if unknown:
        raise ValueError(
            f"未知事件类型: {', '.join(sorted(unknown))}(可选: {', '.join(sorted(KNOWN_EVENTS))})"
        )
    existing = {pref.actor: pref for pref in load_all(workspace_root)}
    existing[preference.actor] = preference
    path = Path(workspace_root) / PREFS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([item.as_dict() for item in existing.values()], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return preference


def push(workspace_root: Path, task_id: str, title: str, event: str) -> None:
    """任务状态变化推送到 IM。**失败不阻塞任何调用方**——发送这一步已经在流程之外,一个配错的
    webhook 不该反过来卡住任务本身或者卡住把它推进到这个状态的那次调用。

    **一处实现,两个调用方。** REST 的 submit/cancel/approval 在请求内同步触发状态变化,能
    直接调这里;但 `ESCALATED`/`COMPLETED` 这两个状态通常是 `agctl task advance` 单步推进
    出来的——那条路径不经过 REST。两个调用方各写一份发送逻辑的话,后者迟早会被漏掉,而现状
    正是如此:这两个订阅项在补上这处调用之前是订了也不会响的静默死档。
    """
    for pref in load_all(workspace_root):
        if not pref.wants(event) or not pref.webhook_url:
            continue
        text = f"【AgentGenome】任务 {task_id}({title})状态变化: {event}"
        approval_notify.send_payload(
            pref.webhook_url, {"text": text, "task_id": task_id, "event": event}
        )


__all__ = ["KNOWN_EVENTS", "PREFS_FILE", "Preference", "for_actor", "load_all", "push", "save"]
