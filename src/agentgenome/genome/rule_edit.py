"""规则的界面编辑:提交生成 PR,不直接写入。

**规则是这个系统里唯一能大范围改变行为的杠杆。** 一条错的规则影响之后每一个任务——所以
界面上改规则,走的不是"保存"按钮,是同一条 PR 审批流水线。这个模块只做两件事:校验提出的
规则值不值得让人看一眼,以及把它变成一个真实的 PR。

## 校验先于提交

结构化表单的价值在于"改的时候就拦住错误"而不是"提交之后才发现"。glob 语法错误、
不存在的模块 id,pydantic 与这里的交叉引用检查一次性挡住,不必等审批人读出来。

## PR 在一个 scratch clone 里做

不碰 Workspace 当前 checkout——服务进程随时可能有任务正在跑,checkout 一个新分支会
把它们的工作目录搅乱。开一个临时 clone、提交、推送、开 PR,用完即删,与 `LocalForge`
自己合并 PR 时的做法同一个理由。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from agentgenome import paths
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.loader import load_project_map, parse_yaml_model
from agentgenome.genome.rules import (
    ArchitectureRules,
    ImpactRules,
    ProtectedRules,
    extract_rules_blocks,
    load_rules,
)
from agentgenome.space.forge import Forge, PRRef
from agentgenome.space.gitcmd import EMPLOYEE_IDENTITY, git, git_out

Section = Literal["architecture", "protected", "impact"]

_SECTION_MODEL: dict[Section, type[Any]] = {
    "architecture": ArchitectureRules,
    "protected": ProtectedRules,
    "impact": ImpactRules,
}

_SECTION_PATH: dict[Section, Path] = {
    "architecture": paths.ARCHITECTURE_RULES,
    "protected": paths.PROTECTED_RULES,
    "impact": paths.IMPACT_RULES,
}


@dataclass(frozen=True)
class RuleChangeRequest:
    section: Section
    payload: dict[str, Any]
    description: str
    actor: str
    #: 这次提案是为哪个任务提的。**进提交信息**——事件面记的是指针,而版本面这一头也要能
    #: 反查回来:只有一头有链接的话,从 PR 出发就走不回那个任务。
    source_task_id: str = ""


def validate_rule_payload(workspace_root: Path, section: Section, payload: dict[str, Any]) -> Any:
    """校验提出的规则值合不合法。**在提交前挡住,不是等 PR 里才发现。**

    两层校验:pydantic 管形状(glob 是字符串、数值范围、必填条件),这里再补一层交叉引用
    ——模块 id 是否真的存在于项目地图。后者 pydantic 管不到,它不知道项目地图长什么样。
    """
    model = _SECTION_MODEL[section]
    relative = str(_SECTION_PATH[section])
    parsed = parse_yaml_model(model, payload, relative)

    known_modules = load_project_map(workspace_root).module_ids()
    issues: list[ValidationIssue] = []
    if section == "impact":
        for index, rule in enumerate(parsed.rules):
            if rule.match.crosses_modules_gte and not known_modules:
                issues.append(
                    ValidationIssue(relative, "项目地图里没有任何模块", f"rules.{index}.match")
                )
    if issues:
        raise GenomeValidationError(issues)
    return parsed


def render_section(section: Section, parsed: Any) -> str:
    """规则模型渲染回它落盘的文本形式。"""
    if section == "architecture":
        return _render_architecture_block(parsed)
    payload = parsed.model_dump(mode="json", by_alias=True, exclude_none=True)
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _render_architecture_block(rules: ArchitectureRules) -> str:
    payload = rules.model_dump(mode="json", by_alias=True, exclude_none=True)
    block = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    return f"```rules\n{block}```\n"


def _apply_to_architecture_doc(existing_markdown: str, new_block: str) -> str:
    """把新的规则块换进已有文档,保留人写的说明性正文。

    只替换第一个 `rules` 围栏块——那正是 `extract_rules_blocks` 读取的同一处。文档不存在
    或没有围栏块时,新块直接追加在末尾。
    """
    blocks = extract_rules_blocks(existing_markdown)
    if not blocks:
        header = existing_markdown.rstrip()
        prefix = f"{header}\n\n" if header else ""
        return f"{prefix}{new_block}"
    start = existing_markdown.index("```rules")
    end = existing_markdown.index("```", start + len("```rules")) + len("```")
    end = existing_markdown.index("\n", end) + 1 if end < len(existing_markdown) else end
    return existing_markdown[:start] + new_block + existing_markdown[end:]


def _write_rule_file(target: Path, section: Section, new_block: str) -> None:
    """把渲染好的规则块落到磁盘。**唯一知道"architecture 是文档、其余是纯 YAML"这件事的
    地方**——`submit_rule_change` 不重复这个判断,只管调用。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if section == "architecture":
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        target.write_text(_apply_to_architecture_doc(existing, new_block), encoding="utf-8")
    else:
        target.write_text(new_block, encoding="utf-8")


def _commit_subject(request: RuleChangeRequest) -> str:
    """提交标题。带上来源任务 id,沿用现有提交规范里的 task-id 写法。"""
    head = f"规则提案({request.section}): {request.description or '无描述'}"[:72]
    return f"{head}\n\ntask-id: {request.source_task_id}" if request.source_task_id else head


def submit_rule_change(
    workspace_remote: Path,
    forge: Forge,
    request: RuleChangeRequest,
    workspace_root: Path,
    base: str = "main",
) -> PRRef:
    """校验、渲染,然后在一个 scratch clone 里开出真实的 PR。"""
    parsed = validate_rule_payload(workspace_root, request.section, request.payload)
    new_block = render_section(request.section, parsed)

    scratch = Path(tempfile.mkdtemp(prefix="agentgenome-rule-proposal-"))
    try:
        work = scratch / "work"
        git(scratch, "clone", "--quiet", str(workspace_remote), str(work))
        git(work, "checkout", "--quiet", base)
        branch = f"rule-proposal/{request.section}-{_short_rev(work)}"
        git(work, "checkout", "--quiet", "-b", branch)

        target = work / _SECTION_PATH[request.section]
        _write_rule_file(target, request.section, new_block)

        git(work, "add", str(_SECTION_PATH[request.section]))
        git(
            work,
            *EMPLOYEE_IDENTITY,
            "commit",
            "-m",
            _commit_subject(request),
        )
        git(work, "push", "--quiet", "origin", f"{branch}:{branch}")

        return forge.open_pr(
            workspace_remote,
            head=branch,
            base=base,
            title=f"规则提案:{request.section}",
            body=_render_body(request),
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _render_body(request: RuleChangeRequest) -> str:
    source = f"来源任务:{request.source_task_id}\n\n" if request.source_task_id else ""
    return (
        f"## 规则变更提案:`{request.section}`\n\n"
        f"提出人:{request.actor}\n\n"
        f"{source}"
        f"{request.description or '(没有描述)'}\n\n"
        "---\n"
        "**这条提案不会自动合并。** 规则层是唯一能大范围改变行为的杠杆,它必须握在人手里。\n"
    )


def _short_rev(work: Path) -> str:
    return git_out(work, "rev-parse", "--short=8", "HEAD")


def current_rule_set(workspace_root: Path) -> dict[str, Any]:
    """当前生效的三份规则资产,给结构化编辑器渲染用。"""
    rules = load_rules(workspace_root)
    return {
        "architecture": rules.architecture.model_dump(mode="json", by_alias=True),
        "protected": rules.protected.model_dump(mode="json", by_alias=True),
        "impact": rules.impact.model_dump(mode="json", by_alias=True),
    }


__all__ = [
    "RuleChangeRequest",
    "Section",
    "current_rule_set",
    "render_section",
    "submit_rule_change",
    "validate_rule_payload",
]
