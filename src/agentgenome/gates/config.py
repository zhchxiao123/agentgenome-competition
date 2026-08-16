"""门禁配置:一个模块到底会跑哪几关。

## 三级来源,高的整份覆盖低的

1. 业务仓根目录的 `gates.yaml`
2. 基因组里按模块 id 索引的配置(不想碰业务仓时用它)
3. 从项目地图推导

**是整份覆盖,不是逐关合并。** 半份来自这儿半份来自那儿的话,"最终会跑什么"没有人能在
不打开三个文件的情况下推理出来——而门禁跑了什么恰恰是出事时第一个要问的。

## 推导降级是硬要求,不是兜底

它兑现的是"业务仓零改造"这个承诺:接入一个新仓不该要求先往里放配置文件。少了它,这套系统
对"不允许改动的遗留仓"完全用不了。

## JUnit 产物的位置由地图声明

不往用户写的任意 `test_cmd` 上拼 `--junitxml`——那是个字符串拼接,对非 pytest 的栈立刻就碎。
项目地图的 `junit_xml_path` 是显式声明,稳得多。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agentgenome import paths
from agentgenome.gates.parsers import KNOWN_PARSERS
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.loader import load_project_map
from agentgenome.genome.models import Module

#: 业务仓里的门禁配置文件名。
REPO_GATES_FILE = "gates.yaml"

#: 全局默认的密钥扫描命令。
#:
#: **默认是 `required: true`。** 工具没装的话任务会因环境类失败升级人工——那是刻意的:
#: 默认值站在安全的那一侧,而"你的环境没装扫描工具"本来就该人来处理,不是让 AI 反复
#: 尝试去修的东西。
DEFAULT_SECRETS_CMD = "gitleaks detect --no-banner --no-git --redact"

#: 每关的默认超时。
DEFAULT_TIMEOUT_S = 600


class GateSource(StrEnum):
    """这份配置从哪儿来。`agctl gate show` 要显示它——"我明明配了这一关它怎么没跑"
    的答案通常是"你配在了被更高优先级覆盖掉的那一层"。"""

    REPO = "repo"
    GENOME = "genome"
    DERIVED = "derived"


class Gate(BaseModel):
    """一关门禁。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    cmd: str
    #: 必需关失败即整体失败。**缺省为真**——写了一半的配置不该是"都不阻塞"。
    required: bool = True
    timeout_s: int = Field(default=DEFAULT_TIMEOUT_S, gt=0)
    #: 输出怎么解析。不声明时按关的 id 选默认解析器。
    parser: str | None = None

    @field_validator("parser")
    @classmethod
    def _known_parser(cls, value: str | None) -> str | None:
        """未知的解析器名在**校验期**就拒。

        跑到一半才发现解析器不存在的话,这一关的失败会被当成解析器的问题——而它其实
        是一处配置笔误。
        """
        if value is not None and value not in KNOWN_PARSERS:
            raise ValueError(f"未知的解析器: {value}(已知: {', '.join(sorted(KNOWN_PARSERS))})")
        return value


class GateFile(BaseModel):
    """一份门禁配置文件。"""

    model_config = ConfigDict(extra="forbid")

    gates: list[Gate] = Field(default_factory=list)


@dataclass(frozen=True)
class EffectiveGates:
    """一个模块最终会跑的那几关。"""

    module_id: str
    module_path: str
    source: GateSource
    gates: tuple[Gate, ...]
    #: JUnit 产物位置,相对模块目录。没声明就是 `None`。
    junit_xml_path: str | None = None
    #: 推导过程中值得让人知道的事,比如"这个模块没声明 test_cmd"。
    notes: tuple[str, ...] = ()

    def gate(self, gate_id: str) -> Gate | None:
        for gate in self.gates:
            if gate.id == gate_id:
                return gate
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "module": self.module_id,
            "path": self.module_path,
            "source": self.source.value,
            "junit_xml_path": self.junit_xml_path,
            "notes": list(self.notes),
            "gates": [gate.model_dump() for gate in self.gates],
        }


def effective_gates(workspace_root: Path, module_id: str) -> EffectiveGates:
    """算出这个模块最终会跑哪几关,以及这份配置从哪儿来。"""
    root = Path(workspace_root)
    module = _module(root, module_id)
    module_path = module.path.rstrip("/")

    repo_file = root / module_path / REPO_GATES_FILE
    if repo_file.is_file():
        return _from_file(repo_file, module, module_path, GateSource.REPO)

    genome_file = root / paths.GATES / f"{module_id}.yaml"
    if genome_file.is_file():
        return _from_file(genome_file, module, module_path, GateSource.GENOME)

    return _derive(module, module_path)


def overall_passed(gates: tuple[Gate, ...], results: dict[str, bool]) -> bool:
    """全部 `required: true` 的关通过即整体通过。

    **缺结果不算通过。** 没跑与跑过了必须分得开——把缺失当成通过的话,漏跑一关会静默放行,
    而那正是门禁存在的理由的反面。
    """
    return all(results.get(gate.id, False) for gate in gates if gate.required)


def _module(root: Path, module_id: str) -> Module:
    project_map = load_project_map(root)
    for module in project_map.modules:
        if module.id == module_id:
            return module
    known = ", ".join(module.id for module in project_map.modules) or "(空)"
    raise LookupError(f"项目地图里没有这个模块: {module_id}(已有: {known})")


def _from_file(path: Path, module: Module, module_path: str, source: GateSource) -> EffectiveGates:
    relative = _relative(path)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise GenomeValidationError([ValidationIssue(relative, f"YAML 解析失败: {exc}")]) from exc
    if not isinstance(payload, dict):
        raise GenomeValidationError([ValidationIssue(relative, "顶层必须是一个映射")])

    try:
        parsed = GateFile.model_validate(payload)
    except ValidationError as exc:
        raise GenomeValidationError(_from_pydantic(exc, relative)) from exc

    _reject_duplicates(parsed.gates, relative)
    return EffectiveGates(
        module_id=module.id,
        module_path=module_path,
        source=source,
        gates=tuple(parsed.gates),
        junit_xml_path=module.junit_xml_path,
    )


def _derive(module: Module, module_path: str) -> EffectiveGates:
    """从项目地图推一套默认门禁。"""
    gates: list[Gate] = []
    notes: list[str] = []
    if module.test_cmd:
        gates.append(Gate(id="unit", cmd=module.test_cmd))
    else:
        # 没声明就是没声明。编一条命令出来跑,失败时人会以为是代码的问题。
        notes.append(f"{module.id} 没有声明 test_cmd,推导不出单元测试关")
    if module.build_cmd:
        gates.append(Gate(id="build", cmd=module.build_cmd))
    else:
        notes.append(f"{module.id} 没有声明 build_cmd,推导不出构建关")
    gates.append(Gate(id="secrets", cmd=DEFAULT_SECRETS_CMD))

    return EffectiveGates(
        module_id=module.id,
        module_path=module_path,
        source=GateSource.DERIVED,
        gates=tuple(gates),
        junit_xml_path=module.junit_xml_path,
        notes=tuple(notes),
    )


def _reject_duplicates(gates: list[Gate], relative: str) -> None:
    """同 id 两关时"哪一关挂了"这个问题没有答案。"""
    seen: set[str] = set()
    issues = []
    for gate in gates:
        if gate.id in seen:
            issues.append(ValidationIssue(relative, f"门禁 id 重复: {gate.id!r}"))
        seen.add(gate.id)
    if issues:
        raise GenomeValidationError(issues)


def _from_pydantic(error: ValidationError, relative: str) -> list[ValidationIssue]:
    issues = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or None
        message = detail["msg"]
        if detail["type"] == "extra_forbidden" and location:
            message = f"{message}: {location}"
        issues.append(ValidationIssue(relative, message, location))
    return issues


def _relative(path: Path) -> str:
    """报错时给人看的路径。基因组里的用文件名,业务仓里的带上模块目录。"""
    if path.parent.name == paths.GATES.name:
        return str(paths.GATES / path.name)
    return str(Path(*path.parts[-2:]))


__all__ = [
    "DEFAULT_SECRETS_CMD",
    "DEFAULT_TIMEOUT_S",
    "REPO_GATES_FILE",
    "EffectiveGates",
    "Gate",
    "GateFile",
    "GateSource",
    "effective_gates",
    "overall_passed",
]
