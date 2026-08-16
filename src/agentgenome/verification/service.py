"""验证规格的正式写入路径：版本提交与配置事件必须一起成功。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agentgenome.core.events import SYSTEM_SUBJECT, ActorKind, EventLog, LogKind
from agentgenome.space.gitcmd import ORCHESTRATOR_IDENTITY, git, git_out
from agentgenome.verification.models import NeedsConfirmation, VerificationSpec
from agentgenome.verification.storage import (
    pending_verification_path,
    verification_spec_path,
    write_pending_verification,
    write_verification_spec,
)


@dataclass(frozen=True)
class VerificationChange:
    path: Path
    rev: str


def record_confirmed_spec(
    workspace_root: Path,
    spec: VerificationSpec,
    *,
    actor: str,
    entrance: str,
) -> VerificationChange:
    root = Path(workspace_root)
    target = verification_spec_path(root, spec.module)
    pending = pending_verification_path(root, spec.module)
    paths = (target, pending) if pending.exists() else (target,)
    return _record(
        root,
        module_id=spec.module,
        actor=actor,
        entrance=entrance,
        paths=paths,
        mutate=lambda: write_verification_spec(root, spec),
    )


def record_pending_spec(
    workspace_root: Path,
    module_id: str,
    resolution: NeedsConfirmation,
    *,
    actor: str,
    entrance: str,
    actor_kind: ActorKind | None = None,
    proposal_task_id: str | None = None,
) -> VerificationChange:
    root = Path(workspace_root)
    target = pending_verification_path(root, module_id)
    return _record(
        root,
        module_id=module_id,
        actor=actor,
        actor_kind=actor_kind,
        entrance=entrance,
        paths=(target,),
        mutate=lambda: write_pending_verification(
            root, module_id, resolution, proposal_task_id=proposal_task_id
        ),
    )


def _record(
    root: Path,
    *,
    module_id: str,
    actor: str,
    actor_kind: ActorKind | None = None,
    entrance: str,
    paths: tuple[Path, ...],
    mutate: Callable[[], Path],
) -> VerificationChange:
    originals = {path: path.read_bytes() if path.is_file() else None for path in paths}
    target = mutate()
    relative = tuple(str(path.relative_to(root)) for path in paths)
    git(root, "add", "-A", "--", *relative)
    changed = git(root, "diff", "--cached", "--quiet", "HEAD", "--", *relative, check=False)
    if changed.returncode == 0:
        # 事件必须指向真正修改这份配置的提交，而不是重试时碰巧所在的 HEAD。
        # 后者可能已经前移到一笔完全无关的提交，会让缺口检测把错误的 SHA 当成已记录。
        rev = git_out(root, "log", "-1", "--format=%H", "--", *relative)
        _ensure_event(root, module_id, actor, actor_kind, entrance, rev)
        return VerificationChange(path=target, rev=rev)
    try:
        git(
            root,
            *ORCHESTRATOR_IDENTITY,
            "commit",
            "--only",
            "-m",
            f"chore(gates): update verification for {module_id}",
            "--",
            *relative,
        )
    except Exception:
        _restore(root, originals)
        raise
    rev = git_out(root, "rev-parse", "HEAD")
    _ensure_event(root, module_id, actor, actor_kind, entrance, rev)
    return VerificationChange(path=target, rev=rev)


def _ensure_event(
    root: Path,
    module_id: str,
    actor: str,
    actor_kind: ActorKind | None,
    entrance: str,
    rev: str,
) -> None:
    log = EventLog(root)
    section = f"verification:{module_id}"
    if any(
        event.payload.get("section") == section and event.payload.get("rev") == rev
        for event in log.all_events(kind=LogKind.CONFIG_CHANGED)
    ):
        return
    log.append(
        SYSTEM_SUBJECT,
        actor=actor,
        actor_kind=actor_kind,
        kind=LogKind.CONFIG_CHANGED,
        payload={
            "section": section,
            "entrance": entrance,
            "rev": rev,
        },
    )


def _restore(root: Path, originals: dict[Path, bytes | None]) -> None:
    for path, content in originals.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    git(
        root,
        "reset",
        "-q",
        "--",
        *(str(path.relative_to(root)) for path in originals),
        check=False,
    )


__all__ = ["VerificationChange", "record_confirmed_spec", "record_pending_spec"]
