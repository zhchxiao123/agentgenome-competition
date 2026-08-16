"""`unit-gate` 这个确定性 Procedure 的入口。

**脚本本身只有三行:import、跑、退出。** 核心逻辑活在只能靠子进程测的脚本里的话,门禁的
测试会立刻变慢变脆——而门禁是这套系统里最需要被密集测试的东西之一。

它在隔离工作区里跑,所以"这一轮改了什么"直接从那棵树上算,不需要 Workspace 根的任何东西。
编排本身与 `agctl gate run` 共用 `task_gates.run_modules`——两份编排会各自演进。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agentgenome.gates.task_gates import previous_gate_report, run_modules, targets_for
from agentgenome.space.gitcmd import GitError, git, git_out
from agentgenome.space.scope_guard import touched_paths
from agentgenome.verification.report import GateReport


def main() -> int:
    """跑这次涉及的模块的门禁,写出报告。

    **退出码永远是 0。** 门禁失败不是脚本失败——结论在产物里,让退出码也表达一遍的话,
    "脚本崩了"与"门禁没过"就分不开了。
    """
    workdir = Path(os.environ["AGENTGENOME_WORKDIR"])
    output_dir = Path(os.environ["AGENTGENOME_OUTPUT_DIR"])
    inputs = json.loads(os.environ.get("AGENTGENOME_INPUTS") or "{}")
    output_dir.mkdir(parents=True, exist_ok=True)

    report = _run(workdir, output_dir, inputs)
    report.write(output_dir)
    (output_dir / "result.json").write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


def _run(workdir: Path, output_dir: Path, inputs: dict[str, Any]) -> GateReport:
    """算出这一轮改了什么、该跑哪些模块,然后交给共用的编排。"""
    changed = sorted(touched_paths(workdir, _baseline(workdir)))
    return run_modules(
        workdir=workdir,
        task_id=inputs.get("task_id", "ad-hoc"),
        output_dir=output_dir,
        changed=changed,
        targets=targets_for(workdir, changed),
        previous=previous_gate_report(output_dir, output_dir.name.split("-", 1)[-1]),
        control_root=_control_root(workdir),
    )


def _control_root(workdir: Path) -> Path:
    """从 Git common dir 找 Workspace 控制 checkout；已存在任务也能看到新确认规格。"""
    common = Path(git_out(workdir, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = workdir / common
    return common.resolve().parent


def _baseline(workdir: Path) -> str | None:
    """这一轮从哪儿开始。算不出来时退回"只看工作树"——比整个门禁跑不起来好。"""
    try:
        return git_out(workdir, "merge-base", "main", "HEAD")
    except GitError:
        pass
    result = git(workdir, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


__all__ = ["main"]
