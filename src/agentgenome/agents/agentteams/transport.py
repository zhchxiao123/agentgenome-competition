"""平台传输层:适配器与 AgentTeams 平台之间的全部往来收敛在这一个窄接口上。

**这是 PRD 31 唯一的新测试缝。** 语义层(`runtime`)只认这里的形状;真实的
HTTP/Matrix/存储调用、测试替身、录制与回放都是这同一个接口的不同实现。

## 为什么是"一次往返"而不是一组细粒度方法

派任务、等结果、传文件拆成多个方法的话,事务边界就散在调用方手里——录制要
拼接多次调用才能还原一次执行,回放要模拟中间状态。收敛成一次往返之后,
一份录制就是一对 `(TransportJob, TransportOutcome)`,事务性由类型保证。

## 为什么字段全是 JSON 可序列化的

录制素材(issue 06)就是这两个类型落盘。带上 Path/对象引用的话,素材要么
写不下来,要么写下来换台机器就失效。

**消费 token 不在 `TransportJob` 里**——它是传输实现自己的构造参数。这不是
省事:它因此**结构性地**进不了录制素材,而不是靠脱敏逻辑记得把它擦掉。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


class PlatformUnavailable(RuntimeError):
    """AgentTeams 平台整体不可用(不可达、鉴权失败、5xx)。

    单独一类,不混进普通失败:调用方要把它归因到**平台**,而不是员工或工序——
    "平台挂了"和"员工没干成"指向完全不同的下一步。
    """

    #: HTTP 状态码,拿得到时才有。`None` 表示这次失败根本没走到应答
    #: (连不上、超时、应答不是 JSON)。调用方靠它区分"资源不存在"与"平台出事了"。
    status: int | None = None


@dataclass(frozen=True)
class TransportJob:
    """下发给平台的一个任务。**一份录制素材的请求半边。**"""

    task_id: str
    employee_id: str
    procedure_ref: str
    round: int
    attempt: int
    subject: str
    #: 这一次尝试的上下文包全文。适配器负责从落盘的上下文文件读出来——
    #: 传输层不碰本地路径。
    context_text: str
    tools_allow: tuple[str, ...] = ()
    tools_deny: tuple[str, ...] = ()
    timeout_s: int = 1800
    max_tokens: int = 300_000
    #: 产物的 JSON Schema,随任务下发给 Worker(它要按这个交作业)。
    output_schema: dict[str, Any] = field(default_factory=dict)
    #: 随任务下发的交作业手艺(issue 04)。空表示不带。
    craft: str = ""
    #: 工作区快照:相对路径 → 内容(issue 03)。`None` 表示本次不带工作区。
    #:
    #: 契约刻意用最朴素的形状——文件映射。真实传输实现可以换成共享存储引用,
    #: 但那是它自己的优化,不进这份契约。
    workspace: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tools_allow"] = list(self.tools_allow)
        payload["tools_deny"] = list(self.tools_deny)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TransportJob:
        data = dict(payload)
        data["tools_allow"] = tuple(data.get("tools_allow") or ())
        data["tools_deny"] = tuple(data.get("tools_deny") or ())
        return cls(**data)


@dataclass(frozen=True)
class TransportOutcome:
    """平台还回来的执行结果。**一份录制素材的应答半边。**"""

    ok: bool
    detail: str | None = None
    #: 产物文件:文件名 → 内容,由适配器落到产物目录。`result.json` 走这里。
    artifacts: dict[str, str] = field(default_factory=dict)
    #: Worker 对工作区的改动:相对路径 → 新内容,`None` 表示删除(issue 03)。
    #: 未带工作区的任务这里是 `None`。
    changed_files: dict[str, str | None] | None = None
    #: 这次执行烧掉的 token。`None` 表示平台侧拿不到——**不填 0**(issue 05)。
    tokens_used: int | None = None
    #: 归一化事件(`NormalizedEvent.as_dict()` 的形状),进日志面。
    events: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TransportOutcome:
        return cls(**payload)


@runtime_checkable
class AgentTeamsTransport(Protocol):
    """与 AgentTeams 平台的一条通道。"""

    def preflight(self) -> None:
        """平台可达性与消费 token 有效性自检。不行就抛——配置问题在启动期暴露。"""
        ...

    async def run_job(self, job: TransportJob) -> TransportOutcome:
        """下发一个任务并等它结束。平台不可用抛 `PlatformUnavailable`。"""
        ...


__all__ = [
    "AgentTeamsTransport",
    "PlatformUnavailable",
    "TransportJob",
    "TransportOutcome",
]
