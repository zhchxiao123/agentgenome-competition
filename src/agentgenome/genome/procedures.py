"""Procedure 规格与校验。

一个 Procedure 是一个目录:`procedure.yaml` 声明规格,`prompt.md` 放 Agent 型的指令,
`scripts/` 放确定性逻辑,`schemas/` 放产物契约。

## kind 不是分类标签

它是**成本与可靠性的开关**。`deterministic` 的 Procedure 不拉 Agent:零 token、可复现、
毫秒级。原则是**验证类一律确定性,生成类才用 Agent**——跑测试、跑 lint、扫密钥、
同步图谱,这些都是脚本的活,让大模型去"理解并执行"是纯粹的浪费和风险。

所以校验要支撑这条原则被执行:`agentic` 却没有 `prompt.md`、`deterministic` 却没有
脚本,都是写错了,当场拒绝。设计评审时"这个 Procedure 为什么必须是 agentic"应该是一个
需要被论证的问题——默认答案是 deterministic。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml
from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.genome.craft import CRAFT_DIR
from agentgenome.genome.craft import validate as validate_craft
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue

MANIFEST = "procedure.yaml"
PROMPT = "prompt.md"
SCRIPTS_DIR = "scripts"
#: 确定性逻辑的入口文件名。校验与派发共用它——两处各写一遍的话,
#: 校验放过一个 `scripts/gate.sh`,派发时才发现找不到 run.py。
SCRIPT_ENTRY = "run.py"

#: 纯脚本 Procedure 用它声明"不需要任何运行时"。
RUNTIME_NONE = "none"

#: 回放运行时的名字。它对兼容性判定透明——见 `dispatch._check_runtime_compat`。
RUNTIME_REPLAY = "replay"

#: 项目级 Procedure 在 Workspace 里的位置。
PROCEDURES_SUBDIR = Path("genome") / "procedures"

_STRICT = ConfigDict(extra="forbid")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


class ProcedureSource(StrEnum):
    """Procedure 是从哪一级来的。项目级覆盖全局级。"""

    PROJECT = "project"
    GLOBAL = "global"


class ProcedureKind(StrEnum):
    """能不能不拉 Agent。"""

    DETERMINISTIC = "deterministic"
    AGENTIC = "agentic"
    HYBRID = "hybrid"

    @property
    def needs_prompt(self) -> bool:
        return self in (ProcedureKind.AGENTIC, ProcedureKind.HYBRID)

    @property
    def needs_scripts(self) -> bool:
        return self in (ProcedureKind.DETERMINISTIC, ProcedureKind.HYBRID)


class ProcedureOwnership(StrEnum):
    """这道工序的归属要不要**排他**。

    缺省是 `shared`:同一道工序被两个员工声明是完全正常的(门禁、开发这类"谁都可能干"
    的活)。`plan` 是那一类"决定这个任务怎么打"的工序——它必须只有一个员工能干,否则
    事件面上"这个决定该质询谁"没有答案,而复盘时那正是唯一想问的问题。

    **标在工序上,不标在员工上。** 排他是这道工序自己的性质;写在员工那边的话,
    加一个员工就要记得同时改另一份声明,而漏改没有任何外部症状。
    """

    SHARED = "shared"
    PLAN = "plan"


class Trigger(BaseModel):
    model_config = _STRICT

    #: 允许被派发的任务状态。空表示不限制。
    states: list[TaskState] = Field(default_factory=list)


class Inputs(BaseModel):
    # `schema` 在 pydantic 上是保留名,存储时改叫 schema_,对外仍是 `schema`。
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: 入参的 JSON Schema。派发前校验。
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")


class Outputs(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    artifacts: list[str] = Field(default_factory=list)
    #: 产物 schema 的路径,相对 Procedure 目录。
    schema_ref: str | None = None
    #: 树片段产出的种类(PRD 34)。声明后派发方给 Job 挂对应的确定性校验:
    #: 产出写在产物目录 `staging/` 下,成败由校验裁决,result.json 只是小票。
    #: 枚举而不是自由字符串——认不出的种类等于没有校验,而那正是这条声明要防的。
    staging: Literal["lessons", "knowledge-tree"] | None = None


class Tools(BaseModel):
    model_config = _STRICT

    required_cmds: list[str] = Field(default_factory=list)
    #: 需要的 MCP server。本期只记录不消费——实际注入见 PRD 15。
    mcp: list[str] = Field(default_factory=list)


class Failure(BaseModel):
    model_config = _STRICT

    #: 环境类失败的基础重试次数。
    retry: int = Field(default=0, ge=0)
    #: 语义失败时状态机该收到的事件。词汇与状态机共用一处,不做映射。
    on_fail: TaskEvent | None = None
    #: 连续失败 N 次升级人工。
    escalate_after: int | None = Field(default=None, gt=0)


class Budget(BaseModel):
    """这个 Procedure 自己的额度上限。

    **只能收紧,不能放宽。** 与员工额度取小值——反过来的话,一个 Procedure 就能给自己开一张
    超过所属员工限额的额度,而员工定义才是被评审的那份资产。

    典型用途是廉价判断类的 Procedure:它只需要读一小段摘要给一个是否,却挂在一个额度按写代码
    配的员工名下。
    """

    model_config = _STRICT

    max_tokens: int | None = Field(default=None, gt=0)


class Compat(BaseModel):
    model_config = _STRICT

    #: 兼容的运行时。`none` 表示纯脚本。空表示不限制。
    runtimes: list[str] = Field(default_factory=list)


class ProcedureSpec(BaseModel):
    """一个 Procedure 的完整规格。

    **不可变。** 任务跑到一半有人改了它的 version,事件流里记的 `procedure@version`
    就会对不上实际跑的东西,而这种对不上几乎无法追查。要派生新值用 `model_copy`。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str
    summary: str = ""
    kind: ProcedureKind
    #: 归属要不要排他。缺省共享——绝大多数工序本来就可以被多个角色调用。
    ownership: ProcedureOwnership = ProcedureOwnership.SHARED
    trigger: Trigger = Field(default_factory=Trigger)
    inputs: Inputs = Field(default_factory=Inputs)
    outputs: Outputs = Field(default_factory=Outputs)
    tools: Tools = Field(default_factory=Tools)
    failure: Failure = Field(default_factory=Failure)
    budget: Budget = Field(default_factory=Budget)
    compat: Compat = Field(default_factory=Compat)

    #: 以下由加载器填,不在 procedure.yaml 里声明。
    path: Path = Field(default=Path("."), exclude=True)
    prompt: str = Field(default="", exclude=True)
    output_schema: dict[str, Any] = Field(default_factory=dict, exclude=True)
    #: 来源:项目级还是全局级。注册表填。
    source: ProcedureSource = ProcedureSource.PROJECT
    #: 依赖的命令缺失时为 False,但仍然注册——见注册表的说明。
    available: bool = True
    unavailable_reason: str | None = None

    @property
    def ref(self) -> str:
        """事件流里记录的标识。报告能精确复现"当时是哪一版能力干的活"。"""
        return f"{self.id}@{self.version}"

    def script_entry(self) -> Path | None:
        """确定性逻辑的入口脚本。不存在时返回 None。"""
        entry = self.path / SCRIPTS_DIR / SCRIPT_ENTRY
        return entry if entry.is_file() else None

    @property
    def craft_dir(self) -> Path | None:
        """本工序自带的手艺目录。**没有是正常情况**——手艺是增强不是前置依赖。"""
        directory = self.path / CRAFT_DIR
        return directory if directory.is_dir() else None


def load_procedure(directory: Path) -> ProcedureSpec:
    """加载并校验一个 Procedure 目录。问题一次报全。"""
    directory = Path(directory)
    relative = f"{directory.name}/{MANIFEST}"
    manifest = directory / MANIFEST
    if not manifest.is_file():
        raise GenomeValidationError([ValidationIssue(relative, "缺少 procedure.yaml")])

    try:
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise GenomeValidationError([ValidationIssue(relative, f"YAML 解析失败: {exc}")]) from exc
    if not isinstance(payload, dict):
        raise GenomeValidationError([ValidationIssue(relative, "顶层必须是一个映射")])

    # 语义检查直接看原始 payload,不依赖 pydantic 是否通过——否则两层错误会分成
    # 两批报出来:修完第一批再跑一次才看到第二批,而"一次报全"正是这里要的。
    issues: list[ValidationIssue] = []
    spec: ProcedureSpec | None = None
    try:
        spec = ProcedureSpec.model_validate(payload)
    except ValidationError as exc:
        issues += _from_pydantic(exc, relative)

    issues += _check_identity(payload, directory, relative)
    issues += _check_kind_requirements(payload, directory, relative)
    issues += _check_contracts(payload, directory, relative)
    issues += _check_craft(directory, relative)

    if issues or spec is None:
        raise GenomeValidationError(issues)

    return spec.model_copy(update=_derived(spec, directory))


def _derived(spec: ProcedureSpec, directory: Path) -> dict[str, Any]:
    """procedure.yaml 里不声明、由磁盘内容决定的字段。"""
    prompt = directory / PROMPT
    output_schema: dict[str, Any] = {}
    if spec.outputs.schema_ref:
        target = directory / spec.outputs.schema_ref
        if target.is_file():
            output_schema = json.loads(target.read_text(encoding="utf-8"))
    return {
        "path": directory,
        "prompt": prompt.read_text(encoding="utf-8") if prompt.is_file() else "",
        "output_schema": output_schema,
    }


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


def _check_identity(
    payload: dict[str, Any], directory: Path, relative: str
) -> list[ValidationIssue]:
    issues = []
    procedure_id = payload.get("id")
    if isinstance(procedure_id, str) and procedure_id != directory.name:
        issues.append(
            ValidationIssue(
                relative,
                f"id 与目录名不一致: id={procedure_id!r} 目录={directory.name!r}。"
                "目录名是它在注册表里的身份,对不上会让人分不清改的是哪一个",
                "id",
            )
        )
    version = payload.get("version")
    if isinstance(version, str) and not _SEMVER.match(version):
        issues.append(ValidationIssue(relative, f"version 不是合法 semver: {version!r}", "version"))
    return issues


def _check_kind_requirements(
    payload: dict[str, Any], directory: Path, relative: str
) -> list[ValidationIssue]:
    """kind 蕴含的结构要求。写错的 Procedure 当场拒绝,而不是派发时才发现它什么也不做。"""
    raw_kind = payload.get("kind")
    if not isinstance(raw_kind, str):
        return []
    try:
        kind = ProcedureKind(raw_kind)
    except ValueError:
        return []  # kind 本身非法,由 pydantic 那边报,这里不重复

    issues = []
    if kind.needs_prompt and not (directory / PROMPT).is_file():
        issues.append(ValidationIssue(relative, f"kind={kind.value} 却没有 {PROMPT}", "kind"))

    entry = directory / SCRIPTS_DIR / SCRIPT_ENTRY
    if kind.needs_scripts and not entry.is_file():
        issues.append(
            ValidationIssue(
                relative,
                f"kind={kind.value} 却没有 {SCRIPTS_DIR}/{SCRIPT_ENTRY}——它的逻辑全在脚本里,"
                "没有入口它什么也不会做。放了别的名字的脚本也不算:"
                "校验放过它,派发时才发现找不到入口",
                "kind",
            )
        )
    return issues


def _check_craft(directory: Path, relative: str) -> list[ValidationIssue]:
    """手艺目录的校验。

    **只拒绝"它根本不是一个手艺包"**(缺 `SKILL.md`,挂上去也没用)。行数超预算是内容
    质量问题,由 `craft.validate` 报成告警,不在这里拒绝加载——为一份稍长的手艺把整个
    工序打挂是完全不成比例的后果。
    """
    errors, _ = validate_craft(directory / CRAFT_DIR)
    return [ValidationIssue(relative, message) for message in errors]


def _check_contracts(
    payload: dict[str, Any], directory: Path, relative: str
) -> list[ValidationIssue]:
    issues = []
    inputs_schema = (payload.get("inputs") or {}).get("schema")
    if isinstance(inputs_schema, dict):
        try:
            Draft202012Validator.check_schema(inputs_schema)
        except SchemaError as exc:
            issues.append(
                ValidationIssue(
                    relative,
                    f"inputs.schema 不是合法 JSON Schema: {exc.message}",
                    "inputs.schema",
                )
            )

    schema_ref = (payload.get("outputs") or {}).get("schema_ref")
    if isinstance(schema_ref, str):
        target = directory / schema_ref
        if not target.is_file():
            issues.append(
                ValidationIssue(
                    relative,
                    f"outputs.schema_ref 指向不存在的文件: {schema_ref}",
                    "outputs.schema_ref",
                )
            )
        else:
            try:
                json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                issues.append(
                    ValidationIssue(
                        relative, f"{schema_ref} 不是合法 JSON: {exc}", "outputs.schema_ref"
                    )
                )
    return issues


class ProcedureRegistry:
    """加载完成后不可变的 Procedure 集合。

    **加载完就冻结。** 热重载不做:任务执行到一半能力定义发生变化是审计噩梦——
    事件流里记的 `procedure@version` 会对不上实际跑的东西,而这种错对不上时几乎无法追查。
    """

    def __init__(
        self,
        specs: dict[str, ProcedureSpec],
        overrides: dict[str, ProcedureSource],
        rejected: dict[str, str],
    ) -> None:
        self._specs = MappingProxyType(dict(specs))
        #: 被项目级覆盖掉的 id -> 它原本的来源。
        #: 记下来是因为"我明明改了全局的怎么没生效"不该变成一次漫长的排查。
        self.overrides = MappingProxyType(dict(overrides))
        #: 校验失败因而没被装进来的 id -> 原因。
        self.rejected = MappingProxyType(dict(rejected))

    def all(self) -> tuple[ProcedureSpec, ...]:
        """全部 Procedure,按 id 排序。

        顺序稳定是为了让 `agctl procedure list` 的输出能被 diff。
        """
        return tuple(self._specs[key] for key in sorted(self._specs))

    def available(self) -> tuple[ProcedureSpec, ...]:
        """依赖齐备、可以真的被派发的那些。"""
        return tuple(spec for spec in self.all() if spec.available)

    def exclusive_ids(self) -> tuple[str, ...]:
        """归属排他的工序 id。花名册校验按它求交。"""
        return tuple(
            spec.id for spec in self.all() if spec.ownership is not ProcedureOwnership.SHARED
        )

    def get(self, procedure_id: str) -> ProcedureSpec:
        try:
            return self._specs[procedure_id]
        except KeyError:
            raise KeyError(
                f"未注册的工序: {procedure_id}"
                f"(已注册: {', '.join(sorted(self._specs)) or '(空)'})"
            ) from None

    def __contains__(self, procedure_id: object) -> bool:
        return procedure_id in self._specs

    def __len__(self) -> int:
        return len(self._specs)


def check_availability(spec: ProcedureSpec) -> ProcedureSpec:
    """依赖的命令装没装。

    缺依赖的 Procedure **仍然注册**,只是标为不可用。直接把它从列表里拿掉的话,
    `agctl procedure list` 显示的是"没有这个能力",而真相是"这个能力因为环境里没装
    gitleaks 而不可用"——后者能直接行动,前者只会让人去翻代码找它为什么消失了。
    """
    missing = [cmd for cmd in spec.tools.required_cmds if shutil.which(cmd) is None]
    if not missing:
        return spec
    return spec.model_copy(
        update={"available": False, "unavailable_reason": f"缺少命令: {', '.join(missing)}"}
    )


#: 全局级 Procedure 的默认位置。可用环境变量覆盖。
GLOBAL_PROCEDURES_ENV = "AGENTGENOME_GLOBAL_PROCEDURES"


def project_procedures_root(workspace_root: Path) -> Path:
    return workspace_root / PROCEDURES_SUBDIR


def global_procedures_root() -> Path:
    """全局级 Procedure 的位置。

    策略住在这里而不是 CLI:后续接上 REST 时两条路要走同一套,不然会各自推导一遍。
    """
    override = os.environ.get(GLOBAL_PROCEDURES_ENV)
    return Path(override) if override else Path.home() / ".agentgenome" / "procedures"


def load_workspace_registry(workspace_root: Path) -> ProcedureRegistry:
    """按约定位置装出一个 Workspace 的 Procedure 注册表。"""
    return load_registry(project_procedures_root(workspace_root), global_procedures_root())


def load_registry(project_root: Path, global_root: Path | None = None) -> ProcedureRegistry:
    """扫描两级来源,装出一个冻结的注册表。

    加载顺序是先全局后项目,于是同 id 时项目级自然覆盖全局级。
    """
    specs: dict[str, ProcedureSpec] = {}
    overrides: dict[str, ProcedureSource] = {}
    rejected: dict[str, str] = {}

    for root, source in (
        (global_root, ProcedureSource.GLOBAL),
        (project_root, ProcedureSource.PROJECT),
    ):
        if root is None:
            continue
        for directory in _procedure_dirs(Path(root)):
            try:
                spec = load_procedure(directory)
            except GenomeValidationError as error:
                # 一个手滑写坏的实验性 Procedure 不该让编排器罢工。
                rejected[directory.name] = error.render()
                continue
            spec = spec.model_copy(update={"source": source})
            if spec.id in specs:
                overrides[spec.id] = specs[spec.id].source
            specs[spec.id] = check_availability(spec)

    return ProcedureRegistry(specs, overrides, rejected)


def _procedure_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        # 新 Workspace 还没积累 Procedure,这是正常状态而非错误。
        return []
    return sorted(item for item in root.iterdir() if item.is_dir())


__all__ = [
    "MANIFEST",
    "PROMPT",
    "RUNTIME_NONE",
    "SCRIPTS_DIR",
    "Compat",
    "Failure",
    "Inputs",
    "Outputs",
    "ProcedureKind",
    "ProcedureRegistry",
    "ProcedureSource",
    "ProcedureSpec",
    "global_procedures_root",
    "load_workspace_registry",
    "project_procedures_root",
    "Tools",
    "Trigger",
    "check_availability",
    "load_registry",
    "load_procedure",
]
