"""任务级验证规格：开发后发现，交付后才提升到项目控制面。"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel, ConfigDict

from agentgenome.genome.loader import load_project_map
from agentgenome.verification.evidence import validate_spec_evidence
from agentgenome.verification.models import VerificationSpec
from agentgenome.verification.service import VerificationChange, record_confirmed_spec
from agentgenome.verification.storage import verification_spec_path

BOOTSTRAP_SPECS = "verification-bootstrap.json"


class BootstrapSpecs(BaseModel):
    """某次门禁从任务工作树重新发现出的临时规格。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    specs: tuple[VerificationSpec, ...]


def record_bootstrap_spec(output_dir: Path, spec: VerificationSpec) -> Path:
    """把临时规格留在门禁产物面；同一模块只保留最后一次发现。"""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    by_module = {item.module: item for item in load_bootstrap_specs(root)}
    by_module[spec.module] = spec
    bundle = BootstrapSpecs(specs=tuple(by_module[key] for key in sorted(by_module)))
    target = root / BOOTSTRAP_SPECS
    rendered = json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=root, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(rendered)
    temporary.replace(target)
    return target


def load_bootstrap_specs(output_dir: Path) -> tuple[VerificationSpec, ...]:
    """读取门禁产物里的临时规格；不存在表示本轮没有冷启动。"""
    target = Path(output_dir) / BOOTSTRAP_SPECS
    if not target.is_file():
        return ()
    try:
        return BootstrapSpecs.model_validate_json(
            target.read_text(encoding="utf-8")
        ).specs
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"任务级验证规格不可读: {target}: {error}") from error


def promote_bootstrap_specs(
    workspace_root: Path,
    output_dir: Path,
    *,
    actor: str,
    entrance: str,
) -> tuple[VerificationChange, ...]:
    """把已交付任务的临时规格逐模块提升为版本化项目事实。"""
    root = Path(workspace_root)
    project_map = load_project_map(root)
    promotable: list[VerificationSpec] = []
    for spec in load_bootstrap_specs(output_dir):
        # 任务打开期间可能有人完成了确认。正式事实一旦存在，旧任务制品无权覆盖它。
        if verification_spec_path(root, spec.module).is_file():
            continue
        module = project_map.module(spec.module)
        issue = validate_spec_evidence(spec, root / module.path)
        if issue is not None:
            raise ValueError(f"{spec.module} 临时验证规格不可提升: {issue}")
        promotable.append(spec)
    return tuple(
        record_confirmed_spec(
            root,
            spec,
            actor=actor,
            entrance=entrance,
        )
        for spec in promotable
    )


__all__ = [
    "BOOTSTRAP_SPECS",
    "BootstrapSpecs",
    "load_bootstrap_specs",
    "promote_bootstrap_specs",
    "record_bootstrap_spec",
]
