"""把确定性发现留下的歧义交给架构员工形成候选，再由平台验真。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentgenome.verification.environments import (
    AdapterRegistry,
    AdapterUnavailable,
    default_registry,
)
from agentgenome.verification.evidence import seal_agent_proposal, validate_spec_evidence
from agentgenome.verification.models import PendingVerification, VerificationProposalSpec

EMPLOYEE_ID = "arch-employee"
PROCEDURE_ID = "verification-propose"
PROCEDURE_VERSION = "1.0.0"


class VerificationProposal(BaseModel):
    """架构员工交付的结构化候选；它本身不是项目控制面事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    producer: str
    rationale: str
    spec: VerificationProposalSpec


RECEIPT_SCHEMA = VerificationProposal.model_json_schema()


def build_prompt(
    task_id: str,
    module_id: str,
    module_path: str,
    pending: PendingVerification,
    *,
    registry: AdapterRegistry | None = None,
) -> str:
    """给架构员工一份只做证据调查、不做授权的上下文包。"""
    adapters = registry or default_registry()
    return f"""# 模块验证规格候选

你是架构员工。请只读调查模块 `{module_id}`（Workspace 相对路径
`{module_path}`），为平台复核提出一份 v2 `VerificationSpec` 候选。

确定性发现留下的缺口：
```json
{json.dumps(pending.model_dump(mode="json"), ensure_ascii=False, indent=2)}
```

约束：
- 命令必须来自仓库中可重新定位的作者声明，例如 Make target、package.json script、
  CI 配置或项目文档；不要凭经验发明 pytest/npm/uv 等命令。
- 命令必须是结构化 argv，不使用 shell 字符串。
- 只可使用以下已注册环境及其 options 契约：
{adapters.proposal_guidance()}
  所有声明文件必须已存在于模块目录内。Adapter 只隔离执行环境，不会替命令安装项目或补写
  lockfile；候选命令自身必须是作者声明的完整入口，不能省略文档明确写出的准备步骤。
- 非平台门禁的 provenance 必须是 `origin=agent-proposal`、
  `producer=arch-employee`；evidence.path 相对模块目录，locator 必须是
  `target:<name>`、`/scripts/<name>` 或 `file`。digest 可填空串，平台会根据 locator
  读取当前事实并盖章；不要自行声称一个未经平台复核的摘要。
- 不要提交 `origin=platform` 的门禁，也不要使用保留的 `secrets` 门禁 id；平台会在
  封存候选时注入 canonical secrets 门禁及其 `host.trusted` 环境。
- 如果只读调查确认仓库没有任何作者声明的验证入口，允许提交空的 environments 与 gates；
  不要为了满足格式而伪造命令。平台仍会在封存时注入安全门禁。
- 你只提出候选，不得写入 `genome/gates/`，也不得把候选称为“已确认”。
- 最终严格按给定 JSON Schema 交付；task_id 必须为 `{task_id}`，producer 必须为
  `arch-employee`，spec.module 必须为 `{module_id}`。
"""


def proposal_output_check(
    task_id: str,
    module_id: str,
    module_root: Path,
    *,
    pending: PendingVerification | None = None,
    registry: AdapterRegistry | None = None,
) -> Callable[[Path], str | None]:
    """验证真实候选与当前仓库证据，而不是员工的自述。"""
    adapters = registry or default_registry()

    def check(output_dir: Path) -> str | None:
        result = output_dir / "result.json"
        try:
            proposal = VerificationProposal.model_validate_json(
                result.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as error:
            return f"验证候选不可读: {error}"
        if proposal.task_id != task_id:
            return f"task_id 不匹配: {proposal.task_id} != {task_id}"
        if proposal.producer != EMPLOYEE_ID:
            return f"producer 必须是 {EMPLOYEE_ID}"
        if proposal.spec.module != module_id:
            return f"spec.module 不匹配: {proposal.spec.module} != {module_id}"
        if not proposal.spec.gates and not (
            pending is not None
            and any(issue.code == "NO_STANDARD_ENTRYPOINT" for issue in pending.issues)
        ):
            return "只有确定性发现 NO_STANDARD_ENTRYPOINT 时才能提交空门禁候选"
        for gate in proposal.spec.gates:
            if gate.provenance.origin == "platform":
                return f"{gate.id} 不能由架构员工声明为平台门禁"
            if (
                gate.provenance.origin != "agent-proposal"
                or gate.provenance.producer != EMPLOYEE_ID
            ):
                return f"{gate.id} 的候选 provenance 不是架构员工提案"
        try:
            sealed = seal_agent_proposal(proposal.spec, module_root)
        except (OSError, UnicodeError, ValueError) as error:
            return f"候选证据不可定位: {error}"
        for name, reference in sealed.environments.items():
            try:
                adapters.validate(reference, module_root)
            except AdapterUnavailable as error:
                return f"执行环境声明不可用: {name}: {error}"
        return validate_spec_evidence(sealed, module_root)

    return check


def load_proposal(path: Path) -> VerificationProposal:
    return VerificationProposal.model_validate_json(path.read_text(encoding="utf-8"))


__all__ = [
    "EMPLOYEE_ID",
    "PROCEDURE_ID",
    "PROCEDURE_VERSION",
    "RECEIPT_SCHEMA",
    "VerificationProposal",
    "VerificationProposalSpec",
    "build_prompt",
    "load_proposal",
    "proposal_output_check",
]
