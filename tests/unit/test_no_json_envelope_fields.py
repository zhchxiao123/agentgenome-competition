"""守卫:「文件进信封」的字段不再出现在代码与契约里(PRD 34)。

`doc_markdown` / `card_markdown` 曾经是知识工序契约的核心:整篇 Markdown 转义后塞进
JSON 字符串,编排器再解包写成真实文件。PRD 34 把产出通道换成了 staging 树,这两个字段
理应绝迹——但字符串字面量不参与类型检查,残留一处照样导入成功、测试照样绿,直到某个
调用方按旧字段取值拿到 None 才炸,而那时没人会想到是契约改造漏了一行。

所以判据不能是"测试过了",得是"搜不到了"。`.scratch/`(PRD 历史)与 `docs/`(ADR 里
讲这段历史)不在扫描范围内——历史记录里的旧词是记录,不是残留。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

SCANNED = ("src", "tests", "genome", "web/src")

SUFFIXES = {".py", ".ts", ".tsx", ".yaml", ".yml", ".json", ".md"}

_ENVELOPE = re.compile(r"doc_markdown|card_markdown")

#: 守卫自己当然满篇是这两个词,扫自己会永远红。
SELF = Path(__file__).resolve()


def test_no_file_in_envelope_fields_survive_the_contract_change() -> None:
    hits: list[tuple[Path, int, str]] = []
    for base in SCANNED:
        target = ROOT / base
        if not target.is_dir():
            continue
        for path in target.rglob("*"):
            if not path.is_file() or path.suffix not in SUFFIXES:
                continue
            if path.resolve() == SELF:
                continue
            if any(part in {"__pycache__", "node_modules", "dist"} for part in path.parts):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if _ENVELOPE.search(line):
                    hits.append((path.relative_to(ROOT), number, line.strip()))
    rendered = "\n".join(f"  {path}:{number}  {line[:100]}" for path, number, line in hits)
    assert not hits, f"「文件进信封」的字段还剩 {len(hits)} 处:\n{rendered}"
