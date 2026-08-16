"""READY_TO_COMMIT:一道完全确定性的关卡。

顺序执行,**不经过任何 Agent**:

1. 越权复查——diff 范围是否超出这个任务各员工授权路径的并集;
2. 密钥扫描——暂存内容里有没有凭证;
3. 风险评级——低风险直接合,高风险停下等人。

前两道任一失败 → `precheck_fail` → 回 `DEVELOPING`,越权/泄密的具体内容作为诊断注入下一轮。
第三道不会"失败",它只决定去 `MERGING` 还是 `REVIEWING`。

## 为什么风险评级排在检查之后

一个越权的改动不需要知道它的风险等级——它压根不该往下走。反过来先评级的话,事件流里会出现
"高风险 → 越权失败"这种读起来像是被审批拦下的记录,而实际原因完全不同。

## 幂等

这一关没有 token 成本,重跑一遍是安全的,所以不做产物幂等——重跑反而更对:崩溃恢复时工作树
可能已经变了,拿旧结论往下走才是错的。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentgenome.core.states import TaskEvent
from agentgenome.core.verdict import VerdictKind
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.rules import ProtectedRules
from agentgenome.security.precheck import PrecheckReport, run_prechecks
from agentgenome.security.risk import DiffStat, RiskAssessment, assess
from agentgenome.space.gitcmd import GitError, git

#: 产物 stage 名。
STAGE_PRECHECK = "precheck"


@dataclass(frozen=True)
class PipelineOutcome:
    """流水线跑完的结论。"""

    event: TaskEvent
    report: PrecheckReport
    risk: RiskAssessment | None = None

    def payload(self) -> dict[str, Any]:
        found = self.report.as_dict()
        if self.risk is not None:
            found |= self.risk.as_dict()
        return found


def run_pipeline(
    task_id: str,
    workdir: Path,
    changed: list[str],
    policies: Any,
    protected: ProtectedRules,
    project_map: ProjectMap | None,
    stat: DiffStat | None = None,
    widened: Sequence[str] = (),
) -> PipelineOutcome:
    """跑完整条关卡,给出该送进状态机的事件。"""
    report = run_prechecks(task_id=task_id, workdir=workdir, changed=changed, policies=policies)
    if not report.passed:
        return PipelineOutcome(event=TaskEvent.PRECHECK_FAIL, report=report)

    risk = assess(changed, stat or diff_stat(workdir), protected, project_map, widened)
    event = TaskEvent.RISK_HIGH if risk.is_high else TaskEvent.RISK_LOW
    return PipelineOutcome(event=event, report=report, risk=risk)


def diff_stat(workdir: Path, base: str = "main") -> DiffStat:
    """这次改动加了多少行、删了多少行。

    算不出来时返回零而不是抛错:拿不到行数只会让"大批量删除"这一条规则失效,而整条流水线
    因此崩掉的代价大得多。**这个降级方向要留痕**——所以调用方拿到的是一个 `DiffStat(0, 0)`,
    它在报告里看得见。
    """
    for spec in (f"{base}...HEAD", "HEAD"):
        try:
            raw = git(workdir, "diff", "--numstat", spec, check=False)
        except GitError:
            continue
        if raw.returncode != 0:
            continue
        added, deleted = _parse_numstat(raw.stdout)
        if added or deleted:
            return DiffStat(added=added, deleted=deleted)
    return DiffStat()


def _parse_numstat(raw: str) -> tuple[int, int]:
    """`git diff --numstat` 的两列。二进制文件那两列是 `-`,跳过。"""
    added = deleted = 0
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        added += int(parts[0])
        deleted += int(parts[1])
    return added, deleted


def is_environment_failure(report: PrecheckReport) -> bool:
    """这次失败是不是环境问题。是的话该升级人工,不该进修复循环。"""
    return report.kind is VerdictKind.ENVIRONMENT


__all__ = [
    "STAGE_PRECHECK",
    "PipelineOutcome",
    "diff_stat",
    "is_environment_failure",
    "run_pipeline",
]
