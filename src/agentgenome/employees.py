"""数字员工的定义与权限。

**员工 = 一个 YAML 文件,不是代码。** 调整角色定位就是改文件、走 git 评审,不必改代码
重新发版。

## 默认值站在安全的那一侧

空的 `write_paths` 表示"什么都不能写",空的 `procedures` 表示"什么都不能调用"。反过来
(空 = 不限制)看起来更方便,但它意味着一份写了一半的员工定义拥有完全权限——而写了一半
的配置恰恰是最常见的形态。

## 路径判定不在这里

`write_paths` / `forbid_paths` 的匹配语义住在 `core.scope`:同一套规则既要在派发时
回答"这个员工能写哪儿",又要在 Job 结束后回答"它实际碰的这些路径越界了没有"。
两处各写一遍的话,迟早有一处的 glob 语义更宽——而更宽的那一处就是漏洞。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentgenome.config import HUMAN_RUNTIME
from agentgenome.core.scope import ScopePolicy
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue

#: 信任爬坡的三档。**与指标 `agentgenome_execution_mode_total` 同一套词**:一个说"配置成
#: 了什么",一个说"实际发生了什么",两者必须能并排读——各叫各的名字就读不到一起去,而
#: "自动化率"这个数恰恰只有并排读才有意义(见 PRD 37 的 Goodhart 提醒)。
AUTO = "auto"
ASSISTED = "assisted"
MANUAL = "manual"

if TYPE_CHECKING:
    from agentgenome.config import Config

#: 员工定义在 Workspace 里的位置。
EMPLOYEES_SUBDIR = Path("employees")

#: `write_paths` 里的任务占位符。
TASK_ID_PLACEHOLDER = "{task_id}"

#: 本次任务涉及的业务模块。**它展开成 N 条 glob,每个模块一条**——这是它与 `{task_id}`
#: 唯一的形态差别,也是展开函数返回列表而不是字符串的原因。
#:
#: 选占位符而不是在匹配层做隐式交集,理由有三:从 `dev.yaml` 能一眼读出"这个角色是按任务
#: 收敛的"(隐式收窄会把这条最重要的性质藏进代码);对不用它的员工零影响;glob 匹配层
#: 一个字不用改。
TASK_MODULES_PLACEHOLDER = "{task_modules}"

_STRICT = ConfigDict(extra="forbid")


class EmployeeNotFound(LookupError):
    """没有这个员工。"""


class ProcedureNotAllowed(PermissionError):
    """这个 Procedure 不在该员工的白名单里。

    这是**员工能力边界**,与 Procedure 自己的 `trigger.states` 是不同层次的检查:
    那道防的是配置错误(把门禁 Procedure 派给开发态),这道防的是越界调用。
    """


class Tools(BaseModel):
    model_config = _STRICT

    allow: list[str] = Field(default_factory=list)
    #: 黑名单。开发员工默认断网靠的就是它。
    deny: list[str] = Field(default_factory=list)


class Permissions(BaseModel):
    model_config = _STRICT

    #: 可写路径的 glob。**空表示什么都不能写。**
    write_paths: list[str] = Field(default_factory=list)
    #: 禁写路径的 glob。优先级高于 `write_paths`。
    forbid_paths: list[str] = Field(default_factory=list)


class Limits(BaseModel):
    model_config = _STRICT

    #: 缺省表示"这个角色没有特殊要求",由根配置兜底。
    job_timeout_s: int | None = Field(default=None, gt=0)
    max_tokens_per_job: int | None = Field(default=None, gt=0)


class EmployeeConfig(BaseModel):
    """一个数字员工。

    **不可变。** 任务跑到一半有人改了它的权限,事件流里记的责任归属就对不上实际发生的事。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    #: 给人看的名字(「架构员工」「开发员工」)。**空就退回 id**——现有的员工定义里都没有
    #: 这个字段,不兜底会让界面上那一列突然空掉。
    #:
    #: 早先控制面直接把 id 摊给用户看,理由是"领域模型里没有角色这个概念"。那个判断反了:
    #: 设计稿要的中文名是存在的东西,拒绝造它并不会让 id 变得适合展示。
    name: str = ""
    runtime: str
    model: str = "default"
    #: 角色提示词文件,相对 employees/ 目录。
    prompt: str
    #: 这个员工是**谁**——只有跑在 human 运行时上的员工才有意义:它的活会变成这个人的待办。
    #:
    #: 放在员工定义里而不是只放在图的节点上:一个"资深工程师"角色是**长期存在**的,而节点
    #: 是一次派发。两处都能给,节点上的优先——那是"这一次交给别人"的正常需求。
    assignee: str = ""
    #: 允许调用的 Procedure。**空表示什么都不能调用。**
    procedures: list[str] = Field(default_factory=list)
    #: 这个员工随身带的通用手艺(`genome/procedures/_common/craft/` 里的名字)。
    #:
    #: **不是权限字段**,不进 `ScopePolicy`。手艺没有成败裁决,给多给少只影响干得好不好,
    #: 不影响能不能干——当权限管的话它会进越权检查,而越权失败要回滚工作区,对"少带了一份
    #: 方法论"这件事是完全不成比例的后果。
    #:
    #: 空表示不带通用手艺(而不是"带全部")。理由与 `procedures` 白名单一致:显式授予。
    crafts: list[str] = Field(default_factory=list)
    tools: Tools = Field(default_factory=Tools)
    permissions: Permissions = Field(default_factory=Permissions)
    limits: Limits = Field(default_factory=Limits)
    #: 要注入这个员工进程的环境变量名。只给它这次需要的,不是全部。
    credentials: list[str] = Field(default_factory=list)

    #: 由加载器填。
    prompt_text: str = Field(default="", exclude=True)

    @property
    def display_name(self) -> str:
        """界面上叫它什么。**兜底回 id**,见 `name` 的说明。"""
        return self.name or self.id

    def execution_mode(self, assisted_employees: Collection[str] = ()) -> str:
        """这个员工现在停在信任爬坡的哪一档。

        **三档住在两个存储上**:`manual` 是这份员工定义里的运行时(版本化资产),
        `assisted` 是根配置里的确认名单。**但那是存储的分工,不是人的分工**——人要表达的
        是"这个角色我信到什么程度",所以界面上它是一个三选一,不是两个控件。

        与指标 `agentgenome_execution_mode_total` 用的是同一套词:那个数是"实际发生了
        什么",这里是"配置成了什么",两者要能并排读——各叫各的名字就读不到一起去。
        """
        if self.runtime == HUMAN_RUNTIME:
            return MANUAL
        return ASSISTED if self.id in assisted_employees else AUTO

    # --- 权限 ----------------------------------------------------------------

    def write_globs(
        self, task_id: str | None = None, module_paths: Sequence[str] = ()
    ) -> list[str]:
        return _expand_all(self.permissions.write_paths, task_id, module_paths)

    def forbid_globs(
        self, task_id: str | None = None, module_paths: Sequence[str] = ()
    ) -> list[str]:
        return _expand_all(self.permissions.forbid_paths, task_id, module_paths)

    def scope(
        self,
        task_id: str | None = None,
        protected: Sequence[str] = (),
        module_paths: Sequence[str] = (),
        extra_forbid: Sequence[str] = (),
    ) -> ScopePolicy:
        """这个员工在这个任务里的授权范围。占位符已展开、受保护路径已叠加。

        派发前的"能不能写"与 Job 结束后的"实际碰了什么"用的是同一个对象——两处
        各自推导的话,迟早有一处更宽。

        **收窄住在这里,不在调用方各自做。** 授权在三处构造(Job 派发、结对会话结束后的
        检查、提交前的越权复查),三处必须拿到同一个收敛结果;各自收窄的话迟早有一处更宽,
        而更宽的那一处就是漏洞。这与上一段是同一条理由,只是换了个维度。

        `extra_forbid` 是**任务级**的禁令,不是角色级的。写集分离("这个任务里开发员工不许
        写测试")就属于这一类:它随策略与这次的生效模块变化,写不进员工定义。它与员工自己的
        声明**走同一套占位符展开**——调用方自己拼一遍的话,两处的拼法迟早分叉,而分叉出来的
        那一份如果更宽,就是一个没人看得见的漏洞。缺省为空,此时授权范围与不给这个入参
        逐字节相同。
        """
        declared = ScopePolicy(
            write_paths=tuple(self.write_globs(task_id, module_paths)),
            forbid_paths=tuple(self.forbid_globs(task_id, module_paths)),
        )
        task_level = _expand_all(extra_forbid, task_id, module_paths)
        return declared.with_forbidden(task_level).with_protected(protected)

    def may_write(
        self, path: str, task_id: str | None = None, module_paths: Sequence[str] = ()
    ) -> bool:
        """这个员工能不能写这条路径。先看禁止再看允许。"""
        return self.scope(task_id, module_paths=module_paths).allows(path)

    # --- 限额 ----------------------------------------------------------------

    def effective_timeout(self, config: Config) -> int:
        """员工声明了就以它为准,否则用部署参数兜底。"""
        return self.limits.job_timeout_s or config.limits.job_timeout_s

    def effective_max_tokens(self, config: Config) -> int:
        return self.limits.max_tokens_per_job or config.budgets.per_job_tokens

    # --- 展示 ----------------------------------------------------------------

    def as_dict(
        self, task_id: str | None = None, module_paths: Sequence[str] = ()
    ) -> dict[str, Any]:
        """有效配置。占位符已展开——人算不清叠加之后的结果,得让机器算。

        收窄之后"这个任务能写哪儿"是**算出来的**:员工定义里的占位符 × 计划里的模块 ×
        项目的受保护路径。不展开的话这条命令给出的是一份看起来像配置、其实不是生效值的
        东西——比不展示更容易误导。
        """
        return {
            "id": self.id,
            "runtime": self.runtime,
            "model": self.model,
            "prompt": self.prompt,
            "procedures": list(self.procedures),
            "crafts": list(self.crafts),
            "tools": {"allow": list(self.tools.allow), "deny": list(self.tools.deny)},
            "permissions": {
                "write_paths": self.write_globs(task_id, module_paths),
                "forbid_paths": self.forbid_globs(task_id, module_paths),
            },
            "limits": {
                "job_timeout_s": self.limits.job_timeout_s,
                "max_tokens_per_job": self.limits.max_tokens_per_job,
            },
            "credentials": list(self.credentials),
        }


class EmployeeRegistry:
    """加载完成后不可变的员工集合。"""

    def __init__(
        self,
        employees: dict[str, EmployeeConfig],
        rejected: dict[str, str],
        conflicts: Sequence[str] = (),
    ) -> None:
        self._employees = MappingProxyType(dict(employees))
        #: 校验失败因而没被装进来的 id -> 原因。
        self.rejected = MappingProxyType(dict(rejected))
        #: 排他归属被两个员工同时声明。**不是"装不进来"**:定义本身是合法的,坏的是它们
        #: 合在一起的样子。非严格加载时留在这里让上层说出来——静默的话,"这个决定归谁"
        #: 会在半年后的一次归因争议里才被发现。
        self.conflicts = tuple(conflicts)

    def all(self) -> tuple[EmployeeConfig, ...]:
        return tuple(self._employees[key] for key in sorted(self._employees))

    def get(self, employee_id: str) -> EmployeeConfig:
        try:
            return self._employees[employee_id]
        except KeyError:
            known = ", ".join(sorted(self._employees)) or "(空)"
            raise EmployeeNotFound(f"没有这个员工: {employee_id}(已定义: {known})") from None

    def __contains__(self, employee_id: object) -> bool:
        return employee_id in self._employees

    def __len__(self) -> int:
        return len(self._employees)


def ensure_procedure_allowed(employee: EmployeeConfig, procedure_id: str) -> None:
    """员工能力边界。不在白名单里即拒绝,且不拉起任何东西。"""
    if procedure_id not in employee.procedures:
        allowed = ", ".join(employee.procedures) or "(空)"
        raise ProcedureNotAllowed(f"{employee.id} 不允许调用 {procedure_id}(白名单: {allowed})")


def ownership_conflicts(
    employees: Iterable[EmployeeConfig], exclusive: Collection[str]
) -> list[str]:
    """排他归属被谁同时声明了。每条一句话,含修复命令。

    **只看给出的那几道工序。** 共享归属的工序被多个角色声明是正常状态(门禁、开发这类
    "谁都可能干"的活),把它们一并禁掉的话,这条检查会立刻变成一个所有人都要绕开的东西。
    """
    claims: dict[str, list[str]] = {}
    for employee in employees:
        for procedure_id in employee.procedures:
            if procedure_id in exclusive:
                claims.setdefault(procedure_id, []).append(employee.id)
    return [
        f"{procedure_id} 是排他归属的工序,却被 {'、'.join(sorted(owners))} 同时声明——"
        f"事件面上「这个决定该质询谁」因此没有答案。跑 `agctl roster migrate` 收敛归属。"
        for procedure_id, owners in sorted(claims.items())
        if len(owners) > 1
    ]


def load_employees(
    root: Path, strict: bool = True, exclusive: Collection[str] = ()
) -> EmployeeRegistry:
    """加载一个 Workspace 的全部员工定义。

    `strict=False` 时单个坏定义只跳过它并记下原因——一个手滑写坏的员工不该让整个
    编排器起不来。默认严格,因为启动期就该发现配置错误。

    `exclusive` 是**归属排他**的工序 id(工序自己声明 `ownership: plan`)。给了就查:
    严格模式下拒绝加载,非严格模式下记在 `conflicts` 里由上层说出来。不给就不查——
    这一层不认识工序注册表,让它去加载的话,看一眼员工名单就要把整个基因组读一遍。
    """
    root = Path(root)
    employees: dict[str, EmployeeConfig] = {}
    rejected: dict[str, str] = {}
    issues: list[ValidationIssue] = []

    for path in _definition_files(root):
        try:
            employee = _load_one(path, root)
        except GenomeValidationError as error:
            if strict:
                issues.extend(error.issues)
            else:
                rejected[path.stem] = error.render()
            continue
        employees[employee.id] = employee

    conflicts = ownership_conflicts(employees.values(), exclusive) if exclusive else []
    if strict:
        issues.extend(ValidationIssue(str(EMPLOYEES_SUBDIR), message) for message in conflicts)
    if issues:
        raise GenomeValidationError(issues)
    return EmployeeRegistry(employees, rejected, conflicts)


def workspace_employees_root(workspace_root: Path) -> Path:
    return Path(workspace_root) / EMPLOYEES_SUBDIR


def _definition_files(root: Path) -> list[Path]:
    if not root.is_dir():
        # 还没定义员工是正常状态,不是错误。
        return []
    return sorted(path for path in root.glob("*.yaml") if path.is_file())


def _load_one(path: Path, root: Path) -> EmployeeConfig:
    relative = f"{EMPLOYEES_SUBDIR}/{path.name}"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise GenomeValidationError([ValidationIssue(relative, f"YAML 解析失败: {exc}")]) from exc
    if not isinstance(payload, dict):
        raise GenomeValidationError([ValidationIssue(relative, "顶层必须是一个映射")])

    # 与 Procedure 加载器同样的两层并行收集:pydantic 层与语义层的问题一起报出来,
    # 不是修完一批再跑一次才看到下一批。
    issues: list[ValidationIssue] = []
    employee: EmployeeConfig | None = None
    try:
        employee = EmployeeConfig.model_validate(payload)
    except ValidationError as exc:
        issues.extend(_from_pydantic(exc, relative))

    issues.extend(_check_identity(payload, path, relative))
    prompt_text, prompt_issues = _read_prompt(payload, root, relative)
    issues.extend(prompt_issues)
    issues.extend(_check_crafts(payload, root, relative))

    if issues or employee is None:
        raise GenomeValidationError(issues)
    return employee.model_copy(update={"prompt_text": prompt_text})


def _check_crafts(payload: dict[str, Any], root: Path, relative: str) -> list[ValidationIssue]:
    """员工声明的通用手艺得真的存在。

    **在加载期报,不是派发时。** 配置写错该在启动时暴露,而不是任务跑一半才发现员工少带了
    一份方法论——那时候的症状是"它这次表现不太好",没有任何东西指向这份 yaml。
    """
    from agentgenome.genome import craft

    wanted = payload.get("crafts")
    if not wanted:
        return []
    # `root` 是 `employees/` 目录(其余校验都以它为基准解析提示词路径),而通用手艺库挂在
    # 基因组下,所以要往上一级回到 Workspace 根。
    common = craft.common_root(root.parent)
    missing = craft.missing_common(common, wanted)
    return [
        ValidationIssue(relative, f"crafts 里声明的通用手艺不存在: {name}") for name in missing
    ]


def _check_identity(payload: dict[str, Any], path: Path, relative: str) -> list[ValidationIssue]:
    employee_id = payload.get("id")
    if isinstance(employee_id, str) and employee_id != path.stem:
        return [
            ValidationIssue(
                relative,
                f"id 与文件名不一致: id={employee_id!r} 文件={path.stem!r}。"
                "文件名是它的身份,对不上会让人分不清改的是哪一个",
                "id",
            )
        ]
    return []


def _read_prompt(
    payload: dict[str, Any], root: Path, relative: str
) -> tuple[str, list[ValidationIssue]]:
    """提示词是员工的全部人设。指错文件的话它会带着空人格上岗。"""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return "", []
    target = root / prompt
    if not target.is_file():
        return "", [ValidationIssue(relative, f"提示词文件不存在: {prompt}", "prompt")]
    return target.read_text(encoding="utf-8"), []


def _from_pydantic(error: ValidationError, relative: str) -> list[ValidationIssue]:
    issues = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or None
        message = detail["msg"]
        if detail["type"] == "extra_forbidden" and location:
            message = f"{message}: {location}"
        elif isinstance(detail.get("input"), str | int | float | bool):
            message = f"{message}(当前值: {detail['input']!r})"
        issues.append(ValidationIssue(relative, message, location))
    return issues


def _expand_all(
    globs: Sequence[str], task_id: str | None, module_paths: Sequence[str]
) -> list[str]:
    """把一组声明展开成实际生效的 glob。

    **一条声明可能展开成零条或多条。** 带 `{task_modules}` 的那条按模块数展开;模块集合为空
    时它展开成**零条**——于是这个员工一行业务代码都写不进去,这正是想要的:计划没写清楚该当场
    卡住,而不是静默地变成权限全开。
    """
    expanded: list[str] = []
    for glob in globs:
        expanded.extend(_expand(glob, task_id, module_paths))
    return expanded


def _expand(glob: str, task_id: str | None, module_paths: Sequence[str]) -> list[str]:
    rendered = glob.replace(TASK_ID_PLACEHOLDER, task_id) if task_id else glob
    if TASK_MODULES_PLACEHOLDER not in rendered:
        return [rendered]
    if task_id is None:
        # **不针对某个任务问的时候,原样保留。** 展开成零条的话,`employee show` 不带 `--task`
        # 会把"可写业务代码"这条规则整条吞掉,人看到的是一份**少了一条规则**的定义——
        # 比看到一个未展开的占位符误导得多。与 `{task_id}` 在同一情形下的行为一致。
        return [rendered]
    # 挂载点在根索引里带尾斜杠,而 glob 自己也带一个。不去掉的话拼出来是 `a//**`,
    # 匹配不上任何东西——一条看起来写了的授权实际是空的。
    return [rendered.replace(TASK_MODULES_PLACEHOLDER, path.rstrip("/")) for path in module_paths]


__all__ = [
    "EMPLOYEES_SUBDIR",
    "TASK_MODULES_PLACEHOLDER",
    "EmployeeConfig",
    "EmployeeNotFound",
    "EmployeeRegistry",
    "Limits",
    "Permissions",
    "ProcedureNotAllowed",
    "Tools",
    "ensure_procedure_allowed",
    "load_employees",
    "workspace_employees_root",
]
