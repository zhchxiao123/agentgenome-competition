"""YAML 文本 → pydantic 模型,附带可读的错误。

这两个函数与"基因组里有哪些资产"完全无关——它们只知道"读一个 YAML、按一个模型校验、
把 pydantic 的报错翻译成人能改的话"。

**它们从加载器里搬出来,是为了断掉一个环。** 加载器要装配知识树,知识树要读 YAML,于是
两边互相 import,只能靠函数内延迟导入绕。绕得开不等于没有环:环还在,只是被藏进了函数体,
下一个人加一处顶层导入就会在启动时炸。真正的问题是这两个原语放错了模块。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from agentgenome.genome.errors import GenomeValidationError, ValidationIssue

ModelT = TypeVar("ModelT", bound=BaseModel)

#: `---` 包住的 front matter 加正文。经验卡片与功能卡片是同一种形状,各写一份正则的话,
#: 两边对"什么算合法的 front matter"迟早给出不同答案。
_FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def split_front_matter(text: str, relative: str) -> tuple[dict[str, Any], str]:
    """把 front matter 与正文分开。"""
    match = _FRONT_MATTER.match(text)
    if match is None:
        raise GenomeValidationError([ValidationIssue(relative, "缺少 YAML front matter")])
    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as error:
        raise GenomeValidationError(
            [ValidationIssue(relative, f"front matter 解析失败: {error}")]
        ) from error
    if not isinstance(front, dict):
        raise GenomeValidationError([ValidationIssue(relative, "front matter 必须是一个映射")])
    return front, match.group(2).strip()


def read_yaml(path: Path, relative: str) -> dict[str, Any]:
    if not path.is_file():
        raise GenomeValidationError([ValidationIssue(relative, "文件不存在")])
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise GenomeValidationError([ValidationIssue(relative, f"YAML 解析失败: {exc}")]) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise GenomeValidationError([ValidationIssue(relative, "顶层必须是一个映射")])
    return loaded


def issues_from_pydantic(error: ValidationError, relative: str) -> list[ValidationIssue]:
    issues = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or None
        message = detail["msg"]
        # pydantic 的默认文案不含"用户实际写了什么",而那恰恰是使用者要看的东西。
        if detail["type"] == "extra_forbidden" and location:
            message = f"{message}: {location}"
        elif isinstance(detail.get("input"), str | int | float | bool):
            message = f"{message}(当前值: {detail['input']!r})"
        issues.append(ValidationIssue(relative, message, location))
    return issues


def parse_yaml_model(model: type[ModelT], payload: dict[str, Any], relative: str) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise GenomeValidationError(issues_from_pydantic(exc, relative)) from exc


__all__ = ["issues_from_pydantic", "parse_yaml_model", "read_yaml", "split_front_matter"]
