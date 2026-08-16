"""命令证据的 locator 级摘要；发现与执行必须使用同一算法。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from agentgenome.verification.models import (
    ArgvCommand,
    CommandEvidence,
    CommandProvenance,
    EnvironmentRef,
    VerificationGate,
    VerificationProposalSpec,
    VerificationSpec,
)


def platform_secrets_gate() -> VerificationGate:
    """平台内建的 secrets 关；生成与验真必须共享这一份事实。"""
    return VerificationGate(
        id="secrets",
        environment="host",
        command=ArgvCommand(
            argv=(
                "gitleaks",
                "detect",
                "--source",
                ".",
                "--no-banner",
                "--no-git",
                "--redact",
                "--exit-code",
                "1",
            )
        ),
        provenance=CommandProvenance(
            origin="platform",
            producer="agentgenome.secrets@1",
            evidence=(
                CommandEvidence(
                    kind="platform-policy",
                    path="",
                    locator="builtin:gitleaks-detect",
                    digest="builtin:agentgenome.secrets@1",
                ),
            ),
        ),
    )


def located_digest(path: Path, locator: str) -> str:
    """摘要 locator 对应的语义控制面；无法可靠切片时采用保守的整文件。"""
    if locator.startswith("target:"):
        # Make target 的实际命令可以由目标块外的变量、include 与 prerequisite 决定。
        # 只摘要 recipe 会让员工改 `PROJECTS :=` 后少跑测试而证据仍然有效。先确认
        # locator 存在，再保守地封住整份 Makefile；等有可靠的 Make 语义解析器再收窄。
        _make_target(path.read_text(encoding="utf-8"), locator.removeprefix("target:"))
        return _digest(path.read_bytes())
    elif locator.startswith("/scripts/"):
        script = locator.removeprefix("/scripts/")
        payload = json.loads(path.read_text(encoding="utf-8"))
        scripts = payload.get("scripts") if isinstance(payload, dict) else None
        if not isinstance(scripts, dict) or script not in scripts:
            raise ValueError(f"命令证据定位不到: {path.name}#{locator}")
        value = json.dumps(scripts[script], ensure_ascii=False, separators=(",", ":"))
    elif locator == "file":
        return _digest(path.read_bytes())
    else:
        raise ValueError(f"不支持的命令证据 locator: {locator}")
    return _digest(value.encode("utf-8"))


def verify_evidence(evidence: CommandEvidence, module_root: Path) -> str | None:
    """返回仓库证据的漂移原因；平台证据由规格级精确匹配处理。"""
    if evidence.kind in {"platform-policy", "test-fixture"}:
        return f"仓库命令证据不能声明为受信类型: {evidence.kind}"
    root = Path(module_root).resolve()
    path = (root / evidence.path).resolve()
    if not path.is_relative_to(root):
        return f"命令证据越出模块目录: {evidence.path}"
    if not path.is_file():
        return f"命令证据已变化: {evidence.path} 不存在"
    try:
        digest = located_digest(path, evidence.locator)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        return f"命令证据已变化: {evidence.path}#{evidence.locator}: {error}"
    if digest != evidence.digest:
        return f"命令证据已变化: {evidence.path}#{evidence.locator}"
    return None


def validate_spec_evidence(spec: VerificationSpec, module_root: Path) -> str | None:
    platform_gates = 0
    for gate in spec.gates:
        if gate.provenance.origin == "platform":
            platform_gates += 1
            if gate != platform_secrets_gate() or spec.environments.get(
                gate.environment
            ) != EnvironmentRef(adapter="host.trusted"):
                return f"平台门禁声明不受信: {gate.id}"
            continue
        for evidence in gate.provenance.evidence:
            issue = verify_evidence(evidence, module_root)
            if issue is not None:
                return issue
    if platform_gates != 1:
        return "验证规格必须且只能包含一个平台 secrets 门禁"
    return None


def seal_agent_proposal(
    spec: VerificationProposalSpec, module_root: Path
) -> VerificationSpec:
    """封存员工提出的仓库门禁，并由平台注入不可委托的安全策略。"""
    gates = []
    for gate in spec.gates:
        if gate.provenance.origin == "platform":
            raise ValueError("架构员工不能声明平台门禁")
        if gate.provenance.origin != "agent-proposal":
            raise ValueError(f"{gate.id} 不是架构员工提案")
        if gate.id == platform_secrets_gate().id:
            raise ValueError(f"架构员工不能使用平台保留门禁 id: {gate.id}")
        evidence = []
        for item in gate.provenance.evidence:
            path = Path(module_root) / item.path
            if not path.is_file():
                raise ValueError(f"命令证据不存在: {item.path}")
            evidence.append(
                item.model_copy(update={"digest": located_digest(path, item.locator)})
            )
        gates.append(
            gate.model_copy(
                update={
                    "provenance": gate.provenance.model_copy(
                        update={"evidence": tuple(evidence)}
                    )
                }
            )
        )
    trusted_host = EnvironmentRef(adapter="host.trusted")
    declared_host = spec.environments.get("host")
    if declared_host is not None and declared_host != trusted_host:
        raise ValueError("架构员工不能覆盖平台保留环境: host")
    environments = dict(spec.environments)
    environments["host"] = trusted_host
    return VerificationSpec(
        version=spec.version,
        module=spec.module,
        environments=environments,
        gates=(*gates, platform_secrets_gate()),
    )


def _make_target(text: str, target: str) -> str:
    pattern = re.compile(
        rf"^{re.escape(target)}\s*:[^\n]*(?:\n\t[^\n]*)*",
        re.MULTILINE,
    )
    found = pattern.search(text)
    if found is None:
        raise ValueError(f"Make target 不存在: {target}")
    return found.group(0)


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "located_digest",
    "platform_secrets_gate",
    "seal_agent_proposal",
    "validate_spec_evidence",
    "verify_evidence",
]
