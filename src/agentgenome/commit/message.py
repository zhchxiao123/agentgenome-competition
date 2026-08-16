"""提交信息与 PR 描述。两份文本,都是纯函数产出。

## 任务编号进正文

三个月后有人 `git blame` 到一行改动,唯一能顺藤摸瓜回到"当初为什么改它"的入口就是那个编号。
少了它,自动化产生的历史是一堆无法追溯的补丁。

## PR 描述里材料要齐

评审者不该为了判断"这个 PR 能不能合"去四处翻产物目录。**材料齐了人才会真的看**——描述里少
一样,评审就退化成"看起来还行,合了吧",而那等于没有评审。

## 摘要来源要诚实

开发员工没给出可用摘要时写明"(员工未提供摘要)",**不要用文件名列表冒充摘要**。那种东西读
起来像摘要但不含任何信息,而读的人要花几秒钟才能发现这一点——每次都花。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

#: 主题行上限。超了截断,正文不截断。
SUBJECT_LIMIT = 72

_DEFAULT_TYPE = "feat"


@dataclass(frozen=True)
class CommitInputs:
    """生成提交信息需要的材料。"""

    task_id: str
    title: str
    #: 开发员工 `result.json` 里的变更摘要。
    summary: str = ""
    #: 参与这个任务的数字员工 id。
    employees: tuple[str, ...] = ()
    #: Conventional Commits 的 type 与 scope。
    type_: str = _DEFAULT_TYPE
    scope: str = ""


@dataclass(frozen=True)
class PullRequestInputs:
    """生成 PR 描述需要的材料。"""

    task_id: str
    title: str
    #: 计划产物里的验收标准。
    acceptance: tuple[str, ...] = ()
    risk: str = "low"
    matched_rules: tuple[str, ...] = ()
    #: 报告名到相对路径。
    reports: dict[str, str] = field(default_factory=dict)
    summary: str = ""


def commit_message(inputs: CommitInputs) -> str:
    """按 Conventional Commits 生成提交信息。"""
    scope = f"({inputs.scope})" if inputs.scope else ""
    subject = f"{inputs.type_}{scope}: {inputs.title.strip() or inputs.task_id}"
    if len(subject) > SUBJECT_LIMIT:
        subject = subject[: SUBJECT_LIMIT - 1] + "…"

    lines = [subject, "", f"Task: {inputs.task_id}", ""]
    lines.append(inputs.summary.strip() or "(员工未提供摘要)")
    if inputs.employees:
        lines.append("")
        # 归属写清楚:提交历史里人和机器的贡献要分得开。
        lines += [
            f"Co-Authored-By: {item} <noreply@agentgenome.local>" for item in inputs.employees
        ]
    return "\n".join(lines) + "\n"


def pull_request_body(inputs: PullRequestInputs) -> str:
    """生成 PR 描述。评审者需要的材料一次给全。"""
    lines = [f"## 任务 {inputs.task_id}", "", inputs.summary.strip() or "(员工未提供摘要)", ""]

    lines += ["## 验收标准", ""]
    if inputs.acceptance:
        # 勾选框而不是普通列表:评审者可以边看边勾,勾不动的那条就是要打回的理由。
        lines += [f"- [ ] {item}" for item in inputs.acceptance]
    else:
        lines.append("(计划产物里没有验收标准)")
    lines.append("")

    lines += ["## 风险评级", "", f"**{inputs.risk}**"]
    if inputs.matched_rules:
        lines.append(f"命中规则:{', '.join(inputs.matched_rules)}")
    lines.append("")

    lines += ["## 报告", ""]
    if inputs.reports:
        lines += [f"- {name}: `{path}`" for name, path in sorted(inputs.reports.items())]
    else:
        lines.append("(这个任务没有产出报告)")
    return "\n".join(lines) + "\n"


def employees_of(events: Sequence[Any]) -> tuple[str, ...]:
    """从事件流里取参与过的数字员工,按首次出现排序。

    从事件流取而不是从配置取:配置说的是"谁可以参与",事件流说的是"谁真的干了活",而署名
    要署后者。
    """
    found: list[str] = []
    for event in events:
        actor = getattr(event, "actor", "")
        if actor.endswith("-employee") and actor not in found:
            found.append(actor)
    return tuple(found)


__all__ = [
    "SUBJECT_LIMIT",
    "CommitInputs",
    "PullRequestInputs",
    "commit_message",
    "employees_of",
    "pull_request_body",
]
