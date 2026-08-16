"""模块验证规格发现：仓库事实进，带证据的规格或待确认结果出。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentgenome.verification import (
    ArgvCommand,
    CommandEvidence,
    CommandProvenance,
    EnvironmentRef,
    NeedsConfirmation,
    Ready,
    load_verification_spec,
    resolve_verification,
    write_verification_spec,
)
from agentgenome.verification.models import VerificationGate, VerificationSpec


def test_a_makefile_entrypoint_and_uv_lock_resolve_without_a_model(tmp_path: Path) -> None:
    module = tmp_path / "python-project"
    module.mkdir()
    (module / "Makefile").write_text(
        "test:\n\tuv run pytest\n\ninstall:\n\tuv sync --locked\n",
        encoding="utf-8",
    )
    (module / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")
    (module / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    resolution = resolve_verification("example", module)

    assert isinstance(resolution, Ready)
    assert resolution.spec.module == "example"
    assert resolution.spec.environments["project"].adapter == "python.uv"
    assert resolution.spec.gate("unit").command.argv == ("make", "test")
    assert resolution.spec.gate("unit").provenance.evidence[0].path == "Makefile"
    assert resolution.spec.gate("unit").provenance.evidence[0].locator == "target:test"


def test_package_scripts_and_lockfile_resolve_a_node_environment(tmp_path: Path) -> None:
    module = tmp_path / "frontend"
    module.mkdir()
    (module / "package.json").write_text(
        json.dumps(
            {
                "name": "frontend",
                "scripts": {"test": "vitest run", "build": "vite build"},
            }
        ),
        encoding="utf-8",
    )
    (module / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")

    resolution = resolve_verification("frontend", module)

    assert isinstance(resolution, Ready)
    assert resolution.spec.environments["project"].adapter == "node.npm"
    assert resolution.spec.gate("unit").command.argv == ("npm", "run", "test")
    assert resolution.spec.gate("build").command.argv == ("npm", "run", "build")
    assert resolution.spec.gate("unit").provenance.evidence[0].locator == "/scripts/test"


def test_yarn_classic_and_berry_keep_their_install_semantics(tmp_path: Path) -> None:
    for name, declared, expected in (
        ("classic", "yarn@1.22.22", "classic"),
        ("berry", "yarn@4.2.0", "berry"),
    ):
        module = tmp_path / name
        module.mkdir()
        (module / "package.json").write_text(
            json.dumps(
                {"packageManager": declared, "scripts": {"test": "vitest run"}}
            ),
            encoding="utf-8",
        )
        (module / "yarn.lock").write_text("# lock\n", encoding="utf-8")

        resolution = resolve_verification(name, module)

        assert isinstance(resolution, Ready)
        assert resolution.spec.environments["project"].options["generation"] == expected


def test_a_package_manager_that_conflicts_with_the_lockfile_needs_confirmation(
    tmp_path: Path,
) -> None:
    module = tmp_path / "frontend"
    module.mkdir()
    (module / "package.json").write_text(
        json.dumps(
            {
                "name": "frontend",
                "packageManager": "pnpm@9.0.0",
                "scripts": {"test": "vitest run"},
            }
        ),
        encoding="utf-8",
    )
    (module / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")

    resolution = resolve_verification("frontend", module)

    assert isinstance(resolution, NeedsConfirmation)
    assert resolution.issues[0].code == "CONFLICTING_TOOLCHAIN"


def test_a_ready_spec_round_trips_through_the_single_versioned_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    module = workspace / "repos" / "example"
    module.mkdir(parents=True)
    (module / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    resolution = resolve_verification("example", module)
    assert isinstance(resolution, Ready)

    target = write_verification_spec(workspace, resolution.spec)
    loaded = load_verification_spec(workspace, "example")

    assert target == workspace / "genome/gates/example.yaml"
    assert loaded == resolution.spec


def test_an_empty_confirmed_spec_cannot_issue_a_green_light() -> None:
    with pytest.raises(ValueError, match="至少需要一个必需门禁"):
        VerificationSpec(
            module="example",
            environments={"host": EnvironmentRef(adapter="host.process")},
            gates=(),
        )


def test_an_unknown_parser_is_rejected_before_execution() -> None:
    with pytest.raises(ValueError, match="没有这个解析器"):
        VerificationGate(
            id="unit",
            environment="host",
            command=ArgvCommand(argv=("true",)),
            provenance=CommandProvenance(
                origin="human",
                producer="human",
                evidence=(
                    CommandEvidence(
                        kind="human",
                        path="",
                        locator="manual",
                        digest="manual",
                    ),
                ),
            ),
            parser="typo",
        )


def test_malformed_or_unknown_spec_versions_are_configuration_errors(
    tmp_path: Path,
) -> None:
    target = tmp_path / "genome/gates/example.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("version: [\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML 不可读"):
        load_verification_spec(tmp_path, "example")

    target.write_text("version: 99\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不支持"):
        load_verification_spec(tmp_path, "example")
