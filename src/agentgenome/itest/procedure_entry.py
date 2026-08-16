"""`itest-run` 这个 hybrid Procedure 的脚本入口。

**脚本本身只有三行:import、跑、退出。** 核心逻辑活在只能靠子进程测的脚本里的话,集成
测试的测试会立刻变慢变脆。

## 脚本写完整的 result.json,Agent 只做增量

这是"诊断挂了不该丢掉测试结果"那条要求的实现方式:测试跑完的那一刻,一份合契约的
`result.json` 已经在盘上了。Agent 之后可以覆盖它、往里补 `suspect_files` 与 `suggestion`,
但即使 Agent 超时、契约不符、压根没被拉起来,状态机读到的仍然是完整的测试结果。

反过来做——让 Agent 负责写 result.json——的话,一次诊断失败会让整轮集成测试的结果凭空
消失,任务只能重跑一遍那几分钟的容器。

## 全绿的那一轮不拉 Agent

没有失败就没有东西可诊断。中间产物里的 `skip_agent` 让派发层跳过 Agent 那一段(见
`dispatch._skips_agent`),于是通过的集成测试是零 token 的。

## 涉及模块从 diff 算,不只信入参

入参里的 `modules` 来自计划,那是员工动手**之前**的声明。真正改了什么只有工作树知道,
而集成测试要建的是"这份代码"的镜像。两者取并集——漏建一个改过的模块是静默的错误结果,
多建一个没改过的基本免费。

## 退出码

环境类失败**照样返回 0**。结论在产物的 `kind` 字段里,让退出码也表达一遍的话,"脚本崩了"
与"环境没起来"就分不开了——而前者该修脚本,后者该叫人。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agentgenome.config import ItestConfig, load_config
from agentgenome.gates.runner import modules_touched
from agentgenome.genome.dispatch import INTERMEDIATE, SKIP_AGENT
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.loader import load_project_map
from agentgenome.itest.report import ItestKind, ItestReport
from agentgenome.itest.task_itest import run_task_itest
from agentgenome.space.gitcmd import GitError, git, git_out
from agentgenome.space.scope_guard import touched_paths


def main() -> int:
    workdir = Path(os.environ["AGENTGENOME_WORKDIR"])
    output_dir = Path(os.environ["AGENTGENOME_OUTPUT_DIR"])
    inputs = json.loads(os.environ.get("AGENTGENOME_INPUTS") or "{}")
    output_dir.mkdir(parents=True, exist_ok=True)

    report = run_task_itest(
        workdir=workdir,
        task_id=inputs.get("task_id", "ad-hoc"),
        output_dir=output_dir,
        modules=involved_modules(workdir, inputs.get("modules") or []),
        config=itest_config(workdir),
    )
    write_outputs(report, output_dir)
    return 0


def involved_modules(workdir: Path, declared: list[str]) -> list[str]:
    """这次集成测试要覆盖哪些模块:diff 算出来的,并上计划声明的。

    算不出 diff 时(不是 git 仓、拿不到基线)退回计划声明的那份——比整个集成测试跑不起来好。
    """
    changed = _changed_paths(workdir)
    if not changed:
        return sorted(set(declared))
    try:
        module_paths = load_project_map(workdir).module_paths()
    except GenomeValidationError:
        return sorted(set(declared))
    return sorted(set(declared) | set(modules_touched(changed, module_paths)))


def itest_config(workdir: Path) -> ItestConfig:
    """集成测试的配置。读不出来时退回缺省值——一份坏掉的根配置不该让集成测试变成
    "永远不超时"。"""
    try:
        return load_config(workdir).itest
    except GenomeValidationError:
        return ItestConfig()


def write_outputs(report: ItestReport, output_dir: Path) -> Path:
    """把报告落成三份产物:报告本身、result.json、给 Agent 的中间产物。

    独立成函数是因为手工重跑那条路要用同一份落盘逻辑(见 `agctl itest run`)。
    """
    report.write(output_dir)
    payload = report.as_dict()
    result = output_dir / "result.json"
    result.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / INTERMEDIATE).write_text(
        json.dumps(
            {
                # 全绿或环境没起来时都没有诊断可做:前者没有失败,后者的答案是"叫人",
                # 不是"让 Agent 猜猜看"。
                SKIP_AGENT: report.passed or report.kind is ItestKind.ENVIRONMENT,
                "task_id": report.task_id,
                "failures": payload["failures"],
                "env": payload["env"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return result


def _changed_paths(workdir: Path) -> list[str]:
    try:
        return sorted(touched_paths(workdir, _baseline(workdir)))
    except GitError:
        return []


def _baseline(workdir: Path) -> str | None:
    """这一轮从哪儿开始。与 `gates.procedure_entry` 同一套算法——两处各写一遍的话,
    门禁与集成测试会对"这一轮改了什么"给出两个不同的答案。"""
    try:
        return git_out(workdir, "merge-base", "main", "HEAD")
    except GitError:
        pass
    result = git(workdir, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


__all__ = ["involved_modules", "itest_config", "main", "write_outputs"]
