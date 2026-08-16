"""模块验证执行：已确认规格进，统一门禁报告出。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agentgenome.verification import (
    ArgvCommand,
    CommandEvidence,
    CommandProvenance,
    EnvironmentRef,
    Ready,
    VerificationGate,
    VerificationSpec,
    resolve_verification,
    run_verification,
)
from agentgenome.verification.evidence import located_digest, platform_secrets_gate
from agentgenome.verification.execution import VerificationContext
from agentgenome.verification.report import GateOutcome, GateReportKind


def _provenance(module_root: Path) -> CommandProvenance:
    evidence_file = module_root / ".verification-test-entry"
    evidence_file.write_text("fixture command\n", encoding="utf-8")
    tools = module_root / ".test-tools"
    tools.mkdir(exist_ok=True)
    scanner = tools / "gitleaks"
    scanner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    scanner.chmod(0o755)
    os.environ["PATH"] = os.pathsep.join((str(tools), os.environ.get("PATH", "")))
    return CommandProvenance(
        origin="human",
        producer="test-fixture",
        evidence=(
            CommandEvidence(
                kind="repository-entrypoint",
                path=evidence_file.name,
                locator="file",
                digest=located_digest(evidence_file, "file"),
            ),
        ),
    )


def _spec(
    *,
    module: str,
    environments: dict[str, EnvironmentRef],
    gates: tuple[VerificationGate, ...],
) -> VerificationSpec:
    return VerificationSpec(
        module=module,
        environments={**environments, "host": EnvironmentRef(adapter="host.trusted")},
        gates=(*gates, platform_secrets_gate()),
    )


def test_host_process_executes_structured_argv_without_a_shell(tmp_path: Path) -> None:
    spec = _spec(
        module="example",
        environments={"project": EnvironmentRef(adapter="host.process")},
        gates=(
            VerificationGate(
                id="unit",
                environment="project",
                command=ArgvCommand(
                    argv=(sys.executable, "-c", "print('verified')")
                ),
                provenance=_provenance(tmp_path),
            ),
        ),
    )

    report = run_verification(
        spec,
        VerificationContext(
            task_id="ag-1",
            module_root=tmp_path,
            output_dir=tmp_path / "artifacts",
        ),
    )

    assert report.passed is True
    assert report.kind is GateReportKind.NONE
    assert report.gate("unit").outcome is GateOutcome.PASSED
    assert report.gate("unit").log_tail == "verified"


def test_python_uv_uses_trusted_tools_outside_the_employee_virtualenv(tmp_path: Path) -> None:
    module = tmp_path / "module"
    module.mkdir()
    (module / "Makefile").write_text(
        "test:\n\tuv run employee-only-test\n",
        encoding="utf-8",
    )
    (module / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    (module / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    employee_bin = tmp_path / "employee-venv" / "bin"
    employee_bin.mkdir(parents=True)
    for name in ("uv", "employee-only-test"):
        executable = employee_bin / name
        executable.write_text("#!/bin/sh\necho 'forged pass'\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)

    trusted_bin = tmp_path / "trusted-bin"
    trusted_bin.mkdir()
    trusted_uv = trusted_bin / "uv"
    trusted_uv.write_text(
        "#!/bin/sh\n"
        "if [ -n \"$UV_PROJECT_ENVIRONMENT\" ]; then echo 'shared project env'; fi\n"
        "echo 'trusted uv'\nexit 3\n",
        encoding="utf-8",
    )
    trusted_uv.chmod(0o755)

    gitleaks = trusted_bin / "gitleaks"
    gitleaks.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gitleaks.chmod(0o755)

    spec = _spec(
        module="example",
        environments={
            "project": EnvironmentRef(
                adapter="python.uv",
                options={"project_file": "pyproject.toml", "lockfile": "uv.lock"},
            )
        },
        gates=(
            VerificationGate(
                id="unit",
                environment="project",
                command=ArgvCommand(argv=("make", "test")),
                provenance=_provenance(module),
            ),
        ),
    )

    report = run_verification(
        spec,
        VerificationContext(
            task_id="ag-1",
            module_root=module,
            output_dir=tmp_path / "artifacts",
            environment={
                "VIRTUAL_ENV": str(employee_bin.parent),
                "PATH": os.pathsep.join(
                    (str(employee_bin), str(trusted_bin), "/usr/bin", "/bin")
                ),
            },
        ),
    )

    assert report.passed is False
    assert report.gate("unit").outcome is GateOutcome.FAILED
    assert "trusted uv" in report.gate("unit").log_tail
    assert "forged pass" not in report.gate("unit").log_tail
    assert "shared project env" not in report.gate("unit").log_tail


def test_node_npm_installs_from_the_lockfile_before_running_the_script(tmp_path: Path) -> None:
    module = tmp_path / "frontend"
    module.mkdir()
    (module / "package.json").write_text(
        '{"name":"frontend","scripts":{"test":"vitest run"}}\n', encoding="utf-8"
    )
    (module / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")

    employee_bin = tmp_path / "employee-venv" / "bin"
    employee_bin.mkdir(parents=True)
    employee_npm = employee_bin / "npm"
    employee_npm.write_text("#!/bin/sh\necho 'forged npm'\nexit 0\n", encoding="utf-8")
    employee_npm.chmod(0o755)

    trusted_bin = tmp_path / "trusted-bin"
    trusted_bin.mkdir()
    calls = tmp_path / "npm-calls"
    trusted_npm = trusted_bin / "npm"
    trusted_npm.write_text(
        f"#!/bin/sh\necho \"$@\" >> {calls}\n"
        "if [ \"$1\" = ci ]; then exit 0; fi\n"
        "echo 'trusted npm test'\n",
        encoding="utf-8",
    )
    trusted_npm.chmod(0o755)

    gitleaks = trusted_bin / "gitleaks"
    gitleaks.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gitleaks.chmod(0o755)

    spec = _spec(
        module="frontend",
        environments={
            "project": EnvironmentRef(
                adapter="node.npm",
                options={"manifest": "package.json", "lockfile": "package-lock.json"},
            )
        },
        gates=(
            VerificationGate(
                id="unit",
                environment="project",
                command=ArgvCommand(argv=("npm", "run", "test")),
                provenance=_provenance(module),
            ),
        ),
    )

    report = run_verification(
        spec,
        VerificationContext(
            task_id="ag-1",
            module_root=module,
            output_dir=tmp_path / "artifacts",
            environment={
                "VIRTUAL_ENV": str(employee_bin.parent),
                "PATH": os.pathsep.join(
                    (str(employee_bin), str(trusted_bin), "/usr/bin", "/bin")
                ),
            },
        ),
    )

    assert report.passed is True
    assert calls.read_text(encoding="utf-8").splitlines() == ["ci", "run test"]
    assert report.gate("unit").log_tail == "trusted npm test"


def test_platform_gate_does_not_use_a_forged_employee_tool(tmp_path: Path) -> None:
    employee_bin = tmp_path / "employee-venv" / "bin"
    employee_bin.mkdir(parents=True)
    forged = employee_bin / "gitleaks"
    forged.write_text("#!/bin/sh\necho 'forged scanner'\nexit 0\n", encoding="utf-8")
    forged.chmod(0o755)

    trusted_bin = tmp_path / "trusted-bin"
    trusted_bin.mkdir()
    trusted = trusted_bin / "gitleaks"
    trusted.write_text("#!/bin/sh\necho 'trusted scanner'\nexit 3\n", encoding="utf-8")
    trusted.chmod(0o755)
    spec = _spec(
        module="example",
        environments={},
        gates=(),
    )

    report = run_verification(
        spec,
        VerificationContext(
            task_id="ag-1",
            module_root=tmp_path,
            output_dir=tmp_path / "artifacts",
            environment={
                "VIRTUAL_ENV": str(employee_bin.parent),
                "PATH": os.pathsep.join((str(employee_bin), str(trusted_bin), "/usr/bin")),
            },
        ),
    )

    assert report.gate("secrets").outcome is GateOutcome.FAILED
    assert "trusted scanner" in report.gate("secrets").log_tail
    assert "forged scanner" not in report.gate("secrets").log_tail


def test_yarn_classic_uses_the_frozen_lockfile_flag(tmp_path: Path) -> None:
    module = tmp_path / "frontend"
    module.mkdir()
    (module / "package.json").write_text("{}\n", encoding="utf-8")
    (module / "yarn.lock").write_text("# yarn v1\n", encoding="utf-8")
    trusted_bin = tmp_path / "trusted-bin"
    trusted_bin.mkdir()
    calls = tmp_path / "yarn-calls"
    yarn = trusted_bin / "yarn"
    yarn.write_text(
        f"#!/bin/sh\necho \"$@\" >> {calls}\nexit 0\n", encoding="utf-8"
    )
    yarn.chmod(0o755)
    gitleaks = trusted_bin / "gitleaks"
    gitleaks.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gitleaks.chmod(0o755)

    spec = _spec(
        module="frontend",
        environments={
            "project": EnvironmentRef(
                adapter="node.yarn",
                options={
                    "manifest": "package.json",
                    "lockfile": "yarn.lock",
                    "generation": "classic",
                },
            )
        },
        gates=(
            VerificationGate(
                id="unit",
                environment="project",
                command=ArgvCommand(argv=("yarn", "run", "test")),
                provenance=_provenance(module),
            ),
        ),
    )

    report = run_verification(
        spec,
        VerificationContext(
            task_id="ag-1",
            module_root=module,
            output_dir=tmp_path / "artifacts",
            environment={"PATH": os.pathsep.join((str(trusted_bin), "/usr/bin"))},
        ),
    )

    assert report.passed is True
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "install --frozen-lockfile",
        "run test",
    ]


def test_changed_command_evidence_refuses_the_confirmed_spec(tmp_path: Path) -> None:
    module = tmp_path / "module"
    module.mkdir()
    makefile = module / "Makefile"
    makefile.write_text("test:\n\ttrue\n", encoding="utf-8")
    resolution = resolve_verification("example", module)
    assert isinstance(resolution, Ready)
    makefile.write_text("test:\n\t@echo weakened\n", encoding="utf-8")

    report = run_verification(
        resolution.spec,
        VerificationContext(
            task_id="ag-1",
            module_root=module,
            output_dir=tmp_path / "artifacts",
        ),
    )

    assert report.kind is GateReportKind.TAMPERED
    assert report.gate("unit").outcome is GateOutcome.REFUSED
    assert "命令证据已变化" in report.gate("unit").detail


def test_any_makefile_change_conservatively_stales_the_test_entrypoint(tmp_path: Path) -> None:
    module = tmp_path / "module"
    module.mkdir()
    makefile = module / "Makefile"
    makefile.write_text("test:\n\ttrue\n", encoding="utf-8")
    resolution = resolve_verification("example", module)
    assert isinstance(resolution, Ready)
    makefile.write_text("test:\n\ttrue\n\ndocs:\n\tfalse\n", encoding="utf-8")

    report = run_verification(
        resolution.spec,
        VerificationContext(
            task_id="ag-1",
            module_root=module,
            output_dir=tmp_path / "artifacts",
        ),
    )

    assert report.gate("unit").outcome is GateOutcome.REFUSED


def test_a_make_variable_change_cannot_silently_narrow_the_test_scope(
    tmp_path: Path,
) -> None:
    module = tmp_path / "module"
    module.mkdir()
    makefile = module / "Makefile"
    makefile.write_text("PROJECTS := a b\ntest:\n\tfor p in $(PROJECTS); do true; done\n")
    resolution = resolve_verification("example", module)
    assert isinstance(resolution, Ready)
    makefile.write_text("PROJECTS := a\ntest:\n\tfor p in $(PROJECTS); do true; done\n")

    report = run_verification(
        resolution.spec,
        VerificationContext(
            task_id="ag-1",
            module_root=module,
            output_dir=tmp_path / "artifacts",
        ),
    )

    assert report.kind is GateReportKind.TAMPERED
    assert report.gate("unit").outcome is GateOutcome.REFUSED
