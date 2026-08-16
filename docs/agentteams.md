# AgentTeams 支持:设计方案与配置指南

> 状态:已实现并经真机验收(2026-08-11,完整研发任务在真实 AgentTeams 容器
> 员工上从需求走到 READY_TO_COMMIT)。PRD 见 `.scratch/agentteams-runtime/`,
> 平台源码分析见其中的 `source-analysis.md`。能力对照表见 `runtime-matrix.md`。

## 一、定位:第三个注册运行时,不是编排层替换

`agentteams` 把 Job 派给 [AgentTeams](https://github.com/agentscope-ai/agentteams)
平台上的 Worker **容器**执行。它的价值主张是**隔离与横向扩展,不是功能**:

- **要它的理由**:单机并发顶不住;或需要容器级隔离与网关持钥
  (Worker 只持平台消费 token,仓库凭证与真实密钥永远不出本地)。
- **不要它的理由**:多一整套平台运维;会话开不了;执行中预算掐断做不到。

编排器、执行池、任务状态机、契约校验、越权检查、门禁、提交流水线**零改动**
——适配器只是运行时接缝(`agents/runtime.py`)上的又一个实现,与本地子进程
运行时按员工混配。

## 二、架构:一道缝,两条通道

```
Orchestrator / AgentPool / SessionService        (零改动)
        │  JobSpec → JobResult
        ▼
AgentTeamsRuntime(任务语义层)                    agents/agentteams/runtime.py
  - 翻译 JobSpec ↔ 平台任务;交作业手艺随 Job 下发
  - 契约校验与重试(与子进程共用同一件)
  - 事务性工作区落地;凭证矛盾显式拒绝
        │  TransportJob → TransportOutcome        ← 唯一的测试缝
        ▼
AgentTeamsTransport(平台传输层,三个实现)
  - MatrixMinioTransport:平台真实约定(生产推荐)  agents/agentteams/matrix_minio.py
  - HttpAgentTeamsTransport:假设的任务桥          agents/agentteams/http.py
  - Recording/ReplayTransport:录制与回放          agents/agentteams/recording.py
```

平台侧事实(源码分析结论):AgentTeams **没有任务 API**——控制面 REST 只管
资源 CRUD;任务派发是「MinIO 任务目录 + Matrix 消息」的约定。所以生产通道
`matrix-minio` 直接说这套约定的母语:

1. 渲染任务目录(`spec.md` + `meta.json` + `workspace/` 快照)经 `mc` 推送;
2. 往 Worker 的 Matrix 房间发 @mention;
3. 轮询 `meta.json` 到终态(SUCCESS/BLOCKED/…),沉降一拍;
4. 拉回工作区与产物,差分装配成结果。

## 三、本地仓库怎么被改动:以副本换差分

**容器从头到尾碰不到本地仓库。** 数据流:

```
本地仓库(唯一真相源)
  └─ 编排器从 HEAD 开隔离工作区(git worktree)
       │ ① 适配器拍快照(纯文件映射;不含 .git/符号链接/二进制)推到 MinIO
       ▼
     shared/tasks/{任务引用}/workspace/**          ← 云上副本
       │ ② Worker 容器 file-sync 拉取、在容器里改、同步回去、置终态
       ▼
       │ ③ 适配器拉回,与①差分 → changed_files(含删除)
       ▼
  隔离工作区 ← ④ 事务性落地:路径越界拒绝、.git 拒改、任务面文件过滤、
       │         要么全落要么逐字节恢复原状
       ⑤ 以下全是本地动作:越权检查(git diff 基线)→ 门禁(真 pytest)
          → 提交流水线 commit/push(用本地身份)
```

三条要点:

- **写本地仓库的永远是本地进程。** Worker 的输出只是"待审的差分",落地后
  仍要过越权检查与门禁;`git commit/push` 在本地提交流水线里用本地凭证完成。
- **凭证边界干净。** 容器里没有仓库凭证、没有本地文件系统、看不到 `.git`。
  员工配置声明了直连凭证的,派发时报配置矛盾,不静默丢弃。
- **断点续接。** 编排器进程崩溃重启后重派同一作业:远端现场已在就不重推
  (不抹 Worker 已完成/进行中的活),仅非终态补一次知会(txn 每次唯一,
  不被 Matrix 去重吞掉)。任务引用含「任务-工序-轮次-尝试」全维度,
  不同工序绝不共用远端目录。

## 四、如实声明的边界

| 边界 | 事实 | 后果与对策 |
|---|---|---|
| 逐任务 token 用量 | 平台网关只做 key-auth,无计量 API | 用量恒标**不可得**(不填 0);成本精算需上游补计量 |
| 执行中预算掐断 | 平台计量是事后的 | 单 Job `max_tokens` 是软上限;任务预算的派发前拒绝照常生效 |
| 会话(咨询/质询/结对) | Matrix 消息粒度承载不了逐 token 流式 | 能力矩阵声明 `sessions: false`,会话侧自动拒绝并解释 |
| 工具 allow/deny | 平台无逐任务工具限制机制 | 降级为 spec.md 里的劝导性文字,不是硬约束 |
| **只读员工** | 承上:工具面收窄不了,所以能力矩阵声明 `enforces_read_only: false` | **只读员工(工具只有 Read/Grep/Glob,如 `reviewer-employee`)在这个运行时上一律被派发前拒绝。** 补兼容声明也没用——那是另一道闸。界面在选运行时那一刻就把它置灰并给出理由 |
| **业务仓工具链** | `code-develop` / `write-tests` 要求 Agent 自跑本模块 `test_cmd`,而"本地"是**容器里那份副本** | **Worker 镜像必须自带项目工具链**(语言运行时、包管理器、测试框架)。缺了的话表现为"测试跑不起来"被写进 `self_test`,而不是一次显式的配置报错——就绪检查探不到它,它是运行期才暴露的 |
| 二进制文件 / git 历史 | 快照是文本映射;Worker 看不到 `.git` | Worker 看不到二进制资产与 `git log`;认知靠上下文包补 |
| Worker 习惯动作 | 会把 result.md 等复制进 workspace/ | 任务面过滤(`strip_task_plane`)只滤顶层,不当成代码改动 |

## 五、配置指南

### 5.1 平台侧要准备的 6 个值

| 值 | 去哪拿 |
|---|---|
| controller 地址(`http://<IP>:8090`) | `agentteams-controller` 容器;端口未发布时用容器 IP 或做端口转发 |
| controller token | `docker exec agentteams-manager env \| grep AUTH_TOKEN`(K8s SA token) |
| Matrix 地址(`http://<IP>:18080`) | 网关端口(经 Host 路由到 Tuwunel) |
| Matrix access token + Worker 房间 ID | 用会员账号 `POST /_matrix/client/v3/login`;房间 ID 形如 `!xxx:域` ,该账号必须已在 Worker 房间里 |
| MinIO 地址 + access/secret key | `docker exec agentteams-manager env \| grep -E "FS_ACCESS\|FS_SECRET"`;地址是 controller 容器的 `:9000` |
| Worker 名 | 平台上创建的 Worker(如 `developer`) |

本机还需 `mc` 客户端,并配置别名:

```bash
mc alias set agentteams http://<MinIO地址>:9000 <ACCESS_KEY> <SECRET_KEY>
mc ls agentteams/agentteams-storage/shared/   # 能列出 tasks/ 即通
```

注意:MinIO 策略通常只放行 `shared/` 作用域(桶顶层拒绝)——所以
`storage_prefix` 要配到 `.../shared`,任务目录自然落在平台自己的
`shared/tasks/` 约定上,预检的 `ls` 探测也在授权范围内。

### 5.2 根配置(agentgenome.yaml)

```yaml
runtime:
  default: claude-code
  claude-code: {cmd: claude, max_turns: 40}
  agentteams:
    transport: matrix-minio
    endpoint: http://<controller>:8090
    consumer_token_env: AGENTTEAMS_CONSUMER_TOKEN     # token 本体只走环境变量
    matrix_homeserver: http://<matrix>:18080
    matrix_token_env: AGENTTEAMS_MATRIX_TOKEN
    storage_prefix: agentteams/agentteams-storage/shared
    # mc_cmd: mc                                      # 可选,默认 mc
    # 可选:模型档位 → 具体模型与提供商(见 5.5)
    # model_tiers:
    #   cheap: {model: qwen-turbo, provider: dashscope}
    #   strong: {model: qwen-max}
    # 可选:固定路由。**既有部署的兜底**——两个成对给就是"所有员工共用一个
    # Worker"的老行为;都不给则按员工解析(推荐,见 5.5)。只给一半会报错。
    # worker: developer
    # matrix_room: "!xxx:matrix-local.agentteams.io:18080"
```

缺字段、`http` 传输带 matrix 字段、本地 CLI 条目带平台字段、固定路由只配一半——
都在配置装配层报错,不留到运行期。

### 5.3 环境变量与工序声明

```bash
export AGENTTEAMS_CONSUMER_TOKEN=<controller token>
export AGENTTEAMS_MATRIX_TOKEN=<matrix access token>
```

**工序显式声明兼容**(兼容闸的语义是"只在一个运行时上验证过的工序不该
悄悄换台跑",对容器运行时照常生效):

```yaml
# genome/procedures/<id>/procedure.yaml
compat:
  runtimes: [claude-code, agentteams]
```

员工侧按需混配:`employees/<id>.yaml` 里 `runtime: agentteams`,或派发时
`agctl task run <id> --runtime agentteams` 整体覆盖。

**从界面做**(PRD 33):员工管理页每一行的运行时下拉框会当场列出这个员工还差哪些
兼容声明,并给一个显式的补声明按钮——**列出来不等于自动补**,判断仍归人。

默认花名册(`agctl roster migrate` 生成的那份)对 agentteams 的情况:

| 工序 | kind | 要不要补声明 |
|---|---|---|
| `requirement-analysis` / `itest-decide` / `code-develop` / `write-tests` / `adversarial-probe` / `code-critique` / `experience-distill` | agentic | **要** |
| `itest-run` | hybrid | **要**。脚本半段在编排器本地跑(不经过运行时),只有 Agent 半段进容器 |
| `unit-gate` | deterministic | **不要**。它声明 `compat: [none]`——"我不碰任何运行时",派发闸对它放行 |

`code-critique` 的宿主 `reviewer-employee` 是只读员工,补了声明也跑不到容器上,
见"如实声明的边界"那一节。

### 5.5 员工供应(PRD 32)

把花名册声明式地对齐到平台上——**每个员工一个自己的 Worker**,角色隔离在容器
一侧因此才成立:

```bash
agctl employee provision                 # 整份花名册,幂等,可反复执行
agctl employee provision arch-employee   # 只对齐一个
agctl employee provision --dry-run       # 先看计划:哪些新建、更新、跳过
agctl employee provision --sleep         # 用完回收:容器停掉,资源还回去
agctl employee provision --delete        # 不再需要的删掉
```

几条要点:

- **首次派发自动供应**。员工声明 `runtime: agentteams` 就是在声明所需运行编制;
  派发发现 Worker 不存在时会幂等创建、等待就绪再交 Job。显式 `provision` 命令仍用于
  部署前预热、批量对齐与 dry-run,不是正常任务流程的前置步骤。
- **只碰我们供应的**。Worker 名带 `agenome-` 前缀;平台上人工建的、别的系统建的
  一律不读不写。
- **房间不落盘**。派发时按员工向平台解析,进程内缓存。Worker 重建会换房间 id
  (真机实测),缓存在配置里就是一颗"派发石沉大海且无报错"的雷。
- **soul 只有最小身份 + 有界的角色概要**。人格、工序、知识切片、失败报告仍由每个
  Job 的上下文包注入——员工定义会频繁改,写死进 soul 就要靠重新供应才能更新,
  而陈旧人格与新鲜上下文同时在场时冲突是**静默的**(Worker 侧每轮都读身份文件)。
- **休眠的 Worker 在派发时自动唤醒**。否则休眠这个纯成本优化会表现为"派下去
  没反应",而那是最难归因的一类症状。

### 5.4 验证与运行

```bash
# 预检:一步验 controller / Matrix 令牌 / 存储前缀三样,失败指向配置段
# (任何装配运行时的 agctl 命令都会先跑预检)
agctl task submit --requirement "..." --workspace <ws> --json
agctl task run <task-id> --workspace <ws> --runtime agentteams --steps 1
```

崩溃/中断后直接重跑同一条命令即可——断点续接会回收 Worker 已完成的现场,
不重做、不覆盖。

### 5.5 CI 与素材

CI **不起任何真实平台**:假传输覆盖行为分支,`tests/fixtures/golden/agentteams/`
的黄金素材(录自真机往返,令牌结构性进不了报文)回放真机形状。刷新素材:

```bash
AGENTGENOME_RECORD=1 AGENTGENOME_RECORDINGS=<库> <对真实平台跑一次任务>
# 人工过目后替换 golden 目录
```

## 六、真机验收记录(2026-08-11)

单机 Docker 版 AgentTeams(Tuwunel + Higress + MinIO + Hermes Worker):

- 完整研发任务 plan → dev → 3 轮修复循环 → unit 门禁 → itest-decide →
  READY_TO_COMMIT,全部 Job 由容器员工执行;
- 本地门禁的失败报告(pytest log_tail)经上下文回注,Worker 据此修对
  ——修复闭环跨平台边界成立;
- plan 的严格 schema 产物一次合格;交作业合同内联 spec.md 即可生效;
- 会话中断多次,断点续接均正确回收,无一次现场丢失或重做。
