"""轮次对比:`regressions[]` 与 `fixed[]`。

"这轮是修好了还是修坏了别的"——只看单轮报告答不出来。这是对抗"改 A 坏 B"震荡的
关键信息。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentgenome.gates.comparison import compare, failure_key, previous_report
from agentgenome.verification.report import GATE_REPORT


def _report(*failures: tuple[str, str, str]) -> dict:
    """`(gate, test, message)` 三元组攒一份最小报告。"""
    by_gate: dict[str, list[dict]] = {}
    for gate, test, message in failures:
        by_gate.setdefault(gate, []).append({"test": test, "message": message})
    return {
        "task_id": "ag-1",
        "producer": "unit-gate",
        "created_at": "x",
        "passed": not failures,
        "module": "order-service",
        "gates": [
            {"id": gate, "passed": False, "failures": items} for gate, items in by_gate.items()
        ],
    }


# --- 差集 -------------------------------------------------------------------


def test_a_new_failure_is_a_regression() -> None:
    previous = _report(("unit", "t::a", "挂了"))
    current = _report(("unit", "t::a", "挂了"), ("unit", "t::b", "也挂了"))

    regressions, fixed = compare(previous, current)

    assert [item["test"] for item in regressions] == ["t::b"]
    assert fixed == []


def test_a_disappeared_failure_is_fixed() -> None:
    previous = _report(("unit", "t::a", "挂了"), ("unit", "t::b", "也挂了"))
    current = _report(("unit", "t::a", "挂了"))

    regressions, fixed = compare(previous, current)

    assert regressions == []
    assert [item["test"] for item in fixed] == ["t::b"]


def test_fixing_one_and_breaking_another_shows_both() -> None:
    """这正是"改 A 坏 B"的样子。两个数组各有一条时,员工立刻知道自己干了什么。"""
    previous = _report(("unit", "t::a", "挂了"))
    current = _report(("unit", "t::b", "挂了"))

    regressions, fixed = compare(previous, current)

    assert [item["test"] for item in regressions] == ["t::b"]
    assert [item["test"] for item in fixed] == ["t::a"]


def test_two_identical_rounds_show_nothing() -> None:
    same = _report(("unit", "t::a", "挂了"))

    assert compare(same, same) == ([], [])


def test_the_first_round_has_nothing_to_compare_against() -> None:
    assert compare(None, _report(("unit", "t::a", "挂了"))) == ([], [])


def test_an_all_green_round_after_failures_lists_them_all_as_fixed() -> None:
    previous = _report(("unit", "t::a", "挂了"), ("unit", "t::b", "挂了"))

    regressions, fixed = compare(previous, _report())

    assert regressions == []
    assert len(fixed) == 2


# --- 失败的身份 -------------------------------------------------------------


def test_identity_is_the_case_not_the_message() -> None:
    """同一个用例两轮的报错信息可能不同(行号变了、断言值变了),但它还是同一个失败。

    按文本比的话每轮都会报"全是新回归",这个字段就废了。
    """
    previous = _report(("unit", "t::a", "期望 3 实际 0"))
    current = _report(("unit", "t::a", "期望 3 实际 1"))

    regressions, fixed = compare(previous, current)

    assert regressions == []
    assert fixed == []


def test_the_same_case_in_two_gates_is_two_failures() -> None:
    """单测挂的 `t::a` 与 lint 挂的 `t::a` 不是一回事。"""
    assert failure_key("unit", {"test": "t::a"}) != failure_key("lint", {"test": "t::a"})


def test_a_failure_without_a_test_name_falls_back_to_the_message() -> None:
    """解析不出用例名时也要有个身份,不然它每轮都算新的。"""
    assert failure_key("unit", {"message": "整关挂了"}) == failure_key(
        "unit", {"message": "整关挂了"}
    )
    assert failure_key("unit", {"message": "整关挂了"}) != failure_key(
        "unit", {"message": "另一件事挂了"}
    )


# --- 从产物目录找上一轮 -----------------------------------------------------


def test_the_previous_report_comes_from_the_previous_slot(tmp_path: Path) -> None:
    """按轮次找,靠 PRD 05 的不变式:某阶段的第 k 个产物目录就是它的第 k 轮。"""
    from agentgenome.jobs.artifacts import ArtifactBus

    bus = ArtifactBus(tmp_path)
    first = bus.allocate("unit-gate")
    (first.path / GATE_REPORT).write_text(json.dumps(_report(("unit", "t::a", "挂了"))))
    bus.allocate("unit-gate")

    found = previous_report(bus, "unit-gate", attempt=2)

    assert found is not None
    assert found["gates"][0]["failures"][0]["test"] == "t::a"


def test_the_first_attempt_has_no_previous(tmp_path: Path) -> None:
    from agentgenome.jobs.artifacts import ArtifactBus

    bus = ArtifactBus(tmp_path)
    bus.allocate("unit-gate")

    assert previous_report(bus, "unit-gate", attempt=1) is None


def test_a_previous_slot_without_a_report_is_treated_as_absent(tmp_path: Path) -> None:
    """上一轮崩在写报告之前是可能的。当成"没有上一轮"比抛异常好。"""
    from agentgenome.jobs.artifacts import ArtifactBus

    bus = ArtifactBus(tmp_path)
    bus.allocate("unit-gate")
    bus.allocate("unit-gate")

    assert previous_report(bus, "unit-gate", attempt=2) is None
