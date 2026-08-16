"""门禁编排共用的纯函数；命令执行只存在于 verification Module。"""

from __future__ import annotations

from agentgenome.core.scope import is_under
from agentgenome.verification.report import GateReportKind


def modules_touched(paths: list[str], module_paths: dict[str, str]) -> list[str]:
    """从改动路径反查涉及模块，按目录边界而非字符串前缀。"""
    return sorted(
        module_id
        for module_id, module_path in module_paths.items()
        if any(is_under(path, module_path) for path in paths)
    )


SEVERITY = (
    GateReportKind.NONE,
    GateReportKind.SEMANTIC,
    GateReportKind.ENVIRONMENT,
    GateReportKind.TAMPERED,
)


def worse(left: GateReportKind, right: GateReportKind) -> GateReportKind:
    """合并模块报告时保留最需要优先处理的失败性质。"""
    return max(left, right, key=SEVERITY.index)


__all__ = ["SEVERITY", "modules_touched", "worse"]
