"""把一个任务的门禁跑起来。

`runner` 只知道"给一个目录和一份配置,跑出一份报告"。这一层负责把任务接上去:这一轮改了
什么、因此该跑哪几个模块、多个模块的报告怎么并成一份。

**只有一份编排。** 状态机走的那条路(`procedure_entry`)与手工重跑那条路(`agctl gate run`)
用的是同一个 `run_modules`。写两份的话它们会各自演进——第一版就已经分叉了:一边捕获配置
错误、一边不捕获,一边算轮次差集、一边不算。

**改动路径只算一次。** 它同时决定两件事:跑哪些模块,以及门禁配置本身有没有被这一轮改过。
两处各自推导的话,迟早出现"按 A 算跑了这个模块、按 B 算它没被改过"这种自相矛盾。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentgenome.core.store import task_dir
from agentgenome.gates.comparison import compare, previous_report
from agentgenome.gates.runner import modules_touched, worse
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.loader import load_project_map
from agentgenome.jobs.artifacts import ArtifactBus, Slot
from agentgenome.space.git_ws import GitWorkspace
from agentgenome.space.scope_guard import touched_paths as scope_touched_paths
from agentgenome.verification import (
    NeedsConfirmation,
    PendingVerification,
    Ready,
    VerificationContext,
    load_bootstrap_specs,
    load_verification_spec,
    record_bootstrap_spec,
    resolve_verification,
    run_verification,
    verification_spec_path,
)
from agentgenome.verification.report import (
    GateOutcome,
    GateReport,
    GateReportKind,
    GateResult,
)


def changed_paths(workspace_root: Path, task_id: str) -> list[str]:
    """这一轮改了哪些路径。worktree 不在时返回空——没开工的任务没有改动。

    走越权检查那一套(`scope_guard.touched_paths`)而不是 `GitWorkspace.diff`:顶层的
    `git status` 把子仓的改动折叠成一个目录名(` M repos/order-service`),而门禁要判断的是
    `repos/order-service/gates.yaml` 这种具体路径——折叠之后那条判断永远不成立。**"这一轮改了什么"
    只该有一个答案来源。**
    """
    workspace = GitWorkspace(workspace_root)
    worktree = workspace.worktree_path(task_id)
    if not worktree.is_dir():
        return []
    return sorted(scope_touched_paths(worktree, workspace.merge_base(task_id)))


def targets_for(workdir: Path, changed: list[str], module_id: str | None = None) -> list[str]:
    """这一轮该跑哪些模块。涉及模块从 diff 路径反查项目地图。"""
    module_paths = load_project_map(workdir).module_paths()
    if module_id:
        return [module_id] if module_id in module_paths else []
    return modules_touched(changed, module_paths)


def run_modules(
    workdir: Path,
    task_id: str,
    output_dir: Path,
    changed: list[str],
    targets: list[str],
    previous: dict[str, Any] | None = None,
    control_root: Path | None = None,
) -> GateReport:
    """跑这几个模块的门禁,并成一份报告。

    **一份而不是每模块一份**:状态机消费的是一个 `result.json`,而"这次门禁过没过"本来
    就是一个问题,不是 N 个。
    """
    merged = GateReport(
        task_id=task_id,
        module=", ".join(targets) or "(无)",
        passed=bool(targets),
        kind=GateReportKind.NONE,
    )
    if not targets:
        # 一个模块都没碰到的门禁没有验证任何东西。判它通过等于凭空发一张绿灯。
        return _nothing_to_verify(merged)

    for target in targets:
        spec_root = Path(control_root) if control_root is not None else workdir
        try:
            module = load_project_map(workdir).module(target)
            confirmed = load_verification_spec(spec_root, target)
            bootstrapped = False
            if confirmed is None:
                task_specs = {spec.module: spec for spec in load_bootstrap_specs(output_dir)}
                confirmed = task_specs.get(target)
                if confirmed is not None:
                    bootstrapped = True
                else:
                    resolution = resolve_verification(target, workdir / module.path)
                    if isinstance(resolution, Ready):
                        confirmed = resolution.spec
                        record_bootstrap_spec(output_dir, confirmed)
                        bootstrapped = True
                    else:
                        assert isinstance(resolution, NeedsConfirmation)
                        pending = PendingVerification(
                            module=target,
                            issues=resolution.issues,
                            candidates=resolution.candidates,
                        )
                        report = _verification_unconfirmed(task_id, target, pending)
                        merged = _merge(merged, report)
                        continue
            relative_spec = verification_spec_path(Path("."), target).as_posix()
            protected = {
                relative_spec,
                (Path(module.path) / "gates.yaml").as_posix(),
            }
            changed_config = next((path for path in changed if path in protected), None)
            if changed_config is not None:
                report = _verification_tampered(task_id, target, changed_config)
            else:
                report = run_verification(
                    confirmed,
                    VerificationContext(
                        task_id=task_id,
                        module_root=workdir / module.path,
                        output_dir=output_dir,
                    ),
                )
                if bootstrapped:
                    report.notes = report.notes + (f"bootstrap_verification:{target}",)
        except (GenomeValidationError, ValueError) as error:
            # 门禁配置本身坏了是配置问题,不是代码问题——别让它吃掉修复轮次。
            detail = error.render() if isinstance(error, GenomeValidationError) else str(error)
            return _config_broken(task_id, target, detail)
        merged = _merge(merged, report)

    merged.regressions, merged.fixed = compare(previous, merged.as_dict())
    return merged


def run_task_gates(
    workspace_root: Path,
    task_id: str,
    output_dir: Path,
    module_id: str | None = None,
) -> GateReport:
    """在任务现有的工作区上跑门禁。`agctl gate run` 走这条路。

    `module_id` 是手工重跑时的收窄——排查环境问题时不必等所有模块跑完。
    """
    root = Path(workspace_root)
    changed = changed_paths(root, task_id)
    workdir = GitWorkspace(root).worktree_path(task_id)
    if not workdir.is_dir():
        # 还没开隔离工作区时退回主 checkout:`agctl gate run` 要能在任何时候被人手工跑。
        workdir = root

    report = run_modules(
        workdir=workdir,
        task_id=task_id,
        output_dir=output_dir,
        changed=changed,
        targets=targets_for(workdir, changed, module_id),
        control_root=root,
    )
    report.write(output_dir)
    return report


def gate_slot(workspace_root: Path, task_id: str, stage: str) -> Slot:
    """给手工重跑开一个新的产物目录。

    不覆盖上一轮:手工重跑常常是"我想看看现在还挂不挂",而覆盖掉的那份正是要对比的基线。
    """
    return ArtifactBus(task_dir(workspace_root, task_id)).allocate(stage)


def previous_gate_report(output_dir: Path, stage: str) -> dict[str, Any] | None:
    """上一轮的门禁报告。

    从产物目录自己找,不靠调用方传:多一条参数的话,忘了传的表现是差集永远为空,而空的
    差集看起来完全正常。
    """
    bus = ArtifactBus(output_dir.parent.parent)
    slots = bus.by_stage(stage)
    attempt = next((index + 1 for index, slot in enumerate(slots) if slot.path == output_dir), None)
    if attempt is None:
        return None
    return previous_report(bus, stage, attempt)


def _merge(merged: GateReport, report: GateReport) -> GateReport:
    """多个模块的报告合成一份。任一模块没过即整体没过。"""
    merged.gates += report.gates
    merged.passed = merged.passed and report.passed
    merged.created_at = merged.created_at or report.created_at
    # 环境类压过语义类:先把环境修好再谈代码对不对。
    merged.kind = worse(merged.kind, report.kind)
    merged.notes = merged.notes + report.notes
    merged.verification_requests = (
        merged.verification_requests + report.verification_requests
    )
    return merged


def _nothing_to_verify(merged: GateReport) -> GateReport:
    merged.passed = False
    merged.kind = GateReportKind.SEMANTIC
    merged.gates = [
        GateResult(
            id="scope",
            outcome=GateOutcome.FAILED,
            required=True,
            detail="这一轮的改动没有落在任何模块里,门禁没有验证任何东西。",
        )
    ]
    return merged


def _config_broken(task_id: str, module: str, detail: str) -> GateReport:
    return GateReport(
        task_id=task_id,
        module=module,
        passed=False,
        kind=GateReportKind.ENVIRONMENT,
        gates=[
            GateResult(
                id="config",
                outcome=GateOutcome.UNAVAILABLE,
                required=True,
                detail=f"门禁配置读不出来: {detail}",
            )
        ],
    )


def _verification_tampered(task_id: str, module: str, path: str) -> GateReport:
    return GateReport(
        task_id=task_id,
        module=module,
        passed=False,
        kind=GateReportKind.TAMPERED,
        gates=[
            GateResult(
                id="config",
                outcome=GateOutcome.REFUSED,
                required=True,
                detail=f"任务修改了受保护的验证规格: {path}",
            )
        ],
    )


def _verification_unconfirmed(
    task_id: str, module: str, pending: PendingVerification | None
) -> GateReport:
    details = (
        "; ".join(issue.detail for issue in pending.issues)
        if pending is not None
        else ""
    )
    reason = details or "没有已确认的 v2 模块验证规格"
    return GateReport(
        task_id=task_id,
        module=module,
        passed=False,
        kind=GateReportKind.ENVIRONMENT,
        gates=[
            GateResult(
                id="config",
                outcome=GateOutcome.UNAVAILABLE,
                required=True,
                detail=f"验证规格需要深入分析: {reason}。编排器将自动派架构员工调查。",
            )
        ],
        verification_requests=(pending,) if pending is not None else (),
    )


__all__ = [
    "changed_paths",
    "gate_slot",
    "previous_gate_report",
    "run_modules",
    "run_task_gates",
    "targets_for",
]
