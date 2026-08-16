"""REST 的请求与响应模型。

**全部显式定义,不用 `dict` 兜底。** `dict` 会让 OpenAPI 里的类型退化成 `object`,而生成的
TypeScript 客户端就此失去全部价值——那正是选 FastAPI 的唯一理由。多写几个类的成本,换的是
"接口一变前端编译期就报错"。
"""

from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from agentgenome.config import (
    ApprovalConfig,
    BudgetConfig,
    ConcurrencyConfig,
    Config,
    GenomeTaskConfig,
    ItestConfig,
    LimitConfig,
    QualityLineConfig,
    RuntimeConfig,
    TopologyConfig,
)
from agentgenome.core.task import ItestOverride, Task, TaskMode, TaskRunStatus
from agentgenome.core.transitions import visible_escalation_reason
from agentgenome.genome.rules import ArchitectureRules, ImpactRules, ProtectedRules
from agentgenome.jobs.handlers import can_advance

#: `of` 在子类上要返回子类。写死成基类的话,`TaskDetail.of` 的返回类型是 `TaskSummary`,
#: 而那正好丢掉了详情多出来的那个字段。
SelfT = TypeVar("SelfT", bound="TaskSummary")


class PendingTodo(BaseModel):
    """任务当前正在等的人工待办指针。

    任务状态只表达治理位置；同一个 CREATED 既可能还没启动，也可能已经产出拆分提案等人确认。
    前端不能从 ``state + can_run`` 反推这个区别，所以由服务端把事实直接带出来。
    """

    id: str
    kind: str
    assignee: str


class TaskSummary(BaseModel):
    """任务概览。列表与详情共用——两份的话字段迟早对不上。"""

    id: str
    title: str
    state: str
    priority: int
    fix_rounds: int
    #: 需求解析已经重试了几次。CREATED 自环后仍靠它区分首次启动与失败重试。
    plan_retries: int
    needs_itest: str
    itest_override: str
    #: 自主执行还是结对。前端据此决定要不要显示"开结对会话"入口。
    mode: str = "autonomous"
    #: 提交时选的执行拓扑。**空表示"跟随项目缺省"**,不是 `single`——展开的话,"没表态"
    #: 与"明确选了 single"在记录上会变成同一件事。
    topology: str = ""
    risk_level: str | None = None
    tokens_used: int
    budget_tokens: int | None = None
    branch: str | None = None
    escalate_reason: str | None = None
    #: 人工介入是否已了结。终态仍是 ESCALATED；前端用这两个字段区分待处理与历史记录。
    intervention_resolved_at: str | None = None
    intervention_successor_task_id: str | None = None
    #: 所属需求。**None 表示存量任务**(需求实体之前建的),前端据此不渲染反链——
    #: 渲染一个空链接等于假装它有需求。
    requirement_id: str | None = None
    created_at: str
    updated_at: str
    #: `POST /tasks/{id}/run` 现在点了有没有用。**服务端算,前端不重复猜状态机的规则**——
    #: 猜的话,状态机加一条新状态时这里要跟着改,漏改的后果只是按钮出现在不该出现的地方,
    #: 而且没有任何测试会红,因为前端自己那份"规则"测的是它自己编的规则。判断逻辑与
    #: `POST /tasks/{id}/run`(`server/app.py`)、`Orchestrator.advance` 共用
    #: `jobs.handlers.can_advance`——三处都在问同一个问题,答案不该有三份。
    can_run: bool = False
    #: 当前这一轮是否真的在后台执行。生命周期 state 不承担瞬时执行态。
    execution_status: TaskRunStatus = TaskRunStatus.IDLE
    #: 当前正在等哪张人工待办。None 表示没有待办；kind 区分拆分确认与人工执行。
    pending_todo: PendingTodo | None = None

    @classmethod
    def of(cls: type[SelfT], task: Task, *, execution_status: TaskRunStatus | None = None) -> SelfT:
        payload = task.as_dict()
        fields = {name: payload[name] for name in cls.model_fields if name in payload}
        fields["escalate_reason"] = visible_escalation_reason(task)
        fields["can_run"] = can_advance(task)
        fields["execution_status"] = execution_status or task.run_status
        return cls(**fields)


class RequirementSummary(BaseModel):
    """一个需求在列表里的样子。

    `state` 是**算出来的**(`core.requirement.derive_state`),这里只是把推导结果摆出来
    ——需求表没有 status 列,界面也就不可能读到一份与尝试链分叉的状态。
    """

    id: str
    title: str
    #: 当前文本。各次尝试的快照在任务上,不在这里。
    text: str
    priority: int
    state: str
    #: 搁置原因。空串表示没搁置。
    parked: str = ""
    #: 历次尝试的数量。链本身在详情里(43/02),列表只要个数。
    attempts: int = 0
    #: 母需求 id,空串 = 不在任何树上(PRD 48)。前端靠它把子需求收进母需求名下。
    parent_id: str = ""
    #: 本批兄弟里的前置(需求 id)。
    blocked_by: list[str] = []
    #: 子需求进度:x/N 已交付。没有 children 时都是 0——前端据此不渲染树区。
    children_total: int = 0
    children_delivered: int = 0
    created_at: str
    updated_at: str


class RequirementPatch(BaseModel):
    """改一个需求。全部字段可选,`None` 表示这一样不动。

    搁置与恢复是**两个字段**:用"空原因的搁置"表示恢复的话,一次手滑的空提交就会把
    搁置的需求悄悄放回队列。冲突与空原因由服务层拒绝——校验住在
    `core.requirement.revise`,CLI 与这里同一句报错。
    """

    text: str | None = None
    priority: int | None = Field(default=None, ge=0)
    #: 搁置原因。必须非空;`None` 表示不动这一项。
    park: str | None = None
    resume: bool = False


class AttemptView(BaseModel):
    """尝试链里的一环:一次研发任务在需求详情里的样子。"""

    id: str
    state: str
    #: 生命周期状态之外的瞬时执行态。任务刚启动时 state 仍可能是 CREATED；不把这项
    #: 带到需求尝试链，需求页就会在同一时刻说“CREATED”、任务页说“需求解析中”。
    execution_status: TaskRunStatus = TaskRunStatus.IDLE
    #: 那次尝试为什么停在等人接管。没升级过就是 None。
    escalate_reason: str | None = None
    tokens_used: int = 0
    created_at: str


class RequirementChildView(BaseModel):
    """子需求在母需求详情里的样子:树上一格,不是完整详情。"""

    id: str
    title: str
    state: str
    blocked_by: list[str] = []
    parked: str = ""
    #: 它名下有几次尝试。0 = 还没开工(排队或被前置挡着)。
    attempts: int = 0
    #: 最近一次尝试的任务态,空串 = 还没有尝试。树上"停在哪"就看它:推导状态说
    #: "排队中"时,这里的 ESCALATED 才是那个要人管的停点。
    last_attempt_state: str = ""


class RequirementDetail(RequirementSummary):
    """需求详情:概览 + 尝试链 + 总账。"""

    #: 历次尝试,按发起时间排——链就是这个顺序本身。
    chain: list[AttemptView] = []
    #: 历次尝试烧掉的 token 之和。**聚合已有的任务级账,不新建成本记录**。
    total_tokens: int = 0
    #: 子需求(按批内顺序)。空列表 = 平面需求,前端不渲染树区。
    children: list[RequirementChildView] = []
    #: 整棵树的账:自己 + 全部后代的历次尝试之和(PRD 48 D8)。平面需求时等于
    #: `total_tokens`——树级计量必须一眼可见,预算闸门按过渡政策后置。
    tree_tokens: int = 0


class ScopeGrantView(BaseModel):
    """一次被批准的扩权,给界面看的样子。"""

    module: str
    reason: str
    round: int


class TaskDetail(TaskSummary):
    """详情比概览多一份需求原文。列表里不带它——一屏几十条需求原文没人读得下去。

    `of` 从 `TaskSummary` 继承下来:它按 `model_fields` 取字段,子类多出来的那个自然被带上。
    各写一份的话,加字段时要改两处,而漏掉的那一处不会有任何症状。
    """

    requirement: str
    #: 这个任务**当前**授权的模块:计划命中的,加上中途批准扩到的。收窄之后"这个任务能写
    #: 哪儿"是算出来的,人算不清叠加结果——与 `agctl employee show --task` 同一个理由。
    effective_modules: list[str] = Field(default_factory=list)
    #: 中途扩到的模块。**没扩过就是空列表**,不是 "扩权 0 次" 这种要人自己过滤的噪声。
    #: 审批人尤其需要它——扩过权的任务必经他过目,而他面对的是一份可能横跨两个域的 diff;
    #: 不告诉他"原本只授权了订单域、中途申请加了库存域、理由是 X",他就得自己从 diff 里
    #: 把这件事重新推一遍,而那恰恰是最容易漏看的部分。
    #:
    #: **用具体模型而不是裸 dict**:裸 dict 在 OpenAPI 里是 `additionalProperties: true`,
    #: 于是这个字段改名不会让前端的生成类型炸掉——而 ADR-0001 的全部前提就是它该炸。
    scope_grants: list[ScopeGrantView] = Field(default_factory=list)


class TaskTraceStage(BaseModel):
    """一个 stage 的执行轨迹。**块的形状与对话工作台的 `BlockItem` 是同一份**——
    两处数据源头不同,归一化之后的样子不该有第二份,前端也因此能直接复用同一套渲染。
    """

    stage: str
    number: int
    blocks: list[BlockItem]


class TaskTrace(BaseModel):
    """一个任务从 CREATED 到现在,每个 stage 各自的执行轨迹,按分配顺序升序。"""

    task_id: str
    stages: list[TaskTraceStage]


class SubmitRequest(BaseModel):
    requirement: str = Field(min_length=1, description="需求原文")
    title: str = ""
    priority: int = Field(default=5, ge=0)
    budget_tokens: int | None = Field(default=None, gt=0)
    itest: ItestOverride = ItestOverride.AUTO
    #: 自主执行还是结对。**与 CLI 的 `--interactive` 同一件事**——两条路只走一套语义,
    #: 不然网页建的任务与命令行建的会有一个不带 mode。
    mode: TaskMode = TaskMode.AUTONOMOUS
    #: 这一步跑哪张图。**与 CLI 的 `--topology` 同一件事**,而且两条路共用同一个校验函数
    #: (`jobs.catalog.check_choice`)——各判一次的话,第一次改文案就会分叉。
    #:
    #: 空表示跟随项目缺省。**不在这里展开成具体模板名**:见 `TaskSummary.topology`。
    topology: str = ""
    #: 这个任务是从哪次咨询会话转过来的。进事件面,不进任务表——它是一次性的来源标注,
    #: 不是任务的状态。
    source_session_id: str | None = None
    #: 在哪个既有需求下发起新尝试(「再试一次」)。**None 表示新需求**:服务端先建需求
    #: 再建首次尝试。带上它时 `requirement` 文本作为这次尝试的快照,同时成为需求的最新
    #: 表述;不存在的 id 在提交那一刻被拒,与 CLI 同一句报错。
    requirement_id: str | None = None


class InterventionResolveRequest(BaseModel):
    """结束一张人工介入待办。备注可空，处理人只取认证身份。"""

    note: str = Field(default="", max_length=2000)


class InterventionRetryRequest(BaseModel):
    """修改当前需求并从一条升级任务创建后继尝试。"""

    requirement: str = Field(min_length=1, description="修改后的需求文本")


class LineComment(BaseModel):
    """挂在 diff 具体一行上的批注。

    `file` + `line` 是回注给 AI 的精确锚点——一句"第 42 行那个地方处理得不对"和一条挂在
    第 42 行的批注,对模型来说信息量差很多。
    """

    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    side: Literal["old", "new"] = "new"
    content: str = Field(min_length=1)


class ApprovalRequest(BaseModel):
    """批准或驳回。

    `actor` 是**声明**,服务端二次校验——不校验的话这道关卡就只是个仪式。
    """

    actor: str = Field(min_length=1, description="审批人身份")
    approved: bool
    comment: str = ""
    #: 驳回时的行级批注。空列表就是"只有整体意见,没有行级的"。
    line_comments: list[LineComment] = Field(default_factory=list)


class ApprovalPreview(BaseModel):
    """提交驳回前,AI 将实际收到的完整意见文本。

    **这个预览很重要**——它让审批人意识到自己在跟一个不会读心的系统对话,而不是在填一张
    只有人会看的表单。
    """

    text: str


class EventItem(BaseModel):
    task_id: str
    ts: str
    actor: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class EventPage(BaseModel):
    items: list[EventItem]
    total: int
    offset: int
    limit: int


class LogLine(BaseModel):
    #: 行号,从 1 起。**游标是行号而不是字节偏移**:日志在追加,偏移量会随内容变化错位。
    line: int
    text: str


class LogPage(BaseModel):
    items: list[LogLine]
    #: 下一页从这一行开始。到底了就是 `None`。
    next_cursor: int | None
    total: int


class ArtifactEntry(BaseModel):
    path: str
    size: int


class ArtifactList(BaseModel):
    items: list[ArtifactEntry]


class ReportResponse(BaseModel):
    task_id: str
    markdown: str


class AlertRequest(BaseModel):
    """一条生产告警。运维侧的监控推给我们。"""

    id: str = Field(min_length=1)
    service: str = Field(min_length=1)
    summary: str = ""


class AlertResponse(BaseModel):
    """告警处理的结果。**定位不到时 `task_id` 是 None**——瞎猜一个模块比不建任务更糟。"""

    alert_id: str
    located: bool
    modules: list[str]
    recent_tasks: list[str]
    task_id: str | None = None
    reason: str = ""


class TopologyOption(BaseModel):
    """提交页上的一个执行拓扑。文案与可用性都由服务端算——前端硬编码一份的话,加模板时
    漏改的表现是下拉里出现一个没有说明的选项。
    """

    id: str
    name: str
    summary: str
    #: 选了它之后整条流水线会发生什么。右栏那份步骤清单直接渲染它。
    steps: list[str] = Field(default_factory=list)
    #: 机制在、结论还没有。**不该被读成推荐。**
    experimental: bool = False
    #: 现在点不点得了。点不了的也要列出来——静默消失会让人以为这个能力不存在。
    available: bool = True
    unavailable_reason: str = ""
    #: 比单路贵几倍。**N 倍成本必须是一个被看见的决定**(PRD 39),所以它进契约,不靠
    #: 前端从别处推。
    cost_multiplier: int = 1
    #: 折成绝对 token 的估算。**没有历史数据时是 null**——编不出来的数字不显示,一个假的
    #: 绝对值比不显示更糟,因为人会拿它做决定。
    cost_estimate_tokens: int | None = None


class TopologyCatalog(BaseModel):
    """能选的执行拓扑,以及不选时会用哪个。"""

    #: 项目缺省。"跟随项目缺省"那一项要显示它到底是谁,不然那句话等于什么都没说。
    default: str
    options: list[TopologyOption]


class SettingsView(BaseModel):
    """现在生效的配置里,能从界面改的那几段。

    **只有白名单里的段。** 多返回一段它就变成了配置全文接口;少一段就是界面上一个
    改不了的旋钮。运行时段只存命令、入口和凭据的环境变量名,不存凭据值。边界由测试钉住。

    **字段类型直接复用配置模型本身**,不另抄一份:抄一份的话,配置加字段而这里没跟上时,
    前端拿到的生成类型会少一个字段,而没有任何东西会报错。这也正好实现了"读写同形"——
    `PUT /settings` 的 `value` 就是这里对应段的形状。
    """

    #: 这个调用方能不能改。**服务端算,前端不重新猜权限矩阵**——猜的话,加一个角色时
    #: 界面与后端会给出两个答案,而界面那个是错的那个。与 `TaskSummary.can_run` 同一条。
    can_edit: bool = False

    #: 容器运行时那一段。**端出它不等于端出密钥**——里面存的是令牌的环境变量**名**,
    #: 值只在服务端进程里(PRD 33)。界面要能"改一个已知状态"而不是"重填一份配置",
    #: 所以这一段必须读得到。
    runtime: RuntimeConfig
    concurrency: ConcurrencyConfig
    budgets: BudgetConfig
    limits: LimitConfig
    itest: ItestConfig
    genome_tasks: GenomeTaskConfig
    approval: ApprovalConfig
    topology: TopologyConfig
    quality_line: QualityLineConfig

    @classmethod
    def of(cls, config: Config, can_edit: bool) -> SettingsView:
        return cls(
            can_edit=can_edit,
            runtime=config.runtime,
            concurrency=config.concurrency,
            budgets=config.budgets,
            limits=config.limits,
            itest=config.itest,
            genome_tasks=config.genome_tasks,
            approval=config.approval,
            topology=config.topology,
            quality_line=config.quality_line,
        )


class RuntimeBlockView(BaseModel):
    """这个员工**根本跑不到**的一个运行时,以及为什么。

    与兼容缺口分开:缺口补一次声明就没了,这个补不掉——它说的是运行时本身给不出这个
    员工要的保证(比如强制只读)。混成一件事的话,人会去补声明,补完照样被拒。
    """

    runtime: str
    reason: str


class RuntimeChoiceView(BaseModel):
    """一个员工现在跑在哪儿、能跑在哪儿,以及换过去还差什么。

    **选项由服务端给**:前端自己枚举的话,配置里没配的运行时会出现在下拉框里,
    选中之后装配失败,而失败发生在下一次派发、不在点击那一刻。
    """

    current: str
    options: list[str]
    can_edit: bool = False
    #: 换到 `candidate` 之前,这个员工的哪些工序还没声明兼容。没给 candidate 时为空。
    #: **列出来不等于自动补**——补声明是显式动作,见 `declare_compat`。
    compat_gap: list[str] = Field(default_factory=list)
    #: 这些运行时**接不了这个员工**,补兼容声明也没用。
    #:
    #: **兼容闸不是派发前唯一那道闸。** 只报缺口的话,一个只读员工会走完"选运行时 →
    #: 补声明 → 提交进 git"整条路,然后在派发时撞上一句关于只读的、听起来毫不相干的
    #: 报错——而它从第一步起就注定发生。
    blocked: list[RuntimeBlockView] = Field(default_factory=list)


class RuntimeChoiceRequest(BaseModel):
    runtime: str = Field(min_length=1)


class CompatDeclareRequest(BaseModel):
    """给工序补兼容声明。`procedures` 为空表示"这个员工当前的全部缺口"。"""

    runtime: str = Field(min_length=1)
    procedures: list[str] = Field(default_factory=list)


class ExecutionRequest(BaseModel):
    """把一个员工挪到信任爬坡的哪一档。

    **一个请求管三档**,尽管它们落在两个存储上:人做的是一个动作,存储的分工不该摊给
    调用方——摊出去的话,每个调用方都得先学会这个系统的文件布局。
    """

    #: **多余的键一律拒绝。** 这条路径只放开运行时与指派人;权限、写集、工序白名单是安全
    #: 边界,而默默忽略一个 `procedures:` 等于默默拒绝——调用方会以为自己改成了。
    model_config = ConfigDict(extra="forbid")

    execution: str = Field(min_length=1, description="auto / assisted / manual")
    #: 这一档的活归谁。`manual` 必填(没有主人的待办只会静默超时);留空表示沿用员工
    #: 定义里已有的指派人。
    assignee: str = ""


class SettingsRequest(BaseModel):
    section: str = Field(min_length=1)
    value: dict[str, Any] = Field(default_factory=dict)


class ReadinessItemView(BaseModel):
    """一项就绪检查的结论。`detail` 给人看,**不含任何凭证值**。"""

    name: str
    ok: bool
    detail: str


class ReadinessView(BaseModel):
    """容器运行时的就绪检查。

    **分项返回而不是一个布尔**:平台、存储、Matrix、服务端凭证指向四个不同的运维动作,合成一个的话
    "哪一项挂了"就答不出来——而那正是人点这个按钮时唯一想知道的事。
    """

    ok: bool
    items: list[ReadinessItemView]


class WorkerStatusView(BaseModel):
    """一个容器员工此刻在平台上的样子。

    `status` 取 `absent` / `running` / `sleeping` / `unknown`。**`unknown` 与 `absent`
    分开**:前者是"平台没答上来",后者是"确实没供应过";合成一个的话,平台一挂,整份
    花名册看起来就像从没供应过,而那会诱人去点一次本不必要的供应。
    """

    employee_id: str
    status: str
    #: 平台上的 Worker 名。未供应时为空。
    worker: str = ""
    #: 这个 Worker 的房间 id。**每次去问平台**——Worker 重建会换房间。
    room: str = ""
    detail: str = ""


class WorkerStatusListView(BaseModel):
    """容器员工的状态表。**只列容器运行时的员工**。

    跑在本地的员工不在这张表里,而不是以"未供应"出现——后者会让人以为该去供应它。
    """

    items: list[WorkerStatusView]
    #: 这个调用方点不点得动供应动作。**可用性由服务端算**:前端自己判的话,
    #: 每个前端都得复刻一遍权限矩阵,而复刻错的那一份要到点下去才说话。
    can_provision: bool = False


class WorkerPlanRowView(BaseModel):
    """对齐预演里的一行。`action` 取 `created` / `updated` / `unchanged` / `unknown`。"""

    employee_id: str
    action: str
    detail: str = ""


class WorkerPlanView(BaseModel):
    """ "这次点下去会发生什么"。**只读**:算这份计划不会在平台上写任何东西。"""

    items: list[WorkerPlanRowView]
    can_provision: bool = False


class WorkerProvisionResult(BaseModel):
    """对齐一个员工的结果。

    **动作要回出来**:"什么都没变"与"新建了一个"是完全不同的两件事,而只回一个引用的话
    这两者长得一模一样——界面于是只能显示"完成了",而人想知道的正是它到底做了什么。
    """

    employee_id: str
    #: `created` / `updated` / `unchanged`。
    action: str
    worker: str
    room: str


class WorkerLifecycleResult(BaseModel):
    """回收一个容器的结果。`action` 取 `slept` / `deleted`。

    **回出来的是做了哪一种**,不是一个 204:两个动作的后果差得很远(一个可逆、一个不),
    而界面要据此决定这一行接下来显示什么。
    """

    employee_id: str
    action: str


class SettingsChange(BaseModel):
    actor: str
    section: str
    at: str
    #: 从哪个入口改的。
    entrance: str = ""
    #: 结果提交。**改成了什么去这个提交里看**——这一层不回传前值后值,理由见 `server.settings`。
    rev: str = ""


class WorkspaceEntry(BaseModel):
    """一个项目在切换器里的样子。"""

    name: str
    #: 还有业务仓没挂上。**从磁盘算出来的**(挂载点下有没有 `.git`),不是存的标记。
    #: 初始化中的项目能看设置、事件与基因组任务,但提不了研发任务。
    initializing: bool = False


class WorkspaceList(BaseModel):
    """这个服务端在服务哪几个工作空间。

    **不带路径。** 界面要的是"能切到哪几个",而磁盘路径是部署细节;回传它等于把服务器的
    目录结构挂在一个读接口上——而这一层的读接口是不设防的。

    `items` 与 `entries` 说的是同一批项目:前者是老形状(只有名字),留着不破坏既有
    调用方;后者带初始化标记。**两者都从注册表现算**,不存在谁抄谁。
    """

    items: list[str] = Field(default_factory=list)
    entries: list[WorkspaceEntry] = Field(default_factory=list)


class WorkspaceCreateRequest(BaseModel):
    """界面上建一个项目。"""

    name: str = Field(min_length=1)
    #: 顶层 Workspace 的独立 Git 仓库。它保存治理配置、知识资产与业务仓指针。
    workspace_repo: str = Field(min_length=1)
    #: 业务仓地址,`<url>` 或 `<url>@<branch>`。至少一个:Workspace 是协作仓,
    #: 本身不含业务代码。
    repos: list[str] = Field(min_length=1)


class WorkspaceCreated(BaseModel):
    """建项目的回执。**不带路径**——与 `WorkspaceList` 同一条理由。"""

    name: str
    #: 异步挂载的那条 MOUNT 基因组任务。进度、失败、重试都看它。
    #: **None 表示没有要挂的**:认领的既有目录业务仓早就齐了。
    mount_task_id: str | None = None
    #: 这次是认领了一个已存在的项目目录(典型来源:内存注册表 + 重启留下的孤儿),
    #: 不是新建。目录里的任何东西都没被动过。
    adopted: bool = False


class Health(BaseModel):
    status: Literal["ok"] = "ok"


class Version(BaseModel):
    """前后端契约对齐用。版本错配要能被及时发现,而不是表现为某个字段莫名其妙是空的。"""

    version: str
    api: str


# --- 基因组管理(PRD 12) -----------------------------------------------------


class ModuleNode(BaseModel):
    id: str
    path: str
    lang: str | None = None
    summary: str = ""
    depends_on: list[str] = Field(default_factory=list)
    confidence: float | None = None
    doc: str | None = None


class InterfaceEdge(BaseModel):
    id: str
    kind: str
    provider: str
    consumers: list[str] = Field(default_factory=list)
    confidence: float | None = None


class ProjectMapResponse(BaseModel):
    version: int
    updated_at: str | None
    project_name: str
    modules: list[ModuleNode]
    interfaces: list[InterfaceEdge]


class ProjectMapVersionItem(BaseModel):
    rev: str
    at: str
    author: str
    subject: str
    #: 哪个任务的经验触发了这次更新。**内容去版本面看**——这一层只给指针。
    source_task_id: str = ""


class ProjectMapVersionList(BaseModel):
    items: list[ProjectMapVersionItem]


class ProjectMapDiffResponse(BaseModel):
    from_rev: str
    to_rev: str
    diff: str


class EvidenceItem(BaseModel):
    task_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    note: str = ""


class LessonCardResponse(BaseModel):
    id: str
    title: str
    modules: list[str]
    path_globs: list[str]
    scenario: str
    conclusion: str
    evidence: list[EvidenceItem]
    confidence: float
    level: str
    hits: int
    created_from: str
    archived: bool


class LessonList(BaseModel):
    items: list[LessonCardResponse]
    total: int


class SuspectEntry(BaseModel):
    """可疑账里的一条信号(PRD 41)。"""

    kind: str
    card: str
    task_id: str
    changed: list[str]
    round: int


class DeepenQueueEntry(BaseModel):
    """深化队列里的一项:哪张卡、有多热。"""

    card: str
    summary: str
    churn: int


class LessonCreateRequest(BaseModel):
    title: str = Field(min_length=1)
    modules: list[str] = Field(default_factory=list)
    path_globs: list[str] = Field(default_factory=list)
    scenario: str = ""
    conclusion: str = Field(min_length=1)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class RuleSetResponse(BaseModel):
    architecture: ArchitectureRules
    protected: ProtectedRules
    impact: ImpactRules


class RuleProposalRequest(BaseModel):
    section: Literal["architecture", "protected", "impact"]
    payload: dict[str, Any]
    description: str = ""
    actor: str = Field(min_length=1)
    #: 这次提案是为哪个任务提的。**空着也行**——人主动改规则时本来就没有来源任务,那时它
    #: 挂在系统主体下。给了的话,那个任务的时间线上会出现一条指向这个 PR 的记录。
    source_task_id: str = ""


class RuleProposalResponse(BaseModel):
    repo: str
    number: int
    head: str
    base: str


class GenomeTaskSummary(BaseModel):
    """一个基因组任务。

    **与 `TaskSummary` 是两个模型,不共用。** 两类任务的字段只有四条真正重合(id、状态、
    成本、时间),硬塞进一个模型的结果是每一类都带着一半永远为空的字段——而读的人无从判断
    那是"还没填"还是"不适用"。
    """

    id: str
    title: str
    #: 初始化 / 重建 / 蒸馏 / 补写。
    kind: str
    #: 人发起的还是系统自发的。**它决定失败该不该惊动人。**
    origin: str
    state: str
    #: 作用在哪个模块上。全量初始化没有。
    subject: str | None = None
    #: 由哪个研发任务触发。人发起的初始化没有。
    source_task_id: str | None = None
    failure_reason: str | None = None
    tokens_used: int = 0
    budget_tokens: int | None = None
    created_at: str
    updated_at: str
    #: 这个闸门等太久了。**提醒不判死**——一个等人的健康任务不该因为人休假而失败。
    overdue: bool = False


class GenomeTaskList(BaseModel):
    items: list[GenomeTaskSummary]


class ModuleProgress(BaseModel):
    """一个模块在这次深读里的状态。"""

    module_id: str
    #: pending / done / failed。**三态而不是布尔**——"还没读"与"读失败了"是完全不同的两件事,
    #: 而一个 `ok: false` 把它们合成了一个。
    status: str
    detail: str = ""
    #: 读了多久,秒。"哪个模块特别慢"是下一次调预算与并发时唯一有用的那条线索。
    duration_s: float = 0.0


class GenomeTaskProgress(BaseModel):
    """逐模块进度。**基因组任务特有的那一块。**"""

    task_id: str
    #: 还没开始跑深读。**与"零个模块"不是一回事**,界面上要说不同的话。
    started: bool = False
    modules: list[ModuleProgress] = Field(default_factory=list)
    #: 这个任务产出的知识 PR。**是指针不是内容**——改成了什么去那个 PR 里看。
    pull_requests: list[str] = Field(default_factory=list)


class ReinitRequest(BaseModel):
    modules: list[str] = Field(min_length=1)


class BoundaryModule(BaseModel):
    """草案里的一个模块,也是答复里的一个模块。

    **草案与答复用同一个形状。** 人要做的是"这两个合并、那个拆开",在草案上直接改;让他
    重新组织一遍数据结构,是最容易让人放弃复核的那种设计。
    """

    id: str = Field(min_length=1)
    #: 这个模块覆盖哪几个目录。**是列表而不是一个字符串**:合并两个模块的结果就是一个模块
    #: 覆盖两个目录,而把它们拼成 `"order/ pay/"` 这样一个字符串,下游拿到的是一条谁都解析
    #: 不对的路径——那正是"人在界面上改一改"最容易悄悄产出的坏数据。
    paths: list[str] = Field(default_factory=list)
    summary: str = ""
    #: 系统为什么这么分。**只给一个模块列表的话,人无从判断该不该改它**——而这是他唯一
    #: 能复核的东西。答复里可以留空:改过的那几行,理由在备注里。
    rationale: str = ""


class GateDraft(BaseModel):
    """一个待回答的闸门。"""

    task_id: str
    state: str
    modules: list[BoundaryModule] = Field(default_factory=list)
    #: 草案本身附的一句话,说明它是怎么推出来的。
    note: str = ""
    #: 已经回答过的话,这是上一次的答复。**先看再答是常态。**
    answered: bool = False


class GateAnswer(BaseModel):
    """人给出的最终模块列表。

    改名、合并、拆分、剔除**在数据上就是"给出最终列表"**——不为每种动作定义单独的指令,
    否则每加一种人工操作都要动一次协议,而后端会多出四条各自要校验的路径。
    """

    modules: list[BoundaryModule] = Field(min_length=1)
    #: 人为什么这么改。后来的人要知道理由。
    note: str = ""


class GateResult(BaseModel):
    task_id: str
    #: 任务真的往下走了吗。**回答被接受不等于状态变了**——同一个答复重放一次是幂等的。
    moved: bool
    state: str


class ModuleKnowledge(BaseModel):
    """一个模块的知识状态。"""

    module_id: str
    #: 建了几个功能点、其中几个有卡片。
    features: int = 0
    cards: int = 0
    #: 声明了「无需卡片」的那几个。**完备不等于每个功能点都有卡片**——带理由的声明也算数,
    #: 但它要能被一眼扫到,否则「不值得写」会变成偷懒的托词。
    no_cards: int = 0
    #: 这个模块的地图置信度。低的要人复核。
    confidence: float | None = None


class NoCardDeclaration(BaseModel):
    module_id: str
    feature_id: str
    #: 为什么这里不值得写卡片。**空理由等于没写**,所以这里也不接受空串。
    reason: str = Field(min_length=1)


class CardHit(BaseModel):
    module_id: str
    feature_id: str
    title: str = ""
    #: 被命中且那次任务成功的次数。长期零命中的卡片该被淘汰。
    hits: int = 0
    confidence: str | None = None


class KnowledgeStatus(BaseModel):
    """基因组管理页要的那一屏:知识现在是什么状态。"""

    modules: list[ModuleKnowledge] = Field(default_factory=list)
    #: 置信度低、建议人复核的条目。
    review: list[CardHit] = Field(default_factory=list)
    no_cards: list[NoCardDeclaration] = Field(default_factory=list)
    cards: list[CardHit] = Field(default_factory=list)
    #: 可疑账余额(PRD 41):软信号,由知识更新工序消费,这里只读透出。
    suspects: list[SuspectEntry] = Field(default_factory=list)
    #: 深化队列,按变更热度降序。
    deepen_queue: list[DeepenQueueEntry] = Field(default_factory=list)


class ProcedureStat(BaseModel):
    id: str
    version: str
    source: str
    kind: str
    available: bool
    call_count: int
    failure_count: int
    failure_rate: float


class ProcedureStatsList(BaseModel):
    items: list[ProcedureStat]


# --- 观测中心(PRD 12) -------------------------------------------------------


class TrendMetric(BaseModel):
    name: str
    direction: str
    previous: float
    current: float
    samples: int
    enough: bool
    improving: bool | None


class TrendReport(BaseModel):
    period: str
    metrics: list[TrendMetric]
    has_enough: bool


class CostSlice(BaseModel):
    key: str
    tokens: int


class CostReport(BaseModel):
    by_employee: list[CostSlice]
    by_task: list[CostSlice]
    total_tokens: int


class RosterMember(BaseModel):
    """花名册上的一个员工,连同它到目前为止的出场与花费。

    **出场为 0 的也要在名单里。** "这个项目根本没用过对抗"与"页面上没有这一行"是完全不同
    的两件事,而后者会让人以为那个角色不存在。
    """

    id: str
    name: str
    runtime: str
    #: 信任爬坡的档位:`auto` / `assisted` / `manual`。**服务端算**——它由两个存储合成
    #: (员工定义的运行时 + 根配置的确认名单),前端自己拼的话,加一个存储位置时界面会漏判。
    execution: str = "auto"
    #: 这个员工自己的指派人(员工定义里的那个字段)。**这才是可编辑的那一个。**
    assignee: str = ""
    #: 这一档的活**实际上**归谁:项目配了统一确认人就是它,否则退回 `assignee`。
    #: **空表示没人**——而没人收的待办只会静默超时。
    #:
    #: 与 `assignee` 分成两个字段而不是合成一个:合成之后,一个项目配了统一确认人时,
    #: 界面上那一格显示的是全局值、改的却是员工自己的字段,于是保存成功而显示不变——
    #: 一次没有任何报错的空操作。
    confirmer: str = ""
    #: 一句话说清它是干什么的。从员工定义的提示词首段取。
    summary: str = ""
    #: 被派了几次活。**与 tokens 同源**——各数各的话,对账会对不上。
    appearances: int = 0
    tokens: int = 0


class QualityDial(BaseModel):
    """质量线上的一个旋钮当前拧在哪一档。"""

    key: str
    value: str
    #: 这一档的意思,给不熟悉配置的人看。
    note: str = ""


class RosterReport(BaseModel):
    """员工管理页的数据源:七类员工 + 三个旋钮。

    **聚合在这里做,配置不搬家。** 评审那一档住在 `topology.critique`,搬进质量线配置节
    是一次破坏性迁移,而收益只是"排在一起好看"——排在一起是展示层的事。
    """

    employees: list[RosterMember]
    dials: list[QualityDial]


class AuditEventItem(BaseModel):
    task_id: str
    ts: str
    actor: str
    #: 行为主体的类别:人 / 员工 / 编排器 / 门禁 / 集成入口。按类别筛比按名字筛更常用。
    actor_kind: str = ""
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditEventPage(BaseModel):
    items: list[AuditEventItem]


class GapItem(BaseModel):
    """一个改了配置、却没有对应事件的提交。"""

    rev: str
    author: str
    at: str
    files: list[str] = Field(default_factory=list)


class GapReportResponse(BaseModel):
    """记录平面的缺口检测结果。**只报告**——它不拦截、不改状态。"""

    watched: list[str] = Field(default_factory=list)
    commits: int = 0
    gaps: list[GapItem] = Field(default_factory=list)
    #: 让这次比对不完整的已知情况(浅克隆、历史被改写、没纳入比对的路径)。
    notes: list[str] = Field(default_factory=list)
    #: 完全没法比对时说明原因。空报告与"比不了"必须能分辨,后者是零信息。
    unavailable: str = ""


# --- 体验补齐(PRD 12) -------------------------------------------------------


class RejectionPreviewRequest(BaseModel):
    comment: str = ""
    line_comments: list[LineComment] = Field(default_factory=list)


class ImportRequest(BaseModel):
    url: str = Field(min_length=1)


class ImportResult(BaseModel):
    title: str
    body: str
    source: str


class ImWebhookRequest(BaseModel):
    text: str = Field(min_length=1)
    user: str = Field(min_length=1)
    channel: str = ""


class ImWebhookResponse(BaseModel):
    task_id: str | None
    reply: str


class NotificationPreference(BaseModel):
    actor: str = Field(min_length=1)
    events: list[str] = Field(default_factory=list)
    webhook_url: str | None = None


class NotificationPreferenceList(BaseModel):
    items: list[NotificationPreference]


__all__ = [
    "AlertRequest",
    "AlertResponse",
    "ApprovalPreview",
    "ApprovalRequest",
    "ArtifactEntry",
    "ArtifactList",
    "AuditEventItem",
    "AuditEventPage",
    "CostReport",
    "CostSlice",
    "EventItem",
    "EventPage",
    "EvidenceItem",
    "Health",
    "ImWebhookRequest",
    "ImWebhookResponse",
    "ImportRequest",
    "ImportResult",
    "InterventionResolveRequest",
    "InterfaceEdge",
    "LessonCardResponse",
    "LessonCreateRequest",
    "LessonList",
    "LineComment",
    "LogLine",
    "LogPage",
    "ModuleNode",
    "NotificationPreference",
    "NotificationPreferenceList",
    "ProjectMapDiffResponse",
    "ProjectMapResponse",
    "ProjectMapVersionItem",
    "ProjectMapVersionList",
    "RejectionPreviewRequest",
    "ReportResponse",
    "RuleProposalRequest",
    "RuleProposalResponse",
    "RuleSetResponse",
    "SettingsChange",
    "SettingsRequest",
    "ProcedureStat",
    "ProcedureStatsList",
    "SubmitRequest",
    "ScopeGrantView",
    "TaskDetail",
    "TaskSummary",
    "TrendMetric",
    "TrendReport",
    "Version",
]


# --- 会话 ----------------------------------------------------------------------


class SessionSummary(BaseModel):
    """一个会话的概要。"""

    id: str
    employee_id: str
    #: 员工给人看的名字。**列表里显示它而不是 id**——`dev-employee` 不是人话。
    employee_name: str = ""
    #: 最后一条**用户**消息的一行预览。**后端给,前端算不出来**:前端要自己拿,得为列表里
    #: 每条会话各拉一遍消息,一页就是 N 次请求。标题重复时,这一行是认出"是哪一场"的依据。
    last_question: str = ""
    #: 能不能改代码。**静态属性,创建后不可改**——前端据此把它渲染成标签而不是可点的开关。
    writable: bool = False
    #: 旧的三选一模式名。**派生值,保留一个版本给还没跟上的调用方**,真相是
    #: `writable` 与 `task_id`。前端切完之后连同 `SessionCreateRequest.mode` 一起删。
    #:
    #: 用 `json_schema_extra` 而不是 `deprecated=True` 标记:后者会让 pydantic 在**每次
    #: 读这个字段**时发一条 DeprecationWarning,而这个字段每序列化一个会话就要读一次——
    #: 日志会被这条警告淹掉,而它想提醒的人(写调用方的人)根本不看服务端日志。OpenAPI
    #: 两种写法都认。
    mode: str = Field(default="", json_schema_extra={"deprecated": True})
    state: str
    title: str = ""
    task_id: str | None = None
    #: 挂起原因。只显示「已挂起」不说为什么,用户的第一反应是「它坏了」。
    suspend_reason: str | None = None
    #: 这会儿有没有一轮在后台跑。**进程内派生,不是 `Session` 自己的字段**——它和
    #: `_handles` 一样只活在内存里,服务重启后自然归零。前端据此决定重新打开页面/
    #: 切回来时要不要接直播(`GET .../messages/stream`),而不是傻等一条永远不会来的块。
    generating: bool = False
    tokens_used: int = 0
    max_tokens: int = 0
    idle_timeout_s: int = 0
    last_seq: int = 0
    #: 这次会话装载了什么。**上下文条渲染它**——没有它,"员工带着什么在回答"只能写成
    #: 一句笼统的话,而那正是这个控件存在的理由。
    context_items: list[str] = Field(default_factory=list)
    #: 用户钉住的那些。钉住的不参与截断。
    pinned: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, session: Any) -> SessionSummary:
        payload = session.as_dict()
        return cls(**{key: payload[key] for key in cls.model_fields if key in payload})


class SessionCreateRequest(BaseModel):
    """开一个会话要的东西。**两个自由度,不是三选一**——见 PRD 45。"""

    employee: str
    #: 让它改代码吗。**创建后不可更改** —— 只读与可写是两套工具集。
    writable: bool = False
    #: 关联哪个任务。可选,与 `writable` 正交:只读会话关了任务就预载它的产物(出口是回注
    #: 审批意见),可写会话关了任务就在它的隔离工作区里改。两个都不给也完全正常。
    task_id: str | None = None
    #: 旧的三选一模式名,**deprecated**。给还没切过来的调用方留一个版本:
    #: `consult`/`inquiry` → 只读,`pair` → 可写。与 `writable` 同时给且矛盾时报 400,
    #: **不静默挑一个**——静默挑的那次会让调用方以为自己的参数生效了。
    mode: str | None = Field(default=None, json_schema_extra={"deprecated": True})
    title: str = ""


class SessionMessageRequest(BaseModel):
    message: str


class BlockItem(BaseModel):
    """一个消息块。前端按 `kind` 查渲染器注册表。"""

    seq: int
    kind: str
    text: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class BlockPage(BaseModel):
    """历史块。断线补齐与回放共用它。"""

    session_id: str
    items: list[BlockItem]
    #: 本页最后一个块的序号。前端拿它作为下次补齐的起点。
    last_seq: int = 0


class SessionList(BaseModel):
    items: list[SessionSummary]
    #: 各状态各有几条。**不随当前筛选变化**——前端只拿到筛出来的那一页,数不出别的状态
    #: 有几条,而筛选器上的计数正是用来回答"别处有没有积压"的。
    counts: dict[str, int] = Field(default_factory=dict)
    #: 全部会话数(不受筛选影响)。列表标题上那句「共 N 条」。
    total: int = 0


class EmployeeSummary(BaseModel):
    """员工在选择器里的样子。

    **字段限于展示所需。** 工具白名单、授权范围、凭证名一律不出现在这里——那些是安全相关
    的配置,展示不需要它们;顺着一个展示接口漏出去,等于把"这个员工能碰哪些路径"公开给了
    任何能打开控制台的人。

    `name` 是给人看的名字(「架构员工」),**空就退回 id**。早先这里只给 id,理由是"领域
    模型里没有角色这个概念"——那个判断反了:设计稿要的中文名是存在的东西,拒绝造它并不会
    让内部 id 变得适合展示。
    """

    id: str
    #: 界面上叫它什么。花名册没写时等于 id。
    name: str
    runtime: str
    #: 这个员工能不能开会话。**由能力矩阵算出来,不让前端按运行时名去猜**——前端硬编码
    #: 运行时名的话,接第三个运行时时它会漏判,而漏判的表现是选择器里一个开不了会话的员工
    #: 看起来可选,用户选完、提交、再吃一个错误。
    can_session: bool
    #: 开不了的原因,能开时为空。**置灰要说明为什么**,否则用户只会以为是自己没权限。
    session_blocked_reason: str = ""


class EmployeeList(BaseModel):
    items: list[EmployeeSummary]


class TaskDraft(BaseModel):
    """会话转任务的草稿。

    **返回草稿而不是直接建任务。** 自动建任务会让「随口问一句」变成「莫名多了个任务」,
    而一次轻量提问的全部价值就在于它足够轻,轻到人愿意随便问。
    """

    title: str
    requirement: str
    modules: list[str] = Field(default_factory=list)
    #: 这份草稿是从哪次对话来的。建出来的任务要记得住它。
    source_session_id: str
    #: 可写会话已经改好的东西在哪条分支上。只读会话为空串。
    #:
    #: **没有它,那些改动就找不回来了**——它既不在主线上,也不在任何任务分支上。而"转成
    #: 任务"是可写会话唯一的出口,这条线索是这个出口能不能走通的前提。
    source_branch: str = ""


class PinRequest(BaseModel):
    item: str
    pinned: bool = True


class FeedbackRequest(BaseModel):
    """对一条回答表态。

    **有用则给这次装载的知识卡片记一次命中,没用则什么都不记——不倒扣。** 一次没帮上忙
    不等于这张卡片是错的,而倒扣会让少数几次不满意把一张长期有用的卡片打下去。
    """

    useful: bool


class FeedbackResponse(BaseModel):
    #: 这次记了账的卡片。空表示这次回答没带任何知识卡片。
    credited: list[str] = Field(default_factory=list)


class InjectRequest(BaseModel):
    """把质询结论回注为审批意见或驳回意见。"""

    decision: str = "approve"
    comment: str


class TodoItem(BaseModel):
    """一张待办在列表里的样子。

    **要交什么与什么时候之前交都在里面。** 少了前者,人会交一份"看起来对"的东西然后被校验
    打回;少了后者,待办会在列表里慢慢烂掉。
    """

    id: str
    task_id: str
    stage: str
    node: str
    assignee: str
    employee_id: str
    procedure_id: str
    #: `artifact`(网页交产物)、`worktree`(去工作树里改代码)或 `split`(裁决一份
    #: 拆分提案)。人一眼要知道去哪儿干活。
    kind: str
    state: str
    #: 提醒发过没有、改派过几次——"这张待办卡在谁那儿、卡了多久"的那一半答案。
    reminded: bool
    reassignments: int
    created_at: str
    #: 拆分待办才有:等着人裁决的那份提案(children + rationale)。别的 kind 是 None。
    proposal: dict[str, Any] | None = None
    updated_at: str
    #: 什么时候之前要交(按当前配置的改派窗口算)。**列表里必须有它**——没有截止时间的待办
    #: 会在列表里慢慢烂掉,而那正是这套三段机制要防的事。
    due_at: str = ""


class TodoDetail(TodoItem):
    """打开一张待办时多出来的:上下文包、工作树、以及**产物契约**。"""

    context_file: str
    output_dir: str
    workdir: str = ""
    #: 要交什么:字段与必填项。**与硅基员工用的是同一份 schema。**
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")

    model_config = ConfigDict(populate_by_name=True)


class TodoList(BaseModel):
    items: list[TodoItem]


class TodoSubmitRequest(BaseModel):
    """交活。

    产物类待办带 `result`;工作树类不带——那一类的产出是工作树里的真实改动,由越权检查与
    门禁裁决,而 `changed_files` 由 git 填,不由人自报。
    """

    result: dict[str, Any] | None = None


class TodoSubmitResponse(BaseModel):
    ok: bool
    todo: TodoItem
    #: 没过契约时的原因,**与硅基员工拿到的是同一份**。
    detail: str = ""
    #: 交完之后任务现在在哪一态。
    task_state: str = ""
