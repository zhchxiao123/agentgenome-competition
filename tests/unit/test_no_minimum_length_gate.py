"""守卫:知识卡片没有"最少行数"类门禁(PRD 41)。

薄卡的修法是内容纪律与深化工序,不是长度下限——硬凑长度逼出来的是编造,而编造的认知
会被当成事实误导之后每一个任务。这条约束是 PRD 41 的 Non-goal 与 Invariant 2:它很容易
在某次"提高卡片质量"的顺手改动里被违反,而违反的那一刻所有测试照样绿。

判据与信封守卫同一套:不能是"测试过了",得是"搜不到了"。只扫源码——PRD 与 ADR 里
讨论这条规则本身的地方是记录,不是残留。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

_MINIMUM = re.compile(r"min_lines|最少行数|行数下限|至少\s*\d+\s*行")

SELF = Path(__file__).resolve()


def test_no_minimum_line_count_gate_exists_for_cards() -> None:
    hits: list[tuple[Path, int, str]] = []
    for path in (ROOT / "src").rglob("*.py"):
        if not path.is_file() or path.resolve() == SELF:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _MINIMUM.search(line):
                hits.append((path.relative_to(ROOT), number, line.strip()))
    rendered = "\n".join(f"  {path}:{number}  {line[:100]}" for path, number, line in hits)
    assert not hits, f"出现了'最少行数'类门禁的苗头 {len(hits)} 处:\n{rendered}"
