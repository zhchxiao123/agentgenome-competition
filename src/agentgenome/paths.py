"""Workspace 内的约定路径。

集中在一处，避免路径字符串散落到各个模块里各写一遍。全部为 Workspace 相对路径。
"""

from __future__ import annotations

from pathlib import Path

ROOT_CONFIG = Path("agentgenome.yaml")

GENOME = Path("genome")
KNOWLEDGE = GENOME / "knowledge"
#: L0 根索引。**文件名沿用**:它仍然是"打开知识目录第一个该读的东西",换个名字只会让
#: 所有既有的指路说明失效。变的是它的内容——从装下一切,变成只装身份与指针。
PROJECT_MAP = KNOWLEDGE / "project-map.yaml"
#: 跨模块契约与数据存储。它们不属于任何一个模块,所以不挂在任何一个模块名下。
INTERFACES = KNOWLEDGE / "interfaces.yaml"
MODULES = KNOWLEDGE / "modules"
DECISIONS = KNOWLEDGE / "decisions"
LESSONS = KNOWLEDGE / "lessons"

#: 这四个是 **Workspace 相对路径的字符串键**,不是本仓库(`agentgenome` 这份源码)自己
#: 携带的文件。它们描述的是 `agctl init` 生成出来的 Workspace 会长什么样——真正的
#: `genome/rules/architecture.md` 只存在于跑过 `agctl init` 的 Workspace 里,不存在于
#: `agentgenome` 这份 pip 包/源码检出本身。读这份源码给某个模块写认知(比如自举场景里
#: 把 `agentgenome` 当成业务仓来跑 `knowledge init`)时,不要把这几个常量的取值当成
#: "这个仓库里真的有这些文件"的证据。
RULES = GENOME / "rules"
ARCHITECTURE_RULES = RULES / "architecture.md"
CODING_RULES = RULES / "coding.md"
PROTECTED_RULES = RULES / "protected.yaml"
IMPACT_RULES = RULES / "impact.yaml"

PROCEDURES = GENOME / "procedures"
#: 基因组里按模块 id 索引的门禁配置。放这里就不必碰业务仓。
GATES = GENOME / "gates"
#: 业务仓的挂载根。**它是权限模型的支点**——`repos/**` 就是"全部业务代码"这句话的写法,
#: 架构员工的禁写、开发员工的可写、集测员工的边界全都挂在它上面。
#:
#: 挂载根必须是一个**跨项目稳定的常量**,不能做成可配置项:做成可配置的话,"全部业务代码"
#: 这条 glob 就得随项目变,而随项目变的权限支点没有办法在默认花名册里写死。
#:
#: 单仓项目也照样多这一层。省掉它看着诱人,但会让权限 glob 分叉成"单仓一套、多仓一套",
#: 而且从一个仓长到两个仓时整个 Workspace 结构要重排。
REPOS = Path("repos")

EMPLOYEES = Path("employees")
EMPLOYEE_PROMPTS = EMPLOYEES / "prompts"
SCRIPTS = Path("scripts")
TASKS = Path("tasks")
#: 任务库。与 `tasks/` 一样不进 git。
DATABASE = TASKS / "agentgenome.db"

#: 审计包的归档根。**刻意与 `tasks/` 并列而不是住在它里面**:审计包存在的唯一理由是在原始
#: 日志被清理之后仍然留得住,住在任务目录里的话清理任务目录会把它一起清掉——恰恰是它为之
#: 准备的那一刻。同样不进 git:它是打包的快照,不是会演进的资产,入库会把协作仓撑爆。
ARCHIVE = Path("archive")

#: 隔离工作区的存放根，位于 Workspace 之外以保证物理隔离。
WORKTREES_HOME = Path.home() / ".agentgenome" / "worktrees"

#: 会话日志在 Workspace 里的位置。**这份是回放的事实源**——Claude Code 侧那份原生副本
#: 受它自己的 `cleanupPeriodDays` 管，编排器控制不了，随时可能消失。
SESSIONS = TASKS / "sessions"
