"""测试用已确认验证规格；流程测试不再靠 legacy 门禁偷偷执行。"""

from __future__ import annotations

import os
from pathlib import Path

from agentgenome.genome.loader import load_project_map
from agentgenome.verification import (
    ArgvCommand,
    CommandEvidence,
    CommandProvenance,
    EnvironmentRef,
    VerificationGate,
    VerificationSpec,
    write_verification_spec,
)
from agentgenome.verification.evidence import located_digest, platform_secrets_gate

GateCommand = tuple[str, tuple[str, ...]]


def write_test_verification(
    workspace: Path,
    module_id: str,
    *commands: GateCommand,
) -> Path:
    """写一份由测试夹具显式确认的 host.process 规格。"""
    module_root = workspace / load_project_map(workspace).module(module_id).path
    evidence_file = _stable_evidence_file(module_root)
    relative = evidence_file.relative_to(module_root).as_posix()
    evidence = CommandEvidence(
        kind="repository-entrypoint",
        path=relative,
        locator="file",
        digest=located_digest(evidence_file, "file"),
    )
    _install_fake_gitleaks(workspace)
    spec = VerificationSpec(
        module=module_id,
        environments={
            "project": EnvironmentRef(adapter="host.process"),
            "host": EnvironmentRef(adapter="host.trusted"),
        },
        gates=(
            *(
                VerificationGate(
                id=gate_id,
                environment="project",
                command=ArgvCommand(argv=argv),
                provenance=CommandProvenance(
                    origin="human",
                    producer="test-fixture@1",
                    evidence=(evidence,),
                ),
                junit_xml_path=_junit_path(argv),
            )
                for gate_id, argv in commands
            ),
            platform_secrets_gate(),
        ),
    )
    return write_verification_spec(workspace, spec)


def _stable_evidence_file(module_root: Path) -> Path:
    for name in ("pyproject.toml", "package.json", "Makefile", "README.md"):
        candidate = module_root / name
        if candidate.is_file():
            return candidate
    found = next(
        (
            item
            for item in sorted(module_root.rglob("*"))
            if item.is_file() and ".git" not in item.parts
        ),
        None,
    )
    if found is None:
        raise ValueError(f"测试模块没有可用的仓库证据文件: {module_root}")
    return found


def _install_fake_gitleaks(workspace: Path) -> None:
    tools = workspace / ".test-tools"
    tools.mkdir(exist_ok=True)
    executable = tools / "gitleaks"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    current = os.environ.get("PATH", "")
    if str(tools) not in current.split(os.pathsep):
        os.environ["PATH"] = os.pathsep.join((str(tools), current))


def _junit_path(argv: tuple[str, ...]) -> str | None:
    prefix = "--junitxml="
    return next((item[len(prefix) :] for item in argv if item.startswith(prefix)), None)


__all__ = ["GateCommand", "write_test_verification"]
