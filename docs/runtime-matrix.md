# 运行时兼容性矩阵

哪个运行时能干什么、干得怎么样。**选型依据公开在这里**，而不是散在各人的印象里。

## 能力

数据源是 `agentgenome.agents.capabilities`——这张表是那份数据结构的人类可读投影。
两者不一致时以代码为准，并且请修这张表：一张过时的表比没有表更误导。

| 能力 | claude-code | qwen-code | agentteams | human |
|---|---|---|---|---|
| `read_file` | `Read` | `read_file` | `read_file`（经平台抽象） | 全部支持 |
| `write_file` | `Write` | `write_file` | `write_file` | 全部支持 |
| `edit_file` | `Edit` | `edit_file` | `edit_file` | 全部支持 |
| `run_command` | `Bash` | `run_shell_command` | `run_command` | 全部支持 |
| `search` | `Grep` | `search_file_content` | `search` | 全部支持 |
| `list_files` | `Glob` | `list_directory` | `list_files` | 全部支持 |
| `fetch_url` | `WebFetch` | **不支持** | `fetch_url` | 全部支持 |
| `web_search` | `WebSearch` | **不支持** | `web_search` | 全部支持 |

human 那一列的"全部支持"不是偷懒:能力矩阵在那里回答的不是"他手里有哪个命令",而是
**"这份活能不能派给他"**——答案永远是能,他手里是一台电脑。

| 属性 | claude-code | qwen-code | agentteams | human |
|---|---|---|---|---|
| 上下文窗口 | 200k | 128k | 200k | 不按 token 裁（给个宽值，免得把该给人看的材料截掉） |
| 流式输出 | 是 | 是 | **否**（平台传消息，不传 token 流） | 否 |
| token 用量可获取 | 是 | **否**（归一化事件里标 `unavailable`，不填 0） | 是（网关按 Job 给；个别缺失标 `unavailable`） | **不可得**（人的工时没有 token 账；填 0 会让成本看板把它算成免费） |
| 结构化输出可靠性 | 1.0 | 0.8 | 0.9（取决于 Worker 侧运行时，保守取值） | 1.0（产物过同一套契约校验，不合格打回重交） |
| 会话（咨询/质询/结对） | 是 | 否 | **否**（消息粒度承载不了逐 token 流式） | 不适用（人和人聊天不归系统管） |
| 执行中预算掐断 | 是（实时掐进程） | **否**（用量本就不可得，实时掐断无从谈起） | **否**（网关计量是事后的，单 Job `max_tokens` 是软上限） | 不适用（`max_tokens` 为 0；墙钟到期走提醒 → 改派 → 升级三段） |
| 技艺包物化 | 是 | 否（降级为内联摘要） | 否（降级为内联摘要） | **否**（给人物化一堆方法论目录是噪音） |

## human:执行三态

`human` 让"把这个 Job 直接交给人做"成为一件与派给硅基员工同构的事——同一个契约、同一道门禁、
同一条时间线。为什么它是运行时而不是审批的一种,见 **ADR-0009**。

| 三态 | 怎么配 | 它是什么 |
|---|---|---|
| `auto` | 什么都不配 | 今天的样子:员工自己干完 |
| `manual` | 员工 `runtime: human`,或图里的节点 `executor: manual` | 这一步整个交给人。能写代码的活会给出工作树路径,人在那儿改;点完成时跑与自主 Job **同一条**越权检查 |
| `assisted` | `topology.assisted.employees: [<员工 id>]` | 机器干、人确认。它是**组合**(自动节点 + 确认节点),不是第三种原语——所以确认自动获得待办、超时三段、改派、RBAC |

**三态比例是可汇报指标,但必须与门禁一次通过率成对看**(`agentgenome_execution_mode_total`
与 `agentgenome_gate_first_pass_ratio`):"自动化率从 30% 爬到 70%"关掉确认节点就能刷,
单独上报它等于奖励一个把信任爬坡走成信任跳崖的动作。

待办到期不会一步进终态:**提醒 → 改派 → 升级人工**三段(`human.reminder_after_days` /
`reassign_after_days` / `backups`)。直接升级的话,"等另一个人接管"这句话接不回来——已升级
人工是终态。

**映射不到的能力会直接抛 `UnsupportedCapability`**，不静默降级。静默的话，一个声明要用
`web_search` 的 Procedure 会在 qwen-code 上跑起来然后产出"我找不到相关资料"——看起来像是资料
的问题。

## agentteams:适用场景与边界

> 完整的设计方案(架构、本地仓库如何被改动、断点续接语义)与逐步配置指南
> 见 **`docs/agentteams.md`**。本节只保留速查。

`agentteams` 把 Job 派给 AgentTeams 平台上的 Worker **容器**执行(PRD 31)。它的价值
主张是**隔离与横向扩展,不是功能**——没有这两个需求的部署不该启用它:

- **要它的理由**:单机并发顶不住;或需要容器级隔离与网关持钥(Worker 只见消费
  token,真实凭证留在平台网关)。
- **不要它的理由**:它比本地子进程多一整套平台运维;会话开不了;执行中预算
  掐断做不到(任务预算的**派发前拒绝**照常生效,那在池里)。

启用步骤(都是显式动作,没有静默默认):

1. 根配置加条目:`agentteams: {endpoint: ..., consumer_token_env: <环境变量名>}`。
   token 本体只由环境变量给,不进配置文件。**传输选型**:缺省 `transport: http`
   假设平台侧有任务桥;对上游原生形态(无任务 API,派发是 MinIO 任务目录 +
   Matrix 消息约定,见 `.scratch/agentteams-runtime/source-analysis.md`)用
   `transport: matrix-minio`,并补 `matrix_homeserver` / `matrix_room` /
   `matrix_token_env` / `storage_prefix` / `worker`(可选 `mc_cmd`)。
   matrix-minio 下用量**恒为不可得**——平台没有逐任务计量。
2. 员工定义里 `runtime: agentteams`,按员工混配。声明了直连凭证的员工配不了——
   派发时报配置矛盾,不静默丢弃。
3. **工序显式声明兼容**:`compat.runtimes` 加上 `agentteams`。兼容闸的语义是
   "只在一个运行时上验证过的工序不该悄悄换台跑",对它照常生效。

CI 姿势:传输层录制/回放(`agents.agentteams.recording`),不起真实平台。
录制开关与 Job 级录制共用 `AGENTGENOME_RECORD` / `AGENTGENOME_RECORDINGS`。

## 一致性测试套件

一组标准任务，任何运行时接入或版本升级时跑一遍。度量：任务成功率、平均修复轮次、结果契约
一次通过率、token 消耗、耗时。

**这套测试必须跑真实 Agent，所以不进常规 CI。** 手工触发，结果填在下面。

### 任务集

| # | 任务 | 考察什么 |
|---|---|---|
| 1 | 给一个函数补一个单测 | 最小闭环：读代码、写文件、跑测试、写合契约的产物 |
| 2 | 修一个由失败报告指出的断言错误 | 能不能读懂并利用上一轮的失败报告 |
| 3 | 跨两个模块改一个接口 | 多文件协同、越权边界 |
| 4 | 需求写得含糊，正确做法是提问 | 会不会擅自决定（`questions[]` 用得对不对） |
| 5 | 一个改不动的环境问题 | 会不会承认失败而不是伪造通过 |

### 结果

| 运行时 | 版本 | 日期 | 成功率 | 平均修复轮次 | 契约一次通过率 | token | 耗时 |
|---|---|---|---|---|---|---|---|
| claude-code | — | — | — | — | — | — | — |
| qwen-code | — | — | — | — | — | — | — |

> 表里全是横杠，因为**这套测试还没跑过**。它需要两个运行时的真实凭证与配额，属于
> `ready-for-human`。跑之前不要把任何一行当作选型依据。
