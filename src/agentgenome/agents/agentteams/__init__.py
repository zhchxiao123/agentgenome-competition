"""AgentTeams 运行时适配器(PRD 31)。

把"派给 AgentTeams 平台上的 Worker 容器执行"适配成既有的运行时协议。
对编排器、执行池、工序、基因组而言,它与本地子进程运行时没有任何区别。

包内分两层:

- `transport`:平台传输层——**本 PRD 唯一的新测试缝**,收敛全部对 AgentTeams
  的调用。测试用假的/回放的传输实现驱动,不起任何真实平台。
- `runtime`:任务语义层——把 JobSpec 翻译成平台任务、把平台结果还原成 JobResult。
"""

from agentgenome.agents.agentteams.runtime import AgentTeamsRuntime
from agentgenome.agents.agentteams.transport import (
    AgentTeamsTransport,
    PlatformUnavailable,
    TransportJob,
    TransportOutcome,
)

__all__ = [
    "AgentTeamsRuntime",
    "AgentTeamsTransport",
    "PlatformUnavailable",
    "TransportJob",
    "TransportOutcome",
]
