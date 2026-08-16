"""按剧本行事的 AgentTeams 平台替身。

与 `fake_agent` 同一条原则:不 mock 适配器内部,只替换传输缝另一端的平台。
适配器的任务翻译、产物落盘、契约校验、超时管理全是真实路径,只有"平台"是假的。
"""

from __future__ import annotations

import asyncio

from agentgenome.agents.agentteams.transport import (
    PlatformUnavailable,
    TransportJob,
    TransportOutcome,
)


class FakeTransport:
    """一条假通道。剧本是一串 `TransportOutcome`,依次出队,最后一份重复使用。"""

    def __init__(
        self,
        outcomes: list[TransportOutcome] | None = None,
        *,
        unavailable: str | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._outcomes = list(outcomes or [])
        self._unavailable = unavailable
        self._delay_s = delay_s
        #: 收到过的全部任务,测试断言"平台看到了什么"用。
        self.jobs: list[TransportJob] = []
        self.preflights = 0

    def preflight(self) -> None:
        self.preflights += 1
        if self._unavailable is not None:
            raise PlatformUnavailable(self._unavailable)

    async def run_job(self, job: TransportJob) -> TransportOutcome:
        self.jobs.append(job)
        if self._unavailable is not None:
            raise PlatformUnavailable(self._unavailable)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if not self._outcomes:
            raise AssertionError("FakeTransport 剧本用尽:没有可用的 TransportOutcome")
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        return outcome


__all__ = ["FakeTransport"]
