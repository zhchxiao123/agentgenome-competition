# AgentGenome · 研发基因组 —— 详细设计文档

> 版本化原生的自进化研发协同底座
> 版本:v0.9(完整系统设计) | 2026-08 | 鲸智百应
> 技术基线:Python 3.11+ 编排器 · CLI Agent 数字员工运行时(Claude Code / Qwen Code 可插拔)

---

## 0. 阅读指南与术语

| 术语 | 含义 |
|---|---|
| **Workspace(项目空间)** | 以 Git 仓库承载的协作根空间,含子模块、基因组目录与运行时目录 |
| **Genome(研发基因组)** | 项目的知识(knowledge)、规则(rules)、工序(procedures)三类可版本化资产的统称 |
| **数字员工(Employee)** | 一个"CLI Agent 运行时 + 角色系统提示词 + 工序集 + 权限配置"的组合 |
| **Task(任务)** | 一个用户需求对应的任务实例,由状态机驱动其生命周期 |
| **Job(作业)** | 编排器派发给某个员工的一次具体执行(员工 × 工序 × 上下文) |
| **工序(Procedure)** | 标准化作业契约:声明调用条件、输入输出、依赖工具与失败处理;编排器视角的能力单元 |
| **手艺(Craft Skill)** | coding-agent 技艺包(如 Claude Code Skill),教员工"怎么干得好",由工序装载(§7.4);全文 "skill" 一词专指此层 |
| **Gate(门禁)** | 单测、静态检查、构建、安全扫描等质量关卡的统一执行器 |
| **Artifact(产物)** | Job 产出的结构化文件(报告、补丁、日志),经产物总线在员工间传递 |

标注约定:各节中 **[MVP]** 表示复赛最小可运行集必须实现;**[FULL]** 表示完整版能力,可在决赛及以后交付。

---

## 1. 设计目标与原则

### 1.1 目标

1. 一条命令提交需求,系统自动完成 架构准备 → 开发 → 质量验证 → 提交 的全流程,人只在审批点介入。
2. 每次任务执行后,项目基因组(知识/规则/工序)得到增量更新——系统越用越强。
3. 数字员工不可直接破坏主分支;所有变更可追溯、可回滚、可审计。
4. 底座与具体 Agent 运行时解耦:Claude Code / Qwen Code 等 CLI Agent 可插拔替换。
5. 与具体版本管理系统解耦:抽象为"版本化项目空间"接口,当前实现基于 Git。

### 1.2 设计原则

- **状态即事实**:编排器不保存内存态真相,一切以状态存储 + 产物文件为准,支持断点续跑。
- **文件即协议**:员工之间不直接通信,通过版本化文件与产物目录交换信息(黑板模式,天然可审计)。
- **基因走门禁**:基因组的更新与代码一样要经过验证与审批,防止"知识污染"。
- **最小权限**:每个员工只拿到完成当前 Job 所需的目录、工具与凭证。
- **人级兜底**:任何自动循环都有次数/时间/预算上限,超限升级人工。

---

## 2. 总体架构

```
                        ┌─────────────────────────────────────────────┐
                        │                agctl CLI / REST API / Web    │  交互层
                        └──────────────────────┬──────────────────────┘
                                               │
┌──────────────────────────────────────────────▼──────────────────────────────────────────┐
│                              Orchestrator(Python / asyncio)                            │
│  ┌────────────┐ ┌────────────┐ ┌──────────────┐ ┌─────────────┐ ┌────────────────────┐  │
│  │ TaskManager│ │ StateMachine│ │  Scheduler   │ │ ContextAsm  │ │  ApprovalService   │  │
│  └────────────┘ └────────────┘ └──────────────┘ └─────────────┘ └────────────────────┘  │
│  ┌────────────┐ ┌────────────┐ ┌──────────────┐ ┌─────────────┐ ┌────────────────────┐  │
│  │ AgentPool  │ │ ArtifactBus│ │  GateRunner  │ │ EventLog    │ │  EvolutionPipeline │  │
│  └─────┬──────┘ └────────────┘ └──────────────┘ └─────────────┘ └────────────────────┘  │
└────────┼────────────────────────────────────────────────────────────────────────────────┘
         │ spawn (subprocess, headless)
┌────────▼─────────┐  ┌──────────────────┐  ┌──────────────────┐
│ 架构设计员工      │  │ 开发员工          │  │ 集成测试员工      │        员工层(CLI Agent)
│ arch-employee    │  │ dev-employee     │  │ itest-employee   │
└────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
         │                     │                     │
┌────────▼─────────────────────▼─────────────────────▼─────────────────────────────────┐
│  Workspace(Git)   genome/(knowledge·rules·procedures)   tasks/(运行态)   code-*/(子模块) │  空间层
└──────────────────────────────────────────────────────────────────────────────────────┘
         │                                            │
┌────────▼──────────┐                       ┌─────────▼─────────┐
│ Gate 工具链        │                       │ UModel 语义图谱    │  平台层
│ pytest·lint·build │                       │ (生产↔代码映射)    │
│ gitleaks·docker   │                       │  [FULL]           │
└───────────────────┘                       └───────────────────┘
```

### 2.1 关键数据流

1. **需求流入**:`agctl submit "需求描述"` → TaskManager 创建 Task(CREATED)→ 状态机开始驱动。
2. **Job 派发**:每个状态对应一个 Handler,Handler 组装上下文 → AgentPool 拉起对应员工进程 → 员工在隔离工作区执行 → 产物写入 ArtifactBus。
3. **验证回路**:GateRunner 输出结构化结果;失败 → 状态回退并携带失败报告;通过 → 状态前进。
4. **经验回流**:Task 终态(COMPLETED / ESCALATED)触发 EvolutionPipeline → 架构员工蒸馏经验 → 基因组更新 PR。

---

## 3. Workspace 与研发基因组设计

### 3.1 目录结构 [MVP]

```
workspace/                          # 协作根仓库(Git)
├── .git/
├── .gitmodules
├── agentgenome.yaml                # 底座配置(见 §13.3)
├── genome/                         # ★ 研发基因组(与代码同仓,版本化)
│   ├── knowledge/                  #   项目知识层
│   │   ├── project-map.yaml        #   项目地图(结构化,见 3.2)
│   │   ├── modules/<module>.md     #   每模块认知卡片
│   │   ├── decisions/ADR-XXXX.md   #   架构决策记录
│   │   └── lessons/L-XXXX.md       #   经验教训卡片(自进化产物)
│   ├── rules/                      #   架构规则层
│   │   ├── architecture.md         #   模块边界/依赖方向(含机器可读块)
│   │   ├── coding.md               #   编码规范
│   │   ├── impact.yaml             #   变更影响规则(集成测试触发,见 6.3)
│   │   └── protected.yaml          #   受保护路径与高风险路径(见 §9)
│   └── procedures/                 #   工序层(项目级工序,可覆盖全局)
│       └── <proc-name>/procedure.yaml + prompt.md + scripts/ + craft/
├── employees/                      # 员工定义(见 §6.1)
│   ├── arch.yaml  dev.yaml  itest.yaml
│   └── prompts/arch.md  dev.md  itest.md
├── scripts/                        # 公共脚本(初始化、门禁包装等)
├── tasks/                          # 运行态(gitignore,只归档不提交)
│   └── ag-20260901-001/            #   单任务目录(见 §4.4)
├── code-1/                         # 业务仓库(Git 子模块)
└── code-2/
```

设计取舍:

- **workspace 是纯协作仓,不含业务代码**:workspace 仓自身只版本化 genome/、employees/、scripts/ 与配置;`code-*/` 是 Git 子模块,workspace 中仅存指向业务仓某 commit 的指针(gitlink)。业务仓零改造接入,退出 AgentGenome 也不留痕——这是"现有仓库即插即用"的实现基础。
- **genome 与 workspace 同仓**:保证"子模块指针前移 + 知识变更"出现在同一个 workspace 提交/PR 里,评审者一次看全。跨项目复用通过全局基因库(§10.4)解决,不靠拆仓。
- **tasks/ 不入库**:运行态高频写入,入库会污染历史;任务结束后归档摘要(manifest + 报告)到 `genome/knowledge/lessons/` 由进化管道决定去留。

### 3.1.1 双层提交拓扑 [MVP]

双仓结构决定了一个任务的变更落在两层:

```
任务 ag-...-001(跨 order/inventory 两模块)
├── 子仓层:code-1 上分支 task/ag-...-001 → PR#12(业务代码变更)
│          code-2 上分支 task/ag-...-001 → PR#7 (业务代码变更)
└── 顶层:  workspace 上分支 task/ag-...-001 → PR#33
            (内容 = code-1/code-2 子模块指针前移 + genome 知识增量)
```

约定与保障:

- **分支同名**:任务在 workspace 与所有受影响子仓使用同名分支,便于追踪与清理。
- **合并顺序**:先合并全部子仓 PR → 编排器更新 workspace 分支的子模块指针指向合并后 commit → 最后合并 workspace PR。workspace PR 是任务完成的"原子提交点":它合并之前,主 workspace 的指针仍指向旧版本,任何时刻 clone 顶层仓都是一致状态。
- **原子性边界**:跨模块任务的多个子仓 PR 之间不保证平台级原子合并(Git 平台不支持跨仓事务);以"子仓先全绿全并、顶层指针最后统一前移"逼近原子语义,且 REVIEWING 审批针对的是顶层 PR——审批人看到的是任务全貌。
- **门禁与集测在哪层跑**:单元门禁在各子仓 worktree 内跑;集成测试按 workspace 分支的指针组合拉起环境——测的是"这组指针在一起"是否成立,这正是顶层仓存在的意义。
- **回滚**:revert 顶层 workspace 的一个指针提交即回滚整个任务的组合状态,子仓历史无需改写。

### 3.2 分层知识地图与渐进式加载 [MVP]

**设计动机**:单文件项目地图在大型项目(50+ 模块、数千功能点)下必然失效——要么膨胀到不可维护,要么全量注入撑爆上下文预算。因此知识地图不是一个文件,而是**一棵与代码结构同构的索引树**:每层有行数预算,上下文按"路由命中"逐层展开,未命中的知识永远不进上下文。

#### 3.2.1 三层结构(可递归)

```
genome/knowledge/
├── project-map.yaml                 # L0 根索引(预算 ≤150 行)
├── interfaces.yaml                  # 全局接口/数据存储索引(跨模块契约)
├── modules/
│   └── order-service/
│       ├── map.yaml                 # L1 模块地图(预算 ≤200 行)
│       ├── overview.md              # 模块认知卡(面向人)
│       └── features/                # L2 功能卡片(细节知识)
│           ├── reserve-flow.md
│           ├── refund-flow.md
│           └── payment/…            # 超大模块继续分形:子目录 + 子 map,规则不变
├── decisions/ADR-*.md
└── lessons/L-*.md
```

**L0 根索引**——只回答"项目是什么、有哪些模块、谁和谁有契约":

```yaml
version: 12
project: {name: example-mall, summary: 电商中台}
modules:
  - {id: order-service,     path: code-1/, summary: 订单域,   map: modules/order-service/map.yaml}
  - {id: inventory-service, path: code-2/, summary: 库存域,   map: modules/inventory-service/map.yaml}
interfaces_index: interfaces.yaml
```

**L1 模块地图**——只回答"这个模块怎么跑、有哪些功能、依赖谁":

```yaml
id: order-service
lang: python
entrypoints: [src/order/app.py]
test_cmd: "pytest -q"
build_cmd: "make build"
depends_on: [inventory-service]
features:
  - {id: reserve-flow, summary: 下单预占库存流程, scope: ["src/order/reserve/**"], card: features/reserve-flow.md}
  - {id: refund-flow,  summary: 退款流程,         scope: ["src/order/refund/**"],  card: features/refund-flow.md}
```

**L2 功能卡片**——细节知识,Markdown 正文 + front-matter 声明身份与命中范围:

```markdown
---
id: reserve-flow
scope: ["src/order/reserve/**", "code-2/src/inventory/hold/**"]
summary: 下单预占:order 调 inventory 的 reserve-api,超时走补偿
confidence: high        # high/medium/low,蒸馏时标注
hits: 17                # 被命中且任务成功的次数(§10.2 自然选择依据)
links: [decisions/ADR-0007.md, lessons/L-0031.md]
updated_at: 2026-09-01
---
(细节:时序、坑点、不变量、测试要点……单卡预算 ≤300 行)
```

#### 3.2.2 渐进式加载协议(ContextAssembler 路由)

| 阶段 | 注入内容 | 预算(默认) |
|---|---|---|
| plan(需求解析) | L0 全量 + interfaces.yaml | ~1k tokens |
| dev / itest Job | L0 摘要 + 涉及模块 L1 全量 + **路由命中的 L2 卡片** | L1 ~2k,L2 ~4k |
| 命中规则 | plan 关键词 / diff 路径 匹配卡片 scope glob;失败报告中的文件路径同样参与路由 | — |
| 未命中卡片 | **只注入目录行(id + summary)**,员工在工作区内可自行 Read 展开 | ~0.5k |
| 超预算 | 命中集合按 hits 降序截断,截断情况写入上下文包头部声明 | — |

关键洞察:CLI Agent 的工作区里本来就有完整 genome/,因此上下文包的本质是**"目录 + 已命中内容"而非全量灌入**——这与 Claude Code procedure 的渐进式披露是同一机制,已被生产验证。

#### 3.2.3 一致性与防膨胀治理

- loader 启动校验:scope 路径存在、无孤儿卡片(map 未引用)、无指针悬空;
- 同一路径命中 >3 张卡片 → 告警(知识碎片化信号,提示架构员工合并);
- 行数预算是门禁项:L0/L1/单卡超限 → 知识更新 PR 不予通过,架构员工必须先拆分;
- **约束不变**:employees 只读知识树;写入必须通过架构员工的知识更新工序(§6.2),防止认知漂移。

### 3.3 规则文件的机器可读块 [MVP]

规则文档面向人书写,但嵌入 YAML front-block 供编排器与门禁消费:

```markdown
<!-- rules/architecture.md -->
```rules
forbidden_deps:
  - from: code-2/**        # 库存不得反向依赖订单
    to: code-1/**
layering:
  - "api 层不得直接访问 db 层"
max_fix_rounds: 3           # 单任务自动修复上限
```
(下接人类可读说明……)
```

### 3.4 版本化抽象 [FULL]

为兑现"版本化原生、不绑 Git",空间层定义 `VersionedWorkspace` 接口:

```python
class VersionedWorkspace(Protocol):
    def checkout_isolated(self, task_id: str) -> Path: ...   # 隔离工作区(Git: worktree)
    def diff(self, base: str, head: str) -> ChangeSet: ...
    def commit(self, msg: str, paths: list[Path]) -> Rev: ...
    def open_merge_request(self, title, body, base) -> MRRef: ...
    def protected_paths(self) -> list[Glob]: ...
```

当前唯一实现 `GitWorkspace`(git + worktree + gh/glab CLI)。未来接入 Jujutsu、Perforce 或云端工作区仅需新实现,不动上层。

---

## 4. 任务模型与状态机

### 4.1 Task 数据模型 [MVP]

SQLite(`state.db`,SQLModel)持久化 + 任务目录冗余一份 JSON 快照(便于人查看与容灾):

```python
class Task(SQLModel, table=True):
    id: str                    # ag-YYYYMMDD-NNN
    title: str
    requirement: str           # 原始需求全文
    state: TaskState
    branch: str                # task/ag-.../slug
    priority: int = 0
    fix_rounds: int = 0        # 已消耗修复轮次
    needs_itest: bool | None   # 集成测试判定结果(None=未判定)
    risk_level: str | None     # low/medium/high(安全检查产出)
    created_at / updated_at: datetime
    budget_tokens: int         # 任务级 token 预算
    escalate_reason: str | None
```

事件表 `TaskEvent(task_id, ts, actor, kind, payload_json)` 记录每次状态迁移、Job 起止、门禁结果——审计与回放的最小事实集。

### 4.2 状态机定义 [MVP]

```
CREATED ──plan──▶ DEVELOPING ──gate:unit──▶ UNIT_TESTING
   UNIT_TESTING ──pass & needs_itest──────▶ INTEGRATION_TESTING
   UNIT_TESTING ──pass & !needs_itest─────▶ READY_TO_COMMIT
   INTEGRATION_TESTING ──pass─────────────▶ READY_TO_COMMIT
   READY_TO_COMMIT ──risk:high────────────▶ REVIEWING ──approve──▶ MERGING
   READY_TO_COMMIT ──risk:low─────────────▶ MERGING
   MERGING ──merged───────────────────────▶ COMPLETED ──▶ (EvolutionPipeline)
   任何测试/门禁失败 ─────────────────────▶ DEVELOPING (fix_rounds += 1, 携带失败报告)
   fix_rounds > max_fix_rounds 或超预算 ──▶ ESCALATED(人工处理终态)
   REVIEWING ──reject(附意见)─────────────▶ DEVELOPING
```

迁移表(节选,完整表见附录 A):

| 当前态 | 事件 | 守卫条件 | 目标态 | 副作用 |
|---|---|---|---|---|
| CREATED | plan_done | 需求解析产物存在 | DEVELOPING | 创建分支/worktree;派发 dev Job |
| UNIT_TESTING | gate_pass | needs_itest 已判定 | INTEGRATION_TESTING / READY_TO_COMMIT | 归档门禁报告 |
| * (测试态) | gate_fail | fix_rounds < max | DEVELOPING | 失败报告注入下轮上下文 |
| * (测试态) | gate_fail | fix_rounds ≥ max | ESCALATED | 通知人工;冻结分支 |
| REVIEWING | approve | 审批人 ∈ approvers | MERGING | 记录审批人与意见 |

实现:显式迁移表(`transitions.py` 中的字典)而非状态机框架——迁移即文档,评审直读。

### 4.3 需求解析与任务拆分 [FULL]

CREATED 态的 plan Handler 由架构员工执行 `requirement-analysis` 工序:输出 `plan.yaml`(涉及模块、是否跨模块、验收标准、预估风险)。**[MVP] 简化**:单任务不拆子任务,plan 仅做模块定位与验收标准提取;**[FULL]**:支持拆分为带依赖 DAG 的子任务集,Scheduler 按依赖并行派发。

### 4.4 任务目录布局 [MVP]

```
tasks/ag-20260901-001/
├── task.json                  # Task 快照
├── plan.yaml                  # 需求解析产物
├── context/                   # 每个 Job 实际收到的上下文包(可复现)
│   └── job-03-dev.md
├── artifacts/
│   ├── 01-plan/ 02-dev/ 03-unit-gate/ 04-itest/ ...
│   │   └── manifest.json      # {producer, inputs, outputs[], summary}
├── logs/
│   └── job-03-dev.jsonl       # Agent stream 事件(stream-json 落盘)
└── report.md                  # 终态汇总(给人看)
```

---

## 5. 编排器(Python)设计

### 5.1 进程与并发模型 [MVP]

- 单进程 asyncio 服务:`agentgenome serve`。Job 执行是子进程(CLI Agent),编排器只做 I/O 等待,asyncio 足够。
- `Scheduler` 维护两级并发闸:全局最大并行 Job 数(默认 3)与每任务串行(同一任务同时只有一个 Job)。**[FULL]**:多任务并行时按 worktree 隔离,互不加锁。
- 崩溃恢复:启动时扫描 state.db 中非终态任务,依据"状态即事实"重放当前态 Handler(Handler 必须幂等:先检查产物是否已存在)。

### 5.2 模块划分(Python 包)

```
agentgenome/
├── cli.py                # agctl 入口(typer)
├── server.py             # FastAPI:REST + 审批 + Webhook + 控制台
├── core/
│   ├── task.py  states.py  transitions.py  scheduler.py
│   └── events.py         # EventLog(JSONL + DB 双写)
├── agents/
│   ├── runtime.py        # AgentRuntime 抽象(见 5.3)
│   ├── claude_code.py  qwen_code.py
│   └── pool.py           # 并发闸/超时/预算控制
├── space/
│   ├── workspace.py      # VersionedWorkspace 抽象
│   └── git_ws.py         # Git 实现(worktree/diff/PR)
├── genome/
│   ├── loader.py         # project-map/rules 解析与校验(pydantic)
│   ├── procedures.py     # 工序注册表与校验
│   └── evolution.py      # 经验蒸馏管道(§10)
├── jobs/
│   ├── context.py        # ContextAssembler(5.4)
│   ├── handlers/         # 每状态一个 Handler
│   └── artifacts.py      # ArtifactBus
├── gates/runner.py       # GateRunner(§8)
├── security/             # 扫描、审批、审计(§9)
└── integrations/umodel.py  # [FULL] §11
```

### 5.3 AgentRuntime 抽象 [MVP]

```python
class AgentRuntime(Protocol):
    name: str
    async def run_job(self, spec: JobSpec) -> JobResult: ...

@dataclass
class JobSpec:
    employee: EmployeeConfig      # 角色提示词、允许工具、权限
    procedure: ProcedureSpec      # 本次执行的工序
    workdir: Path                 # 隔离工作区(worktree 根)
    context_file: Path            # 组装好的上下文包(markdown)
    output_dir: Path              # 产物目录
    timeout_s: int
    max_tokens: int
```

`ClaudeCodeRuntime` 实现要点:

- 以 headless 方式拉起:`claude -p "$(cat context.md)" --output-format stream-json --max-turns N`,`cwd=workdir`;
- **craft 物化**:拉起前将本次工序的 craft/ 与员工级通用 craft 装载为工作区 `.claude/skills/`(§7.4);
- 工具白名单/黑名单由 EmployeeConfig 映射为 `--allowedTools/--disallowedTools`;
- stream-json 逐行落盘 `logs/job-*.jsonl`,同时提取 token 用量计入预算;
- 结果契约:工序的 prompt 明确要求"最后将结构化结果写入 `output_dir/result.json`",runtime 校验其存在与 schema,不合格视为 Job 失败并重试一次。

`QwenCodeRuntime` 同构([FULL],参数映射不同)。**运行时选择**:employees/*.yaml 中 `runtime: claude-code` 字段决定,可按员工混配。

**会话模式扩展**(支撑 §6.5 直接对话):

```python
class AgentRuntime(Protocol):
    async def run_job(self, spec: JobSpec) -> JobResult: ...
    async def start_session(self, spec: SessionSpec) -> SessionHandle: ...
    async def send_message(self, h: SessionHandle, msg: str) -> AsyncIterator[Event]: ...
```

Claude Code 实现:每条消息一次 headless 调用,以 `--resume <session-id>` 续接上下文,流式输出经 SSE 透传前端——无需常驻交互进程,会话状态由运行时的 session 机制承载,编排器只记账(会话事件、token、超时)。

### 5.4 ContextAssembler(上下文组装器)[MVP]

每个 Job 的上下文包按固定骨架拼装,并做 token 预算裁剪(超预算时按优先级 4→1 截断):

1. 角色系统提示词(employees/prompts/*.md)
2. 本次工序的 prompt.md + 输入参数
3. 任务上下文:需求原文、plan.yaml、**上一轮失败报告(如有,置顶)**
4. 基因组切片:按 §3.2.2 渐进式加载协议路由——L0 摘要 + 涉及模块 L1 + scope 命中的 L2 卡片 + 未命中卡片目录行,再叠加命中规则(rules)

上下文包完整落盘 `tasks/<id>/context/`,保证任意 Job 可离线复现。

### 5.5 对外接口 [MVP]

```
agctl init                       # 初始化 workspace(调用架构员工)
agctl submit "需求" [--module]   # 创建任务
agctl status [task-id]           # 状态与进度
agctl approve <task-id> [--reject -m "..."]   # 人工审批
agctl logs <task-id> [--follow]
REST: POST /tasks  GET /tasks/{id}  POST /tasks/{id}/approval
      GET /events/stream (SSE)  POST /webhooks/alert [FULL]
Web:  需求提交 / 任务看板 / 审批队列(见 §12.1,与 CLI 同走 REST)
```

---

## 6. 三大数字员工设计

### 6.1 员工定义文件 [MVP]

员工 = 配置,不是代码。`employees/dev.yaml`:

```yaml
id: dev-employee
runtime: claude-code            # 可替换 qwen-code
model: default                  # 交给 runtime 的模型档位
prompt: employees/prompts/dev.md
procedures:                     # 允许调用的工序(白名单)
  - branch-worktree
  - code-develop
  - unit-gate
tools:
  allow: [Bash, Read, Write, Edit, Grep, Glob]
  deny:  [WebFetch]             # 开发员工默认断网,防提示注入外联
permissions:
  write_paths: ["code-*/**", "tasks/{task_id}/**"]   # 只可写业务码与本任务目录
  forbid_paths: ["genome/rules/**", ".github/**"]     # 规则只有架构员工可动
limits:
  job_timeout_s: 1800
  max_tokens_per_job: 200000
```

**权限落地方式**:write_paths/forbid_paths 由两道机制兜底——(1)Job 结束后编排器 `git status` 校验改动范围,越权改动直接判 Job 失败并回滚工作区;(2)提交前安全检查再验一次(§9.2)。不依赖员工"自觉"。

### 6.2 架构设计数字员工(arch-employee)

| 工序 | 触发 | 输入 | 输出(产物契约) | [层级] |
|---|---|---|---|---|
| `workspace-init` | agctl init | 仓库清单/脚手架参数 | workspace 骨架 + .gitmodules + genome 空骨架 | MVP |
| `knowledge-init` | init 后自动 | 全部子模块代码 | project-map.yaml + modules/*.md 初版 | MVP |
| `requirement-analysis` | 任务 CREATED | 需求原文 + project-map | plan.yaml(模块定位/验收标准/风险预判) | MVP |
| `knowledge-update` | 任务 COMPLETED / 代码合并后 | 本次 diff + 任务报告 | 知识增量 PR(map version+1) | MVP |
| `rule-distill` | EvolutionPipeline 判定有规则级经验 | lessons 候选 | rules 增量 PR(必须人工审批) | FULL |

提示词要点(prompts/arch.md):只读优先、输出结构化、"不确定的认知标注 confidence 字段而非编造"。

#### 6.2.1 知识初始化流水线(knowledge-init 展开)[MVP]

大型存量项目无法"一个 Job 读完",初始化按五阶段流水线执行,**骨架优先、懒生成补全**:

```
① 确定性扫描(不烧 token)      目录树、语言与构建文件识别、依赖清单解析、
                              git log 热区分析(近 6 个月高频变更路径)
② 模块划分建议(agentic)      基于扫描结果提出模块边界草案 → 人工确认/调整
                              ★ 全流程唯一必须人参与的节点
③ 逐模块深读(map-reduce)     每模块一个并行 Job:生成 L1 map.yaml + overview.md;
                              从代码提取接口定义 → interfaces.yaml
④ L2 卡片:热区优先           仅为 git 热区与②中人工标记的核心功能生成 L2 卡片;
                              其余 feature 标记 pending(只有目录行)
⑤ 汇总与校对                  生成 L0 根索引;低置信度条目汇成人工校对清单;
                              整体以知识 PR 提交(走 §3.2.3 门禁)
```

- **懒生成**:任务执行中路由命中 pending feature 时,进化管道自动补生成该卡片(证据=该任务的实际读码结果)——冷启动成本从"读懂全库"降为"读懂骨架",知识密度随使用自然生长,与渐进式加载(§3.2.2)构成同一设计的两半。
- **幂等重跑**:`agctl genome reinit --module <id>` 支持按模块重建;重建产物与现有卡片 diff 后走 PR,人工可比对取舍。
- **前端呈现**:初始化以系统任务呈现于任务看板;基因组管理页显示每模块知识状态(已建/pending/低置信度)与校对清单。

### 6.3 开发数字员工(dev-employee)

DEVELOPING 态 Handler 派发 `code-develop` 工序,员工内部工作流(prompt 中固化):

1. 读 context 包(需求 + plan + 失败报告 + 基因组切片);
2. 在既有隔离 worktree(编排器已创建,分支 `task/<id>/<slug>`)内小步开发;
3. 自跑本模块测试(project-map 的 test_cmd)直至本地绿;
4. 写 `result.json`:变更文件清单、自测结果、**自评的变更影响面**(给集成判定做输入)。

随后编排器执行 unit-gate(§8);失败报告(结构化 + 末 200 行日志)注入下一轮 DEVELOPING 上下文。**修复循环由状态机控制而非员工内部 while**——每轮都是全新 Job,防止上下文膨胀与目标漂移。

**集成测试判定(needs_itest)** [MVP]:规则优先、Agent 兜底二级判定:

```
1) 机器规则:diff 路径/接口文件匹配 genome/rules/impact.yaml
   (触碰 interfaces.schema / migrations/ / 部署文件 / 跨 ≥2 个子模块 → 必判 true)
2) 未命中规则 → 用 dev result.json 的影响面自评 + plan.risk 交给架构员工做一次
   cheap judgment call(输出 needs_itest + 理由,存档)
```

### 6.4 集成测试数字员工(itest-employee)

| 步骤 | 实现 | [层级] |
|---|---|---|
| 环境准备 | `docker compose -f itest/compose.yaml up -d --wait`(项目自带编排文件);测试数据集 fixtures 由工序 scripts 灌注 | MVP |
| 构建启动 | 仅构建受影响模块(project-map depends_on 闭包) | MVP |
| 执行 | 接口测试(schemathesis/pytest)、E2E(标记 @e2e 的用例)、兼容性(合同测试,provider/consumer) | MVP(E2E)/FULL(合同) |
| 报告 | `itest-report.json`:{passed, failures[{case, logs_tail, repro_cmd, suspect_files[], suggestion}]} | MVP |
| 清理 | compose down -v;环境是牛不是宠物,失败也销毁,凭 repro_cmd 重现 | MVP |

失败时 `suspect_files + suggestion` 由 itest 员工基于日志与 diff 推理产出——这是回传给开发员工最有价值的三件套(日志/复现/建议)的落地形式。

### 6.5 员工直接对话(Session 交互平面)[MVP 咨询/质询,P1 结对]

**设计原则:对话是第一类交互平面,但变更入库永远走状态机。** 对话可以产生认知、草稿与结论,不能绕过门禁、安全检查与审批把变更合入主线。

#### 6.5.1 三种会话模式

| 模式 | 权限 | 典型场景 | 出口 |
|---|---|---|---|
| **咨询 consult** | 只读(代码 + 基因组) | "这个需求大概动哪些模块?风险在哪?""reserve-flow 的补偿逻辑是什么?" | 纯问答;或一键**转为任务**:对话记录蒸馏成需求描述 → 标准流水线 |
| **质询 inquiry** | 只读(预载指定任务全部产物/日志) | 审批前问执行员工"为什么这么改";复盘失败任务 | 结论可作为审批意见/驳回意见回注 |
| **结对 pair** | 可写(隔离 worktree,员工权限约束不变) | 用户想边聊边改、人为把控方向 | **本质是任务 DEVELOPING 态的交互式 Job**:结束后照走 unit-gate → 集测判定 → 安全提交 → 审批,全链路复用 |

结对模式的建模:`agctl submit --interactive` 或前端"结对开发"入口创建 `mode: interactive` 的任务;状态机不变,仅 DEVELOPING 态的 Job 由自主执行换为交互会话。人接管方向盘,车仍在轨道上。

#### 6.5.2 会话治理

- **上下文**:同一 ContextAssembler 路由(§3.2.2)——咨询按问题关键词命中知识卡片;质询预载任务产物;结对与自主 Job 同构。
- **记录**:会话创建/结束入事件面;完整对话与工具调用入日志面(sessions/<id>/*.jsonl);结对产物入版本面——三平面原则(§12.2)无例外。
- **预算与超时**:会话级 token 预算与空闲超时(默认 30 分钟)自动挂起;质询会话按次预算(便宜,只读推理)。
- **安全**:咨询/质询会话工具白名单为只读集(Read/Grep/Glob);结对会话沿用该员工 write_paths;对话中产生的"要不要改规则"类结论,仍须经架构员工 L2 流程落库。

#### 6.5.3 API 与前端

```
POST /sessions {employee, mode, task_id?}     创建会话
POST /sessions/{id}/messages                  发消息(SSE 流式回包)
POST /sessions/{id}/escalate                  咨询转任务(自动生成需求草稿)
```

前端入口:工作台快捷对话(选员工)、任务详情页「质询该任务」按钮、基因组管理页「问架构员工」、审批中心「质询后再审」;会话列表与回放归入活动流视图。

---

## 7. 工序(Procedure)工程体系

> 命名说明:本设计中编排器派发的作业契约称**工序(Procedure)**;"procedure" 一词专指 coding-agent 手艺层(craft)。对外(赛道评审)语境中,"工序体系 + craft 手艺库"合称即对应 "Procedure 工程体系"。

### 7.1 工序规范 [MVP]

```
genome/procedures/knowledge-init/
├── procedure.yaml       # ★ 契约层:何时调用、输入输出、失败处理(编排器的读者)
├── prompt.md            # 执行主指令(如需 Agent 参与)
├── scripts/             # 确定性脚本(能不用 Agent 就不用)
└── craft/               # ★ 手艺层:coding-agent skill 技艺包(员工的读者,见 §7.4)
    ├── codebase-survey/SKILL.md
    └── map-authoring/SKILL.md
```

```yaml
# procedure.yaml —— 评审 "Procedure 工程体系" 25% 权重的直接对应物
id: unit-gate
version: 1.2.0
summary: 运行单元测试/静态检查/构建并输出结构化结果
kind: deterministic        # deterministic | agentic | hybrid
trigger:                   # 调用条件(何时可被派发)
  states: [UNIT_TESTING]
inputs:
  schema:                  # JSON Schema
    module_id: {type: string}
outputs:
  artifacts: [gate-report.json]
  schema_ref: schemas/gate-report.schema.json
tools:
  required_cmds: [pytest, ruff, make]
  mcp: []                  # 需要的 MCP server(如 umodel)
failure:
  retry: 1                 # 基础重试(环境类失败)
  on_fail: back_to_developing   # 语义失败的状态机反应
  escalate_after: 3        # 连续失败 N 次升级人工
compat:
  runtimes: [claude-code, qwen-code, none]   # none=纯脚本
```

**设计要点**:`kind` 把工序分为确定性(纯脚本,如 gate)、Agent 型(如 code-develop)与混合型。原则:**验证类一律确定性,生成类才用 Agent**——可复现性与成本都更优。

### 7.2 注册、版本与分发

- 注册:编排器启动时扫描 `genome/procedures/`(项目级)与 `~/.agentgenome/procedures/`(全局级),项目级同名覆盖全局级。pydantic 校验 schema,非法工序拒绝加载并告警。[MVP]
- 版本:semver;Task 事件里记录每个 Job 使用的 procedure@version,报告可精确复现"当时是哪版工序干的活"。[MVP]
- 分发:全局基因库(§10.4)作为工序市场,`agctl genome pull procedure/<name>` 拉取模板。[FULL]

### 7.3 与 MCP 的关系

工序声明 `tools.mcp` 后,runtime 拉起员工时注入对应 MCP server 配置(如 UModel 的 AgentGateway)。工序是"何时做什么、成败如何界定"的治理层;MCP 是"能调用什么外部能力"的工具层——两者正交。[FULL]

### 7.4 契约层与手艺层:工序装载手艺的双层结构 [MVP]

工序(Procedure)与 coding agent(Claude Code 等)的 procedure 处于不同层面,设计上明确区分并**由前者装载后者**:

| | 契约层 = 工序(procedure.yaml + prompt.md) | 手艺层 = craft skill(craft/ 内的 SKILL.md) |
|---|---|---|
| 本质 | 作业契约:何时调用、输入输出 schema、失败处理、版本 | 技艺包:这类活怎么干得好(方法论/清单/避坑/脚本) |
| 读者 | 编排器 | 员工(Agent)自己 |
| 触发 | 状态机派发,确定性 | 员工按需渐进式加载,启发式 |
| 成败裁决 | schema 校验 + 门禁 | 无直接裁决,经结果指标间接体现 |
| 比喻 | 岗位工序卡 + 验收标准 | 员工的手艺书 |

**装载机制(craft mounting)**:运行时适配器负责把手艺物化到员工环境——

- ClaudeCodeRuntime:将本次工序的 `craft/` 与员工级通用 craft 复制为工作区 `.claude/skills/`,员工原生渐进式加载;
- 其他运行时:按其工序挂载形式转译,无此机制的运行时降级为 prompt 内联摘要。

手艺内容只写一份、运行时无关,可插拔承诺不因手艺层破坏。

**物化时机与细节(重要,防实现走偏)**:

- **每 Job 物化,而非初始化一次性拷贝**。`genome/procedures/` 是唯一事实源;worktree 内的 `.claude/skills/` 是派生视图,列入 .gitignore(workspace 初始化时唯一相关动作就是写入这条 ignore 规则)。
- **角色定制**:物化集合 = 本次工序的 craft/ + 该员工 employees/*.yaml 中 `crafts:` 声明的通用手艺——开发员工不会看到架构员工的手艺清单。
- **版本一致**:Job 事件记录的 procedure@version 与实际物化内容严格对应,审计可复现"当时带着哪版手艺干的活"。
- **防污染**:员工即使修改挂载副本,因其不入库且每次重新物化,篡改不会持久化;对 craft 的正当改进必须走 L3b 进化 PR 回 genome/。
- **会话同理**:咨询/质询/结对会话创建时物化一次(§6.5)。
- 采用复制而非软链:跨平台稳妥,craft 均为小文件,成本可忽略。

**双速率演进**:工序改动 = 接口变更(影响编排与审计),semver major/minor,必须人工审批、走慢通道;手艺改动 = 内容变更(只影响干得好不好),patch 版本,可由进化管道快速迭代(见 §10.1 L3 拆分)。生物学对应:基因型稳定,表达调控灵活。

**手艺写作规范**:一个 craft 只讲一门手艺;≤200 行;清单式、含反例;不与契约重复(不写输入输出定义)。**质量可测**:录制回放跑同一任务集,挂/不挂 craft 对比修复轮次与门禁一次通过率——手艺是内容,但按工程资产做 CI。

### 7.5 v1 手艺库(Craft Library)清单

优先级:P0 = 复赛前首批;P1 = 决赛前;P2 = 产品化。冷启动可裸奔(契约仅靠 prompt.md 运行),craft 是增强不是前置依赖;后续主要产出来源是失败驱动蒸馏(§10)与生态收编改造。

| 归属 | Craft | 一句话职责 | 优先级 |
|---|---|---|---|
| 通用 | genome-navigation | 基因树导航:如何按需检索/展开知识卡片而非全量阅读 | P0 |
| 通用 | rule-compliance | 提交前对照 rules 的自检清单 | P1 |
| 架构 | codebase-survey | 陌生代码库勘察法:从入口/依赖/热区三线建立认知 | P0 |
| 架构 | map-authoring | 项目地图与知识卡片撰写规范(预算/scope/置信度标注) | P0 |
| 架构 | module-boundary | 模块边界划分准则与常见误划反例 | P1 |
| 架构 | interface-extraction | 从代码提取接口契约的方法 | P1 |
| 架构 | experience-distill | 从任务证据蒸馏经验卡片:证据链、置信度、反例 | P1 |
| 开发 | failure-diagnosis | 失败诊断先于动手:读报告→定位→假设→验证的纪律 | P0 |
| 开发 | output-discipline | result.json 输出纪律与产物契约自检 | P0 |
| 开发 | small-step-dev | 小步提交:一个 commit 一个意图 | P1 |
| 开发 | test-first | 先补失败用例再修复 | P1 |
| 开发 | cross-module-safety | 跨模块改动的安全步骤(契约先行、双向验证) | P1 |
| 集测 | log-forensics | 日志取证与复现步骤撰写(三件套质量标准) | P0 |
| 集测 | env-compose | 集测环境编排与数据 fixtures 准备 | P1 |
| 集测 | contract-testing | provider/consumer 契约测试方法 | P2 |

**定位声明:craft 库不是产品配套,craft 库是基因组的种子内容**——它随全局基因库(§10.4)跨项目分发,是开源交付物中 "Procedure 工程体系"(工序 + 手艺)的主体部分。

---

## 8. 质量门禁(GateRunner)

### 8.1 配置与执行 [MVP]

每个子模块根放 `gates.yaml`(缺省时从 project-map 的 test_cmd/build_cmd 推导):

```yaml
gates:
  - {id: unit,     cmd: "pytest -q --junitxml=out/junit.xml", required: true}
  - {id: lint,     cmd: "ruff check .",                       required: true}
  - {id: build,    cmd: "make build",                         required: true}
  - {id: secrets,  cmd: "gitleaks detect --no-banner -v",     required: true}
  - {id: coverage, cmd: "pytest --cov --cov-fail-under=70",   required: false}  # [FULL]
```

GateRunner 由编排器直接执行(不经 Agent),输出统一 `gate-report.json`:

```json
{"task":"ag-...","module":"order-service","passed":false,
 "gates":[{"id":"unit","passed":false,"duration_s":41,
           "failures":[{"test":"test_reserve_timeout","message":"...","file":"...","line":123}],
           "log_tail":"(末 200 行)"}]}
```

失败报告注入下一轮开发上下文时,failures 结构化条目在前、log_tail 在后——引导员工先看断言差异而非通篇日志。

---

## 9. 安全与提交策略

### 9.1 隔离与权限 [MVP]

- 分支模型:`task/<id>/<slug>`;worktree 位于 `~/.agentgenome/worktrees/<task-id>/`,物理隔离于主 checkout。
- 主分支保护:仓库平台侧开启 protected branch,员工 Git 身份(`dev-bot@agentgenome`)无直接 push main 权限——平台强制,非约定。
- 凭证:员工进程环境仅注入其所需凭证(如只读 UModel token);Git push 凭证由编排器持有,员工产出 commit,推送由编排器执行。
- 受保护路径 `genome/rules/protected.yaml`:规则/CI 配置/密钥文件路径,dev 员工触碰即 Job 失败。

### 9.2 提交流水线 [MVP]

READY_TO_COMMIT 态由编排器顺序执行(确定性,不经 Agent):

```
1) 越权检查: diff 范围 ⊆ 员工 write_paths
2) 敏感信息: gitleaks detect(staged)
3) 风险评级: diff 命中 protected.yaml 的 high_risk 模式(migrations/、auth/、部署文件、
   删除量>500 行等)→ risk=high → 转 REVIEWING;否则 low → 直接 MERGING
4) 规范提交: Conventional Commits;body 含 task-id、变更摘要(dev result.json 生成)、
   Co-Authored-By: <employee>
5) PR/MR:  gh pr create,模板含验收标准勾选、门禁/集测报告链接、风险评级
```

### 9.3 审批与审计 [MVP]

- 审批:`agctl approve/reject`(或 REST/控制台按钮);reject 意见作为上下文回注 DEVELOPING。通知渠道 webhook(钉钉/飞书/Slack)可配。
- 审计:EventLog 全量记录(谁-何时-何态-何产物),`tasks/*/logs/` 保存 Agent 全程输出;`agctl audit <task-id>` 一键导出审计包。


---

## 10. 自进化机制设计(EvolutionPipeline)

### 10.1 触发与流水线 [MVP]

Task 进入终态(COMPLETED / ESCALATED)后触发。蒸馏作业本身以系统任务(`kind: evolution`)入队执行,产生与普通任务同构的事件、日志与产物,前端全程可见(§12.2):

```
收集素材 ──▶ 蒸馏(arch 员工) ──▶ 分级入库 ──▶ 走门禁与审批 ──▶ 基因组版本 +1
```

- **收集素材**:任务全事件流、失败-修复对(每轮 gate 失败报告 + 对应修复 diff)、集测报告、审批意见。ESCALATED 任务是最富矿——人类最终怎么修的,与 AI 的尝试差在哪。
- **蒸馏**(`experience-distill` 工序,agentic):输出候选卡片,每张必须含:适用条件、结论、证据链接(指向任务事件)、置信度。
- **分级入库**:

| 级别 | 内容 | 去向 | 审批 |
|---|---|---|---|
| L1 知识 | 模块认知修正、坑点、依赖事实 | knowledge/lessons/ + modules/*.md 增量 | 自动合并(走 lint 门禁) |
| L2 规则 | 新的边界/规范/impact 规则 | rules/ 增量 PR | **必须人工审批** |
| L3a 工序 | procedure.yaml / prompt.md 变更(接口级) | procedures/ 增量 PR | **必须人工审批**(慢通道)[FULL] |
| L3b 手艺 | craft/ 技艺包新增与改进(内容级) | procedures/*/craft/ 增量 PR | 回放回归验证通过即可合并(快通道)[FULL] |
| L4 全局 | 与项目无关的通用经验 | 全局基因库 PR | 基因库维护者审批 [FULL] |

### 10.2 防知识污染 [MVP]

- 所有蒸馏产物走 PR,diff 可见、可拒绝;
- 卡片强制带证据链接,无证据的"经验"直接拒收;
- knowledge 设容量预算(如 lessons ≤ 200 张),超限触发合并/淘汰(按被引用次数 LRU);
- 每张卡片带 `hits` 字段:被后续任务上下文命中且该任务成功,则 +1;长期 0 命中的卡片周期性归档——**知识也要接受自然选择**。

### 10.3 效果度量 [FULL]

进化是否真的发生,用指标说话(周报自动生成):平均修复轮次、门禁一次通过率、任务时长、ESCALATED 率、知识命中率。目标趋势:随任务量上升,前四项下降、末项上升。

### 10.4 全局基因库(Genome Registry)[FULL]

独立 Git 仓库 `genome-registry`:`templates/`(项目基因组骨架:Python 服务、前端单仓、多模块中台等)+ `procedures/`(通用工序与手艺)+ `lessons/`(跨项目经验)。命令:

```
agctl genome init --template python-multimodule   # 新项目继承基因
agctl genome pull procedure/contract-test         # 拉取工序
agctl genome contribute lessons/L-0042            # 项目经验上交(PR)
```

---

## 11. UModel 集成设计(生产经验层)[FULL]

### 11.1 建模映射

以 UModel Model Package 定义研发域词汇,把生产对象与基因组对象连成一张图:

```
Service(生产服务) ──deployed_from──▶ Module(project-map.modules)
Service ──exposes──▶ Interface(project-map.interfaces)
Alert(告警) ──on──▶ Service
Module ──lives_in──▶ Repo/Path
Task(AgentGenome 任务) ──changed──▶ Module@rev
```

同步作业(`umodel-sync` 工序,deterministic):project-map 变更或任务合并后,将 modules/interfaces/tasks 实体与拓扑写入 UModel EntityStore;生产侧 Service/Alert 实体由运维侧已有链路写入。

### 11.2 告警到代码的反向链路

```
生产告警 webhook ──▶ POST /webhooks/alert
  → 编排器经 UModel MCP 查询: .topo walk alert→service→module→recent tasks
  → 自动创建 FIX 任务(CREATED),plan 阶段注入:告警上下文 + 嫌疑模块 + 最近变更任务列表
  → 走标准状态机(开发→测试→提交),修复经验按 §10 沉淀,并回写 UModel(闭环)
```

价值:排障从"人读日志猜模块"变成"图谱定位 + 数字员工修复",生产经验成为第四层基因。

---

## 12. 可观测性设计

- **事件流** [MVP]:全局 `events.jsonl` + DB;事件含 task_id/job_id/procedure@ver/tokens/duration。任务级 trace:`agctl trace <id>` 输出甘特式时间线。
- **指标** [MVP 简版]:`/metrics`(Prometheus 格式):任务数按状态、门禁通过率、平均修复轮次、token 消耗。
- **报告** [MVP]:任务终态生成 `report.md`(人类可读):需求→做了什么→几轮修复→证据链接,直接可贴进 PR 描述。
- **Web 控制台**:见 §12.1,核心三视图升级为 [MVP]。

### 12.1 前端(Web 应用)设计 —— 生产级

> 定位原则:AgentGenome 是面向生产落地的产品,前端按"企业团队日常使用、多角色协作、长期演进"设计;竞赛演示只是 P0 能力的一个使用场景,不反向决定选型。

#### 12.1.1 角色与使用场景

| 角色 | 核心诉求 | 高频页面 |
|---|---|---|
| 需求方(产品/业务/测试) | 低门槛提需求、追踪进度、验收 | 需求提交、任务详情 |
| 开发/架构师 | 看清员工做了什么、干预与接管、维护基因组 | 任务详情、基因组管理 |
| 审批人(TL/架构师) | 快速做出高质量审批决策 | 审批中心 |
| 平台管理员 | 员工/预算/集成配置、成本与审计 | 系统设置、观测中心 |

CLI(agctl)定位下沉为工程师工具与 CI 集成口;Web 与 CLI 同走一套 REST API,无私有通道。

#### 12.1.2 技术选型(生产基线)

| 层 | 选型 | 理由 |
|---|---|---|
| 框架 | **React 18 + TypeScript(strict)+ Vite** | 人才供给与组件生态最大;TS 契约与后端 schema 对齐(Vue3+Element Plus 为等价替代,团队栈决定) |
| 组件库 | **Ant Design 5** | 企业中后台场景成熟度最高;Token 体系支持主题/暗色模式 |
| 服务端状态 | **TanStack Query** | 缓存/重试/失效语义与"状态即事实"后端天然契合;UI 态用 Zustand,不引入重型全局 store |
| API 契约 | **OpenAPI → 生成 TS client**(openapi-typescript) | 契约先行:FastAPI 自动导出 OpenAPI,前后端类型同源,接口变更编译期暴露 |
| 实时 | **SSE + 事件总线**(见 12.1.5) | 单向推送为主的场景 SSE 运维成本低于 WebSocket;订阅通道抽象,必要时可替换 |
| 专业组件 | diff:react-diff-view / Monaco;日志:虚拟滚动 log viewer;依赖图:AntV X6/G6;图表:ECharts | 任务详情/审批/基因组三大场景的体验决定产品成败,不自造轮子 |

#### 12.1.3 信息架构(IA)与页面能力

```
工作台 /                    我的任务 · 待我审批 · 系统健康 · 快捷提需求
需求管理 /requirements      提交表单(模板化) · 需求列表 · 外部导入(Jira/Issue 链接、IM 机器人)[P1]
任务中心 /tasks             看板(状态机泳道,实时流动)+ 列表(筛选/检索)
  └ 任务详情 /tasks/{id}    状态时间线 · Job 实时日志流 · 变更 diff · 产物/报告 ·
                            PR 链接 · token 成本 · 干预操作(取消/重试/接管)
审批中心 /approvals         队列 · 代码 diff 渲染 · 门禁/集测报告 · 风险评级 ·
                            行级批注 → 驳回意见结构化回注 [批注 P1]
基因组管理 /genome          project-map 依赖图可视化 · 知识卡片库(检索/命中数/生命周期) ·
                            规则管理(变更走审批流) · 工序注册表(版本/使用统计)[P1]
观测中心 /insights          进化指标趋势(修复轮次/一次通过率/时长/知识命中) · 成本看板 ·
                            审计日志检索 [P1]
系统设置 /settings          员工与运行时配置 · 预算与并发 · 集成(Git 平台/IM/UModel) ·
                            用户与角色 [P1-P2]
多工作区                     顶栏 workspace 切换器,所有页面按 workspace 隔离 [P2]
```

#### 12.1.4 分期(生产路线)

| 期 | 范围 | 备注 |
|---|---|---|
| **P0 核心闭环** | 工作台、需求提交、任务看板/详情(日志流+diff+报告)、审批中心(基础) | 生产可用的最小面;复赛演示即 P0 |
| **P1 治理与进化** | 基因组管理、观测中心、审批批注、需求外部导入、IM 通知深度集成 | 决赛前后 |
| **P2 规模化** | 多 workspace/多租户、SSO/RBAC 完整版、UModel 告警任务入口、AgentTeams 托管适配 | 产品化阶段 |

#### 12.1.5 实时与数据层

- 后端事件总线:编排器事件写入 **Redis Streams**(单机部署可降级为进程内队列,接口不变);API 层 `GET /events/stream` 按 workspace/task 维度过滤后以 SSE 推送。多实例水平扩展时前端无感。
- 前端策略:SSE 只做"失效通知 + 增量事件",页面数据以 TanStack Query 拉取为准(推拉结合,断线重连后自动补齐,不依赖推送可靠性)。
- 日志流:Job 运行中 tail 其 jsonl 经 SSE 推送;历史日志走分页 REST,前端虚拟滚动,百万行不卡。

#### 12.1.6 认证与权限

- 认证:**OIDC 单点登录**(企业 IdP / 飞书 / 企业微信扫码);本地部署提供内置账号模式兜底。
- 授权:RBAC 四角色(requester / developer / approver / admin)+ 资源维度(workspace);路由级 + 操作级双重校验,**审批操作服务端二次校验审批人身份**,前端权限只做展示裁剪不做安全边界。
- 审计:所有写操作带操作人身份入 EventLog,与任务审计同池检索。

#### 12.1.7 前端工程化(自举)

ESLint + Prettier + Vitest + Playwright(E2E 跑 P0 主流程)+ Storybook(专业组件);CI 门禁与后端同标准。**前端仓库本身作为一个子模块接入 AgentGenome 管理——用自己研发自己,是最好的生产验证与演示素材。**

#### 12.1.8 部署

静态构建产物经 nginx/CDN 分发,与编排器独立发版;`/api/version` 做前后端契约版本对齐检查;FULL 特性(UModel、多租户)以特性开关控制,灰度可控。

#### 12.1.9 对话交互设计(Chat UX)—— 对话前端是采用率的地基

> 设计立场:这不是"聊天窗口",而是**对话驱动的工作台**。员工回答中流动的是领域对象,不是纯文本。

**a. 消息即块流(Block Stream)** [MVP]

会话消息由类型化块组成,前端以**块渲染器注册表**渲染,与任务详情页共用同一套组件(DiffView/LogViewer/CardView):

| 块类型 | 渲染 | 交互 |
|---|---|---|
| text / code | Markdown / 高亮代码 | 复制 |
| card-ref | 知识卡片摘要卡 | 展开全文;跳基因组页 |
| file-ref | 文件路径徽章 | 点击开源码只读视图(定位到行) |
| diff | 语法高亮 diff | 展开/折叠;结对模式实时更新 |
| tool-step | "正在读 xxx / 正在跑 xxx"可折叠步骤条 | 展开看完整调用与输出 |
| task-card | 任务卡(状态/轮次/风险) | 点进任务详情 |
| gate-report | 门禁/集测结果摘要 | 展开失败明细 |
| action | 按钮组(转为任务/质询/展开更多知识) | 触发对应流程 |

块协议来自运行时 stream-json 事件的映射,后端不为前端造格式——**工具调用过程可视化是信任的来源**:用户看见员工真的在查证,而非凭空作答。

**b. 上下文条(Context Bar)** [MVP]

对话顶部常驻:当前会话已加载的知识卡片、任务产物、规则片段以 chip 呈现;可展开预览、可钉住(pin,防截断)、可移除。"员工带着什么在回答"全程透明——基因组叙事在 UI 上的直接呈现。

**c. 三布局对应三模式**

| 布局 | 场景 | 结构 |
|---|---|---|
| 全局对话抽屉 [MVP] | 随处可问(⌘K 唤起) | 侧滑面板;**自动携带当前页面上下文**:任务页唤起=质询该任务,基因组页唤起=问架构员工 |
| /chat 工作台 [MVP] | 深度咨询 | 左:会话列表(按员工/模式筛选) 中:对话流 右:证据面板(被引用卡片/文件聚合) |
| 结对三栏 [P1] | pair 模式 | 左:对话 中:文件树+代码只读视图 右:实时 diff + 门禁状态;底部一键"提交进流水线" |

**d. 对话闭环回系统** [MVP]

- **转为任务**:内联任务草稿卡(标题/需求/模块预填,可编辑)→ 确认即建任务,不跳页断流;
- **质询回注**:质询结论一键作为审批意见/驳回意见;
- **反馈进化**:每条回答带 有用/没用 反馈,命中的知识卡片 hits 随之记账(§10.2)——**对话本身成为知识自然选择的输入源**;
- 会话可分享(只读链接)、可回放,归档入活动流。

**e. 流式与状态工程** [MVP]

- 前端会话状态机:idle → sending → streaming → tool-running → done/error,可中断(stop)可重试;
- SSE 断线重连后按消息序号补齐;消息列表虚拟滚动;草稿自动保存;
- 发送采用乐观更新;错误块内联呈现(含重试),不弹全局 toast 打断心流。

### 12.2 全链路操作记录:三平面模型 [MVP]

**原则:缺口可检测。** 每次操作至少落在三个记录平面之一,平面之间对不上的地方**能被列出来**(见 `security.gaps`)。

这句话原先写的是"系统内不存在无记录的操作"。**那是一句兑现不了的话**——员工在隔离工作区里删掉一个临时文件、有人手动重跑一次门禁、运维直接推一个提交改配置,总有操作只落在一个平面上或者一个都不落。写一个自己都做不到的绝对承诺,后果是没人会当真:第一次发现有操作没被记录时,大家的反应不会是"这是个 bug",而是"哦这句话本来就是说说的"。绕过界面直接改仓库是合法的运维手段,不拦——但要查得到。

操作尽可能落在三个记录平面之一,并以 task_id / job_id 互相关联:

| 平面 | 载体 | 记录内容 | 回答的问题 |
|---|---|---|---|
| 事件面 | EventLog(DB + events.jsonl) | 状态迁移、Job 起止、门禁/集测结果、审批动作、人工干预、配置变更 | 什么时候发生了什么 |
| 日志面 | tasks/*/logs/*.jsonl(Agent stream 全量) | 员工每次工具调用、文件编辑、命令执行及其输出 | 员工具体怎么干的 |
| 版本面 | Git(workspace + 子仓) | 代码变更、知识/规则/工序的每次修改(commit/PR/diff/作者) | 资产变成了什么样 |

补强规定(堵住两个缺口):

1. **进化作业也是任务**:知识蒸馏/更新以系统任务(`kind: evolution`)入队,产生与普通任务同构的事件与日志——"知识是怎么变的"与"代码是怎么变的"同等可见、可审计。
2. **配置变更入事件面**:通过前端/CLI 修改预算、审批人等配置,除 Git 提交外必须写配置变更事件(操作人的真人身份 / 发起入口 / 被改的段 / 结果提交的 sha)。**不含前值与后值**——内容以版本面为准:前端读到配置到人按下保存之间,配置可能已被别人改过,那时记下的"前值"是前端读到的旧值而不是真实的前值,而它看起来跟真的一模一样。git 的 diff 不会犯这个错。
3. **员工定义暂时是已知盲区**:改员工定义目前只能走 Git,系统里没有会为它写事件的入口,所以缺口检测不比对 `employees/`——但会在报告里说明"这条路径上有 N 个提交,未比对"。把一个已知盲区摆在明面上,好过让"报告里没有"与"那里没事发生"分不开。给它补一个带事件的写入路径是后续的事。

**前端可见性映射**:

| 记录 | 前端呈现位置 |
|---|---|
| 任务全程(事件+日志+diff) | 任务详情页:状态时间线 / Job 日志流 / 变更 diff |
| 知识/规则/工序变更 | 基因组管理页:版本历史、变更 diff、来源任务链接(哪个任务的经验触发了这次更新) |
| 进化作业 | 任务看板中以 evolution 类型卡片呈现,可点开同款详情页 |
| 审批与人工干预 | 审批中心记录页 + 任务时间线内嵌 |
| 全局视角 | **活动流视图 /activity [P1]**:三平面汇聚的统一时间线,按 workspace/人/员工/类型过滤,审计检索的入口 |

---

## 13. 部署与配置

### 13.1 运行形态

| 形态 | 说明 | [层级] |
|---|---|---|
| 单机模式 | `pipx install agentgenome && agctl serve`,SQLite + 本地 worktree,适合个人/演示 | MVP |
| 服务模式 | docker compose:orchestrator + 控制台 + UModel(可选);多项目多 workspace | FULL |
| 平台托管 | 编排器注册为 AgentTeams 的 Worker 集群,复用其 RBAC/监控/成本归属 | FULL |

### 13.2 依赖清单 [MVP]

Python 3.11+;git ≥ 2.40(worktree);gh 或 glab CLI;docker + compose(集测);gitleaks;CLI Agent 运行时(claude-code 或 qwen-code)及其 API 凭证。

### 13.3 agentgenome.yaml(根配置)[MVP]

```yaml
runtime:
  default: claude-code
  claude-code: {cmd: claude, max_turns: 40}
  qwen-code:   {cmd: qwen,   max_turns: 40}
concurrency: {global_jobs: 3}
budgets: {per_task_tokens: 1500000, per_job_tokens: 300000}
limits:  {max_fix_rounds: 3, job_timeout_s: 1800}
approval:
  approvers: [xiao@example.com]
  notify: {webhook: "https://open.feishu.cn/..."}
platform:
  git_host: github            # github | gitlab | gitea
  protected_branch: main
umodel: {enabled: false, gateway: "http://localhost:8080", workspace: mall}   # [FULL]
```

---

## 14. 系统自身的测试与验收

- **单元层**:状态机迁移表全路径测试;ContextAssembler 预算裁剪测试;权限校验(越权 diff 判失败)测试。
- **集成层**:以一个固定的双模块示例仓(`examples/mall/`)做端到端夹具,mock AgentRuntime(录制回放模式:离线回放真实 Agent 输出,CI 不烧 token)。
- **验收场景**(复赛 Demo 即验收用例):
  1. 单模块需求 → 全绿直通 → 自动 PR(无人工介入);
  2. 注入一个必失败测试 → 观察 2 轮自动修复 → 通过;
  3. 跨模块接口变更 → 触发集成测试 → 失败回传 → 修复 → 高风险判定 → 人工审批 → 合并;
  4. 任务完成后 → 知识 PR 生成,project-map version +1;
  5. 修复上限逼出 ESCALATED → 人工接管路径可用。

---

## 15. 演进路线与风险

### 15.1 里程碑

| 里程碑 | 截止 | 范围 |
|---|---|---|
| M1 复赛 Demo | 9.03 | 全部 [MVP] 项:三员工全链路、状态机、门禁、安全提交、L1 自进化、前端 P0 核心闭环、示例仓演示 |
| M2 决赛 | 9.22 | 控制台、任务并行、L2/L3 进化、UModel 反向链路(演示级)、开源发布(Apache-2.0) |
| M3 产品化 | 赛后 | 全局基因库、AgentTeams 托管、多运行时成熟度、VersionedWorkspace 第二实现 |

### 15.2 主要风险与对策

| 风险 | 对策 |
|---|---|
| Agent 输出不满足产物契约 | result.json schema 校验 + 一次自动重试 + 失败即 Job 失败(不带病前进) |
| 修复循环震荡(改 A 坏 B) | 每轮全新 Job + 失败历史全量注入 + 上限逼停;gate 报告含回归对比 |
| 知识污染 / 规则错误沉淀 | §10.2 四道防线;规则层永远人工审批 |
| token 成本失控 | 任务/Job 双层预算,超限 ESCALATED;验证类一律走确定性脚本 |
| 提示注入(恶意 issue/代码注释) | 员工默认断网、工具白名单、上下文来源标注、提交前双重扫描 |
| 演示环境不稳定 | 集测环境容器化 + 录制回放兜底;Demo 脚本化(agctl demo run) |

---

## 附录 A:状态机完整迁移表

| # | From | Event | Guard | To | Side-effects |
|---|---|---|---|---|---|
| 1 | CREATED | plan_done | plan.yaml 有效 | DEVELOPING | 建分支/worktree;派发 dev |
| 2 | CREATED | plan_failed | retry<1 | CREATED | 重试 plan |
| 3 | CREATED | plan_failed | retry≥1 | ESCALATED | 通知 |
| 4 | DEVELOPING | dev_done | result.json 有效 | UNIT_TESTING | 派发 GateRunner |
| 5 | UNIT_TESTING | gate_pass | needs_itest=true | INTEGRATION_TESTING | 派发 itest |
| 6 | UNIT_TESTING | gate_pass | needs_itest=false | READY_TO_COMMIT | 提交流水线 |
| 7 | UNIT_TESTING | gate_fail | fix_rounds<max | DEVELOPING | fix_rounds+1;注入报告 |
| 8 | UNIT_TESTING | gate_fail | fix_rounds≥max | ESCALATED | 冻结分支;通知 |
| 9 | INTEGRATION_TESTING | itest_pass | — | READY_TO_COMMIT | 归档报告 |
| 10 | INTEGRATION_TESTING | itest_fail | fix_rounds<max | DEVELOPING | 注入三件套 |
| 11 | INTEGRATION_TESTING | itest_fail | fix_rounds≥max | ESCALATED | 同 8 |
| 12 | READY_TO_COMMIT | risk_high | — | REVIEWING | 通知审批人 |
| 13 | READY_TO_COMMIT | risk_low | 检查全过 | MERGING | 创建 PR;auto-merge |
| 14 | READY_TO_COMMIT | precheck_fail | — | DEVELOPING | 越权/泄密报告注入 |
| 15 | REVIEWING | approve | 审批人合法 | MERGING | 记录审批 |
| 16 | REVIEWING | reject | — | DEVELOPING | 意见注入 |
| 17 | MERGING | merged | — | COMPLETED | 触发 Evolution;清理 worktree |
| 18 | MERGING | merge_conflict | — | DEVELOPING | rebase 指令注入 |
| 19 | * | cancel(人工) | — | CANCELLED | 清理 |

## 附录 B:gate-report / itest-report / result.json 的 JSON Schema 要点

三份 schema 放入 `agentgenome/schemas/`,共同约束:顶层必含 `task_id, producer, created_at, passed(bool)`;失败必含 `failures[]`,每条含 `message + evidence(file/line 或 log_tail)`;所有路径为 workspace 相对路径。完整 schema 随代码仓发布。

## 附录 C:员工系统提示词骨架(以 dev 为例)

```
你是 AgentGenome 的开发数字员工,在隔离工作区内完成编码任务。
铁律:
1. 只修改 write_paths 允许的路径;禁止触碰 genome/rules 与 CI 配置。
2. 开工前先读上下文包中的失败报告(如有)——先诊断,后动手。
3. 遵循 rules/coding.md;不确定的设计决策,在 result.json 的 questions 字段提出而非擅自决定。
4. 小步提交,每个 commit 一个意图。
5. 结束前必须:跑通本模块 test_cmd;写 result.json(schema 见 …);不要提交任何密钥。
你的输出不是给人看的对话,而是产物文件——一切以文件为准。
```

---

*文档结束。配套物:examples/mall 示例仓、schemas/、复赛 Demo 脚本,随 M1 交付。*
