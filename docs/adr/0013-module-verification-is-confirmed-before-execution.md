# Module verification is confirmed before execution

模块验证不再由运行期按业务仓配置、Workspace 配置和知识地图动态猜选，也不从命令文本推断
工具链。初始化期的确定性发现器产出带命令证据的候选，任务内歧义交给架构员工自动深挖；确认后的
模块验证规格成为唯一可执行事实，运行差异藏在可注册的环境 Adapter 后面。我们接受一次 schema
迁移与依赖环境准备成本，以换取跨语言扩展时不修改核心门禁、错误命令可追溯且员工不能改判卷规则。

## 生命周期

1. `gate discover` 只读取仓库作者声明的标准入口。唯一答案可以直接确认；缺失、冲突或未知
   工具链写成 `.pending.yaml`，此时没有任何可执行命令。
2. `gate propose` 仅在 pending 存在时派 `arch-employee` 只读调查。员工提交结构化 argv、
   Adapter 引用和文件 locator；平台自己读取 locator 并盖摘要，不信任模型自报的 digest。
3. 人通过 `gate show` 审查候选，再用 `gate confirm` 授权。确认会形成独立 Git 提交和
   `config_changed` 事件；关联的基因组任务从待确认直接结束。
4. 自动生命周期和手工重跑只执行两类 v2 规格：Workspace 控制 checkout 中已确认的项目规格，
   或空仓首个任务从当前工作树唯一确定、并留在产物面的临时规格。缺规格、仍有歧义、证据漂移或
   Adapter 不可用都拒绝运行，不回退到旧配置猜选。

空仓是一个刻意的例外时序，而不是降低信任标准：初始化时允许留下
`NO_STANDARD_ENTRYPOINT`；首个开发任务进入门禁时，发现器会在任务工作树上重新读取作者入口。
若此时得到唯一、带可定位证据的规格，门禁直接用这份**任务级临时规格**执行，并把规格留在门禁
产物面。它不会在开发员工自述成功时写进控制 checkout；只有任务实际合入并收到 `MERGED` 事件后，
编排器才把最后一轮通过的临时规格提升为项目规格、形成独立配置提交并清掉旧 pending。任务失败、
取消或升级人工都不会污染项目规格。重新发现仍有歧义时，编排器在任务工作树上自动派只读
`arch-employee` 深入调查；平台重新定位候选证据、盖摘要并真实执行，只有通过的候选才作为
任务级临时规格继续。这个过程不创建人工待办，也不让 Agent 直接写控制面；正式项目规格仍只在
任务收到 `MERGED` 后提升。显式 `gate propose/confirm` 保留给人在任务生命周期之外维护配置。

自动调查只选择能力矩阵明确声明 `enforces_read_only` 的已注册硅基 runtime，并通过统一
Procedure 派发把 `read_file/search/list_files` 翻译成各运行时的原生工具名。`human` 永不参与；
当前 AgentTeams Worker 尚不能证明强制只读，因此也不会被这条安全敏感路径选择。调查失败留在
`UNIT_TESTING`，不消耗代码修复轮次；连续三次失败后任务以明确的系统阻塞原因终止，但不创建人工
分析待办。崩溃恢复只复用同时通过候选复核、且带成功 dispatch receipt 的产物；跨外部 runtime
与本地记账无法原子提交的窄窗采用 at-least-once 语义。

旧的业务仓 `gates.yaml`、Workspace gate map 与知识地图命令字段只作为迁移证据展示，永远不再
被运行期执行。命令内容与运行环境分开：例如前端可以是 `npm run test` + `node.npm`，Python
可以是 `make test` + `python.uv`；新增生态只注册 Adapter，不往通用执行器追加工具名分支。
