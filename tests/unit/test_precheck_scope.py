"""提交前复查的授权范围:每个参与者各自一份,任务级禁令按员工分别叠加。"""

from __future__ import annotations

from agentgenome.employees import EmployeeConfig
from agentgenome.genome.rules import ProtectedRules
from agentgenome.security.precheck import check_scope, participant_policies


def _employee(employee_id: str, write: list[str]) -> EmployeeConfig:
    return EmployeeConfig.model_validate(
        {
            "id": employee_id,
            "runtime": "claude-code",
            "prompt": "prompts/x.md",
            "permissions": {"write_paths": write},
        }
    )


def test_extra_forbid_is_keyed_by_employee_not_shared() -> None:
    """写集分离里两个员工的任务级禁令是**相反**的,给一份共用的会把两边都禁死。"""
    dev = _employee("dev-employee", ["repos/**"])
    tester = _employee("tester-employee", ["repos/**"])

    policies = participant_policies(
        [dev, tester],
        "ag-1",
        ProtectedRules(),
        extra_forbid={
            "dev-employee": ["repos/**/tests/**"],
            "tester-employee": ["repos/**/src/**"],
        },
    )

    dev_policy, tester_policy = policies
    assert dev_policy.allows("repos/order/src/pay.py") is True
    assert dev_policy.allows("repos/order/tests/test_pay.py") is False
    assert tester_policy.allows("repos/order/tests/test_pay.py") is True
    assert tester_policy.allows("repos/order/src/pay.py") is False


def test_a_path_forbidden_for_one_participant_is_still_caught_by_the_or() -> None:
    """复查的判定是**或**:另一个参与者本可以合法写它,就不算越权。

    写集分离因此不会让复查误报——开发员工碰不了测试,但测试员工碰得了,那条路径在任务层面
    仍然是有人负责的。真正被抓住的是**落在所有人授权范围之外**的那些。
    """
    dev = _employee("dev-employee", ["repos/**"])
    tester = _employee("tester-employee", ["repos/**"])
    policies = participant_policies(
        [dev, tester],
        "ag-1",
        ProtectedRules(),
        extra_forbid={
            "dev-employee": ["repos/**/tests/**"],
            "tester-employee": ["repos/**/src/**"],
        },
    )

    assert check_scope(["repos/order/tests/test_pay.py"], policies) == []
    assert check_scope(["somewhere/else.py"], policies) != []


def test_no_extra_forbid_leaves_the_policies_unchanged() -> None:
    """不给任务级禁令时,复查拿到的授权与这个机制不存在时相同。"""
    dev = _employee("dev-employee", ["repos/**"])

    without = participant_policies([dev], "ag-1", ProtectedRules())
    empty = participant_policies([dev], "ag-1", ProtectedRules(), extra_forbid={})

    assert without == empty
    assert without[0].allows("repos/order/tests/test_pay.py") is True
