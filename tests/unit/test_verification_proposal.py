"""架构员工可以补全歧义，但候选必须能由真实仓库证据复核。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentgenome.verification import (
    ArgvCommand,
    CommandEvidence,
    CommandProvenance,
    EnvironmentRef,
    PendingVerification,
    Ready,
    ResolutionIssue,
    VerificationGate,
    VerificationSpec,
    resolve_verification,
)
from agentgenome.verification.environments import AdapterRegistry, TrustedHostAdapter
from agentgenome.verification.evidence import (
    located_digest,
    platform_secrets_gate,
    seal_agent_proposal,
    validate_spec_evidence,
)
from agentgenome.verification.proposal import (
    EMPLOYEE_ID,
    VerificationProposal,
    VerificationProposalSpec,
    build_prompt,
    proposal_output_check,
)


def _proposal(task_id: str, module: Path) -> VerificationProposal:
    resolution = resolve_verification("example", module)
    assert isinstance(resolution, Ready)
    gates = tuple(
        gate.model_copy(
            update={
                "provenance": CommandProvenance(
                    origin="agent-proposal",
                    producer=EMPLOYEE_ID,
                    evidence=gate.provenance.evidence,
                )
            }
        )
        for gate in resolution.spec.gates
        if gate.provenance.origin != "platform"
    )
    environments = {
        name: environment
        for name, environment in resolution.spec.environments.items()
        if name != "host"
    }
    return VerificationProposal(
        task_id=task_id,
        producer=EMPLOYEE_ID,
        rationale="仓库 Makefile 声明了 test target",
        spec=VerificationProposalSpec(
            module=resolution.spec.module,
            environments=environments,
            gates=gates,
        ),
    )


def _environment_issue(
    tmp_path: Path, module: Path, reference: EnvironmentRef
) -> str | None:
    output = tmp_path / "artifacts"
    output.mkdir()
    proposal = _proposal("gn-1", module)
    invalid = proposal.model_copy(
        update={
            "spec": proposal.spec.model_copy(
                update={"environments": {"project": reference}}
            )
        }
    )
    (output / "result.json").write_text(
        json.dumps(invalid.model_dump(mode="json")), encoding="utf-8"
    )
    return proposal_output_check("gn-1", "example", module)(output)


def test_architecture_proposal_is_checked_against_repository_evidence(
    tmp_path: Path,
) -> None:
    module = tmp_path / "module"
    module.mkdir()
    (module / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    output = tmp_path / "artifacts"
    output.mkdir()
    proposal = _proposal("gn-1", module)
    (output / "result.json").write_text(
        json.dumps(proposal.model_dump(mode="json")), encoding="utf-8"
    )

    check = proposal_output_check("gn-1", "example", module)

    assert check(output) is None
    sealed = seal_agent_proposal(proposal.spec, module)
    assert sealed.gate("secrets") == platform_secrets_gate()
    assert sealed.environments["host"] == EnvironmentRef(adapter="host.trusted")
    gate = proposal.spec.gates[0]
    evidence = gate.provenance.evidence[0].model_copy(
        update={"locator": "target:missing"}
    )
    bad_gate = gate.model_copy(
        update={
            "provenance": gate.provenance.model_copy(
                update={"evidence": (evidence,)}
            )
        }
    )
    bad = proposal.model_copy(
        update={
            "spec": proposal.spec.model_copy(
                update={"gates": (bad_gate, *proposal.spec.gates[1:])}
            )
        }
    )
    (output / "result.json").write_text(
        json.dumps(bad.model_dump(mode="json")), encoding="utf-8"
    )
    assert "候选证据不可定位" in (check(output) or "")


def test_empty_repository_proposal_is_sealed_with_the_platform_gate(
    tmp_path: Path,
) -> None:
    """空骨架没有作者声明入口时，平台安全门禁仍能形成合法最终规格。"""
    module = tmp_path / "module"
    module.mkdir()
    output = tmp_path / "artifacts"
    output.mkdir()
    proposal = VerificationProposal(
        task_id="ag-1",
        producer=EMPLOYEE_ID,
        rationale="仓库没有作者声明的验证入口",
        spec=VerificationProposalSpec(module="example"),
    )
    (output / "result.json").write_text(
        json.dumps(proposal.model_dump(mode="json")), encoding="utf-8"
    )

    issue = proposal_output_check(
        "ag-1",
        "example",
        module,
        pending=PendingVerification(
            module="example",
            issues=(
                ResolutionIssue(
                    code="NO_STANDARD_ENTRYPOINT",
                    detail="没有标准入口",
                ),
            ),
        ),
    )(output)
    sealed = seal_agent_proposal(proposal.spec, module)

    assert issue is None
    assert sealed.environments == {
        "host": EnvironmentRef(adapter="host.trusted")
    }
    assert sealed.gates == (platform_secrets_gate(),)


def test_empty_repository_proposal_requires_a_no_entrypoint_finding(
    tmp_path: Path,
) -> None:
    module = tmp_path / "module"
    module.mkdir()
    output = tmp_path / "artifacts"
    output.mkdir()
    proposal = VerificationProposal(
        task_id="ag-1",
        producer=EMPLOYEE_ID,
        rationale="不运行项目门禁",
        spec=VerificationProposalSpec(module="example"),
    )
    (output / "result.json").write_text(
        json.dumps(proposal.model_dump(mode="json")), encoding="utf-8"
    )

    issue = proposal_output_check(
        "ag-1",
        "example",
        module,
        pending=PendingVerification(
            module="example",
            issues=(ResolutionIssue(code="INVALID_DECLARATION", detail="配置损坏"),),
        ),
    )(output)

    assert issue == "只有确定性发现 NO_STANDARD_ENTRYPOINT 时才能提交空门禁候选"


def test_architecture_proposal_rejects_an_incomplete_environment_contract(
    tmp_path: Path,
) -> None:
    module = tmp_path / "module"
    module.mkdir()
    (module / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    issue = _environment_issue(
        tmp_path, module, EnvironmentRef(adapter="python.uv")
    )

    assert issue == (
        "执行环境声明不可用: project: python.uv 缺少字符串 option: project_file"
    )


def test_architecture_proposal_rejects_a_missing_declared_file(tmp_path: Path) -> None:
    module = tmp_path / "module"
    module.mkdir()
    (module / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")

    issue = _environment_issue(
        tmp_path,
        module,
        EnvironmentRef(
            adapter="python.uv",
            options={"project_file": "Makefile", "lockfile": "uv.lock"},
        ),
    )

    assert issue == (
        "执行环境声明不可用: project: python.uv 缺少声明文件: uv.lock"
    )


def test_architecture_proposal_cannot_reference_adapter_files_outside_module(
    tmp_path: Path,
) -> None:
    module = tmp_path / "module"
    module.mkdir()
    (module / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    issue = _environment_issue(
        tmp_path,
        module,
        EnvironmentRef(
            adapter="python.uv",
            options={"project_file": "Makefile", "lockfile": "../uv.lock"},
        ),
    )

    assert issue == (
        "执行环境声明不可用: project: "
        "python.uv option lockfile 必须是模块内相对路径"
    )


def test_architecture_proposal_requires_relative_adapter_file_paths(tmp_path: Path) -> None:
    module = tmp_path / "module"
    module.mkdir()
    (module / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    lockfile = module / "uv.lock"
    lockfile.write_text("version = 1\n", encoding="utf-8")

    issue = _environment_issue(
        tmp_path,
        module,
        EnvironmentRef(
            adapter="python.uv",
            options={"project_file": "Makefile", "lockfile": str(lockfile)},
        ),
    )

    assert issue == (
        "执行环境声明不可用: project: "
        "python.uv option lockfile 必须是模块内相对路径"
    )


def test_architecture_proposal_rejects_unknown_adapter_options(tmp_path: Path) -> None:
    module = tmp_path / "module"
    module.mkdir()
    (module / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    (module / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    issue = _environment_issue(
        tmp_path,
        module,
        EnvironmentRef(
            adapter="python.uv",
            options={
                "project_file": "Makefile",
                "lockfile": "uv.lock",
                "project_flie": "typo",
            },
        ),
    )

    assert issue == (
        "执行环境声明不可用: project: "
        "python.uv 包含未知 options: project_flie"
    )


def test_architecture_proposal_prompt_explains_adapter_contracts() -> None:
    prompt = build_prompt(
        "ag-1",
        "example",
        "repos/example",
        PendingVerification(
            module="example",
            issues=(ResolutionIssue(code="NO_ENTRYPOINT", detail="缺少入口"),),
        ),
    )

    assert "`python.uv`: `project_file`、`lockfile`" in prompt
    assert "声明文件必须已存在" in prompt
    assert "`host.trusted`: 不接受 options" in prompt
    assert "`host.process`: 不接受 options" in prompt
    assert "允许提交空的 environments 与 gates" in prompt


def test_architecture_proposal_prompt_comes_from_the_registered_adapters() -> None:
    prompt = build_prompt(
        "ag-1",
        "example",
        "repos/example",
        PendingVerification(
            module="example",
            issues=(ResolutionIssue(code="NO_ENTRYPOINT", detail="缺少入口"),),
        ),
        registry=AdapterRegistry((TrustedHostAdapter(),)),
    )

    assert "`host.trusted`: 不接受 options" in prompt
    assert "python.uv" not in prompt


def test_agent_cannot_forge_a_platform_gate(tmp_path: Path) -> None:
    forged = VerificationProposalSpec(
        module="example",
        environments={"host": EnvironmentRef(adapter="host.trusted")},
        gates=(
            VerificationGate(
                id="secrets",
                environment="host",
                command=ArgvCommand(argv=("true",)),
                provenance=CommandProvenance(
                    origin="platform",
                    producer="arch-employee",
                    evidence=(
                        CommandEvidence(
                            kind="platform-policy",
                            path="",
                            locator="builtin:gitleaks-detect",
                            digest="builtin:agentgenome.secrets@1",
                        ),
                    ),
                ),
            ),
        ),
    )

    assert "平台门禁声明不受信" in (
        validate_spec_evidence(forged, tmp_path) or ""
    )
    with pytest.raises(ValueError, match="架构员工不能声明平台门禁"):
        seal_agent_proposal(forged, tmp_path)


def test_confirmed_spec_cannot_omit_the_platform_secrets_gate(tmp_path: Path) -> None:
    entry = tmp_path / "verify.sh"
    entry.write_text("exit 0\n", encoding="utf-8")
    spec = VerificationSpec(
        module="example",
        environments={"project": EnvironmentRef(adapter="host.process")},
        gates=(
            VerificationGate(
                id="unit",
                environment="project",
                command=ArgvCommand(argv=("sh", "verify.sh")),
                provenance=CommandProvenance(
                    origin="human",
                    producer="human",
                    evidence=(
                        CommandEvidence(
                            kind="repository-entrypoint",
                            path="verify.sh",
                            locator="file",
                            digest=located_digest(entry, "file"),
                        ),
                    ),
                ),
            ),
        ),
    )

    assert "必须且只能包含一个平台 secrets 门禁" in (
        validate_spec_evidence(spec, tmp_path) or ""
    )


@pytest.mark.parametrize("path", ["../../outside", "/tmp/outside", "..\\outside"])
def test_evidence_path_must_stay_inside_the_module(path: str) -> None:
    with pytest.raises(ValueError, match="模块内相对路径"):
        CommandEvidence(
            kind="repository-entrypoint",
            path=path,
            locator="file",
            digest="sha256:x",
        )
