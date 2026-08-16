"""会话模型。

对话是与看板、审批并列的第一类交互平面,但它守着一条线:**对话能产生认知、草稿与结论,
不能绕过门禁、安全检查与审批把变更合进主线。**

## 两个自由度,不是三选一

一次会话只有两件事可选:**能不能改代码**(`writable`)、**关不关联某个任务**(`task_id`)。
工作目录由这两者推出来(见 `service.workdir_for`),不是第三个选项。

早先这里是一个三选一的 `SessionMode`(咨询/质询/结对),它把三件互不相干的事焊在了一起:
工作目录、写权限、上下文预载。焊在一起的代价是"想看得见代码"必须连带要一个任务和写权限
——而只读会话因此被放进一个空目录,拿着只读工具却无处可读。见 PRD 45。

## 写权限在创建时确定,之后不可变

这不是产品洁癖,是权限边界。只读会话强制只读工具集,可写会话拿的是员工完整的 `ScopePolicy`。
允许中途切换就等于提供了一条**不经任何闸门的提权路径**——而且这条路径长得像一个普通的
界面操作,不会有人觉得它需要审批。

所以 `evolve` 会拒绝改 `writable`,而不是靠调用方记得别改。

## 三态,不叫「已归档」

`CONTEXT.md` 的「已了结」词条把「归档」明确列为 `_Avoid_`,而且归档在本系统里另有所指
(审计包)。会话是**进行中 / 已挂起 / 已结束**。

已挂起**不是终态**:历史可读、可回放,而且可恢复——对 `claude 2.1.220` 的实测确认
`--resume` 没有短期 TTL(21 天前的会话仍可续接),恢复不需要任何额外机制。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

#: 只读会话能用的工具。**在服务层写死,不从员工定义继承**——员工定义里的 allow 是为写代码
#: 准备的,继承过来就等于只读模式名不副实。
READONLY_TOOLS = ("Read", "Grep", "Glob")

#: 空闲多久自动挂起。
DEFAULT_IDLE_TIMEOUT_S = 1800

#: 会话级 token 预算的缺省值。**0 = 不限。**
#:
#: 这里不再猜一个数。历史上猜过两次都猜错了:先是只读会话单独一档 20k(理由"只读推理
#: 便宜"),再是统一 50k;而只读会话搬到项目根上之后会真的去遍历代码,实测**一轮就能烧掉
#: 370k**。任何一个内置常数都会让用户在发出第一句之后就被挂起,而挂起理由只写"token 预算
#: 耗尽"——看不出这是一个拍脑袋定死的数,更看不出去哪儿改。
#:
#: 上限现在是**项目配置**(`budgets.session_tokens`),缺省不限。要设就由用它的人来设,
#: 而系统的默认行为是不拦。
DEFAULT_MAX_TOKENS = 0


def unlimited(max_tokens: int) -> bool:
    """这个上限是不是"不限"。**判定只此一处**——各处各写一遍 `<= 0` 的话,漏掉的那一处
    会把"不限"当成"上限是 0",于是第一轮就判超预算。
    """
    return max_tokens <= 0


def legacy_mode(writable: bool, task_id: str | None) -> str:
    """把两个自由度折回旧的模式名。

    **只为兼容而存在**:存储层的历史列、以及还没跟上的 API 调用方需要它。域模型里没有
    "模式"这个概念了,不要拿这个函数的返回值去做判断——判断读 `writable` 与 `task_id`。
    """
    if writable:
        return "pair"
    return "inquiry" if task_id else "consult"


class SessionState(StrEnum):
    """会话生命周期。**不含「已归档」**——见模块文档。"""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    ENDED = "ended"


class SuspendReason(StrEnum):
    """为什么挂起。

    **必须记下来。** 只显示一个「已挂起」而不说为什么,用户的第一反应是"它坏了"而不是
    "它按规则停了"——这与 `escalate_reason` 要用"下一步该从哪查"的口径写是同一条道理。
    """

    IDLE = "idle"
    BUDGET = "budget"


@dataclass(frozen=True)
class Session:
    """一次会话的全部状态。

    **不可变。** 改状态走 `evolve` 生成新值再存,理由同 `Task`:就地改字段的话,
    "这个对象现在是库里的样子还是改了一半的样子"没人说得清。
    """

    id: str
    employee_id: str
    #: 能不能改代码。**创建时定死,`evolve` 拒改**——见模块文档。
    writable: bool = False
    #: 这个会话跑在哪个运行时上。**记下来而不是每次现挑**——混配部署里"随便挑一个支持
    #: 会话的"会让第二条消息落到另一个运行时,而它的 `--resume` 指向一个不存在的会话。
    runtime: str = ""
    state: SessionState = SessionState.ACTIVE
    title: str = ""
    #: 关联的任务。可选,与 `writable` 正交:只读会话关了任务就预载它的产物与日志(出口是
    #: 回注审批意见),可写会话关了任务就在它的隔离工作区里改。两个都不关也是完全正常的一种。
    task_id: str | None = None
    #: 运行时自己的会话标识(Claude Code 的 `--resume` 参数)。
    native_session_id: str | None = None
    #: 运行时那一侧**真的建立过**这个会话了没有。决定下一条消息用新建参数还是续接参数,
    #: 两者不能同时给。**必须持久化**:它只活在内存里的话,重启后即使句柄重建成功,也会
    #: 因为标记丢了而重新走一次"新建会话"——那会在运行时侧撞上一个已经存在的标识。
    started: bool = False
    #: 会话工作区。由 `(writable, task_id)` 推出来——见 `service.workdir_for`。
    workdir: str = ""
    tokens_used: int = 0
    #: 这个会话的 token 上限。**0 = 不限**,见 `unlimited`。创建时从项目配置
    #: `budgets.session_tokens` 取一份快照。
    max_tokens: int = DEFAULT_MAX_TOKENS
    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S
    suspend_reason: SuspendReason | None = None
    #: 已经落盘的块序号。断线补齐与回放共用它。
    last_seq: int = 0
    #: 这次会话装载了哪些材料(`"card:order-service/reserve-flow"` / `"file:..."` /
    #: `"task:ag-..."`)。**上下文条渲染它**——没有这份清单,"员工带着什么在回答"就只能
    #: 写成一句笼统的话,而那正是这个控件存在的理由。
    context_items: tuple[str, ...] = ()
    #: 用户钉住的那些。**钉住的不参与截断**——预算紧张时先砍没钉的。
    pinned: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # --- 迁移 ----------------------------------------------------------------

    def evolve(self, **changes: Any) -> Session:
        """派生一个新值。**拒绝改 `writable`** ——见模块文档。"""
        if "writable" in changes and bool(changes["writable"]) != self.writable:
            was, now = ("只读", "可写") if changes["writable"] else ("可写", "只读")
            raise ValueError(
                f"会话 {self.id} 的读写权限不可更改({was} → {now})。"
                "只读与可写是两套工具集,中途切换等于一次没有闸门的提权;要换请新建会话。"
            )
        return replace(self, **changes)

    def suspend(self, reason: SuspendReason, now: datetime | None = None) -> Session:
        return self.evolve(
            state=SessionState.SUSPENDED,
            suspend_reason=reason,
            updated_at=now or datetime.now(UTC),
        )

    def resume(self, now: datetime | None = None) -> Session:
        if self.state is SessionState.ENDED:
            raise ValueError(f"会话 {self.id} 已结束,不能恢复。已结束是不可恢复的那一档。")
        return self.evolve(
            state=SessionState.ACTIVE,
            suspend_reason=None,
            updated_at=now or datetime.now(UTC),
        )

    def end(self, now: datetime | None = None) -> Session:
        return self.evolve(
            state=SessionState.ENDED,
            suspend_reason=None,
            updated_at=now or datetime.now(UTC),
        )

    # --- 判定 ----------------------------------------------------------------

    @property
    def accepts_messages(self) -> bool:
        return self.state is SessionState.ACTIVE

    @property
    def over_budget(self) -> bool:
        """超没超预算。**不限时恒为假**——0 是"没有上限",不是"上限是 0"。"""
        return not unlimited(self.max_tokens) and self.tokens_used >= self.max_tokens

    def idle_expired(self, now: datetime) -> bool:
        return (now - self.updated_at).total_seconds() >= self.idle_timeout_s

    @property
    def tools(self) -> tuple[str, ...]:
        """这个会话能用哪些工具。只读会话返回写死的只读集。"""
        return () if self.writable else READONLY_TOOLS

    # --- 展示 ----------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "employee_id": self.employee_id,
            "runtime": self.runtime,
            "writable": self.writable,
            # **派生值,不是真相。** 留一个版本给还在读 `mode` 的调用方,让前端可以分两次
            # 部署;真相是 `writable` 与 `task_id`。见 `legacy_mode`。
            "mode": legacy_mode(self.writable, self.task_id),
            "state": self.state.value,
            "title": self.title,
            "task_id": self.task_id,
            "workdir": self.workdir,
            "tokens_used": self.tokens_used,
            "max_tokens": self.max_tokens,
            "idle_timeout_s": self.idle_timeout_s,
            "suspend_reason": self.suspend_reason.value if self.suspend_reason else None,
            "last_seq": self.last_seq,
            "context_items": list(self.context_items),
            "pinned": list(self.pinned),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


__all__ = [
    "DEFAULT_IDLE_TIMEOUT_S",
    "DEFAULT_MAX_TOKENS",
    "READONLY_TOOLS",
    "Session",
    "SessionState",
    "SuspendReason",
    "legacy_mode",
    "unlimited",
]
