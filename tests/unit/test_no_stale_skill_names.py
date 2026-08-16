"""守卫:改名之后 "skill" 不该再出现在代码里,除了那几个必须保留的位置。

## 为什么这条测试比改名本身更重要

`skill` 曾经在这个仓里有 800 多处。纯 sed 迁移最容易漏掉的是**字符串字面量**——它们不参与
类型检查,漏改了照样导入成功、测试照样绿,直到运行时某个路径拼不出来才炸。而那时候没人会
想到是三个月前那次改名漏了一行。

所以判据不能是"测试过了",得是"搜不到了"。

## 必须保留 "skill" 的位置

- `.claude/skills/` —— 手艺物化的目标路径,Claude Code 的规范,改了就挂不上;
- `SKILL.md` —— 同上,手艺文件名;
- 「手艺(Craft Skill)」—— 术语本身;
- 仓库根的 `.claude/`、`.scratch/`(历史记录)、`.venv/`、`node_modules/` —— 不在扫描范围内。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

#: 扫描这些目录。`.scratch/` 是历史记录不扫,`.claude/` 是本仓自己的 skills 不扫。
SCANNED = ("src", "tests", "web/src")

SUFFIXES = {".py", ".ts", ".tsx", ".yaml", ".yml", ".json", ".md"}

#: 行内含这些片段时豁免——它们是必须保留 "skill" 的那几处。
ALLOWED_MARKERS = (
    ".claude/skills",
    # 物化目标是用 `Path` 拼的,拼出来才是 `.claude/skills`——源码里两段是分开的。
    'Path(".claude") / "skills"',
    "SKILL.md",
    "Craft Skill",
    "手艺(Craft",
    "coding-agent skill",
)

_SKILL = re.compile(r"skill", re.IGNORECASE)
#: 中文那一半。**改名只改标识符是不够的**——`CONTEXT.md` 把「技能」列为工序与手艺的
#: `_Avoid_`,而散文里的旧词不会被任何编译器发现,只会在读文档的人脑子里留下两个概念。
_CHINESE = re.compile(r"技能")


#: 守卫自己当然满篇 "skill",扫自己会永远红。
SELF = Path(__file__).resolve()


def _scan() -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for base in SCANNED:
        for path in (ROOT / base).rglob("*"):
            if not path.is_file() or path.suffix not in SUFFIXES:
                continue
            if path.resolve() == SELF:
                continue
            if any(part in {"__pycache__", "node_modules", "dist"} for part in path.parts):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not _SKILL.search(line) and not _CHINESE.search(line):
                    continue
                if any(marker in line for marker in ALLOWED_MARKERS):
                    continue
                hits.append((path.relative_to(ROOT), number, line.strip()))
    return hits


def test_no_stale_skill_identifiers_survive_the_rename() -> None:
    """`skill` 与「技能」都不再出现在代码里,豁免清单之外一处都不行。"""
    hits = _scan()
    rendered = "\n".join(f"  {path}:{number}  {line[:100]}" for path, number, line in hits)
    assert not hits, f"改名漏了 {len(hits)} 处:\n{rendered}"


@pytest.mark.parametrize(
    "path",
    [
        "src/agentgenome/genome/procedures.py",
        "src/agentgenome/gates/procedure_entry.py",
        "src/agentgenome/itest/procedure_entry.py",
    ],
)
def test_the_renamed_modules_exist(path: str) -> None:
    assert (ROOT / path).is_file(), f"{path} 不存在 —— 改名没做完"


@pytest.mark.parametrize(
    "path",
    [
        "src/agentgenome/genome/skills.py",
        "src/agentgenome/gates/skill_entry.py",
        "src/agentgenome/itest/skill_entry.py",
        "tests/fixtures/skills",
    ],
)
def test_the_old_modules_are_gone(path: str) -> None:
    """不留别名。两个名字长期并存会让新人不知道该用哪个,而这正是这次改名要消灭的问题。"""
    assert not (ROOT / path).exists(), f"{path} 还在 —— 改名留了尾巴"
