"""从迁移表画状态图。

用字典而非状态机框架的代价是没有自带的可视化。这个脚本很便宜,而且它保证**图永远和表
一致**——手画的图三个月后必然与代码对不上,而对不上的图比没有图更误导。
"""

from __future__ import annotations

from agentgenome.core.states import TaskState
from agentgenome.core.transitions import ANY_STATE, TRANSITIONS


def render_mermaid() -> str:
    """把迁移表渲染成 Mermaid 状态图。

    `*`(任何状态)渲染成一个汇聚节点而不是画满 N 条线:七条指向 CANCELLED 的线会把图
    糊掉,而它们表达的其实是同一件事。
    """
    lines = ["stateDiagram-v2", "    [*] --> CREATED"]
    for row in TRANSITIONS:
        source = row.from_state.value if row.from_state is not ANY_STATE else "任何非终态"
        guard = f" [{row.guard.__name__}]" if row.guard else ""
        lines.append(f"    {source} --> {row.to.value}: {row.event.value}{guard}")
    for state in sorted(TaskState.terminal()):
        lines.append(f"    {state.value} --> [*]")
    return "\n".join(lines) + "\n"


def edge_count() -> int:
    """图里有几条迁移边。测试拿它与表长度对比,防止两者漂移。"""
    return sum(1 for line in render_mermaid().splitlines() if "-->" in line and ": " in line)


__all__ = ["edge_count", "render_mermaid"]
