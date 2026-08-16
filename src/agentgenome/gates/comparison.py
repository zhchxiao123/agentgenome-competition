"""相邻两轮门禁报告的差集。

"这轮是修好了还是修坏了别的"——只看单轮报告答不出来。这是对抗"改 A 坏 B"震荡的关键信息:
员工看到 `regressions[]` 非空就知道自己上一轮的修复引入了新问题,而不是只看到"又挂了三个
测试"然后从头猜。

## 身份是用例,不是文本

同一个用例两轮的报错信息可能不同(行号变了、断言值变了),但它还是同一个失败。按文本比的话
每轮都会报"全是新回归",这个字段立刻就废了。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agentgenome.gates.parsers import Failure
from agentgenome.verification.report import GATE_REPORT

if TYPE_CHECKING:
    from agentgenome.jobs.artifacts import ArtifactBus

Report = dict[str, Any]


def failure_key(gate_id: str, failure: Failure) -> str:
    """一条失败的身份。

    带上关的 id:单测挂的 `t::a` 与 lint 挂的 `t::a` 不是一回事。解析不出用例名时退回
    消息文本——不然它每轮都算新的。
    """
    identity = failure.get("test") or failure.get("message") or "(无法标识的失败)"
    return f"{gate_id}::{identity}"


def compare(previous: Report | None, current: Report) -> tuple[list[Failure], list[Failure]]:
    """算出这一轮的回归与修好的。首轮没有可比对象时两个都是空。"""
    if previous is None:
        return [], []
    before = _failures(previous)
    after = _failures(current)
    regressions = [failure for key, failure in after.items() if key not in before]
    fixed = [failure for key, failure in before.items() if key not in after]
    return regressions, fixed


def previous_report(bus: ArtifactBus, stage: str, attempt: int) -> Report | None:
    """上一轮的门禁报告。

    靠 PRD 05 的不变式定位:某个阶段的第 k 个产物目录就是它的第 k 轮。上一轮崩在写报告
    之前是可能的,那种情况当成"没有上一轮"——比抛异常好,差集本来就是锦上添花。
    """
    if attempt < 2:
        return None
    slots = bus.by_stage(stage)
    if len(slots) < attempt - 1:
        return None
    path = slots[attempt - 2].path / GATE_REPORT
    if not path.is_file():
        return None
    try:
        payload: Report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload


def _failures(report: Report) -> dict[str, Failure]:
    found: dict[str, Failure] = {}
    for gate in report.get("gates") or []:
        gate_id = gate.get("id", "?")
        for failure in gate.get("failures") or []:
            found[failure_key(gate_id, failure)] = {"gate": gate_id, **failure}
    return found


__all__ = ["compare", "failure_key", "previous_report"]
