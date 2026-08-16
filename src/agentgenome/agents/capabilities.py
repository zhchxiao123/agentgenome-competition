"""运行时能力矩阵:把某一个 CLI 的假设挤出抽象层。

## 为什么这是一份数据结构而不是文档里的表格

文档里的表格没人会去校验。写成数据之后,"这个运行时支持哪些工具"变成一个可以被断言、被查询、
被派发逻辑消费的东西——而不是一段可能已经过时的散文。

## 规范化能力名

抽象层用 `read_file` / `write_file` / `run_command` / `search` 这样的名字,各运行时映射到自己
的工具名。`Bash` / `Read` / `Write` / `Edit` / `Grep` / `Glob` 是 **Claude Code 的命名**,它们
泄漏到员工定义里之后,换一个运行时就要改每一份员工资产。

## 映射不到的能力必须显式报错

**不允许静默忽略。** 静默降级会让员工失去某项能力却没人知道——症状是"它这次怎么没去读那个
文件",而原因在三层之外的一张映射表里。宁可在派发前炸掉。

## 上下文窗口从这里来

`ContextAssembler` 的预算此前是根配置里的一个数。换一个窗口小一半的运行时,那个数不会跟着
变,表现是**静默截断**——员工看不到该看的东西,而且没有任何报错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agentgenome.config import HUMAN_RUNTIME


class Capability(StrEnum):
    """规范化的能力名。员工定义与 Procedure 声明都用这一套。"""

    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    EDIT_FILE = "edit_file"
    RUN_COMMAND = "run_command"
    SEARCH = "search"
    LIST_FILES = "list_files"
    FETCH_URL = "fetch_url"
    WEB_SEARCH = "web_search"


class ResultDelivery(StrEnum):
    """运行时把最终契约产物交回来的方式。"""

    STRUCTURED_RESPONSE = "structured_response"
    RESULT_FILE = "result_file"
    ARTIFACT = "artifact"


class UnsupportedCapability(LookupError):
    """这个运行时没有这项能力。

    **显式抛出而不是静默跳过。** 静默的话,一个声明要用 `search` 的 Procedure 会在一个不支持
    搜索的运行时上跑起来,然后产出一份"我找不到相关代码"的结果——看起来像是代码的问题。
    """


@dataclass(frozen=True)
class RuntimeCapabilities:
    """一个运行时能做什么、做到什么程度。"""

    name: str
    #: 规范化能力名 → 该运行时自己的工具名。
    tools: dict[Capability, str] = field(default_factory=dict)
    #: 上下文窗口,token。进 `ContextAssembler` 的预算计算。
    context_window: int = 200_000
    streaming: bool = True
    #: 能不能拿到 token 用量。拿不到时归一化事件里标 `unavailable`,**不填假数据**。
    usage_available: bool = True
    #: 对"最后写一份合契约的 result.json"这条指令的遵从度。0–1,选型依据之一。
    structured_output: float = 1.0
    #: 最终小票如何跨运行时边界。调度层按这个字段给交付指令，不按运行时名字猜。
    result_delivery: ResultDelivery = ResultDelivery.ARTIFACT
    #: 能否把只读 Job 的工具面强制收窄，而不只是把 allow-list 当提示词建议。
    enforces_read_only: bool = False
    #: 能不能开交互会话(有 `--resume` 的等价物)。
    #:
    #: **在 `preflight` 阶段暴露,不是等用户创建会话时才失败。** 不支持的运行时创建会话
    #: 直接报错而不是静默降级成一个永远不回话的会话。
    sessions: bool = False
    #: 这个运行时单个 Job 的 token 上限。`None` 表示按员工额度算(绝大多数运行时)。
    #:
    #: **它住在这里而不是派发路径里的一条 if。** 派发那边写 `if runtime == "human": 0` 的话,
    #: 下一个"不烧 token 的执行者"会再加一条同样的分支,而能力矩阵——这份被消费的数据——
    #: 对它一无所知。
    job_max_tokens: int | None = None
    #: 能不能原生渐进式加载技艺包(把 craft 物化到工作区就自己会用)。
    #:
    #: 不能的运行时**降级为把手艺摘要内联进上下文包**,而不是不给。手艺内容只写一份、
    #: 运行时无关——可插拔承诺不能因为多了手艺层就破掉。
    craft_mounting: bool = False

    def tool_for(self, capability: Capability) -> str:
        """这项能力在这个运行时叫什么。映射不到就炸。"""
        found = self.tools.get(capability)
        if found is None:
            known = ", ".join(sorted(item.value for item in self.tools)) or "(空)"
            raise UnsupportedCapability(
                f"{self.name} 不支持 {capability.value}(它支持: {known})。"
                "静默跳过会让员工失去这项能力却没人知道,所以这里直接拒绝。"
            )
        return found

    def capability_of(self, tool_name: str) -> Capability | None:
        """反查:这个工具名对应哪项能力。用来把老的员工定义翻译过来。"""
        for capability, name in self.tools.items():
            if name == tool_name:
                return capability
        return None

    def translate(self, capabilities: list[Capability]) -> list[str]:
        """把一组能力翻成这个运行时的工具名。任一映射不到即整体失败。"""
        return [self.tool_for(item) for item in capabilities]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tools": {key.value: value for key, value in self.tools.items()},
            "context_window": self.context_window,
            "streaming": self.streaming,
            "usage_available": self.usage_available,
            "structured_output": self.structured_output,
            "result_delivery": self.result_delivery.value,
            "enforces_read_only": self.enforces_read_only,
            "sessions": self.sessions,
            "craft_mounting": self.craft_mounting,
        }


CLAUDE_CODE = RuntimeCapabilities(
    name="claude-code",
    tools={
        Capability.READ_FILE: "Read",
        Capability.WRITE_FILE: "Write",
        Capability.EDIT_FILE: "Edit",
        Capability.RUN_COMMAND: "Bash",
        Capability.SEARCH: "Grep",
        Capability.LIST_FILES: "Glob",
        Capability.FETCH_URL: "WebFetch",
        Capability.WEB_SEARCH: "WebSearch",
    },
    context_window=200_000,
    result_delivery=ResultDelivery.STRUCTURED_RESPONSE,
    enforces_read_only=True,
    # 实测 claude 2.1.220:`--resume` 可用，无短期 TTL，但按 cwd 路径分桶。
    sessions=True,
    # `.claude/skills/` 是它自己的规范,物化进去就会渐进式加载。
    craft_mounting=True,
)

QWEN_CODE = RuntimeCapabilities(
    name="qwen-code",
    tools={
        Capability.READ_FILE: "read_file",
        Capability.WRITE_FILE: "write_file",
        Capability.EDIT_FILE: "edit",
        Capability.RUN_COMMAND: "run_shell_command",
        Capability.SEARCH: "grep_search",
        Capability.LIST_FILES: "glob",
    },
    context_window=128_000,
    # 拿不到逐次调用的 token 用量。**标出来而不是填 0**——填 0 会让预算闸以为还有额度。
    usage_available=False,
    # `--json-schema` 原生校验，适配器消费 stream-json 的 structured_result。
    structured_output=1.0,
    result_delivery=ResultDelivery.STRUCTURED_RESPONSE,
    enforces_read_only=True,
    # CLI 虽已有 resume，但本适配器尚未实现 AgentRuntime 的会话协议，暂不虚报支持。
    sessions=False,
    # 没有等价的技艺包机制,走内联摘要那条降级路径。
    craft_mounting=False,
)

AGENTTEAMS = RuntimeCapabilities(
    name="agentteams",
    # Worker 侧真正执行的是平台托管的 coding agent,工具名经平台抽象——
    # 所以映射就是规范化名本身,不翻译成任何一家 CLI 的私有命名。
    tools={capability: capability.value for capability in Capability},
    context_window=200_000,
    # 平台传的是消息不是 token 流。
    streaming=False,
    # 用量由平台网关按 Job 粒度给出,随应答返回;个别拿不到的 Job 在结果里
    # 标"不可得",不填 0。注意:**执行中的预算掐断做不到**——网关计量是事后的,
    # 单 Job 的 max_tokens 对这个运行时是软上限。
    usage_available=True,
    # 取决于 Worker 侧运行时,保守取值。
    structured_output=0.9,
    result_delivery=ResultDelivery.ARTIFACT,
    enforces_read_only=False,
    # Matrix 消息粒度承载不了逐 token 流式,而对话可用性的一半在于看得见它在动。
    # 会话不做,如实声明——见 PRD 31 Out of Scope。
    sessions=False,
    craft_mounting=False,
)

HUMAN = RuntimeCapabilities(
    name=HUMAN_RUNTIME,
    # 人什么都能干。**工具名映射成规范化名本身**:能力矩阵在这里回答的不是"他手里有哪个
    # 命令",而是"这份活能不能派给他"——答案永远是能,他手里是一台电脑。
    tools={capability: capability.value for capability in Capability},
    # 上下文包对人按"读得懂"裁,不按 token 裁。这个数只用来算切片预算,给一个宽的值,
    # 免得把该给人看的材料截掉。
    context_window=1_000_000,
    streaming=False,
    # **不可得,不是 0。** 人的工时没有 token 账;填 0 会让成本看板把它算成"免费",
    # 而"这件事没有账"与"这件事花了 0"是两句不同的话。
    usage_available=False,
    # 产物过与硅基员工完全相同的契约校验,而且不合格会被打回重交——所以遵从度是 1。
    structured_output=1.0,
    result_delivery=ResultDelivery.ARTIFACT,
    enforces_read_only=True,
    # 人一个 token 都不烧。这个 0 同时是执行池**预算预扣**的输入:沿用员工额度的话,
    # 一个预算吃紧的任务会在待办投出去之前就被判"装不下下一个 Job"并升级人工。
    job_max_tokens=0,
    # 人和人聊天不归系统管。
    sessions=False,
    # 给人物化一堆技艺目录是噪音:他要读的是上下文包与产物契约,不是方法论手册。
    craft_mounting=False,
)

#: 回放运行时。它重放的是某个真实运行时当初的输出,所以能力上对齐最宽的那个。
REPLAY = RuntimeCapabilities(
    name="replay",
    tools=dict(CLAUDE_CODE.tools),
    context_window=CLAUDE_CODE.context_window,
    result_delivery=ResultDelivery.ARTIFACT,
    enforces_read_only=True,
    sessions=CLAUDE_CODE.sessions,
    craft_mounting=CLAUDE_CODE.craft_mounting,
)

_MATRIX: dict[str, RuntimeCapabilities] = {
    CLAUDE_CODE.name: CLAUDE_CODE,
    QWEN_CODE.name: QWEN_CODE,
    AGENTTEAMS.name: AGENTTEAMS,
    HUMAN.name: HUMAN,
    REPLAY.name: REPLAY,
}


def capabilities_of(runtime_name: str) -> RuntimeCapabilities | None:
    """查一个运行时的能力。**不认识的返回 `None`,不造一个默认值。**

    造默认值等于替一个我们一无所知的运行时打包票——而打包票的那一项迟早是错的。
    """
    return _MATRIX.get(runtime_name)


def register(capabilities: RuntimeCapabilities) -> None:
    """把一个运行时的能力登记进来。接第三个运行时时用。"""
    _MATRIX[capabilities.name] = capabilities


def known_runtimes() -> list[str]:
    return sorted(_MATRIX)


def capability_of_tool(tool_name: str) -> Capability | None:
    """把规范化名或任一已登记运行时的旧工具名还原为统一能力。"""
    try:
        return Capability(tool_name)
    except ValueError:
        pass
    found: Capability | None = None
    for profile in _MATRIX.values():
        candidate = profile.capability_of(tool_name)
        if candidate is None:
            continue
        if found is not None and found is not candidate:
            raise UnsupportedCapability(
                f"工具名 {tool_name!r} 在能力矩阵里有歧义: {found.value} / {candidate.value}"
            )
        found = candidate
    return found


def context_window_of(runtime_name: str, fallback: int) -> int:
    """这个运行时的上下文窗口。不认识时用调用方给的兜底值。"""
    found = capabilities_of(runtime_name)
    return found.context_window if found else fallback


__all__ = [
    "AGENTTEAMS",
    "CLAUDE_CODE",
    "HUMAN",
    "QWEN_CODE",
    "REPLAY",
    "Capability",
    "ResultDelivery",
    "RuntimeCapabilities",
    "UnsupportedCapability",
    "capabilities_of",
    "capability_of_tool",
    "context_window_of",
    "known_runtimes",
    "register",
]
