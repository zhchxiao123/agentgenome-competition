"""模块验证：发现带证据的规格，并在已注册环境中执行。"""

from agentgenome.verification.bootstrap import (
    BOOTSTRAP_SPECS,
    load_bootstrap_specs,
    promote_bootstrap_specs,
    record_bootstrap_spec,
)
from agentgenome.verification.discovery import resolve_verification
from agentgenome.verification.evidence import seal_agent_proposal, validate_spec_evidence
from agentgenome.verification.execution import VerificationContext, run_verification
from agentgenome.verification.models import (
    ArgvCommand,
    CommandEvidence,
    CommandProvenance,
    EnvironmentRef,
    NeedsConfirmation,
    PendingVerification,
    Ready,
    ResolutionIssue,
    VerificationGate,
    VerificationProposalSpec,
    VerificationResolution,
    VerificationSpec,
)
from agentgenome.verification.service import record_confirmed_spec, record_pending_spec
from agentgenome.verification.storage import (
    load_pending_verification,
    load_verification_spec,
    load_verification_spec_file,
    pending_verification_path,
    verification_spec_path,
    write_pending_verification,
    write_verification_spec,
)

__all__ = [
    "ArgvCommand",
    "BOOTSTRAP_SPECS",
    "CommandEvidence",
    "CommandProvenance",
    "EnvironmentRef",
    "NeedsConfirmation",
    "PendingVerification",
    "Ready",
    "ResolutionIssue",
    "VerificationGate",
    "VerificationContext",
    "VerificationProposalSpec",
    "VerificationResolution",
    "VerificationSpec",
    "resolve_verification",
    "record_confirmed_spec",
    "record_pending_spec",
    "seal_agent_proposal",
    "validate_spec_evidence",
    "run_verification",
    "load_bootstrap_specs",
    "promote_bootstrap_specs",
    "load_verification_spec",
    "load_verification_spec_file",
    "load_pending_verification",
    "pending_verification_path",
    "verification_spec_path",
    "write_verification_spec",
    "write_pending_verification",
    "record_bootstrap_spec",
]
