"""把一个任务的集成测试跑起来。

`env` 只知道"拉一套环境起来、在里面跑命令、销毁";`report` 只知道"给我失败清单,我给你
一份报告"。这一层负责把它们接上任务:这一轮涉及哪些模块、因此该在哪几个服务里跑什么、
跑出来的东西怎么变成一份报告。

**只有一份编排。** 状态机走的那条路(`procedure_entry`)与手工重跑那条路(`agctl itest run`)
用的是同一个 `run_task_itest`。写两份的话它们会各自演进——门禁那边第一版就已经分叉过一次。

## 闭包从 diff 出发,不是从计划出发

计划是员工动手**之前**的声明,diff 是它实际改了什么。只按计划算闭包的话,一个计划里没写、
但确实被改过的模块会用**旧镜像**参与集成测试——测出来的绿灯对应的不是这份代码。那正好是
这套集成测试存在的理由:跨模块的盲区。

计划里的模块仍然并进来:那是员工声明的意图,而多建一个没改过的模块基本是免费的(镜像层
有缓存),漏建一个改过的却是静默的错误结果。

## 跑什么

闭包内每个声明了 `itest_cmd` 的模块,在同名服务里跑那条命令,一个模块一条用例。
**没有任何模块声明 `itest_cmd` 时判失败**,不是判通过——一次什么都没验的集成测试发绿灯,
比不跑更危险,因为它会被当作"验过了"。

## 契约驱动的接口测试与端到端标记不在这一版里

PRD 写的是"基于项目地图的 `interfaces[].schema` 做契约驱动的接口测试"与"跑打了端到端标记
的用例"。这一版**两件都没有单独实现**:仓里没有契约测试引擎,而"端到端标记"是项目自己那套
用例里的事。这里做的是跑项目声明的 `itest_cmd`——接口断言与端到端用例都写在那套用例里,
由项目负责挑选。

报告的 `env.interfaces` 记下这一轮闭包覆盖了哪些契约,这样"这次跑覆盖了哪些接口"至少是
可回答的;但**覆盖不等于验证过**,不要把这个字段读成后者。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agentgenome.config import ItestConfig
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.loader import load_project_map
from agentgenome.genome.models import ProjectMap
from agentgenome.itest.env import (
    ComposeEnvironment,
    EnvPlan,
    EnvUnavailable,
    plan_environment,
)
from agentgenome.itest.report import (
    EnvSnapshot,
    ItestFailure,
    ItestKind,
    ItestReport,
    archive_logs,
    repro_command,
    tail_of,
)


def run_task_itest(
    workdir: Path,
    task_id: str,
    output_dir: Path,
    modules: Sequence[str],
    config: ItestConfig | None = None,
    docker: str = "docker",
) -> ItestReport:
    """在 `workdir` 上跑一次集成测试,产出报告。

    **不抛异常。** 环境起不来、地图读不出来、没有用例可跑——这些都变成一份 `kind` 说明
    性质的报告。抛出去的话状态机就拿不到任何可消费的产物,而"这次为什么没跑成"恰恰是
    最需要落盘的信息。
    """
    workdir = Path(workdir)
    settings = config or ItestConfig()
    try:
        project_map = load_project_map(workdir)
    except GenomeValidationError as error:
        return _environment_report(task_id, f"项目地图读不出来: {error.render()}")

    try:
        plan = plan_environment(workdir, task_id, modules, project_map, Path(settings.compose_file))
    except EnvUnavailable as error:
        return _environment_report(task_id, str(error))

    cases = _cases(plan, project_map)
    snapshot = EnvSnapshot(
        project=plan.project,
        compose_file=str(plan.compose_file.relative_to(workdir)),
        built_modules=plan.modules,
        submodule_pointers=capture_pointers(workdir),
        interfaces=_interfaces_in_scope(plan, project_map),
    )
    if not cases:
        return _nothing_to_run(task_id, snapshot, plan)

    try:
        failures, logs = _execute(workdir, plan, cases, settings, docker)
    except EnvUnavailable as error:
        return _environment_report(task_id, str(error), snapshot)

    report = ItestReport(
        task_id=task_id,
        env=snapshot,
        failures=failures,
        kind=ItestKind.SEMANTIC if failures else ItestKind.NONE,
    )
    report.log_paths = archive_logs(output_dir, logs)
    return report


def capture_pointers(workdir: Path) -> dict[str, str]:
    """这一刻各子仓实际停在哪个 commit 上。

    取子仓工作树的 HEAD,不取顶层 `HEAD:<path>` 记的 gitlink:后者是"上次提交时指向哪",
    而报告要回答的是"**刚才测的是**哪一组版本"。任务分支上指针还没提交时两者不同。

    住在这一层而不是 `report` 里:`report` 是纯的(路径进、结构出),把一次 git 调用放进去
    会让那句"这一层不跑子进程"变成一句假话,而假的模块文档比没有文档更贵。

    读不出来的子仓不出现在结果里,不要用空串冒充——"没记录"与"指向空"是两回事。
    """
    from agentgenome.space.git_ws import submodule_paths
    from agentgenome.space.gitcmd import GitError, git

    root = Path(workdir)
    try:
        modules = sorted(submodule_paths(root))
    except GitError:
        return {}

    found = {}
    for path in modules:
        result = git(root / path, "rev-parse", "HEAD", check=False)
        if result.returncode == 0 and result.stdout.strip():
            found[path] = result.stdout.strip()
    return found


def _interfaces_in_scope(plan: EnvPlan, project_map: ProjectMap) -> tuple[str, ...]:
    """闭包覆盖到的契约 id。provider 或任一 consumer 在闭包内就算覆盖。"""
    inside = set(plan.modules)
    return tuple(
        face.id for face in project_map.interfaces if inside & {face.provider, *face.consumers}
    )


def _cases(plan: EnvPlan, project_map: ProjectMap) -> list[tuple[str, list[str]]]:
    """闭包内每个声明了集成用例命令的模块,一条用例。"""
    commands = {module.id: module.itest_cmd for module in project_map.modules}
    found = []
    for module_id in plan.modules:
        command = commands.get(module_id)
        if command:
            found.append((module_id, command.split()))
    return found


def _execute(
    workdir: Path,
    plan: EnvPlan,
    cases: list[tuple[str, list[str]]],
    settings: ItestConfig,
    docker: str,
) -> tuple[list[ItestFailure], dict[str, str]]:
    """拉起环境、灌数据、逐条跑、收日志。销毁由上下文管理器在 `finally` 里保证。"""
    failures: list[ItestFailure] = []
    logs: dict[str, str] = {}
    with ComposeEnvironment(workdir, plan, timeout_s=settings.timeout_s, docker=docker) as env:
        if settings.seed_cmd:
            # 灌数据在跑用例**之前**,而且失败即中止:没灌上就开跑的话,失败会以
            # "业务逻辑不对"的样子出现,而实际原因是库里没数据。
            env.seed(settings.seed_service, settings.seed_cmd.split())
        for service, argv in cases:
            result = env.run(service, argv)
            if result.ok:
                continue
            tail, truncated = tail_of(result.output, settings.log_tail_lines)
            failures.append(
                ItestFailure(
                    case=f"{service}: {' '.join(argv)}",
                    message=f"集成用例失败(退出码 {result.exit_code})",
                    log_tail=tail,
                    log_truncated=truncated,
                    # 环境跑完就销毁,所以复现命令要带上重建——粘贴它的那台机器上
                    # 什么都没有。
                    repro_cmd=repro_command(plan, service, argv, with_build=True),
                )
            )
        # 收日志放在销毁**之前**:`down -v` 之后容器就不在了,那时再收什么都没有。
        logs = env.logs()
    return failures, logs


def _environment_report(
    task_id: str, detail: str, snapshot: EnvSnapshot | None = None
) -> ItestReport:
    """环境类失败。改业务代码解决不了,每一轮都会以完全相同的方式失败。"""
    return ItestReport(
        task_id=task_id,
        env=snapshot or EnvSnapshot(project="(未拉起)", compose_file="(未知)"),
        failures=[ItestFailure(case="environment", message=detail)],
        kind=ItestKind.ENVIRONMENT,
    )


def _nothing_to_run(task_id: str, snapshot: EnvSnapshot, plan: EnvPlan) -> ItestReport:
    """一条用例都没有的集成测试没有验证任何东西。判它通过等于凭空发一张绿灯。

    算环境类:少的是项目地图里的 `itest_cmd` 声明,让开发员工去改业务代码修不好它。
    """
    return ItestReport(
        task_id=task_id,
        env=snapshot,
        failures=[
            ItestFailure(
                case="scope",
                message=(
                    f"闭包内的模块({', '.join(plan.modules) or '(空)'})"
                    "没有一个声明了 itest_cmd,这一轮什么都没验。"
                ),
            )
        ],
        kind=ItestKind.ENVIRONMENT,
    )


__all__ = ["capture_pointers", "run_task_itest"]
