"""空项目在首个开发任务之后补出模块验证规格。"""

from __future__ import annotations

import os
from pathlib import Path

from agentgenome.gates.task_gates import run_modules
from agentgenome.verification import NeedsConfirmation, resolve_verification
from agentgenome.verification.bootstrap import (
    load_bootstrap_specs,
    promote_bootstrap_specs,
    record_bootstrap_spec,
)
from agentgenome.verification.storage import write_pending_verification, write_verification_spec
from tests.fixtures.git import commit_all, git
from tests.fixtures.mini_map import write_workspace


def test_a_task_can_bootstrap_verification_from_the_entrypoint_it_just_created(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """初始化时为空不是永久事实；门禁必须看开发后的任务工作树。"""
    control = write_workspace(tmp_path / "control")
    workdir = write_workspace(tmp_path / "workdir")
    module_id = "inventory-service"
    control_module = control / "repos" / module_id
    resolution = resolve_verification(module_id, control_module)
    assert isinstance(resolution, NeedsConfirmation)
    write_pending_verification(control, module_id, resolution)

    module = workdir / "repos" / module_id
    tests = module / "tests"
    tests.mkdir()
    (tests / "test_smoke.py").write_text(
        "import unittest\n\n"
        "class SmokeTest(unittest.TestCase):\n"
        "    def test_project_runs(self):\n"
        "        self.assertEqual(2 + 2, 4)\n",
        encoding="utf-8",
    )
    (module / "Makefile").write_text(
        "test:\n\tpython3 -m unittest discover -s tests\n\n"
        "build:\n\tpython3 -m compileall -q .\n",
        encoding="utf-8",
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    gitleaks = tools / "gitleaks"
    gitleaks.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gitleaks.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(tools), os.environ.get("PATH", ""))))

    output = tmp_path / "artifacts"
    report = run_modules(
        workdir=workdir,
        task_id="ag-20260815-001",
        output_dir=output,
        changed=[f"repos/{module_id}/Makefile"],
        targets=[module_id],
        control_root=control,
    )

    assert report.passed is True
    assert report.notes == (f"bootstrap_verification:{module_id}",)
    (spec,) = load_bootstrap_specs(output)
    assert spec.module == module_id
    assert spec.gate("unit").command.argv == ("make", "test")
    assert spec.gate("build").command.argv == ("make", "build")
    assert not (control / "genome" / "gates" / f"{module_id}.yaml").exists(), (
        "任务尚未交付，临时规格不能提前污染项目控制面"
    )


def test_gate_collects_every_ambiguous_module_for_deep_analysis(tmp_path: Path) -> None:
    """一轮里有多个空模块时不能只分析第一个，再把第二个误送回开发。"""
    control = write_workspace(tmp_path / "control")
    workdir = write_workspace(tmp_path / "workdir")
    modules = ("order-service", "inventory-service")
    for module_id in modules:
        module = workdir / "repos" / module_id
        resolution = resolve_verification(module_id, module)
        assert isinstance(resolution, NeedsConfirmation)
        write_pending_verification(control, module_id, resolution)

    report = run_modules(
        workdir=workdir,
        task_id="ag-20260815-ambiguous",
        output_dir=tmp_path / "artifacts",
        changed=[f"repos/{module_id}/README.md" for module_id in modules],
        targets=list(modules),
        control_root=control,
    )

    assert report.passed is False
    assert tuple(request.module for request in report.verification_requests) == modules
    assert len(report.gates) == 2


def test_a_delivered_task_promotes_its_bootstrap_spec_with_an_audit_commit(
    tmp_path: Path,
) -> None:
    """只有交付事实出现后，任务级规格才成为项目唯一可执行事实。"""
    workspace = write_workspace(tmp_path / "workspace")
    git(workspace, "init", "-b", "main")
    module_id = "inventory-service"
    module = workspace / "repos" / module_id
    (module / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
    resolution = resolve_verification(module_id, module)
    assert not isinstance(resolution, NeedsConfirmation)
    pending = resolve_verification(module_id, tmp_path / "still-empty")
    assert isinstance(pending, NeedsConfirmation)
    write_pending_verification(workspace, module_id, pending)
    commit_all(workspace, "chore: empty project initialized")

    output = tmp_path / "gate-output"
    record_bootstrap_spec(output, resolution.spec)
    changes = promote_bootstrap_specs(
        workspace,
        output,
        actor="orchestrator",
        entrance="task-delivered",
    )

    assert [change.path.name for change in changes] == [f"{module_id}.yaml"]
    assert not (workspace / "genome/gates" / f"{module_id}.pending.yaml").exists()
    assert "update verification for inventory-service" in git(
        workspace, "log", "-1", "--format=%s"
    )


def test_delivery_does_not_overwrite_a_spec_confirmed_while_the_task_was_open(
    tmp_path: Path,
) -> None:
    """人工确认比旧门禁制品新，交付副作用不能把人类决定倒回去。"""
    workspace = write_workspace(tmp_path / "workspace")
    git(workspace, "init", "-b", "main")
    module_id = "inventory-service"
    module = workspace / "repos" / module_id
    (module / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
    old = resolve_verification(module_id, module)
    assert not isinstance(old, NeedsConfirmation)
    output = tmp_path / "gate-output"
    record_bootstrap_spec(output, old.spec)

    (module / "Makefile").write_text("test:\n\t@echo confirmed\n", encoding="utf-8")
    confirmed = resolve_verification(module_id, module)
    assert not isinstance(confirmed, NeedsConfirmation)
    write_verification_spec(workspace, confirmed.spec)
    commit_all(workspace, "chore: confirm verification while task is open")

    changes = promote_bootstrap_specs(
        workspace,
        output,
        actor="orchestrator",
        entrance="task-delivered",
    )

    assert changes == ()
    assert (workspace / "genome/gates" / f"{module_id}.yaml").read_text(
        encoding="utf-8"
    )
    assert git(workspace, "log", "-1", "--format=%s").strip() == (
        "chore: confirm verification while task is open"
    )


def test_delivery_refuses_to_promote_evidence_that_drifted_during_merge(
    tmp_path: Path,
) -> None:
    """临时规格通过后入口又变了，合并后的项目事实必须重新验真。"""
    workspace = write_workspace(tmp_path / "workspace")
    git(workspace, "init", "-b", "main")
    module_id = "inventory-service"
    module = workspace / "repos" / module_id
    makefile = module / "Makefile"
    makefile.write_text("test:\n\t@true\n", encoding="utf-8")
    resolution = resolve_verification(module_id, module)
    assert not isinstance(resolution, NeedsConfirmation)
    output = tmp_path / "gate-output"
    record_bootstrap_spec(output, resolution.spec)
    makefile.write_text("test:\n\t@echo changed-after-gate\n", encoding="utf-8")
    commit_all(workspace, "feat: merged implementation changed the entrypoint")

    try:
        promote_bootstrap_specs(
            workspace,
            output,
            actor="orchestrator",
            entrance="task-delivered",
        )
    except ValueError as error:
        assert "证据已变化" in str(error)
    else:
        raise AssertionError("漂移的临时规格被提升了")
    assert not (workspace / "genome/gates" / f"{module_id}.yaml").exists()
