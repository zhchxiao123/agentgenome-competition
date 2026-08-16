"""门禁输出的解析。

失败之后的反馈质量决定了修复效率。把两千行终端日志丢回去,员工会从头读到尾然后可能抓错
重点。真正有用的是**结构化的断言差异**:哪个测试挂了、期望什么、实际什么、在哪个文件第几行。
日志是补充材料,不是主菜。

## 解析是尽力而为的增强,不是必需路径

解析不出来时 `failures[]` 为空,靠 `log_tail` 兜底。**解析器抛异常会让门禁误判**成"这次执行
本身失败了"——那是一个由解析 bug 引起的假红灯,比没有解析糟得多。所以坏输入一律返回空。

唯一会抛的是"这个解析器不存在":那是配置笔误,而配置笔误本该在 `gates.config` 的校验期就被
拦住。走到这里说明有人绕过了校验,静默返回空只会把那个笔误藏起来。

## 为什么优先 JUnit XML

它是跨语言跨框架的事实标准,一个解析器覆盖大多数栈。产物位置从项目地图的 `junit_xml_path`
读——往用户写的任意 `test_cmd` 上拼 `--junitxml` 是字符串拼接,对非 pytest 的栈立刻就碎。
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

#: `tests/x.py:12: AssertionError` —— pytest 把位置写在 failure 正文里,不是属性上。
_TRACE_PATTERN = re.compile(r"^([\w./\\-]+\.\w+):(\d+):", re.M)

#: `file:line: message` 这种几乎所有 lint 工具都在用的格式。
_LINE_PATTERN = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+)(?::\d+)?:\s*(?P<message>.+)$")


@dataclass(frozen=True)
class ParseContext:
    """解析一关的输出需要知道的东西。"""

    gate_id: str
    module_dir: Path
    junit_xml_path: str | None
    output: str


Failure = dict[str, Any]
Parser = Callable[[ParseContext], list[Failure]]


def _junit(context: ParseContext) -> list[Failure]:
    """从 JUnit XML 里挑出失败与错误。

    跳过不算失败:混进去的话员工会去修一个根本没跑的用例。
    """
    if not context.junit_xml_path:
        return []
    path = context.module_dir / context.junit_xml_path
    if not path.is_file():
        return []

    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError:
        # 坏 XML 不是门禁的失败。返回空,让 log_tail 兜底。
        return []

    failures: list[Failure] = []
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None:
            continue
        body = (problem.text or "").strip()
        file, line = _position(case, body)
        failures.append(
            {
                "test": _case_name(case),
                "message": problem.get("message") or body[:500],
                "file": file,
                "line": line,
            }
        )
    return failures


def _position(case: Any, body: str) -> tuple[str | None, int | None]:
    """出问题的位置。

    属性优先,正文兜底:**pytest 的 JUnit XML 根本不写 `file`/`line` 属性**,位置在
    `<failure>` 正文的 `tests/x.py:12: AssertionError` 那一行里。只读属性的话,最常见的
    栈反而拿不到位置——而"直接跳到出问题的地方"正是结构化条目的全部意义。
    """
    if case.get("file"):
        return case.get("file"), _int(case.get("line"))
    found = _TRACE_PATTERN.findall(body)
    if not found:
        return None, None
    # 取最后一条:栈里越靠后越接近真正出问题的那一行。
    file, line = found[-1]
    return file, int(line)


def _lines(context: ParseContext) -> list[Failure]:
    """`file:line: message` 的通用降级。读不懂的行直接跳过,不猜。"""
    failures: list[Failure] = []
    for line in context.output.splitlines():
        match = _LINE_PATTERN.match(line.strip())
        if match is None:
            continue
        failures.append(
            {
                "test": match.group("file"),
                "message": match.group("message"),
                "file": match.group("file"),
                "line": int(match.group("line")),
            }
        )
    return failures


def _none(context: ParseContext) -> list[Failure]:
    """显式声明"这一关的输出不解析"。与"忘了写"分得开。"""
    return []


#: 全部已知解析器。配置里的 `parser` 只能取这里面的名字——跑到一半才发现解析器不存在的话,
#: 这一关的失败会被当成解析器的问题。
KNOWN_PARSERS: dict[str, Parser] = {"junit": _junit, "lines": _lines, "none": _none}

#: 不声明 `parser` 时按关的 id 选,**按顺序试**。常见配置里没人会去写这一行。
#:
#: 单测优先 JUnit;没有 XML 时降级为正则提取,而不是直接放弃——一堵日志墙比几条
#: `file:line: message` 难用得多,哪怕后者不完整。
_BY_GATE_ID: dict[str, tuple[str, ...]] = {
    "unit": ("junit", "lines"),
    "test": ("junit", "lines"),
    "lint": ("lines",),
    "build": ("lines",),
    "secrets": ("lines",),
}


def parse_failures(parser: str | None, context: ParseContext) -> list[Failure]:
    """解析一关的输出。

    **任何坏输入都返回空,不抛。** 唯一的例外是"这个解析器不存在"——那是配置笔误,而
    配置笔误本该在 `Gate` 的校验期就被拦住(见 `gates.config`),走到这里说明有人绕过了
    校验,静默返回空会把那个笔误藏起来。
    """
    names = (parser,) if parser else _BY_GATE_ID.get(context.gate_id, ("none",))
    for name in names:
        if name not in KNOWN_PARSERS:
            raise ValueError(f"没有这个解析器: {name}(已知: {', '.join(sorted(KNOWN_PARSERS))})")
        found: list[Failure] = []
        with contextlib.suppress(Exception):
            # 解析器自己炸了同样不该让门禁误判。
            found = KNOWN_PARSERS[name](context)
        if found:
            return found
    return []


def _case_name(case: Any) -> str:
    classname = case.get("classname") or ""
    name = case.get("name") or "(未命名用例)"
    return f"{classname}::{name}" if classname else name


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


__all__ = ["KNOWN_PARSERS", "Failure", "ParseContext", "Parser", "parse_failures"]
