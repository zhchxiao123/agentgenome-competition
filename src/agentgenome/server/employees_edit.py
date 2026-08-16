"""从界面拧一个员工的执行档位。

## 一个动作,两个存储

`auto` / `assisted` / `manual` 是信任爬坡上的三个点,但它们住在两个地方:`manual` 是员工
定义里的运行时(**版本化资产**),`assisted` 是根配置里的确认名单。**分裂的存储不该变成
分裂的操作**——人要表达的是"这个角色我信到什么程度",不是"这件事存在哪个文件里"。所以
分派在这一层做,不摊给界面。

## 失败时停在**监督更多**的那一边

跨两个存储没有事务。所以写入顺序按一条规则排:**先做加监督的那一半**。

- 往 `manual` 走:先把运行时改成 human,再把它从确认名单里摘掉。中间失败 → 它已经是
  manual(整步交给人),只是名单里还挂着一条不起作用的记录。
- 从 `manual` 下来到 `assisted`:先进确认名单,再把运行时改回机器。中间失败 → 它还是
  manual。

反过来排(先降档)在两条路上都会把人**降到 auto**——一次失败的写入换来一个比请求前监督
更少的状态,而没有人会收到"你现在什么都不用确认了"这个通知。至于"human 却又在确认名单
里",它不是矛盾:`execution_mode` 认运行时,名单那条只是没人读的死记录。

## 只放开两个字段

运行时与指派人。工序白名单、权限、写集是安全边界,从界面改它们是另一件事——而且大概率
不该有。这里用**白名单式重写**:读出原文件、只改这两个键、再整份校验,于是新增字段不会
被这条路径悄悄抹掉。

## 写坏了不会在写的时候报错

员工定义是不可变模型且拒绝未知键:一份写坏的定义不会在写入时炸,会在**下一次工作区启动**
时让整个系统起不来。所以落盘之后立刻跑一次**整册严格加载**,不通过就把文件改回去。

## 审计走设置那一条

同一个问题("谁改了这个项目的配置")不该有两条查法。这里记的 `section` 是
`employees/<id>`,于是 `agctl settings history` 与设置历史页原样就能看到它。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentgenome import paths
from agentgenome.config import HUMAN_RUNTIME, Config, load_config
from agentgenome.core.events import SYSTEM_SUBJECT, EventLog, LogKind
from agentgenome.employees import (
    ASSISTED,
    AUTO,
    MANUAL,
    EmployeeNotFound,
    load_employees,
    workspace_employees_root,
)
from agentgenome.genome.dispatch import (
    DispatchRefused,
    check_runtime_guarantees,
    runtime_compatible,
)
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.procedures import load_workspace_registry
from agentgenome.server.rbac import Action, Principal
from agentgenome.server.settings import Change, Entrance
from agentgenome.server.settings import update as update_settings
from agentgenome.space.gitcmd import ORCHESTRATOR_IDENTITY, git, git_out, is_repo_root

#: 三档的合法取值。
RUNGS = (AUTO, ASSISTED, MANUAL)


class UnknownRung(ValueError):
    """没有这一档。"""


class NeedsAssignee(ValueError):
    """`manual` 必须有指派人。

    **不是防御性编程**:human 运行时的活会变成待办,没有主人的待办不会被任何人看到,
    只会在三窗口超时之后升级——一次静默的死循环。
    """


def set_execution(
    workspace_root: Path,
    principal: Principal,
    employee_id: str,
    rung: str,
    assignee: str = "",
    entrance: Entrance = Entrance.API,
) -> Change:
    """把一个员工挪到 `rung` 这一档。返回最后那次落地的变更记录。"""
    principal.ensure(Action.EDIT_SETTINGS)
    if rung not in RUNGS:
        raise UnknownRung(f"没有 {rung} 这一档(能选的: {', '.join(RUNGS)})")

    root = Path(workspace_root)
    registry = load_employees(workspace_employees_root(root))
    employee = registry.get(employee_id)
    config = load_config(root)

    who = assignee or employee.assignee
    if rung == MANUAL and not who:
        raise NeedsAssignee(
            f"{employee_id} 要转成 manual 必须给指派人:它的活会变成待办,"
            "没有主人的待办不会被任何人看到,只会静默超时。"
        )

    listed = employee_id in config.topology.assisted.employees
    change: Change | None = None

    if rung == MANUAL:
        # **先加监督**:运行时先变 human,再摘确认名单。中途失败时它已经是 manual,
        # 而不是被降到 auto——见模块说明。
        change = _write_definition(
            root, principal, employee_id, runtime=HUMAN_RUNTIME, assignee=who, entrance=entrance
        )
        if listed:
            change = _write_list(
                root, principal, load_config(root), employee_id, listed=False, entrance=entrance
            )
        return change

    wants_list = rung == ASSISTED
    if wants_list != listed:
        change = _write_list(
            root, principal, config, employee_id, listed=wants_list, entrance=entrance
        )
    # 从 manual 下来要换运行时;还有一种情况也得写定义:**这一档填了新的指派人**。
    # 填了不落下去比不给填更糟——人以为自己给这些活指了个主人,而实际上没有。
    coming_down = employee.runtime == HUMAN_RUNTIME
    if coming_down or who != employee.assignee:
        change = _write_definition(
            root,
            principal,
            employee_id,
            runtime=_machine_runtime(config) if coming_down else employee.runtime,
            assignee=who,
            entrance=entrance,
        )
    if change is None:
        # 已经在这一档了。**不写空提交**,但也不报错:重复点一次同一个档位是常事。
        change = Change(
            actor=principal.subject, section=_section(employee_id), at="", entrance=entrance
        )
    return change


class RuntimeNotConfigured(ValueError):
    """选了一个这次部署没有配置的运行时。

    **在写之前拒绝。** 写下去的话,装配不出来的失败会推迟到下一次派发——而那时的
    报错指向的是运行时注册表,不是这次点击。
    """


def machine_runtimes(config: Config) -> list[str]:
    """这次部署实际配置过的**机器**运行时。

    界面的下拉框读它。**前端不自己枚举**:枚举的话,配置里没配的名字会出现在选项里,
    选中之后装配失败,而失败发生在下一次派发、不在点击那一刻。

    人不在里面:它不是"跑在哪儿"的一个选项,而是执行档位那条路的终点——那条路还管
    指派人与确认名单,少走一步就会造出"human 却没有主人"的待办。
    """
    return [name for name in config.runtime.runtimes if name != HUMAN_RUNTIME]


def set_runtime(
    workspace_root: Path,
    principal: Principal,
    employee_id: str,
    runtime: str,
    entrance: Entrance = Entrance.API,
) -> Change:
    """把一个员工挪到另一个机器运行时上。

    **只管"跑在哪儿"**,不碰指派人与确认名单——那些归执行档位那条路。走的是与它同一条
    白名单重写:读原文件、只改这一个键、整册严格加载、提交、记事件。
    """
    principal.ensure(Action.EDIT_SETTINGS)
    root = Path(workspace_root)
    config = load_config(root)
    allowed = machine_runtimes(config)
    if runtime not in allowed:
        raise RuntimeNotConfigured(
            f"{runtime} 不是这次部署配置过的机器运行时(可选: {', '.join(allowed) or '(空)'})。"
            + ("转成人来做要走执行档位那条路。" if runtime == HUMAN_RUNTIME else "")
        )

    employee = load_employees(workspace_employees_root(root)).get(employee_id)
    if employee.runtime == runtime:
        # 重复点一次同一个选项是常事。**不写空提交**,但也不报错——与执行档位那条同一规则。
        return Change(
            actor=principal.subject, section=_section(employee_id), at="", entrance=entrance
        )
    return _write_definition(
        root,
        principal,
        employee_id,
        runtime=runtime,
        assignee=employee.assignee,
        entrance=entrance,
    )


def compat_gap(workspace_root: Path, employee_id: str, runtime: str) -> list[str]:
    """这个员工的工序里,哪些会被派发时的兼容闸挡下。**只读**。

    **判据向派发闸借**(`dispatch.runtime_compatible`),不在这里重写一份。这里问的
    正是它问的那个问题,而重写的那份会漏掉它的豁免:空声明表示"不限制"、纯脚本声明
    `none` 表示"我不碰任何运行时"、人与回放对兼容性透明。漏掉任何一条,界面都会把一道
    **本来就跑得动**的工序列成缺口——而旁边那个一键补声明的按钮会把这个运行时写进一份
    版本化资产里,让它从此声明一件不成立的事。
    """
    root = Path(workspace_root)
    employee = load_employees(workspace_employees_root(root)).get(employee_id)
    registry = load_workspace_registry(root)
    gap: list[str] = []
    for procedure_id in employee.procedures:
        try:
            spec = registry.get(procedure_id)
        except KeyError:
            # 员工声明了一道注册表里没有的工序,那是另一个问题(花名册校验管它),
            # 不该在这里表现为"兼容缺口"。
            continue
        if not runtime_compatible(spec, runtime):
            gap.append(procedure_id)
    return gap


def runtime_blocks(
    workspace_root: Path, employee_id: str, runtimes: list[str]
) -> list[tuple[str, str]]:
    """这些运行时里,哪些**根本接不了**这个员工,以及那句给人看的理由。

    **判据同样向派发闸借**(`dispatch.check_runtime_guarantees`)。与兼容缺口分开报:
    缺口补一次声明就没了,这一类补不掉——它说的是运行时给不出这个员工要的保证。混成
    一件事的话,人会去补声明,补完照样被拒,而那句报错讲的是另一回事。
    """
    root = Path(workspace_root)
    employee = load_employees(workspace_employees_root(root)).get(employee_id)
    blocked: list[tuple[str, str]] = []
    for runtime in runtimes:
        try:
            check_runtime_guarantees(employee, runtime)
        except DispatchRefused as error:
            blocked.append((runtime, str(error)))
    return blocked


def declare_compat(
    workspace_root: Path,
    principal: Principal,
    procedure_ids: list[str],
    runtime: str,
    entrance: Entrance = Entrance.API,
) -> Change:
    """给这些工序补上"兼容这个运行时"的声明。

    **显式动作。** 兼容闸的语义是"只在一个运行时上验证过的工序不该悄悄换台跑";
    在切换运行时时自动补声明等于把这道闸门变成摆设。界面负责把差距列清楚、让补声明
    只要一次点击,判断仍归人。
    """
    principal.ensure(Action.EDIT_GENOME)
    root = Path(workspace_root)
    # 原文先留着。**这一批要么全落,要么一个都不落**:补声明是一次动作,而"三道工序补了
    # 两道"这种半截状态没有任何界面能表达——人看到的是"保存失败",实际上盘上已经变了。
    targets = {pid: root / paths.PROCEDURES / pid / "procedure.yaml" for pid in procedure_ids}
    # **存在性全部先查一遍,一个字节都还没写。** 边写边查的话,列表里第三个不存在时,
    # 前两个已经落在盘上了——而调用方收到的是一个异常,它读起来像"什么都没发生"。
    for procedure_id, path in targets.items():
        if not path.is_file():
            raise EmployeeNotFound(f"没有这道工序的定义: {procedure_id}")

    originals: dict[Path, str] = {}
    for path in targets.values():
        original = path.read_text(encoding="utf-8")
        payload: dict[str, Any] = yaml.safe_load(original) or {}
        compat = dict(payload.get("compat") or {})
        runtimes = list(compat.get("runtimes") or [])
        if runtime in runtimes:
            continue  # 已经声明过。不写空提交。
        runtimes.append(runtime)
        compat["runtimes"] = runtimes
        payload["compat"] = compat
        originals[path] = original
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    touched = list(originals)
    if not touched:
        return Change(
            actor=principal.subject,
            section="genome/procedures",
            at="",
            entrance=entrance,
        )

    try:
        # 重载一遍,**并且看我们写的这几道有没有被拒**。
        #
        # 只调用 `load_workspace_registry` 是不够的:它刻意不抛——一个手滑写坏的实验性
        # 工序不该让编排器罢工,坏的那些进 `rejected`。所以"没抛异常"在这里不等于"没写坏",
        # 而写坏的后果是这道工序从此静默地不可用,没有任何报错指向这次保存。
        rejected = load_workspace_registry(root).rejected
        broken = [pid for pid in procedure_ids if pid in rejected]
        if broken:
            raise GenomeValidationError(
                [
                    ValidationIssue(
                        file=str(targets[pid].relative_to(root)),
                        message=f"补了兼容声明之后这道工序加载不了: {rejected[pid]}",
                    )
                    for pid in broken
                ]
            )
        rev = _commit_paths(root, touched, principal.subject, runtime)
    except Exception:
        for path, original in originals.items():
            path.write_text(original, encoding="utf-8")
            git(root, "reset", "-q", "--", str(path.relative_to(root)), check=False)
        raise
    event = EventLog(root).append(
        SYSTEM_SUBJECT,
        actor=principal.subject,
        kind=LogKind.CONFIG_CHANGED,
        payload={
            "section": "genome/procedures",
            "entrance": entrance.value,
            "rev": rev,
            "runtime": runtime,
        },
    )
    return Change(
        actor=principal.subject,
        section="genome/procedures",
        at=event.ts.isoformat(),
        entrance=entrance,
        rev=rev,
    )


def _commit_paths(root: Path, paths_: list[Path], actor: str, runtime: str) -> str:
    """把这一批工序定义提交进版本面。与员工定义那条同一规则:不是仓库就返回空。"""
    if not is_repo_root(root):
        return ""
    relatives = [str(path.relative_to(root)) for path in paths_]
    git(root, "add", "--", *relatives)
    if git(root, "diff", "--cached", "--quiet", "--", *relatives, check=False).returncode == 0:
        return ""
    git(
        root,
        *ORCHESTRATOR_IDENTITY,
        "commit",
        "-m",
        f"chore(procedures): {actor} 声明兼容 {runtime}",
        "--",
        *relatives,
    )
    return git_out(root, "rev-parse", "HEAD")


def _machine_runtime(config: Config) -> str:
    """从 manual 下来时回到哪个运行时。

    **回项目的缺省运行时。** 原来那个是什么已经不在文件里了(改成 human 时被覆盖掉),
    而项目缺省是这个问题唯一有据可依的答案。
    """
    return config.runtime.default


def _section(employee_id: str) -> str:
    return f"employees/{employee_id}"


def _write_list(
    root: Path,
    principal: Principal,
    config: Config,
    employee_id: str,
    listed: bool,
    entrance: Entrance,
) -> Change:
    """改确认名单。**走设置那条写入路径**,一行新的写入代码都不写。"""
    assisted = config.topology.assisted
    employees = [item for item in assisted.employees if item != employee_id]
    if listed:
        employees.append(employee_id)
    value = config.topology.model_dump(mode="json")
    value["assisted"] = {**value["assisted"], "employees": employees}
    return update_settings(root, principal, "topology", value, entrance=entrance)


def _write_definition(
    root: Path,
    principal: Principal,
    employee_id: str,
    runtime: str,
    assignee: str,
    entrance: Entrance,
) -> Change:
    """改员工定义里的运行时与指派人。别的键原样留着。"""
    path = workspace_employees_root(root) / f"{employee_id}.yaml"
    if not path.is_file():
        raise EmployeeNotFound(f"没有这个员工定义: {employee_id}")

    original = path.read_text(encoding="utf-8")
    payload: dict[str, Any] = yaml.safe_load(original) or {}
    payload["runtime"] = runtime
    payload["assignee"] = assignee

    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    try:
        # **跑一次完整花名册加载**,不只是解析这一份:单份合法而整册不合法是存在的
        # (归属排他冲突),而那种坏法同样会让下一次启动失败。严格模式在这里是关键——
        # 非严格会跳过坏定义,于是这道校验会对一份写坏的文件说"没问题"。
        load_employees(workspace_employees_root(root), strict=True)
        rev = _commit(root, path, employee_id, principal.subject)
    except Exception:
        path.write_text(original, encoding="utf-8")
        git(root, "reset", "-q", "--", str(path), check=False)
        raise

    event = EventLog(root).append(
        SYSTEM_SUBJECT,
        actor=principal.subject,
        kind=LogKind.CONFIG_CHANGED,
        # 与设置变更同一条记录形状:**内容进 git,动作进事件面**。
        payload={"section": _section(employee_id), "entrance": entrance.value, "rev": rev},
    )
    return Change(
        actor=principal.subject,
        section=_section(employee_id),
        at=event.ts.isoformat(),
        entrance=entrance,
        rev=rev,
    )


def _commit(root: Path, path: Path, employee_id: str, actor: str) -> str:
    """把员工定义提交进版本面。不是 git 仓库、或内容没变时返回空——与设置那条同一规则。"""
    if not is_repo_root(root):
        return ""
    relative = str(path.relative_to(root))
    git(root, "add", "--", relative)
    if git(root, "diff", "--cached", "--quiet", "--", relative, check=False).returncode == 0:
        return ""
    git(
        root,
        *ORCHESTRATOR_IDENTITY,
        "commit",
        "-m",
        f"chore(employees): {actor} 改了 {employee_id} 的执行档位",
        "--",
        relative,
    )
    return git_out(root, "rev-parse", "HEAD")


__all__ = [
    "RUNGS",
    "NeedsAssignee",
    "RuntimeNotConfigured",
    "UnknownRung",
    "compat_gap",
    "declare_compat",
    "machine_runtimes",
    "set_execution",
    "set_runtime",
]
