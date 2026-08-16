"""提交阶段的两个处理器:提交前关卡与双层合并。

住在单独一个模块而不是塞进 `jobs.handlers`:那份文件的主题是"派 Procedure",而这两个处理器
一次 Agent 都不拉。放一起的话,读 `handlers.py` 的人会以为每个状态都要派个 Procedure。

## READY_TO_COMMIT 不做产物幂等

这一关零 token,重跑是安全的——而且重跑**更对**:崩溃恢复时工作树可能已经变了,拿旧结论
往下走才是错的。产物照常落盘,但只作为审计材料,不作为跳过的依据。

## MERGING 必须幂等,而且靠的不是产物

平台上重复开 PR 会得到两个。幂等键是任务记录里的各 PR 引用与合并后 revision,
`DualLayerResult.as_dict()` 已经把它们序列化好了。这是整套崩溃恢复里最需要显式幂等键的
地方——其余状态重跑最多浪费 token,这里重跑会在别人的代码库上留下垃圾。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentgenome.commit.pipeline import STAGE_PRECHECK, run_pipeline
from agentgenome.core.scope_grants import granted_modules
from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.task import Task
from agentgenome.jobs.handlers import Outcome

if TYPE_CHECKING:
    from agentgenome.jobs.artifacts import ArtifactBus
    from agentgenome.jobs.orchestrator import JobContext

#: 合并阶段的产物 stage 名。
STAGE_MERGE = "merge"


@dataclass(frozen=True)
class CommitPipelineHandler:
    """`READY_TO_COMMIT`:越权复查 → 密钥扫描 → 风险评级。零 Agent。"""

    state: TaskState = TaskState.READY_TO_COMMIT

    @property
    def stage(self) -> str:
        return STAGE_PRECHECK

    def attempt(self, task: Task) -> int:
        return task.fix_rounds + 1

    def existing_outcome(self, bus: ArtifactBus, attempt: int) -> Outcome | None:
        """**永远返回 None。** 见模块说明:这一关重跑比复用旧结论更对。"""
        return None

    async def run(self, context: JobContext) -> Outcome:
        orchestrator = context.orchestrator
        task = context.task
        slot = context.bus.allocate(self.stage)
        outcome = run_pipeline(
            task_id=task.id,
            workdir=orchestrator.workdir(task),
            changed=orchestrator.changed_paths(task),
            policies=orchestrator.scope_policies(task),
            protected=orchestrator.rules.protected,
            project_map=orchestrator.project_map(),
            # 中途扩过权的任务必经人工审批。这是扩权自动放行的对价——真正要守的性质是
            # "没有一次跨计划的改动能在无人过目的情况下合入",而它就守在这里。
            widened=granted_modules(orchestrator.root, task.id),
        )
        outcome.report.write(slot.path)
        slot.write_manifest(
            producer="precheck",
            outputs=["precheck-report.json"],
            summary=f"提交前检查 {'通过' if outcome.report.passed else '未通过'}",
        )
        payload = outcome.payload()
        if outcome.event is not TaskEvent.PRECHECK_FAIL:
            # 检查过了才推。推一个越权或含密钥的分支上去,等于把问题送到远端——
            # 而 git 历史里的东西清理成本极高。
            payload["pushed"] = orchestrator.publish_branches(task)
        _write_result(slot.path, payload)
        return Outcome(event=outcome.event, valid=True, payload=payload)


@dataclass(frozen=True)
class MergeHandler:
    """`MERGING`:子仓 PR 全合 → 指针前移 → 顶层 PR 合并。"""

    state: TaskState = TaskState.MERGING

    @property
    def stage(self) -> str:
        return STAGE_MERGE

    def attempt(self, task: Task) -> int:
        return task.fix_rounds + 1

    def existing_outcome(self, bus: ArtifactBus, attempt: int) -> Outcome | None:
        """合并这一步的幂等靠任务记录里的 PR 引用,不靠产物目录——见模块说明。"""
        return None

    async def run(self, context: JobContext) -> Outcome:
        orchestrator = context.orchestrator
        slot = context.bus.allocate(self.stage)
        event, payload = orchestrator.merge_task(context.task, slot.path)
        slot.write_manifest(producer="orchestrator", summary=f"双层合并: {event.value}")
        _write_result(slot.path, payload)
        return Outcome(event=event, valid=True, payload=payload)


def _write_result(output_dir: Any, payload: dict[str, Any]) -> None:
    import json
    from pathlib import Path

    (Path(output_dir) / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


__all__ = ["STAGE_MERGE", "CommitPipelineHandler", "MergeHandler"]
