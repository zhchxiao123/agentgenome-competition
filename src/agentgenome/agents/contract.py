"""结果契约的校验。

员工的输出不是给人看的对话,而是产物文件——一切以文件为准。所以"这个 Job 成没成"
的判据是产物**存在且合法**,**不是退出码**。

这不是洁癖。真实观察到的形态:Agent 进了 plan mode,写了份计划等人确认,而 headless
场景没有人;它退出码 0、没有报错、也没有任何产物。只看退出码的话这是一次"成功"。

契约有两半:`result.json` 过 schema,以及(树产出类工序)`JobSpec.output_check`
对产物目录的裁决——后者是 PRD 34 的「验证产物,不验证自述」:staging 树的合法性
JSON Schema 说不了,由派发方给的确定性校验闭包说。两半都算 `CONTRACT` 失败,
都走同一条重试外环。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from agentgenome.agents.artifacts import RESULT_FILENAME

#: 产物目录 → `None`(通过)或拒绝原因。见 `JobSpec.output_check`。
OutputCheck = Callable[[Path], str | None]


@dataclass(frozen=True)
class ContractCheck:
    """一次契约校验的结果。"""

    ok: bool
    detail: str | None = None
    path: Path | None = None


def check_result_contract(
    output_dir: Path, schema: dict[str, Any], output_check: OutputCheck | None = None
) -> ContractCheck:
    """校验产物目录:`output_check`(有的话)加 `result.json` 过 schema。

    错误信息要说清**哪条约束没过**——它会被回注给下一次尝试,含糊的"格式不对"
    只会让第二次错得一模一样。

    **两半的失败一次报全。** 树坏了与小票缺失分两轮报的话,员工修完树才看得见
    小票那条,而契约重试只有一次——第二条错误到达时重试额度已经花完了。
    """
    problems: list[str] = []
    if output_check is not None:
        found = output_check(output_dir)
        if found:
            problems.append(found)

    target = output_dir / RESULT_FILENAME
    path = target if target.is_file() else None
    if path is None:
        problems.append(f"产物目录里没有 {RESULT_FILENAME}: {output_dir}")
    else:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{RESULT_FILENAME} 不是合法 JSON: {exc}")
        else:
            detail = validate_result_payload(payload, schema)
            if detail:
                problems.append(f"{RESULT_FILENAME} 不符合 schema: {detail}")

    if problems:
        return ContractCheck(False, "\n\n".join(problems), path)
    return ContractCheck(True, path=path)


def validate_result_payload(payload: Any, schema: dict[str, Any]) -> str | None:
    """校验内存中的小票；文件契约与运行时恢复共用这一份权威规则。"""
    if not schema:
        return None
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda error: list(error.absolute_path),
    )
    return "; ".join(_render(error) for error in errors) or None


def _render(error: Any) -> str:
    location = ".".join(str(part) for part in error.absolute_path) or "(顶层)"
    return f"{location}: {error.message}"


__all__ = [
    "RESULT_FILENAME",
    "ContractCheck",
    "OutputCheck",
    "check_result_contract",
    "validate_result_payload",
]
