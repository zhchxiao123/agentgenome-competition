# AgentGenome · 研发基因组 —— 详细设计文档

> 版本化原生的自进化研发协同底座
> 版本:v0.9(完整系统设计) | 2026-08 | 鲸智百应
> 技术基线:Python 3.11+ 编排器 · CLI Agent 数字员工运行时(Claude Code / Qwen Code 可插拔)

---

## 0. 阅读指南与术语

| 术语 | 含义 |
|---|---|
| **Workspace(项目空间)** | 以 Git 仓库承载的协作根空间,含子模块、基因组目录与运行时目录 |
| **Genome(研发基因组)** | 项目的知识(knowledge)、规则(rules)、技能(procedures)三类可版本化资产的统称 |
| **数字员工(Employee)** | 一个"CLI Agent 运行时 + 角色系统提示词 + Procedure 集 + 权限配置"的组合 |
| **Task(任务)** | 一个用户需求对应的任务实例,由状态机驱动其生命周期 |
| **Job(作业)** | 编排器派发给某个员工的一次具体执行(员工 × Procedure × 上下文) |
| **Procedure** | 标准化能力单元:声明调用条件、输入输出、依赖工具与失败处理 |
| **Gate(门禁)** | 单测、静态检查、构建、安全扫描等质量关卡的统一执行器 |
| **Artifact(产物)** | Job 产出的结构化文件(报告、补丁、日志),经产物总线在员工间传递 |

标注约定:各节中 **[MVP]** 表示复赛最小可运行集必须实现;**[FULL]** 表示完整版能力,可在决赛及以后交付。

---

## 1. 设计目标与原则

### 1.1 目标

1. 一条命令提交需求,系统自动完成 架构准备 → 开发 → 质量验证 → 提交 的全流程,人只在审批点介入。
2. 每次任务执行后,项目基因组(知识/规则/技能)得到增量更新——系统越用越强。
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
│  Workspace(Git)   genome/(knowledge·rules·procedures)   tasks/(运行态)   repos/(子模块) │  空间层
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
│   └── procedures/                     #   技能层(项目级 Procedure,可覆盖全局)
│       └── <procedure-name>/procedure.yaml + prompt.md + scripts/
├── employees/                      # 员工定义(见 §6.1)
│   ├── arch.yaml  dev.yaml  itest.yaml
│   └── prompts/arch.md  dev.md  itest.md
├── scripts/                        # 公共脚本(初始化、门禁包装等)
├── tasks/                          # 运行态(gitignore,只归档不提交)
│   └── ag-20260901-001/            #   单任务目录(见 §4.4)
└── repos/                          # 业务仓库挂载根(Git 子模块)
    ├── order-service/              #   挂载点 = 仓库名,不是位置编号
    └── inventory-service/
```

设计取舍:

- **workspace 是纯协作仓,不含业务代码**:workspace 仓自身只版本化 genome/、employees/、scripts/ 与配置;`repos/<仓库名>/` 是 Git 子模块,workspace 中仅存指向业务仓某 commit 的指针(gitlink)。业务仓零改造接入,退出 AgentGenome 也不留痕——这是"现有仓库即插即用"的实现基础。
- **挂载点是仓库名,不是位置编号**:路径本身就是身份线索。人在 diff、失败报告、PR 里读到 `repos/order-service/src/reserve.py` 不必回头查根索引;**员工受益更大**——路径名是免费的上下文,而根索引恰恰是上下文预算紧张时最先被裁掉的东西。挂载点由仓库名净化而来(转小写、非白名单字符折成 `-`、规避平台保留名),同名仓依次拿 `-2`、`-3`,并且**挂载时冻结**:上游改名不影响挂载点,因而知识卡片的覆盖范围、门禁配置、diff 基线都不用动。`repos/` 这个父目录名是权限模型的支点(`repos/**` 就是"全部业务代码"),不做成可配置项;单仓项目也照样多这一层,否则权限 glob 会分叉成单仓一套、多仓一套。
- **旧 Workspace 不做自动迁移**:没有升级命令,重新 `agctl init` 即可。业务仓零改造,所以重建的成本只是一次挂载。
- **genome 与 workspace 同仓**:保证"子模块指针前移 + 知识变更"出现在同一个 workspace 提交/PR 里,评审者一次看全。跨项目复用通过全局基因库(§10.4)解决,不靠拆仓。
- **tasks/ 不入库**:运行态高频写入,入库会污染历史;任务结束后归档摘要(manifest + 报告)到 `genome/knowledge/lessons/` 由进化管道决定去留。

### 3.1.0 从零开始:绿地项目怎么起步 [MVP]

本文其余部分默认的是**存量项目**——挂上已有的仓、读懂它、再改它。绿地项目走的是同一条路,
只是起点不同,这里把它写清楚,免得每个人自己撞一遍。

**一、必须先有一个业务仓,且它至少要有一个提交。**

Workspace 是纯协作仓、本身不含业务代码,所以"什么都没有"是挂不上的——`agctl init` 至少要一个
`--repo`。而一个**零提交**的仓(刚在托管平台点了新建)同样挂不上:`git submodule add` 对
没有任何 commit 的远端会失败。给它一个初始提交(一个 README 就够)再挂:

```bash
agctl init my-workspace --repo https://github.com/org/new-app.git
#   new-app -> repos/new-app/
```

挂不上时命令会说清是哪个仓、为什么、下一步做什么,并且**不留痕**——目标目录回到执行之前的
样子,改对地址原样重跑即可。

**二、空仓是合法状态,知识地图此时是"平凡完备"的。**

只有一个 README 的仓照样通过 `agctl genome validate`:`1 个模块,0 个功能点`。这里的**完备**
指的是"地图上没有未知区域"——零个功能点,也就没有任何未被覆盖的功能点。

**这不违反 ADR-0003。** 那条 ADR 否掉的是"按任务历史懒生成"(它会让"系统对某个模块懂多少"
变成任务历史的函数,两个内容相同的需求先提的那个拿到的上下文更差),不是"项目还没有代码时
地图是空的"。前者的覆盖范围不可预测,后者的覆盖范围是**完整的零**。

还没 checkout 出来的挂载点与"已挂载但还没有代码"的仓在闸门上说的是不同的话:前者要求你先跑
`git submodule update --init`(边界规划会当场停下,不会让你对着空目录划模块),后者照常进草案,
标注"已挂载,还没有代码"。

**三、代码长出来之后,让知识跟上。**

写完一批代码之后,按模块重建那个模块的认知:

```bash
agctl genome reinit --module new-app
```

它跳过扫描、划分与闸门(边界已经拍过板了),直接重读这个模块并重写它的模块地图与功能卡片。

> **注意:** 设计文档 §6.2 的工序表里列了一个 `knowledge-update`(「代码变了,卡片要跟上」),
> **它目前尚未实现**——没有对应的工序资产,命令行上也没有入口。绿地项目让知识跟上的机制
> 就是上面这条按模块重建。这一条写在这里是因为照着一个不存在的用法敲一遍的代价,比不写更大。
>
> **它还留下一处待还的账:** ADR-0003 否掉懒生成时,给出的替代路径正是这个工序——
> 原文说「代码变了,卡片要跟上」与「卡片本来就缺,要补上」是同一件事,走同一条经过门禁与
> 架构员工的路径即可,**不需要第三条绕过门禁的写入路径**。那条路径目前由 `genome reinit
> --module` 顶着:它同样经过门禁与架构员工,所以 ADR 的实质约束没有被破坏;但 ADR 点名的
> 那个工序不存在,读 ADR 的人会去找一个找不到的东西。要么实现它,要么修订 ADR-0003 的措辞
> ——本节只负责把这笔账记下来,不替它做决定。

### 3.1.1 双层提交拓扑 [MVP]

双仓结构决定了一个任务的变更落在两层:

```
任务 ag-...-001(跨 order/inventory 两模块)
├── 子仓层:repos/order-service 上分支 task/ag-...-001 → PR#12(业务代码变更)
│          repos/inventory-service 上分支 task/ag-...-001 → PR#7 (业务代码变更)
└── 顶层:  workspace 上分支 task/ag-...-001 → PR#33
            (内容 = 两个子模块指针前移 + genome 知识增量)
```

约定与保障:

- **分支同名**:任务在 workspace 与所有受影响子仓使用同名分支,便于追踪与清理。
- **合并顺序**:先合并全部子仓 PR → 编排器更新 workspace 分支的子模块指针指向合并后 commit → 最后合并 workspace PR。workspace PR 是任务完成的"原子提交点":它合并之前,主 workspace 的指针仍指向旧版本,任何时刻 clone 顶层仓都是一致状态。
- **原子性边界**:跨模块任务的多个子仓 PR 之间不保证平台级原子合并(Git 平台不支持跨仓事务);以"子仓先全绿全并、顶层指针最后统一前移"逼近原子语义,且 REVIEWING 审批针对的是顶层 PR——审批人看到的是任务全貌。
- **门禁与集测在哪层跑**:单元门禁在各子仓 worktree 内跑;集成测试按 workspace 分支的指针组合拉起环境——测的是"这组指针在一起"是否成立,这正是顶层仓存在的意义。
- **回滚**:revert 顶层 workspace 的一个指针提交即回滚整个任务的组合状态,子仓历史无需改写。

### 3.2 项目地图 project-map.yaml [MVP]

项目地图是员工"带着认知上岗"的入口,结构化优先、正文引用 Markdown:

```yaml
version: 3                       # 每次知识更新自增
updated_at: 2026-09-01T10:00:00Z
project:
  name: example-mall
  summary: 电商中台,订单/库存双子模块
modules:
  - id: order-service            # 与子模块/目录对应
    path: repos/order-service/
    lang: python
    summary: 订单域,依赖 inventory 的预占接口
    entrypoints: [src/order/app.py]
    test_cmd: "pytest -q"
    build_cmd: "make build"
    depends_on: [inventory-service]
    doc: genome/knowledge/modules/order-service.md
  - id: inventory-service
    path: repos/inventory-service/
    ...
interfaces:                      # 跨模块契约(集成测试触发的重要依据)
  - id: reserve-api
    kind: http
    provider: inventory-service
    consumers: [order-service]
    schema: repos/inventory-service/api/reserve.yaml
datastores:
  - id: order-db
    kind: postgres
    owner: order-service
    migrations: repos/order-service/migrations/
```

**约束**:employees 只读 project-map 与其 doc 引用;写入必须通过架构员工的知识更新 Procedure(见 §6.2),防止认知漂移。

### 3.3 规则文件的机器可读块 [MVP]

规则文档面向人书写,但嵌入 YAML front-block 供编排器与门禁消费:

```markdown
<!-- rules/architecture.md -->
```rules
forbidden_deps:
  - from: repos/inventory-service/**   # 库存不得反向依赖订单
    to: repos/order-service/**
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

CREATED 态的 plan Handler 由架构员工执行 `requirement-analysis` Procedure:输出 `plan.yaml`(涉及模块、是否跨模块、验收标准、预估风险)。**[MVP] 简化**:单任务不拆子任务,plan 仅做模块定位与验收标准提取;**[FULL]**:支持拆分为带依赖 DAG 的子任务集,Scheduler 按依赖并行派发。

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
│   ├── procedures.py         # Procedure 注册表与校验
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
    procedure: ProcedureSpec              # 本次执行的 Procedure
    workdir: Path                 # 隔离工作区(worktree 根)
    context_file: Path            # 组装好的上下文包(markdown)
    output_dir: Path              # 产物目录
    timeout_s: int
    max_tokens: int
```

`ClaudeCodeRuntime` 实现要点:

- 以 headless 方式拉起:`claude -p "$(cat context.md)" --output-format stream-json --max-turns N`,`cwd=workdir`;
- 工具白名单由 EmployeeConfig 映射为 `--tools`（精确限制可见工具），黑名单仍作纵深防御;
- stream-json 逐行落盘 `logs/job-*.jsonl`,同时提取 token 用量计入预算;
- 结果契约:Claude 以 JSON Schema 约束的结构化输出返回结果，runtime 校验后原子写入 `output_dir/result.json`；普通 Job 不因小票问题暗中重跑完整进程，只有 staging 增量协议可显式补交一次。

`QwenCodeRuntime` 同构([FULL],参数映射不同)。**运行时选择**:employees/*.yaml 中 `runtime: claude-code` 字段决定,可按员工混配。

### 5.4 ContextAssembler(上下文组装器)[MVP]

每个 Job 的上下文包按固定骨架拼装,并做 token 预算裁剪(超预算时按优先级 4→1 截断):

1. 角色系统提示词(employees/prompts/*.md)
2. 本次 Procedure 的 prompt.md + 输入参数
3. 任务上下文:需求原文、plan.yaml、**上一轮失败报告(如有,置顶)**
4. 基因组切片:project-map 相关模块条目 + 其 doc + 命中规则(按 plan 中模块过滤,而非全量塞入)

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
procedures:                         # 允许调用的 Procedure(白名单)
  - branch-worktree
  - code-develop
  - unit-gate
tools:
  allow: [Bash, Read, Write, Edit, Grep, Glob]
  deny:  [WebFetch]             # 开发员工默认断网,防提示注入外联
permissions:
  write_paths: ["repos/**", "tasks/{task_id}/**"]    # 只可写业务码与本任务目录
  forbid_paths: ["genome/rules/**", ".github/**"]     # 规则只有架构员工可动
limits:
  job_timeout_s: 1800
  max_tokens_per_job: 200000
```

**权限落地方式**:write_paths/forbid_paths 由两道机制兜底——(1)Job 结束后编排器 `git status` 校验改动范围,越权改动直接判 Job 失败并回滚工作区;(2)提交前安全检查再验一次(§9.2)。不依赖员工"自觉"。

### 6.2 架构设计数字员工(arch-employee)

| Procedure | 触发 | 输入 | 输出(产物契约) | [层级] |
|---|---|---|---|---|
| `workspace-init` | agctl init | 仓库清单/脚手架参数 | workspace 骨架 + .gitmodules + genome 空骨架 | MVP |
| `knowledge-init` | init 后自动 | 全部子模块代码 | project-map.yaml + modules/*.md 初版 | MVP |
| `requirement-analysis` | 任务 CREATED | 需求原文 + project-map | plan.yaml(模块定位/验收标准/风险预判) | MVP |
| `knowledge-update` | 任务 COMPLETED / 代码合并后 | 本次 diff + 任务报告 | 知识增量 PR(map version+1) | MVP |
| `rule-distill` | EvolutionPipeline 判定有规则级经验 | lessons 候选 | rules 增量 PR(必须人工审批) | FULL |

提示词要点(prompts/arch.md):只读优先、输出结构化、"不确定的认知标注 confidence 字段而非编造"。

### 6.3 开发数字员工(dev-employee)

DEVELOPING 态 Handler 派发 `code-develop` Procedure,员工内部工作流(prompt 中固化):

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
| 环境准备 | `docker compose -f itest/compose.yaml up -d --wait`(项目自带编排文件);测试数据集 fixtures 由 Procedure scripts 灌注 | MVP |
| 构建启动 | 仅构建受影响模块(project-map depends_on 闭包) | MVP |
| 执行 | 接口测试(schemathesis/pytest)、E2E(标记 @e2e 的用例)、兼容性(合同测试,provider/consumer) | MVP(E2E)/FULL(合同) |
| 报告 | `itest-report.json`:{passed, failures[{case, logs_tail, repro_cmd, suspect_files[], suggestion}]} | MVP |
| 清理 | compose down -v;环境是牛不是宠物,失败也销毁,凭 repro_cmd 重现 | MVP |

失败时 `suspect_files + suggestion` 由 itest 员工基于日志与 diff 推理产出——这是回传给开发员工最有价值的三件套(日志/复现/建议)的落地形式。

---

## 7. Procedure 工程体系

### 7.1 Procedure 规范 [MVP]

```
genome/procedures/unit-gate/
├── procedure.yaml
├── prompt.md            # 员工执行此 Procedure 时的指令(如需 Agent 参与)
└── scripts/             # 确定性脚本(能不用 Agent 就不用)
```

```yaml
# procedure.yaml —— 评审 25% 权重的直接对应物
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

**设计要点**:`kind` 把 Procedure 分为确定性(纯脚本,如 gate)、Agent 型(如 code-develop)与混合型。原则:**验证类一律确定性,生成类才用 Agent**——可复现性与成本都更优。

### 7.2 注册、版本与分发

- 注册:编排器启动时扫描 `genome/procedures/`(项目级)与 `~/.agentgenome/procedures/`(全局级),项目级同名覆盖全局级。pydantic 校验 schema,非法 Procedure 拒绝加载并告警。[MVP]
- 版本:semver;Task 事件里记录每个 Job 使用的 procedure@version,报告可精确复现"当时是哪版技能干的活"。[MVP]
- 分发:全局基因库(§10.4)作为 Procedure 市场,`agctl genome pull procedure/<name>` 拉取模板。[FULL]

### 7.3 与 MCP 的关系

Procedure 声明 `tools.mcp` 后,runtime 拉起员工时注入对应 MCP server 配置(如 UModel 的 AgentGateway)。Procedure 是"何时做什么、成败如何界定"的治理层;MCP 是"能调用什么外部能力"的工具层——两者正交。[FULL]

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

- 分支模型:`task/<id>/<slug>`;worktree 位于 `~/.agentgenome/worktrees/<workspace-id>/<task-id>/`,既物理隔离于主 checkout，也与其他 Workspace 的同名任务隔离。
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

Task 进入终态(COMPLETED / ESCALATED)后异步触发:

```
收集素材 ──▶ 蒸馏(arch 员工) ──▶ 分级入库 ──▶ 走门禁与审批 ──▶ 基因组版本 +1
```

- **收集素材**:任务全事件流、失败-修复对(每轮 gate 失败报告 + 对应修复 diff)、集测报告、审批意见。ESCALATED 任务是最富矿——人类最终怎么修的,与 AI 的尝试差在哪。
- **蒸馏**(`experience-distill` Procedure,agentic):输出候选卡片,每张必须含:适用条件、结论、证据链接(指向任务事件)、置信度。
- **分级入库**:

| 级别 | 内容 | 去向 | 审批 |
|---|---|---|---|
| L1 知识 | 模块认知修正、坑点、依赖事实 | knowledge/lessons/ + modules/*.md 增量 | 自动合并(走 lint 门禁) |
| L2 规则 | 新的边界/规范/impact 规则 | rules/ 增量 PR | **必须人工审批** |
| L3 技能 | Procedure prompt/脚本改进 | procedures/ 增量 PR | 人工审批 + 回归验证 [FULL] |
| L4 全局 | 与项目无关的通用经验 | 全局基因库 PR | 基因库维护者审批 [FULL] |

### 10.2 防知识污染 [MVP]

- 所有蒸馏产物走 PR,diff 可见、可拒绝;
- 卡片强制带证据链接,无证据的"经验"直接拒收;
- knowledge 设容量预算(如 lessons ≤ 200 张),超限触发合并/淘汰(按被引用次数 LRU);
- 每张卡片带 `hits` 字段:被后续任务上下文命中且该任务成功,则 +1;长期 0 命中的卡片周期性归档——**知识也要接受自然选择**。

### 10.3 效果度量 [FULL]

进化是否真的发生,用指标说话(周报自动生成):平均修复轮次、门禁一次通过率、任务时长、ESCALATED 率、知识命中率。目标趋势:随任务量上升,前四项下降、末项上升。

### 10.4 全局基因库(Genome Registry)[FULL]

独立 Git 仓库 `genome-registry`:`templates/`(项目基因组骨架:Python 服务、前端单仓、多模块中台等)+ `procedures/`(通用 Procedure)+ `lessons/`(跨项目经验)。命令:

```
agctl genome init --template python-multimodule   # 新项目继承基因
agctl genome pull procedure/contract-test             # 拉取技能
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

同步作业(`umodel-sync` Procedure,deterministic):project-map 变更或任务合并后,将 modules/interfaces/tasks 实体与拓扑写入 UModel EntityStore;生产侧 Service/Alert 实体由运维侧已有链路写入。

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
                            规则管理(变更走审批流) · Procedure 注册表(版本/使用统计)[P1]
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
| Agent 输出不满足产物契约 | 结构化输出 schema 校验 + 普通 Job 单次失败（staging 可显式补交）+ 不带病前进 |
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
