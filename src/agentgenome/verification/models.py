"""模块验证规格的类型：运行什么、在哪运行、为什么可信。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentgenome.gates.parsers import KNOWN_PARSERS

_STRICT_FROZEN = ConfigDict(extra="forbid", frozen=True)


class CommandEvidence(BaseModel):
    """仓库里可重新定位的一条命令证据。"""

    model_config = _STRICT_FROZEN

    kind: str
    path: str
    locator: str
    digest: str

    @field_validator("path")
    @classmethod
    def _module_relative_path(cls, value: str) -> str:
        parts = value.replace("\\", "/").split("/")
        if value.startswith(("/", "\\")) or ".." in parts:
            raise ValueError("命令证据 path 必须是模块内相对路径")
        return value


class CommandProvenance(BaseModel):
    """谁根据哪些证据提出了这条命令。"""

    model_config = _STRICT_FROZEN

    origin: Literal["detector", "agent-proposal", "human", "legacy", "platform"]
    producer: str
    evidence: tuple[CommandEvidence, ...] = Field(min_length=1)


class ArgvCommand(BaseModel):
    """默认命令形态；不经过 shell 解释。"""

    model_config = _STRICT_FROZEN

    kind: Literal["argv"] = "argv"
    argv: tuple[str, ...]
    cwd: str = "."

    @field_validator("argv")
    @classmethod
    def _non_empty_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item or "\0" in item for item in value):
            raise ValueError("argv 必须非空，且参数不能是空串或包含 NUL")
        return value

    @field_validator("cwd")
    @classmethod
    def _module_relative_cwd(cls, value: str) -> str:
        parts = value.replace("\\", "/").split("/")
        if value.startswith(("/", "\\")) or ".." in parts:
            raise ValueError("cwd 必须是模块内相对路径")
        return value or "."


class EnvironmentRef(BaseModel):
    """指向一个由 composition root 注册的执行环境 Adapter。"""

    model_config = _STRICT_FROZEN

    adapter: str
    options: dict[str, Any] = Field(default_factory=dict)


class VerificationGate(BaseModel):
    """模块验证规格中的一关。"""

    model_config = _STRICT_FROZEN

    id: str
    environment: str
    command: ArgvCommand
    provenance: CommandProvenance
    required: bool = True
    timeout_s: int = Field(default=600, gt=0)
    parser: str | None = None
    junit_xml_path: str | None = None

    @field_validator("id", "environment")
    @classmethod
    def _non_empty_reference(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("门禁 id 与 environment 不能为空")
        return value

    @field_validator("parser")
    @classmethod
    def _known_parser(cls, value: str | None) -> str | None:
        if value is not None and value not in KNOWN_PARSERS:
            raise ValueError(
                f"没有这个解析器: {value}(已知: {', '.join(sorted(KNOWN_PARSERS))})"
            )
        return value

    @field_validator("junit_xml_path")
    @classmethod
    def _module_relative_result_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.replace("\\", "/").split("/")
        if value.startswith(("/", "\\")) or ".." in parts:
            raise ValueError("junit_xml_path 必须是模块内相对路径")
        return value


class VerificationSpec(BaseModel):
    """一个模块唯一可执行的、已确认验证事实。"""

    model_config = _STRICT_FROZEN

    version: Literal[2] = 2
    module: str
    environments: dict[str, EnvironmentRef]
    gates: tuple[VerificationGate, ...]

    @field_validator("module")
    @classmethod
    def _safe_module_id(cls, value: str) -> str:
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("module 必须是单段安全 id")
        return value

    @model_validator(mode="after")
    def _references_existing_environments(self) -> VerificationSpec:
        if not self.environments:
            raise ValueError("验证规格至少需要一个环境")
        if not self.gates or not any(gate.required for gate in self.gates):
            raise ValueError("验证规格至少需要一个必需门禁")
        gate_ids = [gate.id for gate in self.gates]
        if len(gate_ids) != len(set(gate_ids)):
            raise ValueError("门禁 id 不能重复")
        missing = sorted(
            {gate.environment for gate in self.gates} - set(self.environments)
        )
        if missing:
            raise ValueError(f"门禁引用了不存在的环境: {', '.join(missing)}")
        return self

    def gate(self, gate_id: str) -> VerificationGate:
        for gate in self.gates:
            if gate.id == gate_id:
                return gate
        raise KeyError(f"模块验证规格里没有门禁: {gate_id}")


class VerificationProposalSpec(BaseModel):
    """架构员工提出的仓库门禁；平台门禁尚未注入。"""

    model_config = _STRICT_FROZEN

    version: Literal[2] = 2
    module: str
    environments: dict[str, EnvironmentRef] = Field(default_factory=dict)
    gates: tuple[VerificationGate, ...] = ()

    @field_validator("module")
    @classmethod
    def _safe_module_id(cls, value: str) -> str:
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("module 必须是单段安全 id")
        return value

    @model_validator(mode="after")
    def _references_existing_environments(self) -> VerificationProposalSpec:
        gate_ids = [gate.id for gate in self.gates]
        if len(gate_ids) != len(set(gate_ids)):
            raise ValueError("门禁 id 不能重复")
        missing = sorted(
            {gate.environment for gate in self.gates} - set(self.environments)
        )
        if missing:
            raise ValueError(f"门禁引用了不存在的环境: {', '.join(missing)}")
        return self


class ResolutionIssue(BaseModel):
    """发现阶段需要模型或人处理的结构化缺口。"""

    model_config = _STRICT_FROZEN

    code: str
    detail: str


@dataclass(frozen=True)
class Ready:
    spec: VerificationSpec


@dataclass(frozen=True)
class NeedsConfirmation:
    candidates: tuple[VerificationSpec, ...]
    issues: tuple[ResolutionIssue, ...]


VerificationResolution = Ready | NeedsConfirmation


class PendingVerification(BaseModel):
    """已持久化的待确认发现结果；它不是可执行规格。"""

    model_config = _STRICT_FROZEN

    version: Literal[1] = 1
    module: str
    issues: tuple[ResolutionIssue, ...]
    candidates: tuple[VerificationSpec, ...] = ()
    proposal_task_id: str | None = None


__all__ = [
    "ArgvCommand",
    "CommandEvidence",
    "CommandProvenance",
    "EnvironmentRef",
    "NeedsConfirmation",
    "PendingVerification",
    "Ready",
    "ResolutionIssue",
    "VerificationGate",
    "VerificationResolution",
    "VerificationProposalSpec",
    "VerificationSpec",
]
