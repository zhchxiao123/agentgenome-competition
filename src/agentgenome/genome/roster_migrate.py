"""把一个存量 Workspace 的花名册迁到当前形状。

## 为什么需要这条命令

脚手架是幂等的,而且**不覆盖已存在的文件**——那是刻意的:使用者改过的员工定义不该被一次
重新初始化抹掉,那正是把员工做成文件的全部理由。代价是花名册扩编时存量工作区会卡在半路:
新员工被补进来了,旧主人的工序白名单却没人去改,于是归属排他校验当场拒绝加载。

这条命令是那个缺口的唯一补法。所以它自己必须极其克制:**只改白名单那一个键**。碰了限额、
提示词指针或者别的什么,人下次就不敢跑它了——而一条没人敢跑的迁移命令等于不存在。

## 为什么按文本改而不是读进模型再写回去

YAML 往返会把注释、键序、引号风格全部抹平。员工定义是**给人读、走 git 评审**的资产,
一次迁移把整个文件重排的话,评审那份 diff 的人没法一眼确认"只动了白名单"——而那正是他
要确认的唯一一件事。
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agentgenome import paths
from agentgenome.genome.roster import PLAN_PROCEDURES, default_employee_ids, scaffold_roster
from agentgenome.genome.roster_legacy import (
    REQUIREMENT_ANALYSIS_V1_MANIFEST,
    REQUIREMENT_ANALYSIS_V1_PROMPT,
    REQUIREMENT_ANALYSIS_V1_SCHEMA,
)

#: 白名单被移交给谁。**每一项都是"从谁手里拿走什么"**,不是"谁现在有什么"——后者是脚手架
#: 里那份定义的事,两处都写的话迟早分叉。
HANDOVERS: tuple[tuple[str, tuple[str, ...]], ...] = (("arch-employee", PLAN_PROCEDURES),)

_KEY = re.compile(r"^procedures:\s*(.*)$")


def drop_procedures(text: str, remove: tuple[str, ...]) -> str | None:
    """从一份员工定义里摘掉这几道工序。**没有改动时返回 `None`。**

    区分"没改动"与"改成了一样的内容"是幂等的全部:调用方据此决定要不要写盘、要不要在
    diff 里提这个文件。一律返回新文本的话,第二次跑会把每个文件都报成"改过了"。

    流式(`procedures: [a, b]`)与块式(`- a` 一行一条)都认。只认一种的话,改过格式的
    工作区会静默地迁移不动——而"静默地没做"是这条命令最坏的失败形态。
    """
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if _KEY.match(line)), None)
    if start is None:
        return None

    match = _KEY.match(lines[start])
    assert match is not None
    inline = match.group(1).strip()
    end = start + 1
    if inline:
        current = _parse_flow(inline)
    else:
        # 块式:紧随其后的 `- x` 行(允许中间夹注释行)都属于这个键。
        current = []
        while end < len(lines):
            stripped = lines[end].strip()
            if stripped.startswith("-"):
                current.append(_item(stripped[1:]))
            elif not stripped.startswith("#"):
                break
            end += 1

    kept = [item for item in current if item not in remove]
    if kept == current:
        return None
    replacement = f"procedures: [{', '.join(kept)}]\n"
    return "".join(lines[:start]) + replacement + "".join(lines[end:])


def _parse_flow(inline: str) -> list[str]:
    loaded = yaml.safe_load(inline)
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def _item(raw: str) -> str:
    """摘掉行尾注释与引号。`- requirement-analysis   # 我加的注释` 也要认得出来。"""
    return raw.split("#")[0].strip().strip("'\"")


@dataclass
class Migration:
    """这次迁移会做什么。**先算完整,再决定要不要动手。**"""

    #: 补进来的文件(相对 employees/)。
    added: list[str] = field(default_factory=list)
    #: 白名单被改写的文件。
    rewritten: list[str] = field(default_factory=list)
    #: 给人看的 unified diff。**这是这条命令唯一的说服力**——看不到它的话,
    #: 人只能在"信它"和"不跑它"之间二选一。
    diff: str = ""
    #: 待写的内容,`run_migration` 用。
    writes: dict[Path, str] = field(default_factory=dict)
    #: 想刷新但**使用者改过**、迁移不敢碰的文件(相对 Workspace 根)。这些要人手动合并;
    #: 静默跳过的话,"迁移跑过了"会被读成"schema 升上去了",而它没有。
    kept: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.added and not self.rewritten


def plan_migration(root: Path) -> Migration:
    """算出这次迁移会改什么。**不碰磁盘。**"""
    root = Path(root)
    employees = root / paths.EMPLOYEES
    migration = Migration()

    for employee_id in default_employee_ids():
        if not (employees / f"{employee_id}.yaml").is_file():
            migration.added.append(f"{employee_id}.yaml")

    diffs: list[str] = []
    for employee_id, moved in HANDOVERS:
        path = employees / f"{employee_id}.yaml"
        if not path.is_file():
            continue
        before = path.read_text(encoding="utf-8")
        after = drop_procedures(before, moved)
        if after is None:
            continue
        migration.rewritten.append(path.name)
        migration.writes[path] = after
        diffs.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path.name}",
                tofile=f"b/{path.name}",
            )
        )
    _plan_procedure_refresh(root, migration, diffs)
    migration.diff = "".join(diffs)
    return migration


#: 工序资产的刷新清单:`(相对工序目录的路径, 1.0.0 原文, 当前版内容工厂)`。
#: 只有 `requirement-analysis`——它是 PRD 48 动过的唯一一道。
def _requirement_analysis_refreshes() -> tuple[tuple[str, str, str], ...]:
    from agentgenome.genome.roster import requirement_analysis_assets

    manifest, schema_text, prompt = requirement_analysis_assets()
    return (
        ("procedure.yaml", REQUIREMENT_ANALYSIS_V1_MANIFEST, manifest),
        (
            "schemas/out.json",
            json.dumps(REQUIREMENT_ANALYSIS_V1_SCHEMA, ensure_ascii=False, indent=2) + "\n",
            schema_text,
        ),
        ("prompt.md", REQUIREMENT_ANALYSIS_V1_PROMPT, prompt),
    )


def _plan_procedure_refresh(root: Path, migration: Migration, diffs: list[str]) -> None:
    """把没动过的 `requirement-analysis` 资产刷新到当前版(PRD 48 R2)。

    三种情形,三种处理:与 1.0.0 逐字相同 → 刷新;已经是当前版 → 什么都不做;
    都不是(使用者改过)→ 记进 `kept`,只报告不覆盖——迁移敢动的只有它确认认识的东西。
    """
    directory = root / paths.PROCEDURES / "requirement-analysis"
    for relative, legacy, current in _requirement_analysis_refreshes():
        path = directory / relative
        if not path.is_file():
            continue  # 缺文件归脚手架补,不归刷新管。
        on_disk = path.read_text(encoding="utf-8")
        if on_disk == current:
            continue
        if on_disk != legacy:
            migration.kept.append(str(path.relative_to(root)))
            continue
        migration.rewritten.append(str(path.relative_to(root)))
        migration.writes[path] = current
        diffs.extend(
            difflib.unified_diff(
                on_disk.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )


def run_migration(root: Path) -> Migration:
    """补齐缺的员工定义,再收敛白名单。**顺序是有意的**:先补人再交接,反过来的话中间
    那一刻会出现"工序没有任何主人",而崩在这中间的工作区连需求解析都派不出去。
    """
    root = Path(root)
    migration = plan_migration(root)
    scaffold_roster(root)
    for path, text in migration.writes.items():
        path.write_text(text, encoding="utf-8")
    return migration


__all__ = [
    "HANDOVERS",
    "Migration",
    "drop_procedures",
    "plan_migration",
    "run_migration",
]
