"""传输层的录制与回放。

构造新测试剧情的省力路径:开录制模式对真实平台跑一次,往返报文落盘;CI 里
回放驱动适配器,不起任何真实平台。与既有 Job 级录制同一套原则:

- **只录成功的交互。** 失败的落成素材之后,回放重现的原因是素材声明的而不是
  真实发生的——要录失败路径,手写 `outcome.json` 显式声明。
- **素材需人工过目再入库**,生产代码里不带指向测试目录的默认值,位置只由
  环境变量给(复用 `AGENTGENOME_RECORD` / `AGENTGENOME_RECORDINGS`)。
- **敏感信息结构性进不了素材**:消费 token 是传输实现的构造参数,
  `TransportJob` 里根本没有凭证字段——不靠脱敏逻辑记得擦。

素材形状:`<库>/agentteams/<键>/job.json` + `outcome.json`。键与 Job 级录制
同构(`员工__工序__r轮次`),外加区分并行模块的 `subject` 与区分契约重试的
`attempt`——不区分的话,回放会给每次尝试同一份产出,而测试照样是绿的。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentgenome.agents.agentteams.transport import (
    AgentTeamsTransport,
    PlatformUnavailable,
    TransportJob,
    TransportOutcome,
)

#: 素材在录制库里的子目录。与 Job 级录制共库不混目录。
SUBDIR = "agentteams"


def transport_key(job: TransportJob) -> str:
    """一次往返的素材键。附加维度只在偏离默认时出现,手写简单剧情不必背全格式。"""
    procedure_id = job.procedure_ref.split("@", 1)[0]
    key = f"{job.employee_id}__{procedure_id}__r{job.round}"
    if job.subject:
        key += f"__{job.subject}"
    if job.attempt > 1:
        key += f"__a{job.attempt}"
    return key


class RecordingTransport:
    """包住一条真通道,把每次成功往返落盘。"""

    def __init__(self, inner: AgentTeamsTransport, library: Path) -> None:
        self._inner = inner
        self._library = library

    def preflight(self) -> None:
        self._inner.preflight()

    async def run_job(self, job: TransportJob) -> TransportOutcome:
        outcome = await self._inner.run_job(job)
        if outcome.ok:
            directory = self._library / SUBDIR / transport_key(job)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "job.json").write_text(
                json.dumps(job.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (directory / "outcome.json").write_text(
                json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return outcome


class ReplayTransport:
    """从素材库回放。缺素材时报**键名**——写剧情的人要知道该补哪个目录。"""

    def __init__(self, library: Path) -> None:
        self._library = library

    def preflight(self) -> None:
        if not self._library.is_dir():
            raise PlatformUnavailable(f"录制库不存在: {self._library}")

    async def run_job(self, job: TransportJob) -> TransportOutcome:
        key = transport_key(job)
        path = self._library / SUBDIR / key / "outcome.json"
        if not path.is_file():
            raise PlatformUnavailable(f"没有这份传输录制: {key}(找过 {path})")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return TransportOutcome.from_dict(payload)


__all__ = ["SUBDIR", "RecordingTransport", "ReplayTransport", "transport_key"]
