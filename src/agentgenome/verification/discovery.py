"""从仓库作者声明的标准入口确定性发现模块验证规格。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agentgenome.verification.evidence import located_digest, platform_secrets_gate
from agentgenome.verification.models import (
    ArgvCommand,
    CommandEvidence,
    CommandProvenance,
    EnvironmentRef,
    NeedsConfirmation,
    Ready,
    ResolutionIssue,
    VerificationGate,
    VerificationResolution,
    VerificationSpec,
)

_MAKE_TARGET = re.compile(r"^([^\s:#=]+)\s*:(?!=)", re.MULTILINE)


@dataclass(frozen=True)
class _NodeToolchain:
    manager: str
    generation: str | None = None


def resolve_verification(module_id: str, module_root: Path) -> VerificationResolution:
    """把模块目录解析成可执行规格；没有确定答案时返回待确认而不是猜。"""
    root = Path(module_root)
    makefile = root / "Makefile"
    if makefile.is_file():
        targets = set(_MAKE_TARGET.findall(makefile.read_text(encoding="utf-8")))
        if "test" in targets:
            return Ready(_makefile_spec(module_id, root, makefile, targets))

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            return NeedsConfirmation(
                candidates=(),
                issues=(
                    ResolutionIssue(
                        code="INVALID_DECLARATION",
                        detail=f"package.json 不可读: {error}",
                    ),
                ),
            )
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if isinstance(scripts, dict) and isinstance(scripts.get("test"), str):
            toolchain, issue = _node_package_manager(root, package)
            if issue is not None:
                return NeedsConfirmation(candidates=(), issues=(issue,))
            if toolchain is not None:
                return Ready(_package_spec(module_id, package_json, scripts, toolchain))

    return NeedsConfirmation(
        candidates=(),
        issues=(
            ResolutionIssue(
                code="NO_STANDARD_ENTRYPOINT",
                detail=f"{module_id} 没有可确定的标准测试入口",
            ),
        ),
    )


def _makefile_spec(
    module_id: str, root: Path, makefile: Path, targets: set[str]
) -> VerificationSpec:
    environment = _environment(root)
    gates = [_make_gate("unit", "test", makefile, environment="project")]
    build_target = "build" if "build" in targets else ("install" if "install" in targets else None)
    if build_target is not None:
        gates.append(_make_gate("build", build_target, makefile, environment="project"))
    gates.append(platform_secrets_gate())
    return VerificationSpec(
        module=module_id,
        environments={"project": environment, "host": EnvironmentRef(adapter="host.trusted")},
        gates=tuple(gates),
    )


def _environment(root: Path) -> EnvironmentRef:
    if (root / "uv.lock").is_file() and (root / "pyproject.toml").is_file():
        return EnvironmentRef(
            adapter="python.uv",
            options={"project_file": "pyproject.toml", "lockfile": "uv.lock"},
        )
    return EnvironmentRef(adapter="host.trusted")


def _node_package_manager(
    root: Path, package: object
) -> tuple[_NodeToolchain | None, ResolutionIssue | None]:
    lockfiles = {
        manager: filename
        for manager, filename in {
            "npm": "package-lock.json",
            "pnpm": "pnpm-lock.yaml",
            "yarn": "yarn.lock",
        }.items()
        if (root / filename).is_file()
    }
    declared = package.get("packageManager") if isinstance(package, dict) else None
    declared_manager = declared.split("@", 1)[0] if isinstance(declared, str) else None
    if len(lockfiles) > 1 or (
        declared_manager is not None and set(lockfiles) != {declared_manager}
    ):
        return None, ResolutionIssue(
            code="CONFLICTING_TOOLCHAIN",
            detail=(
                f"packageManager={declared_manager or '(未声明)'} 与锁文件 "
                f"{', '.join(sorted(lockfiles.values())) or '(无)'} 不一致"
            ),
        )
    manager = declared_manager or next(iter(lockfiles), None)
    if manager not in {"npm", "pnpm", "yarn", None}:
        return None, ResolutionIssue(
            code="UNSUPPORTED_TOOLCHAIN",
            detail=f"尚未注册 Node 包管理器: {manager}",
        )
    if manager is None:
        return None, None
    generation = None
    if manager == "yarn":
        declared_version = declared.split("@", 1)[1] if isinstance(declared, str) else ""
        if declared_version:
            generation = "classic" if declared_version.split(".", 1)[0] == "1" else "berry"
        else:
            lock_text = (root / "yarn.lock").read_text(encoding="utf-8", errors="replace")
            generation = "berry" if "__metadata:" in lock_text else "classic"
    return _NodeToolchain(manager=manager, generation=generation), None


def _package_spec(
    module_id: str,
    package_json: Path,
    scripts: dict[object, object],
    toolchain: _NodeToolchain,
) -> VerificationSpec:
    options = {
        "manifest": "package.json",
        "lockfile": _node_lockfile(toolchain.manager),
    }
    if toolchain.generation is not None:
        options["generation"] = toolchain.generation
    environment = EnvironmentRef(
        adapter=f"node.{toolchain.manager}",
        options=options,
    )
    gates = [_package_gate("unit", "test", package_json, toolchain.manager)]
    if isinstance(scripts.get("build"), str):
        gates.append(_package_gate("build", "build", package_json, toolchain.manager))
    gates.append(platform_secrets_gate())
    return VerificationSpec(
        module=module_id,
        environments={"project": environment, "host": EnvironmentRef(adapter="host.trusted")},
        gates=tuple(gates),
    )


def _node_lockfile(manager: str) -> str:
    return {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock"}[manager]


def _package_gate(
    gate_id: str, script: str, package_json: Path, manager: str
) -> VerificationGate:
    evidence = CommandEvidence(
        kind="repository-entrypoint",
        path=package_json.name,
        locator=f"/scripts/{script}",
        digest=located_digest(package_json, f"/scripts/{script}"),
    )
    return VerificationGate(
        id=gate_id,
        environment="project",
        command=ArgvCommand(argv=(manager, "run", script)),
        provenance=CommandProvenance(
            origin="detector",
            producer="package-json@1",
            evidence=(evidence,),
        ),
    )


def _make_gate(
    gate_id: str, target: str, makefile: Path, *, environment: str
) -> VerificationGate:
    evidence = CommandEvidence(
        kind="repository-entrypoint",
        path=makefile.name,
        locator=f"target:{target}",
        digest=located_digest(makefile, f"target:{target}"),
    )
    return VerificationGate(
        id=gate_id,
        environment=environment,
        command=ArgvCommand(argv=("make", target)),
        provenance=CommandProvenance(
            origin="detector",
            producer="makefile@1",
            evidence=(evidence,),
        ),
    )


__all__ = ["resolve_verification"]
