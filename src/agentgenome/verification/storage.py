"""模块验证规格的唯一版本化存储。"""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

from agentgenome.verification.models import (
    NeedsConfirmation,
    PendingVerification,
    VerificationSpec,
)


def verification_spec_path(workspace_root: Path, module_id: str) -> Path:
    _validate_module_id(module_id)
    return Path(workspace_root) / "genome" / "gates" / f"{module_id}.yaml"


def pending_verification_path(workspace_root: Path, module_id: str) -> Path:
    _validate_module_id(module_id)
    return Path(workspace_root) / "genome" / "gates" / f"{module_id}.pending.yaml"


def write_verification_spec(workspace_root: Path, spec: VerificationSpec) -> Path:
    target = verification_spec_path(workspace_root, spec.module)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_yaml(
        target,
        spec.model_dump(mode="json"),
    )
    pending_verification_path(workspace_root, spec.module).unlink(missing_ok=True)
    return target


def write_pending_verification(
    workspace_root: Path,
    module_id: str,
    resolution: NeedsConfirmation,
    *,
    proposal_task_id: str | None = None,
) -> Path:
    pending = PendingVerification(
        module=module_id,
        issues=resolution.issues,
        candidates=resolution.candidates,
        proposal_task_id=proposal_task_id,
    )
    target = pending_verification_path(workspace_root, module_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_yaml(target, pending.model_dump(mode="json"))
    return target


def load_verification_spec(
    workspace_root: Path, module_id: str
) -> VerificationSpec | None:
    target = verification_spec_path(workspace_root, module_id)
    if not target.is_file():
        return None
    raw = _read_yaml(target)
    if not isinstance(raw, dict) or raw.get("version") != 2:
        version = raw.get("version") if isinstance(raw, dict) else None
        if version not in {None, 1}:
            raise ValueError(f"不支持的模块验证规格版本: {version}")
        return None
    return VerificationSpec.model_validate(raw)


def load_pending_verification(
    workspace_root: Path, module_id: str
) -> PendingVerification | None:
    target = pending_verification_path(workspace_root, module_id)
    if not target.is_file():
        return None
    return PendingVerification.model_validate(_read_yaml(target))


def load_verification_spec_file(path: Path) -> VerificationSpec:
    return VerificationSpec.model_validate(_read_yaml(Path(path)))


def _read_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"YAML 不可读: {path}: {error}") from error


def _atomic_yaml(target: Path, payload: object) -> None:
    rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(rendered)
    temporary.replace(target)


def _validate_module_id(module_id: str) -> None:
    if not module_id or module_id in {".", ".."} or "/" in module_id or "\\" in module_id:
        raise ValueError("module 必须是单段安全 id")


__all__ = [
    "load_verification_spec",
    "load_verification_spec_file",
    "load_pending_verification",
    "pending_verification_path",
    "verification_spec_path",
    "write_pending_verification",
    "write_verification_spec",
]
