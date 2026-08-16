"""容器运行时的就绪检查:填完配置之后,当场知道这条链路通不通。

## 为什么分项报,而不是一个布尔

平台可达、存储可列、Matrix 令牌有效、服务端凭证在位**指向四个不同的运维动作**——去查
平台、去查桶策略、去查 Matrix、去补环境变量。合成一个"就绪 / 未就绪"的话,"哪一项挂了"
这个问题答不出来,而那正是人点这个按钮时唯一想知道的事。

## 与预检的分工

派发路径上的预检**遇到第一个失败就抛**——它的任务是"别带着坏配置往下走"。这里相反:
每一项都要跑完,因为人是来诊断的,不是来执行的。两者探的是同一批端点,但停下来的时机
完全不同,所以不共用一个函数。

## 令牌只答"在不在"

检查读的是环境变量**名**,回答的是它在服务端进程里有没有值。**值本身不进任何一个
detail 字段**——这份报告会被渲染进界面、写进排错记录,而凭证不该出现在那些地方。
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentgenome.agents.agentteams.http import http_json
from agentgenome.agents.agentteams.mirror import MinioMirror
from agentgenome.agents.agentteams.transport import PlatformUnavailable

if TYPE_CHECKING:
    from agentgenome.config import RuntimeEntry


@dataclass(frozen=True)
class ReadinessItem:
    """一项检查的结论。`detail` 给人看,**不含任何凭证值**。"""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ReadinessReport:
    items: tuple[ReadinessItem, ...]

    @property
    def ok(self) -> bool:
        """全通才算就绪。**但调用方要看的是分项**——见模块文档。"""
        return all(item.ok for item in self.items)


def _credentials(entry: RuntimeEntry) -> ReadinessItem:
    """令牌在不在。**本地就能答**,所以平台整个挂掉时它照样有结论。"""
    import os

    names = [entry.consumer_token_env, entry.matrix_token_env]
    missing = [name for name in names if name and not os.environ.get(name)]
    if missing:
        return ReadinessItem(
            name="credentials",
            ok=False,
            detail=(
                f"服务端进程里缺这些环境变量: {', '.join(missing)}。"
                "令牌只由环境变量给——配置里存的是变量名,不是值。"
            ),
        )
    return ReadinessItem(name="credentials", ok=True, detail="需要的令牌环境变量都有值。")


async def _platform(entry: RuntimeEntry) -> ReadinessItem:
    token = _token(entry.consumer_token_env)
    if token is None:
        return ReadinessItem("platform", False, "没有消费 token,无法验证平台。")
    try:
        await asyncio.to_thread(
            http_json, "GET", f"{str(entry.endpoint).rstrip('/')}/api/v1/status", token
        )
    except PlatformUnavailable as error:
        return ReadinessItem("platform", False, str(error))
    return ReadinessItem("platform", True, "平台入口可达且鉴权通过。")


async def _matrix(entry: RuntimeEntry) -> ReadinessItem:
    token = _token(entry.matrix_token_env)
    if token is None:
        return ReadinessItem("matrix", False, "没有 Matrix 令牌,无法验证。")
    endpoint = str(entry.matrix_homeserver or "").rstrip("/")
    if not endpoint:
        return ReadinessItem("matrix", False, "没有配置 Matrix 入口。")
    try:
        await asyncio.to_thread(
            http_json, "GET", f"{endpoint}/_matrix/client/v3/account/whoami", token
        )
    except PlatformUnavailable as error:
        return ReadinessItem("matrix", False, str(error))
    return ReadinessItem("matrix", True, "Matrix 令牌有效。")


async def _storage(entry: RuntimeEntry) -> ReadinessItem:
    if not entry.storage_prefix:
        return ReadinessItem("storage", False, "没有配置存储前缀。")
    mirror = MinioMirror(mc_argv=shlex.split(entry.mc_cmd), prefix=entry.storage_prefix)
    try:
        await asyncio.to_thread(mirror.preflight)
    except PlatformUnavailable as error:
        return ReadinessItem("storage", False, str(error))
    return ReadinessItem("storage", True, "存储前缀可列。")


def _token(env_name: str | None) -> str | None:
    import os

    return os.environ.get(env_name) if env_name else None


async def check_readiness(entry: RuntimeEntry) -> ReadinessReport:
    """把四项**全部**跑完再返回。

    每一项独立捕获自己的失败:一项挂掉不该让其余变成"未知"——那会把一次诊断变成
    一次猜谜。
    """
    credentials = _credentials(entry)
    platform, matrix, storage = await asyncio.gather(
        _platform(entry), _matrix(entry), _storage(entry)
    )
    return ReadinessReport(items=(platform, storage, matrix, credentials))


__all__ = ["ReadinessItem", "ReadinessReport", "check_readiness"]
