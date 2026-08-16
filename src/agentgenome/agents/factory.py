"""按根配置搭出运行时注册表。

## 为什么住在这里而不是命令行层

早先这段逻辑住在 `cli.py` 里、失败时直接 `typer.Exit`,于是**服务端用不了它**——而服务端
同样需要"把配置变成运行时"。结果是控制面手里攥着一个永远为空的注册表,任何创建会话的请求
都以「运行时 X 没有注册」告终,且这条路在测试里从没被走过(会话 e2e 直接把替身塞进应用状态,
跳过了装配)。见 PRD 29。

所以这里**不依赖任何 CLI 框架**:失败抛 `RuntimeAssemblyError`,由命令行侧自己转成退出码。
两个调用方(命令行、控制面)因此共用同一条装配路径,"配了哪些运行时"不会有两个答案。
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from functools import partial
from pathlib import Path

from agentgenome.agents.agentteams import AgentTeamsRuntime, AgentTeamsTransport
from agentgenome.agents.agentteams.directory import (
    FixedDirectory,
    ProvisionedDirectory,
    WorkerDirectory,
)
from agentgenome.agents.agentteams.http import HttpAgentTeamsTransport
from agentgenome.agents.agentteams.matrix_minio import MatrixMinioTransport
from agentgenome.agents.agentteams.mirror import MinioMirror
from agentgenome.agents.agentteams.provision import (
    HttpWorkerProvisioner,
    ReconcileOutcome,
    tiers_from,
)
from agentgenome.agents.agentteams.recording import RecordingTransport
from agentgenome.agents.agentteams.records import record_provision
from agentgenome.agents.capabilities import capabilities_of
from agentgenome.agents.claude_code import ClaudeCodeRuntime
from agentgenome.agents.human import HUMAN, HumanRuntime
from agentgenome.agents.qwen_code import QwenCodeRuntime
from agentgenome.agents.recording import RecordingLibrary
from agentgenome.agents.replay import ReplayRuntime
from agentgenome.agents.runtime import AgentRuntime
from agentgenome.agents.subprocess_runtime import RECORD_MODE_ENV
from agentgenome.config import DEFAULT_MAX_TURNS, Config, RuntimeEntry
from agentgenome.core.events import ORCHESTRATOR
from agentgenome.employees import EmployeeConfig, load_employees, workspace_employees_root

#: 回放库的位置只由环境变量给。**生产代码里不该有指向 tests/ 的默认值。**
RECORDINGS_ENV = "AGENTGENOME_RECORDINGS"


class RuntimeAssemblyError(RuntimeError):
    """根配置搭不出运行时注册表。命令行侧把它转成退出码,控制面转成 HTTP 错误。"""


#: 运行时名字到构造函数的映射。**按名字分派,不猜 `cmd` 字符串里有没有 "claude"。**
#: 猜的话,一个正常写着 `qwen-code: {cmd: qwen}` 的配置会在这里被悄悄换成
#: `ClaudeCodeRuntime`——名字对得上、实现却是另一家的,表现为"配置了 qwen-code
#: 却始终在跑 claude",而且没有任何报错或日志能指向这里。
#:
#: 值是构造函数而不是类本身:两个运行时的 `__init__` 签名不同(各自的 `command`/
#: `max_turns` 是子类自己声明的,基类 `SubprocessRuntime.__init__` 只认 `argv`),
#: 存成 `type[SubprocessRuntime]` 的话按基类签名调用会在 mypy 严格模式下报错。
def _cli_cmd(entry: RuntimeEntry) -> str:
    """本地 CLI 条目的 cmd。配置校验层已保证非空——这里只是把保证翻译给类型系统。"""
    assert entry.cmd is not None
    return entry.cmd


def _required_token(env_name: str, what: str) -> str:
    """从环境变量取一个令牌。缺失在装配层报错——不等预检才发现"token 是空的"。"""
    token = os.environ.get(env_name)
    if not token:
        raise RuntimeAssemblyError(
            f"agentteams 的{what}缺失: 环境变量 {env_name} 未设置或为空。"
            "检查根配置 runtime.agentteams 与部署环境。"
        )
    return token


def _workspace_employee(root: Path, employee_id: str) -> EmployeeConfig:
    """自动供应使用最新的员工定义,不把装配时的一份快照永久塞进运行时。"""
    return load_employees(workspace_employees_root(root)).get(employee_id)


def _record_auto_provision(
    root: Path, employee_id: str, outcome: ReconcileOutcome
) -> None:
    record_provision(
        root,
        actor=ORCHESTRATOR,
        employee_id=employee_id,
        ref=outcome.ref,
        action=outcome.action,
        entrance="auto",
    )


def _build_directory(
    entry: RuntimeEntry, endpoint: str, token: str, root: Path | None
) -> WorkerDirectory:
    """员工 → Worker/房间 怎么解析。

    配了全局 `worker` / `matrix_room` 的**既有部署**走固定兜底,行为逐字节不变;
    没配则按员工向平台解析(角色隔离在容器一侧因此才成立)。两者不能同时生效——
    配置校验层已经拦下矛盾组合。
    """
    if entry.worker and entry.matrix_room:
        return FixedDirectory(worker=entry.worker, room_id=entry.matrix_room)
    return ProvisionedDirectory(
        HttpWorkerProvisioner(endpoint=endpoint, token=token, tiers=tiers_from(entry)),
        employee_provider=None if root is None else partial(_workspace_employee, root),
        on_reconciled=None if root is None else partial(_record_auto_provision, root),
    )


def _build_agentteams(entry: RuntimeEntry, root: Path | None) -> AgentRuntime:
    """装配 agentteams 运行时。按 `transport` 分流,令牌只由环境变量给。"""
    assert entry.endpoint is not None and entry.consumer_token_env is not None
    token = _required_token(entry.consumer_token_env, "消费 token")
    transport: AgentTeamsTransport
    if entry.transport == "matrix-minio":
        # 配置校验层已保证这些字段非空——assert 只是把保证翻译给类型系统。
        assert (
            entry.matrix_homeserver is not None
            and entry.matrix_token_env is not None
            and entry.storage_prefix is not None
        )
        transport = MatrixMinioTransport(
            controller_endpoint=entry.endpoint,
            controller_token=token,
            matrix_homeserver=entry.matrix_homeserver,
            matrix_token=_required_token(entry.matrix_token_env, "Matrix 令牌"),
            directory=_build_directory(entry, endpoint=entry.endpoint, token=token, root=root),
            # shlex 拆分:mc_cmd 可以是 "python /path/mc代理.py" 这类带参形式,
            # 只取第一个词会把参数当可执行文件名弄丢。
            mirror=MinioMirror(mc_argv=shlex.split(entry.mc_cmd), prefix=entry.storage_prefix),
        )
    else:
        transport = HttpAgentTeamsTransport(endpoint=entry.endpoint, token=token)
    # 录制模式与子进程录制同一开关:开着就把真通道包一层落盘。不开时不包——
    # 行为零变化。
    recordings = os.environ.get(RECORDINGS_ENV)
    if os.environ.get(RECORD_MODE_ENV) and recordings:
        transport = RecordingTransport(transport, Path(recordings))
    return AgentTeamsRuntime(transport)


#: 名字 → 构造函数。第二个参数是 Workspace 根:**只有 human 用得上它**(待办要落库),
#: 但签名统一比让一个工厂"有时多一个参数"清楚——后者迟早出现调用方漏传的那一次。
_RUNTIME_FACTORIES: dict[str, Callable[[RuntimeEntry, Path | None], AgentRuntime]] = {
    "claude-code": lambda entry, _root: ClaudeCodeRuntime(
        command=_cli_cmd(entry), max_turns=entry.max_turns
    ),
    "qwen-code": lambda entry, _root: QwenCodeRuntime(
        command=_cli_cmd(entry), max_turns=entry.max_turns
    ),
    "agentteams": _build_agentteams,
    HUMAN: lambda _entry, root: HumanRuntime(_human_root(root)),
}


def _human_root(root: Path | None) -> Path:
    """human 运行时要知道 Workspace 在哪——待办是落库的,不是内存里的。

    没给根就报错,不猜当前目录:猜错的后果是待办投进了另一个工作区的库,而"我的待办不见了"
    这个症状指向的地方与真实原因隔着好几层。
    """
    if root is None:
        raise RuntimeAssemblyError(
            "配了 human 运行时,但装配时没有给 Workspace 根——待办要落在它的库里。"
        )
    return root


def build_runtimes(config: Config, root: Path | None = None) -> dict[str, AgentRuntime]:
    """按根配置搭出运行时注册表。

    只注册配置里出现过的名字,且**按名字选实现**。请求一个没注册的运行时会由池报错,
    而不是悄悄换一个跑起来——那会让配置错误表现为"最近它好像变笨了"这种极难归因的现象。
    """
    runtimes: dict[str, AgentRuntime] = {}
    for name, entry in config.runtime.runtimes.items():
        factory = _RUNTIME_FACTORIES.get(name)
        if factory is None:
            raise RuntimeAssemblyError(
                f"根配置里的运行时 {name!r} 没有对应实现(已知: "
                f"{', '.join(sorted(_RUNTIME_FACTORIES))})。检查是不是拼错了名字。"
            )
        runtimes[name] = factory(entry, root)

    recordings = os.environ.get(RECORDINGS_ENV)
    if recordings:
        runtimes["replay"] = ReplayRuntime(RecordingLibrary(Path(recordings)), name="replay")

    if config.runtime.default not in runtimes:
        if config.runtime.default_is_explicit:
            raise RuntimeAssemblyError(
                f"根配置显式声明了默认运行时 {config.runtime.default!r},"
                "但既没有它的 runtimes 条目,也不是已配置回放库时的 replay。"
            )
        # 用内置默认值(没人显式声明 default)时,给一个能跑的 claude-code 兜底——
        # 单机上手不该被迫先写一份完整配置。
        runtimes[config.runtime.default] = ClaudeCodeRuntime(max_turns=DEFAULT_MAX_TURNS)
    return runtimes


def build_session_runtimes(config: Config, root: Path | None = None) -> dict[str, AgentRuntime]:
    """只留开得了会话的那些。

    **过滤在这里做一次,会话服务就不必再判一遍。** 能力矩阵已经声明了每个运行时支不支持
    会话(`qwen-code` 没有 `--resume` 的等价物),让两处各判一次的话,迟早有一处漏掉。

    过滤掉之后,"这个名字压根没配"与"配了但开不了会话"在注册表里长得一样——所以调用方
    要靠 `why_no_session` 把两者分开,见那个函数的文档。
    """
    return {
        name: runtime
        for name, runtime in build_runtimes(config, root).items()
        if _supports_sessions(name)
    }


def why_no_session(config: Config, name: str) -> str:
    """这个运行时为什么开不了会话。**两种成因必须给两句话。**

    早先两者共用一句「运行时 X 没有注册」,而它们指向完全不同的下一步:一个是运维要去改
    根配置,另一个是用户要换一个员工。合成一句的代价是用户按提示去改了不该改的地方——
    改完当然没用,而错误信息一个字都没变。
    """
    if name not in config.runtime.runtimes and name != config.runtime.default:
        return f"运行时 {name} 没有配置。检查根配置里的 runtime 段。"
    if not _supports_sessions(name):
        return f"运行时 {name} 开不了会话。换一个用支持会话的运行时的员工。"
    return f"运行时 {name} 没有配置。检查根配置里的 runtime 段。"


def _supports_sessions(name: str) -> bool:
    """不认识的运行时**当作开不了会话**。

    与 `capabilities_of` 那条"不认识的返回 None,不造默认值"同一个判断:替一个我们一无所知
    的运行时打包票说它能开会话,而它开不了的话,失败会推迟到用户已经在等回答的那一刻。
    """
    profile = capabilities_of(name)
    return profile is not None and profile.sessions


__all__ = [
    "RECORDINGS_ENV",
    "RuntimeAssemblyError",
    "build_runtimes",
    "build_session_runtimes",
    "why_no_session",
]
