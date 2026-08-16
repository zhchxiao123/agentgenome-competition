"""门禁路由只做纯判断；执行由 verification Module 独占。"""

from agentgenome.gates.runner import modules_touched, worse
from agentgenome.verification.report import GateReportKind


def test_modules_are_selected_by_directory_boundary() -> None:
    modules = {"a": "repos/api", "b": "repos/api-2"}

    assert modules_touched(["repos/api-2/x.py"], modules) == ["b"]


def test_non_module_changes_select_nothing() -> None:
    assert modules_touched(
        ["genome/rules/architecture.md"], {"order": "repos/order"}
    ) == []


def test_environment_failure_has_priority_over_semantic_failure() -> None:
    assert (
        worse(GateReportKind.SEMANTIC, GateReportKind.ENVIRONMENT)
        is GateReportKind.ENVIRONMENT
    )
