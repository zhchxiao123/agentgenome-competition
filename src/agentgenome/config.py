"""根配置 agentgenome.yaml。

部署参数住在这里(并发、预算、审批人、平台)。注意它与基因组规则的分工:
规则是项目资产、跟代码一起演进;根配置是这一次部署怎么跑。两者冲突时规则优先
——见 `agentgenome.genome.rules.effective_max_fix_rounds`。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentgenome import paths
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.loader import parse_yaml_model, read_yaml

_STRICT = ConfigDict(extra="forbid")

GitHost = Literal["github", "gitlab", "gitea", "local"]

DEFAULT_RUNTIME = "claude-code"
#: 新项目先保证员工能把工作做完，再由管理员按真实用量收紧。各单位分开命名，避免
#: 后续只想调整 token 却连带把超时秒数或轮次一起改掉。
INITIAL_TOKEN_LIMIT = 100_000_000
INITIAL_TURN_LIMIT = 100_000_000
INITIAL_TIMEOUT_SECONDS = 100_000_000
INITIAL_ROUND_LIMIT = 100_000_000
DEFAULT_MAX_TURNS = 40


#: 平台型运行时的名字。它的条目形状与本地 CLI 不同,校验按名字分流。
AGENTTEAMS_RUNTIME = "agentteams"

#: 人。**它既不是本地 CLI 也不是平台**:没有可执行命令,也没有入口地址——它的"进程"是
#: 一个人打开工作台。所以它的条目是空的,而校验必须知道这一点,否则"配了 human 却缺 cmd"
#: 会在装配层拦下一个完全正确的配置。
HUMAN_RUNTIME = "human"


class ModelTierEntry(BaseModel):
    """一个模型档位在这次部署下的具体取值。提供商可选(单提供商部署不必填)。"""

    model_config = _STRICT

    model: str
    provider: str = ""


class RuntimeEntry(BaseModel):
    """一个运行时的启动参数。

    两种形状共用一个模型:本地 CLI 条目要 `cmd`,agentteams 平台条目要
    `endpoint` + `consumer_token_env`。哪个名字该长哪种形状由 `RuntimeConfig`
    校验——写错形状在配置装配层报错,不是任务跑一半才炸。
    """

    model_config = _STRICT

    cmd: str | None = None
    max_turns: int = DEFAULT_MAX_TURNS
    #: AgentTeams 平台入口(controller)。
    endpoint: str | None = None
    #: 持平台消费 token 的**环境变量名**。token 本体不进配置文件——
    #: 配置会进版本库,而凭证不该进版本库。
    consumer_token_env: str | None = None
    #: agentteams 走哪条通道:`http`(假设的任务桥)或 `matrix-minio`
    #: (平台真实的文件 + 消息约定,见 source-analysis)。缺省 http,零改动向后兼容。
    transport: str = "http"
    #: matrix-minio 专用:Matrix 入口、Worker 房间、令牌环境变量、
    #: 存储前缀(mc 别名形式)、Worker 名与 mc 命令。
    matrix_homeserver: str | None = None
    matrix_room: str | None = None
    matrix_token_env: str | None = None
    storage_prefix: str | None = None
    worker: str | None = None
    mc_cmd: str = "mc"
    #: 模型档位 → 具体模型与提供商。**员工定义里只写抽象档位**,具体取值由部署给——
    #: 与"运行时私有的工具名不许泄漏进员工定义"同一条理由:泄漏之后换一个部署就要
    #: 改每一份员工资产。不配则不下发模型字段,平台用它自己的默认。
    model_tiers: dict[str, ModelTierEntry] = Field(default_factory=dict)


class RuntimeConfig(BaseModel):
    """默认运行时 + 各运行时的启动参数。"""

    model_config = ConfigDict(extra="allow")

    default: str = DEFAULT_RUNTIME
    runtimes: dict[str, RuntimeEntry] = Field(default_factory=dict)
    #: 使用者是否显式写了 default。显式写的必须有对应条目;用内置默认值的不强求。
    default_is_explicit: bool = False

    @model_validator(mode="before")
    @classmethod
    def _collect_runtime_entries(cls, value: object) -> object:
        """把 `claude-code: {...}` 这类平铺的运行时条目收进 runtimes。

        配置文件里运行时是按名字平铺的(可读性好),模型里收成一个映射(好消费)。
        """
        if not isinstance(value, dict):
            return value
        reserved = {"default", "runtimes", "default_is_explicit"}
        entries = dict(value.get("runtimes") or {})
        for key, item in value.items():
            if key not in reserved:
                entries[key] = item
        return {
            "default": value.get("default", DEFAULT_RUNTIME),
            "default_is_explicit": "default" in value,
            "runtimes": entries,
        }

    @model_validator(mode="after")
    def _entries_match_their_kind(self) -> RuntimeConfig:
        """按名字校验条目形状。

        `cmd` 因平台条目而变为可选,但既有 CLI 条目的校验不能因此放松——漏配
        `cmd` 仍要在装配层报错;反过来,给 agentteams 写 `cmd` 说明使用者把它
        当成了本地 CLI,同样当场纠正。
        """
        for name, entry in self.runtimes.items():
            if name == HUMAN_RUNTIME:
                self._check_human_entry(name, entry)
            elif name == AGENTTEAMS_RUNTIME:
                self._check_agentteams_entry(name, entry)
            else:
                if entry.cmd is None:
                    raise ValueError(f"runtime.{name} 缺 cmd(本地 CLI 的可执行命令)。")
                foreign = (
                    entry.endpoint
                    or entry.consumer_token_env
                    or entry.transport != "http"
                    or entry.matrix_homeserver
                    or entry.matrix_room
                    or entry.matrix_token_env
                    or entry.storage_prefix
                    or entry.worker
                    or entry.model_tiers
                )
                if foreign:
                    raise ValueError(
                        f"runtime.{name} 是本地 CLI 条目,平台字段(endpoint/transport/"
                        f"matrix_*/storage_prefix/worker)只属于 {AGENTTEAMS_RUNTIME}。"
                    )
        return self

    @staticmethod
    def _check_human_entry(name: str, entry: RuntimeEntry) -> None:
        """人的条目是空的。写了 cmd 或平台字段说明使用者把它当成了别的东西。"""
        wrong = entry.cmd or entry.endpoint or entry.consumer_token_env
        if wrong:
            raise ValueError(
                f"runtime.{name} 是人,不接受 cmd 或平台字段——它的执行者是打开工作台的那个人。"
            )

    @staticmethod
    def _check_agentteams_entry(name: str, entry: RuntimeEntry) -> None:
        if entry.cmd is not None:
            raise ValueError(f"runtime.{name} 不是本地 CLI,不接受 cmd——它经平台入口派发。")
        if not entry.endpoint:
            raise ValueError(f"runtime.{name} 缺 endpoint(AgentTeams 平台入口)。")
        if not entry.consumer_token_env:
            raise ValueError(
                f"runtime.{name} 缺 consumer_token_env(持消费 token 的环境变量名)。"
            )
        #: 走 matrix-minio 必给的。**worker/matrix_room 不在里面**——它们是可选的
        #: 固定路由兜底,见下。
        matrix_fields = {
            "matrix_homeserver": entry.matrix_homeserver,
            "matrix_token_env": entry.matrix_token_env,
            "storage_prefix": entry.storage_prefix,
        }
        routing_fields = {"matrix_room": entry.matrix_room, "worker": entry.worker}
        if entry.transport == "matrix-minio":
            missing = [field for field, value in matrix_fields.items() if not value]
            if missing:
                raise ValueError(
                    f"runtime.{name} 的 matrix-minio 传输缺字段: {', '.join(missing)}。"
                )
            # 固定路由是**既有部署的兜底**:两个都给才成立,都不给则按员工解析。
            # 只给一半是配了一半——静默按另一半的缺省走,会让"为什么没发到那个房间"
            # 要查到三层之外的默认值。
            given = [field for field, value in routing_fields.items() if value]
            if len(given) == 1:
                raise ValueError(
                    f"runtime.{name} 只给了 {given[0]}:固定路由要 worker 与 matrix_room "
                    "成对给出;都不给则按员工解析(推荐,每个员工用自己的 Worker)。"
                )
        elif entry.transport == "http":
            written = [
                field for field, value in {**matrix_fields, **routing_fields}.items() if value
            ]
            if written:
                # 写了 matrix 字段却留在 http 传输,说明使用者只改了一半——
                # 静默忽略的话,"为什么没走 matrix"要查到三层之外的默认值。
                raise ValueError(
                    f"runtime.{name} 写了 {', '.join(written)} 却是 transport: http。"
                    "要走平台真实约定,写 transport: matrix-minio。"
                )
        else:
            raise ValueError(
                f"runtime.{name} 的 transport 不认识: {entry.transport!r}"
                "(可选: http, matrix-minio)。"
            )


class ConcurrencyConfig(BaseModel):
    model_config = _STRICT

    global_jobs: int = Field(default=3, gt=0)


class CritiqueConfig(BaseModel):
    """状态内精化环什么时候开。

    **缺省不开。** 两轮 LLM 批判的钱要花在批判有期望收益的地方——一个默认开启的增值步骤,
    会让所有人在还没看到收益之前先看到账单。
    """

    model_config = _STRICT

    enabled: bool = False
    #: 计划命中受保护路径就进环。受保护路径是项目自己声明"这里出事代价大"的地方。
    on_protected: bool = True
    #: 计划命中的模块数达到这个数就进环。**规模的代理指标**:diff 在决定进不进环的那一刻
    #: 还不存在(它由环里的第一个节点产出),所以规模只能用计划期就有的事实来估。
    min_modules: int = 2
    #: 上一轮开发碰过的文件数达到这个数就进环。只有修复轮次拿得到,首轮走上面两条。
    min_changed_files: int = 5
    max_rounds: int = 2
    #: 整个环占任务预算的份额。**归因不是封顶**:轮次上限管得住次数,管不住单轮的花费。
    budget_share: float = 0.3


class HumanConfig(BaseModel):
    """派给人的活等多久、等不到怎么办。

    **窗口以工作日计,不以秒计。** 人的待办超时是常态而不是事故——三天没动多半是这个人在
    休假。所以到期不是一步到位地升级人工(那是终态,接不回来),而是提醒 → 改派 → 升级。
    """

    model_config = _STRICT

    #: 多久之后提醒一次。
    reminder_after_days: float = 1.0
    #: 多久之后换个人。**它必须大于提醒窗口**,否则提醒永远来不及发。
    reassign_after_days: float = 3.0
    #: 最多改派几次。用完才升级人工。
    max_reassignments: int = 1
    #: 接手候选,按顺序试。**空表示不改派**——直接升级人工:与其把待办转给一个根本不知道
    #: 这件事的人,不如让它出现在异常队列里被真正处理。
    backups: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _windows_must_be_ordered(self) -> HumanConfig:
        if 0 < self.reassign_after_days <= self.reminder_after_days:
            raise ValueError(
                "human.reassign_after_days 必须大于 reminder_after_days,"
                "否则提醒还没发就已经改派了。"
            )
        return self


class AssistedConfig(BaseModel):
    """哪些员工的产出要人确认一次。

    **信任爬坡的中间态**:客户从 manual 起步建立信任,逐步放权到 assisted 再到 auto——
    同一套流程、同一套记录、同一个审计面。
    """

    model_config = _STRICT

    #: 这些员工的产出需要确认。空表示全自动(今天的样子)。
    employees: list[str] = Field(default_factory=list)
    #: 确认交给谁。空表示交给那个员工自己的指派人(它是 human 员工时才有)。
    confirmer: str = ""


class Attempt(BaseModel):
    """一路变体:取向进上下文包,键进产物编址与事件面。"""

    model_config = _STRICT

    key: str
    hint: str = ""


class BestOfNConfig(BaseModel):
    """进化模式:N 路变异 + 门禁即适应度 + 裁决择优。

    **永远显式 opt-in**(任务级把模板点成 `best-of-n`)。N 倍成本必须是一个被看见的决定
    ——自动启用等于让系统替人决定花几倍的钱,而这类任务恰恰是错误成本最高的那批。
    """

    model_config = _STRICT

    #: 取向。缺省三路:最小改动 / 性能优先 / 契约先行。
    attempts: list[Attempt] = Field(
        default_factory=lambda: [
            Attempt(key="minimal", hint="最小改动:能不动的地方都别动"),
            Attempt(key="perf", hint="性能优先:热路径上的开销要说得出数量级"),
            Attempt(key="contract", hint="契约先行:先把接口与数据形状定清楚再写实现"),
        ]
    )
    #: 裁决交给谁。缺省是评审员工——它本来就是"只读 + 只批"的那个角色。
    judge_employee: str = "reviewer-employee"
    judge_procedure: str = "code-critique"
    #: 上限。**一代封顶,不做种群迭代**:多代实验留给研究性分支,生产路径一代就够。
    max_attempts: int = 5


class TopologyConfig(BaseModel):
    """状态内部跑哪张图。

    **模板是少数内置,不是工作流语言。** 这里只选一个名字,不接受图的定义——用户自定义
    图灵完备的图,我们会用两年时间重新发明 BPMN。真正的护栏另有三条:节点只能引用已注册
    的工序与员工、图必须过机器可判定的校验、图实例全量进事件面。
    """

    model_config = _STRICT

    #: 缺省模板。`single` 是现状那条路,逐字节不变。
    default: str = "single"
    critique: CritiqueConfig = Field(default_factory=CritiqueConfig)
    assisted: AssistedConfig = Field(default_factory=AssistedConfig)
    best_of_n: BestOfNConfig = Field(default_factory=BestOfNConfig)


class TesterMode(StrEnum):
    """测试由谁写。

    **缺省是 `dev`,也就是今天的样子。** 专职分工是一次真实的成本增加(多一个 Job),
    所以它必须是被看见的决定,不是升级上来就自动生效的东西。
    """

    #: 开发员工顺手写。test-first 只是手艺约束,不是机制约束。
    DEV = "dev"
    #: 测试员工专职出题,写集分离由越权机制双向执行。
    DEDICATED = "dedicated"
    #: 计划命中受保护路径时按专职,否则同 `dev`。
    RISK_BASED = "risk-based"


class AdversaryMode(StrEnum):
    """对抗 QA 什么时候上场。缺省不上。"""

    OFF = "off"
    #: 计划命中受保护路径时上。
    PROTECTED_HIT = "protected-hit"
    ALWAYS = "always"


class QualityLineConfig(BaseModel):
    """质量线拧多紧。

    **只放新旋钮。** 评审员工的策略住在 `topology.critique`,不搬过来:搬家是一次破坏性
    配置迁移(配置模型拒绝未知键,存量文件会直接加载失败),而收益只是"三个旋钮排在一起
    好看"。要一起看的是**界面**的事,不是配置层的事。

    **也只放"拧到哪一档",不放"哪些路径算测试"**:后者是一个已经有主人的事实(测试员工的
    写集),在这里复制一份就等于给同一个问题准备两个答案。
    """

    model_config = _STRICT

    tester: TesterMode = TesterMode.DEV
    adversary: AdversaryMode = AdversaryMode.OFF

    # **这里没有"哪些路径算测试"这个旋钮。** 那个答案住在 `tester-employee.yaml` 的写集里:
    # 开发员工"不许写测试"的禁令从那份定义推出来,于是两侧永远一致。在这里再开一个的话,
    # 改了配置没改定义时,两者不重合的那一段是一块谁都写不了的死区。


class BudgetConfig(BaseModel):
    model_config = _STRICT

    #: 关闭时仍记录 token 用量、仍按运行时上下文窗口裁剪输入，但不因业务预算中止执行。
    enforce: bool = False
    per_task_tokens: int = Field(default=1_500_000, gt=0)
    per_job_tokens: int = Field(default=300_000, gt=0)
    #: 一次对话会话的 token 上限。**0 = 不限,而且这是模型缺省。** 新项目模板会显式
    #: 写一个很大的有限值，便于设置页展示和后续按真实用量收紧。
    #:
    #: 与上面两项不同,这一项允许 0——对话全程有人在看,烧得不对劲的那一刻
    #: 人就会停下来,系统替他猜一个数只会在他正问到一半时
    #: 把会话挂起。历史上猜过 20k 与 50k 两次都猜错了:只读会话搬到项目根之后,实测一轮
    #: 就能烧掉 370k。
    #:
    #: 这一项对**之后新建的会话**生效;已经开着的会话带着自己创建时的那份
    #: 快照,恢复时会按当前值重取(见 `SessionService.resume`)——否则改了配置也救不回
    #: 那个正卡着的会话,而那恰恰是人来改这一项的原因。
    session_tokens: int = Field(default=0, ge=0)


class LimitConfig(BaseModel):
    model_config = _STRICT

    max_fix_rounds: int = Field(default=3, gt=0)
    job_timeout_s: int = Field(default=1800, gt=0)
    #: 一个任务最多能扩权到几个计划外的模块。**与修复轮次分开计数**:两者管的是不同的上限,
    #: 混用会让其中一条静默失效。定得小是有意的——扩权变成常规操作的话,真正的问题在计划
    #: 阶段而不在权限,而这个上限正是让那件事浮出来的东西。
    max_scope_grants: int = Field(default=2, gt=0)
    #: 一次拆分最多拆出几个子需求(PRD 48 D9)。上限防"拆出一百个碎片":审批与集成的
    #: 开销随子需求数线性长,碎到一定程度拆分本身就是浪费。**只能收紧,不能放宽**:
    #: 2..12 是产物 schema 里的硬界限(版本化资产),配置调大于 12 会在这里被拒——
    #: 静默被 schema 压回的话,"我明明调大了怎么没生效"查不出来。
    split_max_children: int = Field(default=12, ge=2, le=12)
    #: 需求树最深几层(母→子→孙 = 2)。子需求的解析仍可再提拆分,树深由 `parent_id` 链
    #: 算出来;触顶的提案按解析失败处理——环与深度都是"没把需求读明白"。
    split_max_depth: int = Field(default=2, gt=0)


class ItestConfig(BaseModel):
    """集成测试怎么跑。

    与 `limits` 分开是因为这几样是**同一件事的不同侧面**,散在两处的话"集成测试该怎么
    配"没有一个能一眼看全的地方。超时也放这里而不是 `limits.job_timeout_s`:集成测试要
    拉容器、建镜像,天然比一次 Agent 调用慢一个量级,共用一个值会逼着人把两者都调大。
    """

    model_config = _STRICT

    #: 编排文件相对 Workspace 根的位置。
    compose_file: str = "itest/compose.yaml"
    #: 环境健康之后、跑用例之前灌测试数据的命令。空表示这个项目不需要灌。
    #: **在哪个服务里跑**由 `seed_service` 说;它是环境的一部分,不是某个模块的用例。
    seed_cmd: str = ""
    seed_service: str = ""
    #: 每条失败带多少行日志尾部。整份日志会挤爆上下文包。
    log_tail_lines: int = Field(default=60, gt=0)
    timeout_s: int = Field(default=1800, gt=0)

    @model_validator(mode="after")
    def _seed_needs_a_service(self) -> ItestConfig:
        """声明了灌数据命令却没说在哪跑,是配了一半——静默不灌的话"每次初始状态一致"
        这条承诺会在没有任何症状的情况下失效。"""
        if self.seed_cmd and not self.seed_service:
            raise ValueError("itest.seed_cmd 需要同时给出 itest.seed_service")
        return self


class EvidenceConfig(BaseModel):
    """证据留多久、留在哪。

    与 `limits` 分开的理由跟 `itest` 一样:归档落点与日志保留期是**同一件事的两个侧面**
    ——"一个任务跑完之后,它的证据会怎样"。散在两处的话,这个问题没有一个能一眼看全的
    地方,而它恰恰是出事之后第一个被问到的问题。
    """

    model_config = _STRICT

    #: 审计包的归档根,相对 Workspace 或绝对路径。指到有备份的挂载点是它存在的理由。
    archive_dir: str = str(paths.ARCHIVE)
    #: 原始日志留多少天。**不要设太长**:逐行 JSON 的 Agent 输出流体积可观,而它的价值
    #: 随时间衰减得很快——一周之后几乎没人会去看逐条工具调用。真正兜底的是审计包。
    log_retention_days: int = Field(default=30, gt=0)


class KnowledgeConfig(BaseModel):
    """知识树的行数预算与碎片化阈值。

    **预算是门禁项,不是建议。** 分层解决了"一个文件装不下",没解决"某一层自己膨胀"——
    一个有两百个功能点的模块,它的模块地图会把单文件地图的全部问题重演一遍,只是换了个位置。

    **不要因为一次超限就把默认值调大。** 超限的正确响应通常是分形(子目录 + 子地图),
    把默认值调大等于取消这条门禁。
    """

    model_config = _STRICT

    #: 一次上下文里功能卡片正文的 token 上限。**只管卡片正文**——目录行不受它约束,
    #: 因为目录行是"知道有什么存在"的唯一载体,砍掉它等于让员工连去读都不知道读什么。
    card_context_tokens: int = Field(default=4_000, gt=0)
    root_index_lines: int = Field(default=150, gt=0)
    module_map_lines: int = Field(default=200, gt=0)
    card_lines: int = Field(default=300, gt=0)
    #: 同一条代码路径被多少张卡片覆盖算切碎了。**只提示不拦截**——切碎有时是对的,
    #: 但它应该是被注意到的。
    fragmentation_threshold: int = Field(default=3, gt=0)


class GenomeTaskConfig(BaseModel):
    """基因组任务怎么跑。

    与研发任务的 `concurrency` / `budgets` 分开:一次全量知识初始化会派发几十个并行深读
    作业,共用研发任务那个并发闸的话它会把整条研发流水线堵死——而堵住的那段时间里,系统
    看起来就是"不干活了"。
    """

    model_config = _STRICT

    #: 知识初始化的热区往回看多久。半年:更长会把早就重构掉的老热点算进来,更短则一次
    #: 版本迭代就能把结论带偏。
    hot_path_since_days: int = Field(default=180, gt=0)
    #: 基因组任务自己的并发上限。**不叫 global_jobs**:在一个按泳道分的配置段里,"global"
    #: 自相矛盾,而且会与 `concurrency.global_jobs` 读起来一模一样却是另一回事。
    #:
    #: **它现在管不到逐模块深读。** 那条路上所有模块作业共用一个任务 id,而池按任务 id 上
    #: 互斥锁(那把锁护着越权检查的基线与回滚,而这些作业写同一个 Workspace)——于是它们排队
    #: 跑,调大这个数字不会加速。写在这里是因为不写的话下一个人一定会去调它。
    #: 见 `genome.pipeline` 的模块说明。
    concurrent_jobs: int = Field(default=2, gt=0)
    #: 单个基因组任务的 token 上限。**一次接入不该吃掉整月额度**,所以它比研发任务的
    #: 单任务预算小一个量级——初始化确实贵,但它是一次性的,而研发任务是天天跑的。
    per_task_tokens: int = Field(default=400_000, gt=0)
    #: 闸门等多久开始提醒。**提醒不判死**——一个等人的健康任务不该因为人休假而失败。
    confirmation_reminder_hours: int = Field(default=24, gt=0)


class NotifyConfig(BaseModel):
    model_config = _STRICT

    webhook: str | None = None


class ApprovalConfig(BaseModel):
    model_config = _STRICT

    approvers: list[str] = Field(default_factory=list)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)


class PlatformConfig(BaseModel):
    model_config = _STRICT

    git_host: GitHost = "github"
    protected_branch: str = "main"


class UModelConfig(BaseModel):
    model_config = _STRICT

    enabled: bool = False
    gateway: str | None = None
    workspace: str | None = None


class Config(BaseModel):
    model_config = _STRICT

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    topology: TopologyConfig = Field(default_factory=TopologyConfig)
    human: HumanConfig = Field(default_factory=HumanConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    limits: LimitConfig = Field(default_factory=LimitConfig)
    itest: ItestConfig = Field(default_factory=ItestConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    genome_tasks: GenomeTaskConfig = Field(default_factory=GenomeTaskConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    umodel: UModelConfig = Field(default_factory=UModelConfig)
    quality_line: QualityLineConfig = Field(default_factory=QualityLineConfig)

    @model_validator(mode="after")
    def _default_runtime_must_be_configured(self) -> Config:
        entries = self.runtime.runtimes
        explicit = self.runtime.default_is_explicit
        if (explicit or entries) and self.runtime.default not in entries:
            raise ValueError(
                f"runtime.default 指向未配置的运行时: {self.runtime.default}"
                f"(已配置: {sorted(entries)})"
            )
        return self


def load_config(workspace_root: Path) -> Config:
    """加载并校验根配置。"""
    relative = str(paths.ROOT_CONFIG)
    payload = read_yaml(workspace_root / paths.ROOT_CONFIG, relative)
    return parse_yaml_model(Config, payload, relative)


def render_default_config() -> str:
    """`agctl init` 写出的根配置模板。"""
    return (
        "# AgentGenome 底座配置。字段说明见 docs/AgentGenome.md §13.3。\n"
        "runtime:\n"
        "  default: claude-code\n"
        f"  claude-code: {{cmd: claude, max_turns: {INITIAL_TURN_LIMIT}}}\n"
        "concurrency: {global_jobs: 3}\n"
        "# 状态内部跑哪张图。single = 一个状态一次派发,也就是现状那条路。\n"
        f"topology: {{default: single, critique: {{max_rounds: {INITIAL_ROUND_LIMIT}}}, "
        f"best_of_n: {{max_attempts: {INITIAL_ROUND_LIMIT}}}}}\n"
        f"budgets: {{enforce: false, per_task_tokens: {INITIAL_TOKEN_LIMIT}, "
        f"per_job_tokens: {INITIAL_TOKEN_LIMIT}, session_tokens: {INITIAL_TOKEN_LIMIT}}}\n"
        f"limits: {{max_fix_rounds: {INITIAL_ROUND_LIMIT}, "
        f"job_timeout_s: {INITIAL_TIMEOUT_SECONDS}}}\n"
        f"itest: {{timeout_s: {INITIAL_TIMEOUT_SECONDS}}}\n"
        f"genome_tasks: {{per_task_tokens: {INITIAL_TOKEN_LIMIT}}}\n"
        "knowledge:\n"
        "  {root_index_lines: 150, module_map_lines: 200, card_lines: 300,\n"
        "   fragmentation_threshold: 3}\n"
        f"evidence: {{archive_dir: {paths.ARCHIVE}, log_retention_days: 30}}\n"
        "approval:\n"
        "  approvers: []\n"
        "platform:\n"
        "  git_host: github\n"
        "  protected_branch: main\n"
        "umodel: {enabled: false}\n"
    )


__all__ = [
    "Config",
    "INITIAL_ROUND_LIMIT",
    "INITIAL_TIMEOUT_SECONDS",
    "INITIAL_TOKEN_LIMIT",
    "INITIAL_TURN_LIMIT",
    "GenomeValidationError",
    "ValidationIssue",
    "load_config",
    "render_default_config",
]
