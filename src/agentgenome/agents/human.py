"""human 运行时:把一份活派给人。

## 人也是一种运行时

派给它的 Job 不拉子进程,而是出现在某人的 Web 待办里。人干完活,产物按同一份 `result.json`
契约交付,过同一套 schema 校验,失败同样打回。**对编排器、状态机、门禁、事件面而言,与硅基
员工无差别**——这句话的价值全部在于它是真的:全代码库不存在一条"因为执行者是人所以放宽"的
分支。

## 它是运行时,不是审批的变体

审批回答的是"这个产出可不可以"(裁决,带否决权,直接驱动状态迁移,面向整个任务);human
Job 回答的是"这份活谁来干"(执行,状态内部的一次作业)。把后者塞进审批体系,会让审批面长
出上传产物、契约校验这些它不该有的器官;把前者塞进 Job,会让人工审批的否决权变成一个 Job
的返回值。

## 投递即返回,不是等在那儿

一份人类作业以**工作日**计。挂在这里等的话:一个协程挂三天;占掉执行池的一个并发名额(而
泳道的理由是"子进程既吃机器也吃 API 配额",人两样都不吃);进程一重启,那一等就没了。

所以 `run_job` 投完待办立刻返回一个**挂起**的结果。它不是失败——失败会让状态机重试、升级、
扣一轮修复轮次,而这三样对"人还没开始干"都是错的。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from agentgenome.agents.runtime import JobResult, JobSpec
from agentgenome.config import HUMAN_RUNTIME
from agentgenome.todo.store import ARTIFACT, WORKTREE, Todo, TodoStore

#: 运行时名。**只定义一次**(在根配置那一层,因为配置校验最先用到它);这里转出来是为了
#: 让"人这个运行时"在 agents 包里也有一个自然的名字,而不是让每个调用方各写一遍字面量。
HUMAN = HUMAN_RUNTIME


def todo_id(spec: JobSpec) -> str:
    """一张待办的标识。

    **从幂等键派生,不是随机的。** 随机 id 意味着"这次派发对应哪张待办"要另存一份映射,
    而崩溃恢复正好发生在那份映射写下之前的那个瞬间。
    """
    raw = f"{spec.task_id}|{spec.procedure_id}|{spec.subject}|{spec.round}"
    return "todo-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _node_and_attempt(subject: str) -> tuple[str, int]:
    """回放键的 `subject` 里藏着"哪个节点的第几轮"。

    `single` 下它是空串,于是节点为空、轮次取 Job 自己的轮次(见 `deliver`)。
    """
    if not subject:
        return "", 0
    node, _, attempt = subject.partition(".")
    return node, int(attempt) if attempt.isdigit() else 0


def _writes_code(spec: JobSpec) -> bool:
    return bool(spec.scope and spec.scope.write_paths)


class HumanRuntime:
    """把 Job 投进某人的待办里。"""

    name = HUMAN

    def __init__(self, workspace_root: Path) -> None:
        self.root = Path(workspace_root).resolve()

    def preflight(self) -> None:
        """自检。**人不需要装什么**,但库得能开——开不了的话待办投不出去,而症状会是
        "任务推了没反应"。"""
        TodoStore(self.root)

    async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
        """投一张待办,然后立刻回来。"""
        if dry_run:
            return JobResult(ok=True, dry_run=True, tokens_available=False)

        todo = self.deliver(spec)
        return JobResult(
            ok=False,
            suspended=True,
            suspended_on=todo.id,
            # **用量按"不可得"记,不是 0。** 记 0 会让成本看板把人的工时算成"免费",
            # 而"这件事没有 token 账"与"这件事花了 0 个 token"是两句不同的话。
            tokens_available=False,
        )

    def deliver(self, spec: JobSpec, now: datetime | None = None) -> Todo:
        """把这次派发变成一张待办。同键的已经在了就返回它。"""
        node, attempt = _node_and_attempt(spec.subject)
        store = TodoStore(self.root)
        return store.deliver(
            Todo(
                id=todo_id(spec),
                task_id=spec.task_id,
                stage=spec.output_dir.name,
                node=node,
                attempt=attempt or spec.round,
                assignee=spec.assignee or spec.employee_id,
                employee_id=spec.employee_id,
                procedure_id=spec.procedure_id,
                output_dir=self._relative(spec.output_dir),
                context_file=self._relative(spec.context_file),
                workdir=self._relative(spec.workdir),
                # **能写代码的活就是"去工作树里干"的活。** 判据取自这次派发的授权范围,
                # 不另立一张"哪些工序算开发"的清单——那张清单迟早与真实授权分叉,而分叉
                # 的表现是人被告知"在网页上交产物",然后他改的代码没有任何地方去。
                kind=WORKTREE if _writes_code(spec) else ARTIFACT,
            ),
            now=now or datetime.now(UTC),
        )

    def _relative(self, path: Path) -> str:
        """路径存相对的。绝对路径存进库里,换一台机器部署之后每一张老待办都指向不存在的地方。"""
        resolved = Path(path).resolve()
        if resolved.is_relative_to(self.root):
            return str(resolved.relative_to(self.root))
        return str(resolved)


__all__ = ["HUMAN", "HumanRuntime", "todo_id"]
