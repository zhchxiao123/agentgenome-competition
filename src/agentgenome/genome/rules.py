"""架构规则层。

规则文档面向人书写,但其中标记为 `rules` 的围栏代码块承载 YAML 供编排器与门禁消费。
同一份文档既是给人看的规范,也是给系统执行的配置——两者不会失联。

本模块只负责 schema 与加载。判定逻辑的消费方在后续 PRD:
影响规则由集成测试判定消费,受保护路径由权限校验与风险评级消费。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentgenome import paths
from agentgenome.config import Config
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.loader import parse_yaml_model, read_yaml

_STRICT = ConfigDict(extra="forbid")

RulesT = TypeVar("RulesT", bound=BaseModel)

#: 只认语言标记恰好是 `rules` 的围栏块。正文里的 ```yaml 示例块不会被误读。
_RULES_BLOCK = re.compile(r"^[ \t]*```rules[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.S | re.M)


class ForbiddenDep(BaseModel):
    """禁止的依赖方向,两端都是 Workspace 相对路径的 glob。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_glob: str = Field(alias="from")
    to_glob: str = Field(alias="to")


class ArchitectureRules(BaseModel):
    model_config = _STRICT

    forbidden_deps: list[ForbiddenDep] = Field(default_factory=list)
    #: 自然语言约束。注入上下文供员工遵守,不做机器强制。
    layering: list[str] = Field(default_factory=list)
    #: 缺省表示"本项目没有意见",由根配置兜底。
    max_fix_rounds: int | None = Field(default=None, gt=0)


class HighRiskPattern(BaseModel):
    """命中即强制转人工审批的模式。"""

    model_config = _STRICT

    id: str
    description: str = ""
    path_globs: list[str] = Field(default_factory=list)
    deleted_lines_gt: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _must_match_something(self) -> HighRiskPattern:
        if not self.path_globs and self.deleted_lines_gt is None:
            raise ValueError(
                f"高风险规则 {self.id!r} 没有任何匹配条件——要么是笔误,要么会静默放过高风险变更"
            )
        return self


class ProtectedPath(BaseModel):
    """一条受保护路径,以及**谁被允许动它**。

    豁免写在项目规则里而不是员工定义里:一个员工要是能在自己的文件里给自己开豁免,
    受保护路径就只是个建议了。规则文件本身也在受保护路径里,于是这份名单只有人能改。
    """

    model_config = _STRICT

    path: str
    #: 允许写这条路径的员工 id。**空表示谁都不能写**——默认值站在安全的那一侧。
    writable_by: list[str] = Field(default_factory=list)


class ProtectedRules(BaseModel):
    model_config = _STRICT

    #: 触碰即判越权,Job 直接失败并回滚。可以为某条路径指定允许写它的角色,
    #: 比如规则文件只有架构员工能动。
    protected_paths: list[ProtectedPath] = Field(default_factory=list)
    high_risk: list[HighRiskPattern] = Field(default_factory=list)

    @field_validator("protected_paths", mode="before")
    @classmethod
    def _accept_bare_globs(cls, value: object) -> object:
        """`- .github/**` 这种裸字符串等价于"谁都不能写"。

        绝大多数受保护路径就是这个语义,让它们保持一行是可读性的净收益。
        """
        if not isinstance(value, list):
            return value
        return [{"path": item} if isinstance(item, str) else item for item in value]

    @property
    def all_paths(self) -> list[str]:
        """全部受保护路径,不分角色。展示与审计看的是全集。"""
        return [entry.path for entry in self.protected_paths]

    def paths_for(self, employee_id: str) -> list[str]:
        """对这个员工生效的受保护路径。

        叠加到员工自己的 `forbid_paths` 上,构成它这次 Job 的禁写集合。
        """
        return [
            entry.path for entry in self.protected_paths if employee_id not in entry.writable_by
        ]

    @model_validator(mode="after")
    def _ids_must_be_unique(self) -> ProtectedRules:
        seen = set()
        for pattern in self.high_risk:
            if pattern.id in seen:
                raise ValueError(f"高风险规则 id 重复: {pattern.id!r}")
            seen.add(pattern.id)
        return self


class ImpactMatch(BaseModel):
    """影响规则的匹配条件。字段即内置谓词,未知谓词一律拒绝。"""

    model_config = _STRICT

    path_globs: list[str] = Field(default_factory=list)
    touches_interface_schema: bool = False
    touches_migrations: bool = False
    crosses_modules_gte: int | None = Field(default=None, gt=0)

    def is_empty(self) -> bool:
        return not (
            self.path_globs
            or self.touches_interface_schema
            or self.touches_migrations
            or self.crosses_modules_gte
        )


class ImpactRule(BaseModel):
    model_config = _STRICT

    id: str
    description: str = ""
    match: ImpactMatch = Field(default_factory=ImpactMatch)
    requires_itest: bool = True

    @model_validator(mode="after")
    def _must_match_something(self) -> ImpactRule:
        if self.match.is_empty():
            raise ValueError(f"影响规则 {self.id!r} 没有任何匹配条件,永远不会命中")
        return self


class ImpactRules(BaseModel):
    model_config = _STRICT

    rules: list[ImpactRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids_must_be_unique(self) -> ImpactRules:
        seen = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"影响规则 id 重复: {rule.id!r}")
            seen.add(rule.id)
        return self


class RuleSet(BaseModel):
    """三份规则资产的合集。"""

    model_config = _STRICT

    architecture: ArchitectureRules = Field(default_factory=ArchitectureRules)
    protected: ProtectedRules = Field(default_factory=ProtectedRules)
    impact: ImpactRules = Field(default_factory=ImpactRules)


def extract_rules_blocks(markdown: str) -> list[str]:
    """取出文档里全部的 `rules` 围栏块内容,按出现顺序。"""
    return [match.group(1) for match in _RULES_BLOCK.finditer(markdown)]


def _merge_blocks(blocks: list[str], relative: str) -> dict[str, Any]:
    """按出现顺序深度合并多个规则块。后写的覆盖先写的。"""
    merged: dict[str, Any] = {}
    for index, block in enumerate(blocks):
        try:
            payload = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            raise GenomeValidationError(
                [ValidationIssue(relative, f"规则块 YAML 解析失败: {exc}", f"rules[{index}]")]
            ) from exc
        if payload is None:
            continue
        if not isinstance(payload, dict):
            raise GenomeValidationError(
                [ValidationIssue(relative, "规则块顶层必须是一个映射", f"rules[{index}]")]
            )
        merged.update(payload)
    return merged


def _load_markdown_rules(root: Path) -> ArchitectureRules:
    relative = str(paths.ARCHITECTURE_RULES)
    path = root / paths.ARCHITECTURE_RULES
    if not path.is_file():
        return ArchitectureRules()
    blocks = extract_rules_blocks(path.read_text(encoding="utf-8"))
    if not blocks:
        return ArchitectureRules()
    return parse_yaml_model(ArchitectureRules, _merge_blocks(blocks, relative), relative)


def _load_yaml_rules(
    root: Path,
    relative_path: Path,
    model: type[RulesT],
    issues: list[ValidationIssue],
) -> RulesT:
    """加载一份 YAML 规则资产。缺文件按空规则集处理——新 Workspace 还没积累规则。

    校验问题攒进 `issues` 而不是当场抛出,这样三份资产的问题能一次报全。
    """
    relative = str(relative_path)
    path = root / relative_path
    if not path.is_file():
        return model()
    try:
        return parse_yaml_model(model, read_yaml(path, relative), relative)
    except GenomeValidationError as error:
        issues.extend(error.issues)
        return model.model_construct()


def load_rules(workspace_root: Path) -> RuleSet:
    """加载三份规则资产。缺文件按空规则集处理,问题一次报全。"""
    issues: list[ValidationIssue] = []

    architecture = ArchitectureRules()
    try:
        architecture = _load_markdown_rules(workspace_root)
    except GenomeValidationError as error:
        issues.extend(error.issues)

    protected = _load_yaml_rules(workspace_root, paths.PROTECTED_RULES, ProtectedRules, issues)
    impact = _load_yaml_rules(workspace_root, paths.IMPACT_RULES, ImpactRules, issues)

    if issues:
        raise GenomeValidationError(issues)
    return RuleSet(architecture=architecture, protected=protected, impact=impact)


def effective_max_fix_rounds(rules: RuleSet, config: Config) -> int:
    """规则文件优先于根配置。

    规则是项目资产、跟代码一起演进;根配置是这一次部署怎么跑。项目对"最多自动修几轮"
    有意见时,它的意见应该跟着仓库走,而不是被部署环境覆盖掉。
    """
    if rules.architecture.max_fix_rounds is not None:
        return rules.architecture.max_fix_rounds
    return config.limits.max_fix_rounds
