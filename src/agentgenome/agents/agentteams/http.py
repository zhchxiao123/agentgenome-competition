"""对真实 AgentTeams 平台的 HTTP 传输实现。

**平台 API 的全部假设收敛在这一个文件。** PRD 31 的三个平台侧先决验证
(Job 粒度用量、按 schema 交作业、同步延迟)对应的就是这里的往返;假设错了
只改这里,语义层与测试缝一概不动。

CI 不触达此处的网络路径:适配器的正确性由传输缝上的契约测试与录制回放保证,
对真实平台的验证走启动期预检与手工试跑。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

from agentgenome.agents.agentteams.transport import (
    PlatformUnavailable,
    TransportJob,
    TransportOutcome,
)

#: 轮询平台任务状态的间隔。
POLL_INTERVAL_S = 2.0

#: 单次 HTTP 往返的超时。与 Job 的墙钟超时(`JobSpec.timeout_s`,由适配器管)
#: 是两回事:这里管的是"一次请求悬死",那里管的是"整个 Job 干太久"。
REQUEST_TIMEOUT_S = 30


def http_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout_s: int = REQUEST_TIMEOUT_S,
) -> dict[str, Any]:
    """一次带 Bearer 鉴权的 JSON 往返。失败一律归因到平台。

    模块级而不是某个传输的方法:controller 预检、Matrix 消息、任务轮询都要
    这一段,各写一份的话"失败怎么归因"迟早有一处不同。令牌走请求头,
    **不进 URL**——URL 会进各级访问日志。
    """
    request = urllib.request.Request(
        url,
        method=method,
        data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 状态码挂在异常上:调用方要能把"这个资源不存在"与"平台出事了"分开处置,
        # 而从错误文案里抠数字是一种迟早会被改文案的人弄坏的做法。
        failure = PlatformUnavailable(f"平台应答 {exc.code}: {method} {url}")
        failure.status = exc.code
        raise failure from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PlatformUnavailable(f"平台不可达: {method} {url}: {exc}") from exc
    try:
        parsed: dict[str, Any] = json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        raise PlatformUnavailable(f"平台应答不是合法 JSON: {method} {url}") from exc
    return parsed


class HttpAgentTeamsTransport:
    """经平台 REST 入口的一条通道。消费 token 是构造参数,结构性地进不了录制素材。"""

    def __init__(
        self, endpoint: str, token: str, poll_interval_s: float = POLL_INTERVAL_S
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._token = token
        self._poll_interval_s = poll_interval_s

    def preflight(self) -> None:
        """平台可达性与消费 token 有效性。失败即抛——错误要指向平台,不是员工或工序。"""
        self._call("GET", "/api/v1/health")

    async def run_job(self, job: TransportJob) -> TransportOutcome:
        submitted = await asyncio.to_thread(self._call, "POST", "/api/v1/jobs", job.as_dict())
        job_id = submitted.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise PlatformUnavailable(f"平台受理应答里没有任务标识: {submitted!r}")
        while True:
            status = await asyncio.to_thread(self._call, "GET", f"/api/v1/jobs/{job_id}")
            if status.get("status") in ("completed", "failed"):
                try:
                    return TransportOutcome.from_dict(status.get("outcome") or {})
                except TypeError as exc:
                    # 形状不合法也要归因到**平台**——不然它以一条 Python 堆栈的
                    # 面目出现,看起来像适配器的 bug。
                    raise PlatformUnavailable(
                        f"平台应答的 outcome 形状不合法: {exc}"
                    ) from exc
            await asyncio.sleep(self._poll_interval_s)

    def _call(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return http_json(method, f"{self._endpoint}{path}", self._token, payload)


__all__ = ["HttpAgentTeamsTransport", "http_json"]
