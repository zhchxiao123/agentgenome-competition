"""门禁输出的解析:结构化条目在前,日志在后。

把两千行终端日志丢回去,员工会从头读到尾然后可能抓错重点。真正有用的是断言差异:
哪个测试挂了、期望什么、实际什么、在哪个文件第几行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome.gates.parsers import KNOWN_PARSERS, ParseContext, parse_failures

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="4" failures="1" errors="1" skipped="1">
    <testcase classname="tests.test_order" name="test_ok" time="0.01"/>
    <testcase classname="tests.test_order" name="test_reserve" time="0.02"
              file="tests/test_order.py" line="12">
      <failure message="AssertionError: 期望 3 实际 0">断言细节在这里</failure>
    </testcase>
    <testcase classname="tests.test_order" name="test_boom" time="0.01"
              file="tests/test_order.py" line="30">
      <error message="ZeroDivisionError: division by zero">栈在这里</error>
    </testcase>
    <testcase classname="tests.test_order" name="test_skipped">
      <skipped message="缺依赖"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def _context(tmp_path: Path, junit: str | None = JUNIT, output: str = "") -> ParseContext:
    if junit is not None:
        target = tmp_path / "reports" / "junit.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(junit, encoding="utf-8")
    return ParseContext(
        gate_id="unit",
        module_dir=tmp_path,
        junit_xml_path="reports/junit.xml",
        output=output,
    )


# --- JUnit --------------------------------------------------------------


def test_failures_and_errors_are_extracted(tmp_path: Path) -> None:
    failures = parse_failures("junit", _context(tmp_path))

    assert [item["test"] for item in failures] == [
        "tests.test_order::test_reserve",
        "tests.test_order::test_boom",
    ]


def test_passing_and_skipped_cases_do_not_show_up(tmp_path: Path) -> None:
    """跳过不是失败。混进去的话员工会去修一个根本没跑的用例。"""
    tests = [item["test"] for item in parse_failures("junit", _context(tmp_path))]

    assert not any("test_ok" in name or "test_skipped" in name for name in tests)


def test_each_failure_carries_where_to_look(tmp_path: Path) -> None:
    """我要能直接跳到出问题的地方。"""
    failure = parse_failures("junit", _context(tmp_path))[0]

    assert failure["message"].startswith("AssertionError")
    assert failure["file"] == "tests/test_order.py"
    assert failure["line"] == 12


def test_a_case_without_position_leaves_the_fields_empty(tmp_path: Path) -> None:
    """拿不到就是拿不到。编一个位置出来,员工会跑去改一个无关的文件。"""
    xml = (
        '<testsuites><testsuite name="s">'
        '<testcase classname="c" name="t"><failure message="挂了"/></testcase>'
        "</testsuite></testsuites>"
    )

    failure = parse_failures("junit", _context(tmp_path, junit=xml))[0]

    assert failure["file"] is None
    assert failure["line"] is None


# --- 降级 ---------------------------------------------------------------


def test_a_broken_xml_returns_nothing_instead_of_exploding(tmp_path: Path) -> None:
    """解析器出 bug 不能导致门禁误判。**解析是尽力而为的增强,不是必需路径。**"""
    assert parse_failures("junit", _context(tmp_path, junit="<这不是 XML")) == []


def test_a_missing_xml_returns_nothing(tmp_path: Path) -> None:
    assert parse_failures("junit", _context(tmp_path, junit=None)) == []


def test_no_declared_junit_path_returns_nothing(tmp_path: Path) -> None:
    context = ParseContext(gate_id="unit", module_dir=tmp_path, junit_xml_path=None, output="")

    assert parse_failures("junit", context) == []


def test_an_empty_suite_returns_nothing(tmp_path: Path) -> None:
    xml = '<testsuites><testsuite name="s" tests="0"/></testsuites>'

    assert parse_failures("junit", _context(tmp_path, junit=xml)) == []


# --- 按关的 id 选默认解析器 --------------------------------------------------


def test_the_unit_gate_parses_junit_by_default(tmp_path: Path) -> None:
    """不声明 parser 时按 id 选。常见配置里没人会去写这一行。"""
    assert parse_failures(None, _context(tmp_path))


def test_an_unknown_gate_id_parses_nothing(tmp_path: Path) -> None:
    context = ParseContext(
        gate_id="whatever", module_dir=tmp_path, junit_xml_path="reports/junit.xml", output=""
    )

    assert parse_failures(None, context) == []


def test_the_none_parser_is_explicit(tmp_path: Path) -> None:
    """显式写 `parser: none` 与"忘了写"要分得开。"""
    assert "none" in KNOWN_PARSERS
    assert parse_failures("none", _context(tmp_path)) == []


# --- 正则降级 ----------------------------------------------------------------


def test_the_generic_parser_pulls_file_and_line_out_of_a_log(tmp_path: Path) -> None:
    """没有 JUnit 时也别只丢一堵日志墙回去。"""
    output = "src/order/app.py:42:1: F401 imported but unused\n"
    context = ParseContext(gate_id="lint", module_dir=tmp_path, junit_xml_path=None, output=output)

    failure = parse_failures("lines", context)[0]

    assert failure["file"] == "src/order/app.py"
    assert failure["line"] == 42
    assert "F401" in failure["message"]


def test_the_generic_parser_ignores_lines_it_cannot_read(tmp_path: Path) -> None:
    context = ParseContext(
        gate_id="lint", module_dir=tmp_path, junit_xml_path=None, output="随便说点什么\n"
    )

    assert parse_failures("lines", context) == []


def test_an_unknown_parser_name_is_refused(tmp_path: Path) -> None:
    """跑到一半才发现解析器不存在的话,这一关的失败会被当成解析器的问题。"""
    with pytest.raises(ValueError, match="没有这个解析器"):
        parse_failures("nope", _context(tmp_path))


# --- 位置从正文里挖 ----------------------------------------------------------
#
# pytest 的 JUnit XML **不写 `file`/`line` 属性**,位置在 failure 正文的
# `tests/x.py:12: AssertionError` 那一行里。只读属性的话最常见的栈反而拿不到位置。


PYTEST_REAL = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" tests="1" failures="1">
<testcase classname="tests.test_reserve" name="test_reserve" time="0.000">
<failure message="AssertionError: 预占没实现">def test_reserve():
&gt;       assert 0 == 3
E       AssertionError: 预占没实现

tests/test_reserve.py:2: AssertionError</failure></testcase>
</testsuite></testsuites>
"""


def test_the_position_is_recovered_from_the_traceback(tmp_path: Path) -> None:
    failure = parse_failures("junit", _context(tmp_path, junit=PYTEST_REAL))[0]

    assert failure["file"] == "tests/test_reserve.py"
    assert failure["line"] == 2


def test_the_deepest_frame_wins(tmp_path: Path) -> None:
    """栈里越靠后越接近真正出问题的那一行。"""
    xml = (
        '<testsuites><testsuite name="s"><testcase classname="c" name="t">'
        "<failure message='挂了'>src/helper.py:10: in call\n"
        "tests/test_x.py:99: AssertionError</failure>"
        "</testcase></testsuite></testsuites>"
    )

    failure = parse_failures("junit", _context(tmp_path, junit=xml))[0]

    assert failure["file"] == "tests/test_x.py"
    assert failure["line"] == 99


def test_an_attribute_still_wins_over_the_traceback(tmp_path: Path) -> None:
    """有些框架确实写属性。写了就信它——那是框架自己给的,比从文本里挖准。"""
    failure = parse_failures("junit", _context(tmp_path))[0]

    assert failure["file"] == "tests/test_order.py"
    assert failure["line"] == 12


# --- 没有 JUnit 时降级 -------------------------------------------------------


def test_the_unit_gate_falls_back_to_the_log_when_there_is_no_xml(tmp_path: Path) -> None:
    """一堵日志墙比几条 `file:line: message` 难用得多,哪怕后者不完整。"""
    context = ParseContext(
        gate_id="unit",
        module_dir=tmp_path,
        junit_xml_path=None,
        output="tests/test_order.py:12: AssertionError: 期望 3 实际 0\n",
    )

    failure = parse_failures(None, context)[0]

    assert failure["file"] == "tests/test_order.py"
    assert failure["line"] == 12


def test_the_xml_wins_when_both_are_available(tmp_path: Path) -> None:
    """XML 是框架自己给的结构,比从日志里挖准。"""
    context = _context(tmp_path, output="somewhere/else.py:99: 别信这一行\n")

    tests = [item["test"] for item in parse_failures(None, context)]

    assert "tests.test_order::test_reserve" in tests
