"""Agent 运行时抽象。

把"拉起一个数字员工干一件事"收敛成一个统一接口:给一份 `JobSpec`,还一份
`JobResult`。具体是 Claude Code 还是别的 CLI,由配置决定,上层完全不感知。

**这个接口就是整套系统的主测试缝。** 测试时换上回放运行时,CI 里跑的就是真实的
状态机、真实的 git、真实的 pytest,唯独 Agent 那一段是确定性的。

## JobSpec 为什么是扁平的

设计文档初稿写的是 `employee: EmployeeConfig` 与 `procedure: ProcedureSpec`,但那两个类型
分别属于 PRD 04 与 PRD 03,而三者本该并行。照初稿实现会把它们串成一条线,或者把
Procedure 注册表、员工权限这些概念漏进运行时层。

运行时实际需要的只是下面这组扁平字段。`employee_id` / `procedure_id` / `procedure_version`
纯粹是记录项(进事件流用),运行时自己不消费。PRD 03 与 PRD 04 各自负责把
`ProcedureSpec` / `EmployeeConfig` 塌缩成 `JobSpec`。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agentgenome.core.scope import ScopePolicy


class FailureKind(StrEnum):
    """Job 为什么没成。

    区分它们是有代价的,但值得:状态机对每一类的反应不同。契约失败该重试,
    预算耗尽该升级人工,越权该回滚工作区——混成一个"失败"就全都做不成了。
    """

    NONE = "none"
    #: 产物不存在或不符 schema。
    CONTRACT = "contract"
    #: 外部运行时结束了，却没有按结构化输出协议交付可落盘的小票。
    #:
    #: 与 CONTRACT 分开：后者说明产物本身不合法；这里说明产物根本没有从运行时边界
    #: 交出来。两者的人工动作不同，前者改产物，后者查 CLI/schema/解析器。
    PROTOCOL = "protocol"
    #: 墙钟超时。
    TIMEOUT = "timeout"
    #: 单 Job 的 token 上限被运行时中途掐断。这一个 Job 的事,状态机按普通失败走修复循环。
    BUDGET = "budget"
    #: **任务**预算装不下这个 Job,池在开始前就拒了。任务级终结,直接升级人工——
    #: 重试只会再撞一次同一堵墙。混成一个的话,一次单 Job 超限会把整个任务错误地终结掉。
    TASK_BUDGET = "task_budget"
    #: 子进程非零退出或起不来。
    PROCESS = "process"
    #: 改动超出授权路径(由 PRD 04 的越权检查填)。
    SCOPE = "scope"
    #: 从持久化记录读到了当前版本不认识的失败类型。保留“不认识”本身，不猜成别的原因。
    UNKNOWN = "unknown"


@dataclass
class JobSpec:
    """一次执行需要知道的全部东西。"""

    task_id: str
    #: 纯记录项:进事件流用,运行时自己不消费。
    employee_id: str
    procedure_id: str
    procedure_version: str
    #: 本 Job 是该任务该 Procedure 的第几轮,从 1 起。
    #:
    #: 它对生产运行时几乎无用,但它是回放缝能表达"修复循环"的唯一途径——同一个
    #: `(employee, procedure)` 在多轮里必须返回不同结果。显式传比让回放自己数调用次数
    #: 好:录制目录名自解释,不与调用顺序隐式耦合。
    round: int
    #: 隔离工作区根,也是子进程的工作目录。
    workdir: Path
    #: 已经组装好的上下文包。运行时不负责组装(那是 PRD 04 的事)。
    context_file: Path
    #: 产物目录。`result.json` 必须落在这里。
    output_dir: Path
    #: 这次作业作用在哪个模块上。**并行派发逼出来的那一维**:逐模块深读会同时派几十个
    #: 同 employee 同 procedure 的作业,不带模块的话回放键会全撞在一起——回放会给每个模块
    #: 返回同一份产出,而测试照样是绿的。空表示不作用于某一个模块(绝大多数作业都是这种)。
    subject: str = ""
    #: 产物的 JSON Schema。
    output_schema: dict[str, Any] = field(default_factory=dict)
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)
    #: 要注入子进程的环境变量。子进程环境从白名单构造,不继承父进程全量环境。
    credentials: dict[str, str] = field(default_factory=dict)
    timeout_s: int = 1800
    max_tokens: int = 300_000
    #: False 时 `max_tokens` 只用于上下文组装，不作为运行中的中止闸。
    enforce_token_limit: bool = True
    #: 这份活派给谁(人)。**只有 human 运行时消费它**:硅基运行时对它一无所知,而
    #: 它必须走到运行时层——待办投给谁,是派发那一刻就定了的事,不是待办系统自己猜的。
    assignee: str = ""
    #: 这个 Job 允许碰哪些路径,占位符已展开、受保护路径已叠加。
    #:
    #: 运行时自己不消费它——**它是 Job 结束后那道无条件越权检查的输入**(见
    #: `AgentPool.submit`)。`None` 表示不做检查,只有 `agctl job run` 这类
    #: 手工原语会这么用:它可以指向任意目录,那里未必是个 git 仓。
    scope: ScopePolicy | None = None
    #: 产物的额外裁决:拿产物目录,返回 `None`(通过)或拒绝原因。
    #:
    #: **树产出类工序的成败判据**(PRD 34):它们的产物是 staging/ 下的真实文件,
    #: JSON Schema 说不了"这棵树合不合法"。与 schema 同属契约的一部分——失败按
    #: `FailureKind.CONTRACT` 处理,走契约重试外环,拒绝原因回注给下一次尝试。
    #: 运行时对它的语义一无所知,只负责在契约校验时机调用——所以它是个闭包而不是
    #: 一段配置:裁决规则属于派发方,不属于运行时层。
    output_check: Callable[[Path], str | None] | None = None
    #: 契约失败后允许再启动几次完整进程。普通研发 Job 必须为 0：少一张小票不能
    #: 让整轮开发暗中重做。只有 staging 这类显式声明为增量修复的树产出可设为 1。
    contract_retries: int = 0
    #: 外部运行时没交付结构化终态时允许补交几次。与产物契约重试分账，避免 staging
    #: 因为能增量修文件就顺带获得一次完整的协议重跑。
    protocol_retries: int = 0

    @property
    def procedure_ref(self) -> str:
        """事件流里记录的工序标识,报告能精确复现"当时是哪版工序干的活"。"""
        return f"{self.procedure_id}@{self.procedure_version}"


@dataclass
class JobResult:
    """一次执行的结果。"""

    ok: bool
    failure_kind: FailureKind = FailureKind.NONE
    failure_detail: str | None = None
    result_path: Path | None = None
    log_path: Path | None = None
    tokens_used: int = 0
    #: token 用量是否可得。拿不到时显式标记,**不填 0**——填 0 会让成本看板
    #: 悄悄少算,比缺数据更糟。
    tokens_available: bool = True
    duration_s: float = 0.0
    exit_code: int | None = None
    #: 实际尝试次数(含契约失败后的那一次重试)。
    attempts: int = 1
    dry_run: bool = False
    #: 录制模式下这次运行被落盘到哪。产出需要人工过一遍再入库。
    recorded_to: Path | None = None
    #: 越权报告的落盘位置。有 `scope` 的 Job 一定有它,不管有没有越权。
    scope_report_path: Path | None = None
    #: 这个 Job **挂起了**:活派出去了,但结果要等外面的人(或系统)回来才有。
    #:
    #: **不复用 `ok=False`**:挂起不是失败。混进失败语义的话,状态机会对它做出错误反应
    #: ——重试、升级、扣一轮修复轮次,而这三样对"人还没开始干"都是错的。
    suspended: bool = False
    #: 挂起时那件事的标识(待办 id)。恢复靠它把人交回来的产物对上这次派发。
    suspended_on: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failure_kind": self.failure_kind.value,
            "failure_detail": self.failure_detail,
            "result_path": str(self.result_path) if self.result_path else None,
            "log_path": str(self.log_path) if self.log_path else None,
            "tokens_used": self.tokens_used if self.tokens_available else None,
            "tokens_available": self.tokens_available,
            "duration_s": round(self.duration_s, 3),
            "exit_code": self.exit_code,
            "attempts": self.attempts,
            "dry_run": self.dry_run,
            "recorded_to": str(self.recorded_to) if self.recorded_to else None,
            "scope_report_path": str(self.scope_report_path) if self.scope_report_path else None,
            "suspended": self.suspended,
            "suspended_on": self.suspended_on,
        }


@dataclass
class SessionSpec:
    """开一个会话需要知道的全部东西。

    **与 `JobSpec` 一样保持扁平**,理由同上:不让会话服务的概念漏进运行时层。

    ## workdir 决定这个会话能活多久

    对 `claude 2.1.220` 的实测:会话按 **cwd 路径**分桶存于
    `~/.claude/projects/<路径转义>/<uuid>.jsonl`,换一个 cwd 续接会直接失败
    (`No conversation found with session ID`)。没有短期 TTL(21 天前的会话仍可续接),
    目录删掉再按同路径重建也照样能续——**只认路径字符串,不认目录内容**。

    所以这个字段不是随便填的:咨询/质询要用稳定路径,结对用任务 worktree(其寿命因此
    等于任务)。决定在 `sessions.service`,这里只承载结果。
    """

    session_id: str
    employee_id: str
    #: 会话工作区,也是子进程的 cwd。
    workdir: Path
    #: 已经组装好的上下文包。运行时不负责组装。
    context_file: Path
    #: 运行时自己的会话标识。首次为 `None`,之后由 `SessionHandle` 带回来续接。
    native_session_id: str | None = None
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)
    credentials: dict[str, str] = field(default_factory=dict)
    max_tokens: int = 50_000
    timeout_s: int = 600


@dataclass
class SessionHandle:
    """一个已经开起来的会话。

    **不含子进程句柄。** 每条消息是一次独立的 headless 调用,进程活不过一条消息;
    会话状态由运行时自己的 session 机制承载,编排器只记账。
    """

    session_id: str
    #: 运行时自己的标识(Claude Code 的 `--resume` 参数)。
    native_session_id: str | None = None
    workdir: Path | None = None
    #: 已经发过至少一条消息。**决定用 `--session-id` 还是 `--resume`** ——两者不能同时给,
    #: 而"这个会话建立了没有"是运行时之外看不出来的事。
    started: bool = False
    #: 纯记录项(生产运行时不消费),但**回放缝靠它定位录制**:同一个会话的多轮必须返回
    #: 不同结果,否则测不出"它记得上一句"。与 `JobSpec.round` 同一条理由——显式传比让
    #: 回放自己数调用次数好。
    employee_id: str = ""
    message_index: int = 1


@dataclass
class SessionTurn:
    """一次问答**结束之后**的账。

    **不含事件**——事件在流里逐条给出去了。把它们再装一份在这里,调用方就会有两条路拿到
    同一批数据,而两条路迟早不一致。
    """

    tokens_used: int = 0
    tokens_available: bool = True
    native_session_id: str | None = None
    ok: bool = True
    failure_detail: str | None = None


@runtime_checkable
class AgentRuntime(Protocol):
    """一个 CLI Agent 运行时。"""

    name: str

    async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
        """执行一次 Job。"""
        ...

    def preflight(self) -> None:
        """启动期自检。依赖缺失时抛异常——配置问题该在启动时暴露,不是任务跑一半才炸。"""
        ...


class SessionStream:
    """一轮问答的事件流,外加结束时的账。

    ## 为什么账要单独挂一个对象

    异步生成器被提前丢弃(客户端断线、用户按了停止)时,`async for` 那一侧根本走不到收尾
    那几行。而这一轮**token 已经烧掉了**——不结账的表现是成本看板长期少算,而且少算的量
    随断线频率变化,没人能对上。

    所以账写在这个对象上,由运行时在自己的 `finally` 里填;调用方无论正常结束还是中途退出,
    都能从 `stream.turn` 读到。
    """

    def __init__(self, events: AsyncIterator[Any], turn: SessionTurn) -> None:
        self._events = events
        #: 这一轮的账。**流结束(含被中断)之后才是最终值。**
        self.turn = turn

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._events

    async def aclose(self) -> None:
        """收摊。**先把剩下的事件排完,再关。**

        直接关掉是不够的:用量事件在流的**最后**才出现(Claude Code 的 `result` 行),
        只收了前几块就关的话,`usage` 根本没被读到,账会记成 0。而那一轮的 token
        **已经烧掉了**——少记的表现是成本看板长期偏低,且偏差随断线频率变化,没人能对上。

        排空是有界的:运行时那一侧本来就有超时(`spec.timeout_s`),回放则是有限的。

        真正的"立刻掐断子进程"是另一回事,这里不做——那需要一条显式的 kill 路径,而
        它与"客户端断线"不是同一个语义。
        """
        with contextlib.suppress(Exception):
            async for _ in self._events:
                pass
        closer = getattr(self._events, "aclose", None)
        if closer is not None:
            with contextlib.suppress(Exception):
                await closer()


@runtime_checkable
class SessionCapableRuntime(Protocol):
    """能开交互会话的运行时。

    **单独一个协议,不塞进 `AgentRuntime`。** 不是所有 CLI Agent 都有 `--resume` 的
    等价物;塞进主协议的话,每个不支持会话的运行时都要实现两个抛 `NotImplementedError`
    的方法,而"没实现"与"实现了但会炸"在类型上分不开。

    能不能开会话由 `capabilities.sessions` 声明,在 `preflight` 阶段暴露,
    不是等用户创建会话时才失败。
    """

    name: str

    async def start_session(self, spec: SessionSpec) -> SessionHandle:
        """开一个会话。**不拉起常驻进程**——见 `SessionHandle`。"""
        ...

    def send_message(self, handle: SessionHandle, message: str) -> SessionStream:
        """发一条消息。**边产边给,不是攒完再给。**

        逐条 yield 归一化事件,结束时把这一轮的账放进 `SessionStream.turn`。token 流本身
        就是交付物——攒完再一次性回放的话,用户看到的是"卡住很久然后突然全出来",而对话的
        可用性有一半在于看得见它在动。
        """
        ...


__all__ = [
    "AgentRuntime",
    "FailureKind",
    "JobResult",
    "JobSpec",
    "SessionCapableRuntime",
    "SessionHandle",
    "SessionSpec",
    "SessionStream",
    "SessionTurn",
]
