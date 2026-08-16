"""拆分提案:需求解析的第三种结局(PRD 48)。

`requirement-analysis` 的产物要么是一份 plan(今天的样子),要么是一份 `split`——
"这不是一个任务能交付的,拆成这几个子需求"。提案不是计划:任务不进 DEVELOPING,
停在 CREATED 挂一张 `split` 待办等人裁决。

这个模块只放**纯的**那部分:提案长什么样、哪些提案不合法、裁决文件读写在哪。
挂待办、发事件、推状态机在编排器;人怎么交裁决在 `todo.service`。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: 产物里装提案的那个键。有它就是提案,没有就是计划——互斥由 schema 的 oneOf 保证。
SPLIT_KEY = "split"

#: 裁决落盘的文件名,与提案(`result.json`)住同一个产物槽。**两个文件、两个作者**:
#: 提案是员工写的,裁决是人写的——覆盖任何一方都等于让记录被后来的人修饰。
VERDICT_FILENAME = "split-verdict.json"

#: 单次拆分的子需求数量界限(PRD D1)。上限防"拆出一百个碎片",下限防"拆了个寂寞"。
MIN_CHILDREN = 2
MAX_CHILDREN = 12

#: 人交回来的裁决的形状。**它不是产物契约**——产物(提案)已经在槽里了,这份只是
#: "通过没有 + 意见",与 assisted 确认节点的产出同族。`children` 可选:带了就是
#: 就地调整过的批次(PRD 48 issue 04),落树以它为准;不带就是原样确认。
VERDICT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["approved"],
    "properties": {
        "approved": {"type": "boolean"},
        "feedback": {"type": "string"},
        "children": {
            "type": "array",
            "minItems": MIN_CHILDREN,
            "maxItems": MAX_CHILDREN,
            "items": {
                "type": "object",
                "required": ["title", "text"],
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "text": {"type": "string", "minLength": 1},
                    "blocked_by": {"type": "array", "items": {"type": "integer"}},
                },
            },
        },
    },
}


def effective_children(
    proposal: dict[str, Any], verdict: dict[str, Any]
) -> list[dict[str, Any]]:
    """确认落树该落哪一批:人调整过就是调整稿,没调整就是提案原样。"""
    edited = verdict.get("children")
    if isinstance(edited, list) and edited:
        return edited
    found = proposal.get("children")
    return found if isinstance(found, list) else []


def proposal_of(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """这份产物是不是一个(读懂了需求的)拆分提案。是就返回提案,不是返回 `None`。

    `passed` 必须为真:解析自己都说没读懂时,附带的任何东西都不该被当成提案。
    """
    if not payload or not payload.get("passed", False):
        return None
    found = payload.get(SPLIT_KEY)
    return found if isinstance(found, dict) else None


def split_issues(proposal: dict[str, Any], max_children: int = MAX_CHILDREN) -> list[str]:
    """这份提案有哪些毛病。合法就是空列表。

    **在提案产出那一刻就拒,不等人来发现**(PRD 35 的教训:环要在载入那一刻拒绝,
    否则症状是静默死锁)。schema 管形状(数量硬界限、字段类型),这里管 schema 说不了
    的:引用批外、自引用、环,以及**配置收紧后的数量上限**——上限的文案只在这一处,
    编排器(提案)与待办服务(编辑稿)都调这里,不各写一份。
    """
    children = proposal.get("children")
    if not isinstance(children, list):
        return ["提案里没有 children 列表"]
    issues: list[str] = []
    count = len(children)
    ceiling = min(max_children, MAX_CHILDREN)
    if not MIN_CHILDREN <= count <= ceiling:
        issues.append(f"子需求数量 {count} 超出 {MIN_CHILDREN}..{ceiling}")
    edges: dict[int, list[int]] = {}
    for index, child in enumerate(children):
        blocked_by = child.get("blocked_by") or []
        for reference in blocked_by:
            if not isinstance(reference, int) or not 0 <= reference < count:
                issues.append(f"第 {index} 个子需求的 blocked_by 引用了批外的 {reference}")
            elif reference == index:
                issues.append(f"第 {index} 个子需求依赖它自己")
            else:
                edges.setdefault(index, []).append(reference)
    if not issues:
        cycle = _find_cycle(count, edges)
        if cycle:
            issues.append("依赖成环: " + " → ".join(str(item) for item in cycle))
    return issues


def _find_cycle(count: int, edges: dict[int, list[int]]) -> list[int]:
    """依赖图里的一个环。没有就是空列表。指名道姓:改提案的人要知道剪哪条边。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = [WHITE] * count
    trail: list[int] = []

    def visit(node: int) -> list[int]:
        color[node] = GRAY
        trail.append(node)
        for upstream in edges.get(node, []):
            if color[upstream] == GRAY:
                return [*trail[trail.index(upstream) :], upstream]
            if color[upstream] == WHITE:
                found = visit(upstream)
                if found:
                    return found
        trail.pop()
        color[node] = BLACK
        return []

    for start in range(count):
        if color[start] == WHITE:
            found = visit(start)
            if found:
                return found
    return []


def closing_snapshot(
    parent_text: str, entries: Sequence[tuple[str, str, str, str]]
) -> str:
    """收口尝试的需求快照:母需求原文 + 各子需求交付摘要 + 职责说明(PRD 48 D6)。

    职责里"把整体验收固化成回归资产"不是修辞:它保证收口永远有实质产出(验收资产
    本身就是 diff),不落进"零改动怎么提交"的死角;同时"集成起来整体成立吗"这个
    最贵的问题不被静默跳过。
    """
    lines = [
        parent_text.rstrip(),
        "",
        "---",
        "",
        "## 收口尝试(系统生成)",
        "",
        "以下子需求已全部交付并合入主干。这次尝试的职责:",
        "",
        "1. **验证母需求的整体验收**——逐条对照上面的原文,集成之后整体成立吗;",
        "2. 修掉验证中发现的残差;",
        "3. **把整体验收固化成回归资产**(如集成测试/基准脚本),让它从此可长期回归。",
        "",
        "### 已交付的子需求",
        "",
    ]
    for title, requirement_id, delivered_task_id, branch in entries:
        lines.append(
            f"- {title}({requirement_id},交付任务 {delivered_task_id},"
            f"分支 {branch or '—'})"
        )
    return "\n".join(lines) + "\n"


def resplit_snapshot(
    parent_text: str,
    kept: Sequence[tuple[str, str]],
    remainder: Sequence[tuple[str, str, str]],
) -> str:
    """重拆分提案任务的需求快照:母需求原文 + 不许动的清单 + 待重切的现文本(PRD 48 D7)。

    决策员工要照着**现场**重新切:已开工的子需求是既成事实,新批次必须与它们互补而不是
    重叠——不把它们摆出来的话,重切出来的批次会把已经在做的事再做一遍。
    """
    lines = [
        parent_text.rstrip(),
        "",
        "---",
        "",
        "## 重新拆分剩余(系统生成)",
        "",
        "这个需求此前拆分过,部分子需求已开工或已交付——**它们不许动,新批次要与它们互补**:",
        "",
    ]
    for title, requirement_id in kept:
        lines.append(f"- [保留] {title}({requirement_id})")
    lines += [
        "",
        "以下子需求还没开工,这次要**替代**它们重新拆(它们的现文本仅供参考):",
        "",
    ]
    for title, requirement_id, text in remainder:
        lines.append(f"### {title}({requirement_id},将被替代)")
        lines.append("")
        lines.append(text.rstrip())
        lines.append("")
    lines.append("产出一份新的 `split` 提案,覆盖上面'将被替代'的全部范围。")
    return "\n".join(lines) + "\n"


def split_todo_id(task_id: str, attempt: int) -> str:
    """拆分待办的标识。**从幂等键派生,不是随机的**——与 human 运行时同一条纪律:
    崩溃恢复会重新走到这里,随机 id 意味着人的列表里长出第二张一模一样的待办。"""
    raw = f"{task_id}|{SPLIT_KEY}|{attempt}"
    return "todo-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def read_verdict(root: Path, output_dir: str) -> dict[str, Any] | None:
    """读人留下的裁决。没有(或读不出)就是 `None`——"没裁决"与"拒绝"是两回事。"""
    target = Path(root) / output_dir / VERDICT_FILENAME
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def write_verdict(root: Path, output_dir: str, verdict: dict[str, Any]) -> Path:
    target = Path(root) / output_dir / VERDICT_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


__all__ = [
    "MAX_CHILDREN",
    "closing_snapshot",
    "effective_children",
    "MIN_CHILDREN",
    "SPLIT_KEY",
    "VERDICT_FILENAME",
    "VERDICT_SCHEMA",
    "proposal_of",
    "read_verdict",
    "resplit_snapshot",
    "split_issues",
    "split_todo_id",
    "write_verdict",
]
