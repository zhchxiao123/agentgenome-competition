"""`agctl gate show`:这个模块最终会跑哪几关。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import agentgenome.cli as cli_module
from agentgenome.cli import app
from agentgenome.core.events import SYSTEM_SUBJECT, ActorKind, EventLog, LogKind
from agentgenome.core.genome_driver import GenomeDriver
from agentgenome.core.genome_task import (
    GenomeTaskKind,
    GenomeTaskState,
    GenomeTaskStore,
    Origin,
)
from agentgenome.core.genome_transitions import GenomeEvent
from agentgenome.verification import (
    NeedsConfirmation,
    Ready,
    ResolutionIssue,
    record_pending_spec,
    resolve_verification,
    write_pending_verification,
    write_verification_spec,
)
from tests.fixtures.git import commit_all, git
from tests.fixtures.mini_map import write_workspace

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = write_workspace(tmp_path / "ws")
    git(root, "init", "--initial-branch=main")
    commit_all(root, "initial")
    return root


def _show(workspace: Path, module_id: str, *extra: str):
    return runner.invoke(app, ["gate", "show", module_id, "--workspace", str(workspace), *extra])


def test_it_shows_the_derived_gates_and_their_source(workspace: Path) -> None:
    result = _show(workspace, "order-service", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source"] == "derived"
    assert payload["executable"] is False
    assert payload["migration_required"] is True
    assert [gate["id"] for gate in payload["gates"]] == ["unit", "build", "secrets"]


def test_a_repo_file_shows_up_as_such(workspace: Path) -> None:
    (workspace / "repos/order-service" / "gates.yaml").write_text(
        "gates:\n  - id: unit\n    cmd: pytest\n", encoding="utf-8"
    )

    payload = json.loads(_show(workspace, "order-service", "--json").output)

    assert payload["source"] == "repo"


def test_the_human_output_says_which_gates_are_optional(workspace: Path) -> None:
    (workspace / "repos/order-service" / "gates.yaml").write_text(
        "gates:\n  - id: lint\n    cmd: ruff\n    required: false\n", encoding="utf-8"
    )

    result = _show(workspace, "order-service")

    assert "可选" in result.output
    assert "不可执行" in result.output
    assert "gate discover" in result.output


def test_a_module_without_a_test_command_says_so(workspace: Path) -> None:
    """静默省略的话,人会以为门禁跑了单测但其实没有。"""
    result = _show(workspace, "inventory-service")

    assert "没有声明 test_cmd" in result.output


def test_an_unknown_module_lists_what_exists(workspace: Path) -> None:
    result = _show(workspace, "ghost")

    assert result.exit_code != 0
    assert "order-service" in result.output
    assert "Traceback" not in result.output


def test_a_broken_gates_file_is_reported_without_a_traceback(workspace: Path) -> None:
    (workspace / "repos/order-service" / "gates.yaml").write_text(
        "gates:\n  - id: unit\n    cmd: pytest\n    surprise: 1\n", encoding="utf-8"
    )

    result = _show(workspace, "order-service")

    assert result.exit_code != 0
    assert "surprise" in result.output
    assert "Traceback" not in result.output


def test_show_prefers_the_confirmed_versioned_spec(workspace: Path) -> None:
    module_root = workspace / "repos/order-service"
    (module_root / "Makefile").write_text("test:\n\tpytest -q\n", encoding="utf-8")
    resolution = resolve_verification("order-service", module_root)
    assert isinstance(resolution, Ready)
    write_verification_spec(workspace, resolution.spec)

    payload = json.loads(_show(workspace, "order-service", "--json").output)

    assert payload["version"] == 2
    assert payload["source"] == "confirmed"
    assert payload["gates"][0]["command"]["argv"] == ["make", "test"]
    assert payload["gates"][0]["provenance"]["evidence"][0]["locator"] == "target:test"


def test_discover_writes_only_a_deterministic_spec(workspace: Path) -> None:
    module_root = workspace / "repos/order-service"
    (module_root / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    (module_root / "package-lock.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "gate",
            "discover",
            "order-service",
            "--write",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert (workspace / "genome/gates/order-service.yaml").is_file()
    assert "update verification for order-service" in git(
        workspace, "log", "-1", "--format=%s"
    )
    event = EventLog(workspace).events(SYSTEM_SUBJECT)[-1]
    assert event.kind is LogKind.CONFIG_CHANGED
    assert event.actor_kind is ActorKind.HUMAN
    assert event.payload["rev"] == git(workspace, "rev-parse", "HEAD")


def test_verification_commit_preserves_an_unrelated_staged_change(workspace: Path) -> None:
    unrelated = workspace / "README.md"
    unrelated.write_text("before\n", encoding="utf-8")
    commit_all(workspace, "add readme")
    unrelated.write_text("already staged by user\n", encoding="utf-8")
    git(workspace, "add", "README.md")
    module_root = workspace / "repos/order-service"
    (module_root / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "gate",
            "discover",
            "order-service",
            "--write",
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    assert git(workspace, "diff", "--cached", "--name-only") == "README.md"
    assert "README.md" not in git(
        workspace, "show", "--format=", "--name-only", "HEAD"
    )


def test_retry_backfills_config_event_after_commit_succeeded(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_root = workspace / "repos/order-service"
    (module_root / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    original = EventLog.append
    refused = False

    def fail_once(self: EventLog, *args: object, **kwargs: object):
        nonlocal refused
        if kwargs.get("kind") is LogKind.CONFIG_CHANGED and not refused:
            refused = True
            raise OSError("event store unavailable")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EventLog, "append", fail_once)
    command = [
        "gate",
        "discover",
        "order-service",
        "--write",
        "--workspace",
        str(workspace),
    ]

    first = runner.invoke(app, command)
    committed = git(workspace, "rev-parse", "HEAD")
    (workspace / "README.md").write_text("HEAD moved after gate commit\n", encoding="utf-8")
    commit_all(workspace, "unrelated change after event failure")
    advanced = git(workspace, "rev-parse", "HEAD")
    second = runner.invoke(app, command)

    assert first.exit_code != 0
    assert second.exit_code == 0, second.output
    events = EventLog(workspace).all_events(kind=LogKind.CONFIG_CHANGED)
    assert any(event.payload.get("rev") == committed for event in events)
    assert not any(event.payload.get("rev") == advanced for event in events)


def test_discover_refuses_to_invent_a_command(workspace: Path) -> None:
    result = runner.invoke(
        app,
        [
            "gate",
            "discover",
            "inventory-service",
            "--write",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["status"] == "needs_confirmation"
    assert payload["issues"][0]["code"] == "NO_STANDARD_ENTRYPOINT"
    assert not (workspace / "genome/gates/inventory-service.yaml").exists()
    assert (workspace / "genome/gates/inventory-service.pending.yaml").is_file()


def test_architecture_proposal_has_a_read_only_dry_run(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(recordings))
    discovered = runner.invoke(
        app,
        [
            "gate",
            "discover",
            "inventory-service",
            "--write",
            "--workspace",
            str(workspace),
        ],
    )
    assert discovered.exit_code == 2

    result = runner.invoke(
        app,
        [
            "gate",
            "propose",
            "inventory-service",
            "--dry-run",
            "--runtime",
            "replay",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "dry_run"
    context = Path(payload["context"]).read_text(encoding="utf-8")
    assert "arch-employee" in context
    assert "不得写入 `genome/gates/`" in context
    task_id = payload["task_id"]
    cancelled = GenomeTaskStore(workspace).get(task_id)
    assert cancelled.state is GenomeTaskState.CANCELLED
    assert cancelled.failure_reason == "dry-run 仅生成验证提案上下文，未调用架构员工"
    assert (workspace / "archive" / task_id / f"{task_id}-audit.zip").is_file()

    again = runner.invoke(
        app,
        [
            "gate",
            "propose",
            "inventory-service",
            "--dry-run",
            "--runtime",
            "replay",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )
    assert again.exit_code == 0, again.output


def test_failed_architecture_proposal_is_sealed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discovered = runner.invoke(
        app,
        [
            "gate",
            "discover",
            "inventory-service",
            "--write",
            "--workspace",
            str(workspace),
        ],
    )
    assert discovered.exit_code == 2

    def fail_proposal(**_: object) -> None:
        raise OSError("proposal runtime failed")

    monkeypatch.setattr(cli_module, "_run_verification_proposal", fail_proposal)
    result = runner.invoke(
        app,
        ["gate", "propose", "inventory-service", "--workspace", str(workspace)],
    )

    assert result.exit_code != 0
    task = GenomeTaskStore(workspace).all_tasks(
        kind=GenomeTaskKind.VERIFICATION, subject="inventory-service"
    )[0]
    assert task.state is GenomeTaskState.FAILED
    assert "proposal runtime failed" in (task.failure_reason or "")
    assert (workspace / "archive" / task.id / f"{task.id}-audit.zip").is_file()
    notes = [event.payload.get("note") for event in EventLog(workspace).events(task.id)]
    assert "audit_sealed" in notes


def test_show_displays_and_exports_the_candidate_for_confirmation(
    workspace: Path, tmp_path: Path
) -> None:
    module = workspace / "repos/inventory-service"
    (module / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    resolution = resolve_verification("inventory-service", module)
    assert isinstance(resolution, Ready)
    write_pending_verification(
        workspace,
        "inventory-service",
        NeedsConfirmation(
            candidates=(resolution.spec,),
            issues=(ResolutionIssue(code="REVIEW", detail="请确认覆盖范围"),),
        ),
    )
    candidate = tmp_path / "candidate.yaml"

    result = _show(
        workspace,
        "inventory-service",
        "--candidate-file",
        str(candidate),
    )

    assert result.exit_code == 0, result.output
    assert "候选 1" in result.output
    assert "host.trusted" in result.output
    assert "make test" in result.output
    assert "Makefile#target:test" in result.output
    exported = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    assert exported["version"] == 2
    assert exported["module"] == "inventory-service"


def test_confirm_finishes_and_seals_the_proposal_task(
    workspace: Path, tmp_path: Path
) -> None:
    module = workspace / "repos/inventory-service"
    (module / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    resolution = resolve_verification("inventory-service", module)
    assert isinstance(resolution, Ready)
    store = GenomeTaskStore(workspace)
    task = store.create(
        title="补全验证规格",
        kind=GenomeTaskKind.VERIFICATION,
        origin=Origin.HUMAN,
        subject="inventory-service",
    )
    GenomeDriver(store, EventLog(workspace), enforce_budget=False).deliver(
        task.id, GenomeEvent.DRAFT_READY
    )
    record_pending_spec(
        workspace,
        "inventory-service",
        NeedsConfirmation(candidates=(resolution.spec,), issues=()),
        actor="arch-employee",
        entrance="test",
        proposal_task_id=task.id,
    )
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text(
        yaml.safe_dump(resolution.spec.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "gate",
            "confirm",
            "inventory-service",
            "--file",
            str(candidate),
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    assert store.get(task.id).state is GenomeTaskState.SUBMITTED
    assert (
        workspace / "archive" / task.id / f"{task.id}-audit.zip"
    ).is_file()
    notes = [event.payload.get("note") for event in EventLog(workspace).events(task.id)]
    assert "audit_sealed" in notes


def test_a_human_can_confirm_a_reviewed_candidate(workspace: Path, tmp_path: Path) -> None:
    module_root = workspace / "repos/inventory-service"
    (module_root / "Makefile").write_text("test:\n\tpytest -q\n", encoding="utf-8")
    resolution = resolve_verification("inventory-service", module_root)
    assert isinstance(resolution, Ready)
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text(
        yaml.safe_dump(resolution.spec.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "gate",
            "confirm",
            "inventory-service",
            "--file",
            str(candidate),
            "--workspace",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "confirmed"
    assert (workspace / "genome/gates/inventory-service.yaml").is_file()
    assert not (workspace / "genome/gates/inventory-service.pending.yaml").exists()
