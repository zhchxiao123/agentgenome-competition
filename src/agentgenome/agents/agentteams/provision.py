"""员工供应:把花名册里的数字员工对齐成平台上的 Worker。

## 为什么是与传输并列的另一道缝,而不是传输的几个方法

传输是**每 Job** 的、短生命周期的;供应是**每员工**的、长生命周期的。而且传输有三个
实现(真实通道、录制、回放),把供应塞进那个协议会逼另外两个各写一堆空方法——与
`SessionCapableRuntime` 当初从主运行时协议里拆出来是同一条理由。

## soul 里放什么、不放什么

Worker 侧运行时每一轮都会读它自己的身份文件。所以 soul 必须立住身份,否则平台预设的
人格(实测:平台自带的 Worker soul 写着"你是 QA 工程师")会被当成本职。

但**人格、工序、知识切片、失败报告不进 soul**——它们由每个 Job 的上下文包注入。员工
定义是版本化资产、会频繁改;写进 soul 就要靠重新供应才能更新,而陈旧的人格与新鲜的
上下文同时在场时,冲突是静默的(两边都言之凿凿,没有任何报错)。

soul 因此只有两段:**归属纪律**(不变)与**角色概要**(有界)。真相仍在员工定义里。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

from agentgenome.agents.agentteams.http import http_json
from agentgenome.agents.agentteams.transport import PlatformUnavailable
from agentgenome.config import AGENTTEAMS_RUNTIME, Config
from agentgenome.employees import EmployeeConfig

#: 我们供应出的 Worker 的名字前缀。**所有权判据靠它**——平台上人工建的、别的系统建的
#: Worker 必须原样存活,而"这个 Worker 是不是我们的"不能靠猜。取一个不易撞名的词。
OWNED_PREFIX = "agenome-"

#: 角色概要的字符上限。**有界是重点**:超出部分留给每个 Job 的上下文包,
#: 那份是新鲜的。
SUMMARY_LIMIT = 400

#: 轮询 Worker 就绪的间隔与上限。平台实测约 10 秒起来,留足余量。
POLL_INTERVAL_S = 3.0
READY_TIMEOUT_S = 180.0


class ProvisionError(RuntimeError):
    """供应没能完成,但平台是活着的(比如受理了却起不来)。

    与 `PlatformUnavailable` 分开:那个指向"去查平台",这个指向"去查这个 Worker"。
    """


class ProvisionUnavailable(RuntimeError):
    """这个工作区根本搭不出供应通道(没配 agentteams、或凭证缺失)。

    与 `ProvisionError` 分开:那个是"动手了没成",这个是"压根没法动手"——
    前者指向平台上那个 Worker,后者指向根配置与部署环境。
    """


@dataclass(frozen=True)
class WorkerRef:
    """平台上一个 Worker 的当下事实。**不落盘**——见 `WorkerProvisioner.resolve`。"""

    name: str
    room_id: str
    phase: str = ""
    sleeping: bool = False
    #: 平台上当前的身份文件与模型选择。**幂等对齐靠它们一起算差异**——
    #: 只比 soul 的话,换了档位会报"无变化",员工一直跑在旧模型上。
    soul: str = ""
    model: str = ""
    model_provider: str = ""


@dataclass(frozen=True)
class ReconcileOutcome:
    """一次对齐做了什么。

    **动作要报出来**:管理员执行一条声明式命令时,"什么都没变"与"更新了三个"是
    完全不同的两件事,而只回一个引用的话这两者长得一模一样。
    """

    ref: WorkerRef
    #: `created` / `updated` / `unchanged`。
    action: str


def worker_name(employee_id: str) -> str:
    """这个员工在平台上的 Worker 名字。带所有权前缀,且看得出是谁。"""
    return f"{OWNED_PREFIX}{employee_id}"


def is_ours(name: str) -> bool:
    """这个 Worker 是不是我们供应的。**不是的一律不碰。**"""
    return name.startswith(OWNED_PREFIX)


def role_summary(prompt_text: str, limit: int = SUMMARY_LIMIT) -> str:
    """从角色提示词里取一段有界的概要。

    取首段而不是截断全文:首段是角色的自我介绍,截断全文会在任意位置断开,
    可能正好停在一句话的反面(「绝不……」被截成「绝不」)。
    """
    paragraphs = [p.strip() for p in prompt_text.split("\n\n") if p.strip()]
    body = [p for p in paragraphs if not p.startswith("#")]
    summary = body[0] if body else ""
    return summary[:limit]


def assemble_soul(employee: EmployeeConfig) -> str:
    """这个员工在平台上的身份文件。

    两段:归属纪律(不变)+ 角色概要(有界)。见模块文档为什么不放全份人格。
    """
    name = employee.name or employee.id
    lines = [
        "# AgentGenome 数字员工",
        "",
        f"你是 **{name}**,由 AgentGenome 编排器统一调度,**不自行接活**。",
        "",
        "每个任务以任务目录 `shared/tasks/<任务引用>/` 为准:",
        "",
        "- `spec.md` 是这次作业的全部指令(角色、工序、知识切片、失败报告都在里面);",
        "- 代码改动只改 `workspace/` 下的文件;",
        "- 所有产物都写在 `artifacts/` 下:结构化结果写 `artifacts/result.json`,",
        "  工序要求的 `staging/...` 写成 `artifacts/staging/...`,小结写 `result.md`;",
        "- 先确认所有文件同步完成,最后才把 `meta.json` 的 status 置为 SUCCESS。",
        "",
        "**spec.md 的指令优先于本文件的任何笼统描述。**",
    ]
    summary = role_summary(employee.prompt_text)
    if summary:
        lines += ["", "## 角色概要", "", summary]
    return "\n".join(lines) + "\n"


class UnknownModelTier(ValueError):
    """员工声明了一个映射表里没有的模型档位。

    **不静默退回默认档**:退回的话,一个本该走便宜档的批量作业会悄悄走贵的,
    而账单要到月底才说话。
    """


@dataclass(frozen=True)
class ModelTier:
    """一个档位落到平台上的具体取值。

    提供商可选:单提供商的部署不该被逼着填一个占位值。
    """

    model: str
    provider: str = ""


def build_payload(
    employee: EmployeeConfig, tiers: dict[str, ModelTier] | None = None
) -> dict[str, Any]:
    """员工定义 → 平台创建载荷。**纯函数**,幂等对齐会拿它算差异。

    ## 档位翻译在这里,不在员工定义里

    员工只写抽象档位(`default` / `cheap` / `strong`),具体模型与提供商由部署配置给。
    理由与"运行时私有的工具名不许泄漏进员工定义"完全一致:泄漏之后,换一个部署就要
    改每一份员工资产。

    没配映射的部署不下发任何模型字段——平台用它自己的默认,行为零变化。
    """
    payload: dict[str, Any] = {
        "name": worker_name(employee.id),
        "soul": assemble_soul(employee),
    }
    table = tiers or {}
    if not table:
        return payload
    tier = table.get(employee.model)
    if tier is None:
        known = ", ".join(sorted(table)) or "(空)"
        raise UnknownModelTier(
            f"员工 {employee.id} 声明的模型档位 {employee.model!r} 不在映射表里"
            f"(已配置: {known})。检查员工定义与根配置的 model_tiers。"
        )
    payload["model"] = tier.model
    if tier.provider:
        payload["modelProvider"] = tier.provider
    return payload


def matches(existing: WorkerRef, payload: dict[str, Any]) -> bool:
    """平台上的现状与我们想要的是否一致。**对齐与预演共用这一条判据。**

    比全部我们管的字段,不只是 soul:漏掉模型字段的话,换档位会被报成"无变化",
    而"为什么它还在用贵的那个模型"要查到三层之外。

    而两处各写一遍的代价更贵:预演说"无变化"、真跑却更新了,人会以为自己看错了。
    """
    return (
        existing.soul == str(payload.get("soul", ""))
        and existing.model == str(payload.get("model", ""))
        and existing.model_provider == str(payload.get("modelProvider", ""))
    )


@runtime_checkable
class WorkerProvisioner(Protocol):
    """把员工对齐成平台 Worker 的一条通道。**本 PRD 唯一的新测试缝。**"""

    async def reconcile(self, employee: EmployeeConfig) -> ReconcileOutcome:
        """把这个员工对齐成一个就绪的 Worker。不存在则创建,有差异则更新,一致则不动。"""
        ...

    async def resolve(self, employee_id: str) -> WorkerRef | None:
        """这个员工当前的 Worker。没有则 `None`。**每次问平台,不读本地缓存。**"""
        ...

    async def wake(self, employee_id: str) -> WorkerRef:
        """确保它醒着。已经在跑的不打扰——见实现。"""
        ...

    async def sleep(self, employee_id: str) -> None:
        """让它休眠。容器停掉、资源还回去,**可逆**——下一次派发会自动唤醒。"""
        ...

    async def delete(self, employee_id: str) -> None:
        """删掉它。**不可逆**:重建会换房间,而房间 id 是不落盘的。"""
        ...


class HttpWorkerProvisioner:
    """经平台控制面 REST 的供应实现。令牌是构造参数,不进任何载荷或日志。"""

    def __init__(
        self,
        endpoint: str,
        token: str,
        poll_interval_s: float = POLL_INTERVAL_S,
        ready_timeout_s: float = READY_TIMEOUT_S,
        tiers: dict[str, ModelTier] | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._token = token
        self._tiers = dict(tiers or {})
        self._poll_interval_s = poll_interval_s
        self._ready_timeout_s = ready_timeout_s

    async def reconcile(self, employee: EmployeeConfig) -> ReconcileOutcome:
        """先看再动。**一致就一个字节都不写**——命令要能放进部署脚本反复执行。"""
        name = worker_name(employee.id)
        payload = self.build_payload(employee)
        existing = await self._get(name)

        if existing is None:
            await self._write("POST", "/api/v1/workers", payload)
            return ReconcileOutcome(ref=await self._wait_ready(name), action="created")

        if matches(existing, payload):
            return ReconcileOutcome(ref=existing, action="unchanged")

        # **更新而不是重建**:重建会换掉房间,而在跑的任务还指着旧房间。
        await self._write("PUT", f"/api/v1/workers/{name}", payload)
        return ReconcileOutcome(ref=await self._wait_ready(name), action="updated")

    async def _write(self, method: str, path: str, payload: dict[str, Any]) -> None:
        """所有写动作的唯一出口。**这里守所有权**——放在调用方记得检查的话,
        "忘了检查"就仍是一种可能的状态。"""
        target = path.rsplit("/", 1)[-1]
        if method != "POST" and not is_ours(target):
            raise ProvisionError(
                f"拒绝写一个不属于我们的 Worker: {target}。"
                "平台上人工创建的 Worker 由创建它的人负责。"
            )
        await asyncio.to_thread(http_json, method, f"{self._endpoint}{path}", self._token, payload)

    async def resolve(self, employee_id: str) -> WorkerRef | None:
        return await self._get(worker_name(employee_id))

    async def sleep(self, employee_id: str) -> None:
        """让这个员工的 Worker 休眠。容器停掉,资源还回去。"""
        name = self._own(employee_id)
        await asyncio.to_thread(
            http_json,
            "POST",
            f"{self._endpoint}/api/v1/workers/{quote(name, safe='')}/sleep",
            self._token,
            {},
        )

    async def delete(self, employee_id: str) -> None:
        """删掉这个员工的 Worker。**只删我们自己的**——名字带前缀,外人的碰不到。"""
        name = self._own(employee_id)
        await asyncio.to_thread(
            http_json,
            "DELETE",
            f"{self._endpoint}/api/v1/workers/{quote(name, safe='')}",
            self._token,
        )

    async def wake(self, employee_id: str) -> WorkerRef:
        """确保这个员工的 Worker 醒着。

        **已经在跑的不打扰**:多余的唤醒调用既浪费也可能打断正在干的活。
        """
        name = self._own(employee_id)
        ref = await self._get(name)
        if ref is None:
            raise ProvisionError(
                f"员工 {employee_id} 的 Worker 在唤醒前消失了。请重试这次派发;"
                "系统会在下一次派发时自动重建。"
            )
        if not ref.sleeping and ref.phase == "Running":
            return ref
        await asyncio.to_thread(
            http_json,
            "POST",
            f"{self._endpoint}/api/v1/workers/{quote(name, safe='')}/wake",
            self._token,
            {},
        )
        return await self._wait_ready(name)

    def _own(self, employee_id: str) -> str:
        """这个员工的 Worker 名,且**已确认归我们**。生命周期操作全走它。

        不用 `assert`:那在 `python -O` 下会被整条剥掉,而这是本模块唯一的
        安全闸。名字里带路径分隔符的一并拒绝——直接拼进 URL 的话它会指到
        另一个资源上。
        """
        name = worker_name(employee_id)
        if not is_ours(name) or "/" in name or ".." in name:
            raise ProvisionError(
                f"拒绝操作一个不属于我们的 Worker: {name!r}。"
                "只有带所有权前缀且名字合法的 Worker 由本系统负责。"
            )
        return name

    def build_payload(self, employee: EmployeeConfig) -> dict[str, Any]:
        """这次部署下,这个员工的平台载荷。档位映射来自根配置。"""
        return build_payload(employee, self._tiers)

    # --- 内部 ----------------------------------------------------------------

    async def _get(self, name: str) -> WorkerRef | None:
        try:
            body = await asyncio.to_thread(
                http_json, "GET", f"{self._endpoint}/api/v1/workers/{name}", self._token
            )
        except PlatformUnavailable as exc:
            if exc.status == 404:
                return None
            raise
        return _ref_from(body)

    async def _wait_ready(self, name: str) -> WorkerRef:
        """轮询到"起来了且有房间"。

        **房间是就绪的一部分**:没有房间就派不了活,这时报 Running 也没有意义。
        """
        waited = 0.0
        last = ""
        while waited < self._ready_timeout_s:
            ref = await self._get(name)
            if ref is not None:
                last = ref.phase
                if ref.phase == "Running" and ref.room_id:
                    return ref
            await asyncio.sleep(self._poll_interval_s)
            waited += self._poll_interval_s
        raise ProvisionError(
            f"Worker {name} 在 {self._ready_timeout_s:.0f} 秒内没有就绪"
            f"(最后状态: {last or '未知'};就绪要求 Running 且已分配房间)。"
        )


def build_provisioner(config: Config) -> WorkerProvisioner:
    """按根配置搭出供应通道。

    与运行时注册表同一条装配原则:**失败抛异常,不依赖任何 CLI 框架**;凭证只由
    环境变量给,缺失在这里就报,而不是等真的去调平台时才发现 token 是空的。
    """
    entry = config.runtime.runtimes.get(AGENTTEAMS_RUNTIME)
    if entry is None or not entry.endpoint or not entry.consumer_token_env:
        raise ProvisionUnavailable(
            "这个工作区没有配置 agentteams 运行时——供应无从谈起。"
            "先在根配置的 runtime 段加一条 agentteams 条目。"
        )
    token = os.environ.get(entry.consumer_token_env)
    if not token:
        raise ProvisionUnavailable(
            f"agentteams 的消费 token 缺失: 环境变量 {entry.consumer_token_env} 未设置或为空。"
            "检查根配置 runtime.agentteams.consumer_token_env 与部署环境。"
        )
    return HttpWorkerProvisioner(endpoint=entry.endpoint, token=token, tiers=tiers_from(entry))


def tiers_from(entry: Any) -> dict[str, ModelTier]:
    """根配置的档位表 → 供应层的形状。**只此一处翻译**,两处各写一遍迟早漂移。"""
    return {
        name: ModelTier(model=tier.model, provider=tier.provider)
        for name, tier in (entry.model_tiers or {}).items()
    }


# --- 只读视图:现状与预演 ------------------------------------------------------
#
# 这两件事界面与命令行都要,而它们只用协议里的 `resolve`。放在这里而不是各自实现:
# 命令行的预演与界面的预演对同一份配置给出两个答案时,没人能判断哪个是对的。

#: 容器状态的四种取值。`unknown` 是"平台没答上来",与"没供应过"是两回事——
#: 合成一个的话,平台一挂,整份花名册看起来就像从没供应过。
ABSENT = "absent"
RUNNING = "running"
SLEEPING = "sleeping"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorkerStatus:
    """一个员工此刻在平台上的样子。**每次问平台算出来,不落盘**。

    真机实测:Worker 重建会换房间 id。缓存下来的那个会静默过期——它仍然是一个
    格式正确的房间 id,发进去的消息不会报错,只是没人在那边听。
    """

    employee_id: str
    status: str
    worker: str = ""
    room: str = ""
    #: 给人看的补充说明:`unknown` 时是平台报的原因,否则是平台侧的 phase。
    detail: str = ""


@dataclass(frozen=True)
class PlanRow:
    """ "这次点下去会发生什么"里的一行。"""

    employee_id: str
    #: `created` / `updated` / `unchanged` / `unknown`。
    action: str
    detail: str = ""


async def survey(
    provisioner: WorkerProvisioner, employees: Sequence[EmployeeConfig]
) -> list[WorkerStatus]:
    """这些员工现在各自是什么状态。**只读**。

    一个员工读不到不牵连其余:平台对某个 Worker 答不上来时,其余那些的状态仍然是
    已知的,而把整张表打成一个错误会让人失去他本来看得见的信息。
    """
    return list(await asyncio.gather(*(_status_of(provisioner, e) for e in employees)))


async def _status_of(provisioner: WorkerProvisioner, employee: EmployeeConfig) -> WorkerStatus:
    try:
        ref = await provisioner.resolve(employee.id)
    except (PlatformUnavailable, ProvisionError) as error:
        return WorkerStatus(employee_id=employee.id, status=UNKNOWN, detail=str(error))
    if ref is None:
        return WorkerStatus(employee_id=employee.id, status=ABSENT)
    return WorkerStatus(
        employee_id=employee.id,
        status=SLEEPING if ref.sleeping else RUNNING,
        worker=ref.name,
        room=ref.room_id,
        detail=ref.phase,
    )


async def plan(
    provisioner: WorkerProvisioner,
    employees: Sequence[EmployeeConfig],
    tiers: dict[str, ModelTier] | None = None,
) -> list[PlanRow]:
    """对齐这些员工**会**做什么。读一遍现状再算,**平台侧零写动作**。

    "将对齐"对管理员没有信息量:他要知道的是新建还是更新——前者会拉起容器并花掉一次
    真实的模型探活,后者不会。
    """
    return list(await asyncio.gather(*(_plan_one(provisioner, e, tiers) for e in employees)))


async def _plan_one(
    provisioner: WorkerProvisioner,
    employee: EmployeeConfig,
    tiers: dict[str, ModelTier] | None,
) -> PlanRow:
    try:
        payload = build_payload(employee, tiers)
        existing = await provisioner.resolve(employee.id)
    except (PlatformUnavailable, ProvisionError, UnknownModelTier) as error:
        return PlanRow(employee_id=employee.id, action=UNKNOWN, detail=str(error))
    if existing is None:
        return PlanRow(employee_id=employee.id, action="created")
    if matches(existing, payload):
        return PlanRow(employee_id=employee.id, action="unchanged")
    return PlanRow(employee_id=employee.id, action="updated")


def _ref_from(body: dict[str, Any]) -> WorkerRef:
    return WorkerRef(
        name=str(body.get("name", "")),
        room_id=str(body.get("roomID") or ""),
        phase=str(body.get("phase") or ""),
        sleeping=str(body.get("state") or "") == "Sleeping",
        soul=str(body.get("soul") or ""),
        model=str(body.get("model") or ""),
        model_provider=str(body.get("modelProvider") or ""),
    )


__all__ = [
    "ABSENT",
    "OWNED_PREFIX",
    "RUNNING",
    "SLEEPING",
    "UNKNOWN",
    "HttpWorkerProvisioner",
    "PlanRow",
    "ProvisionError",
    "ProvisionUnavailable",
    "ReconcileOutcome",
    "WorkerProvisioner",
    "WorkerRef",
    "WorkerStatus",
    "ModelTier",
    "UnknownModelTier",
    "assemble_soul",
    "build_payload",
    "build_provisioner",
    "matches",
    "plan",
    "survey",
    "tiers_from",
    "is_ours",
    "role_summary",
    "worker_name",
]
