"""任务级失败报告:回退时注入下一轮的那份东西。

顺序决定了员工先看到什么。回归排在最前不是排版偏好——它指向上一轮的改动,而一堵
"又挂了三个测试"只能让人从头猜。
"""

from __future__ import annotations

from pathlib import Path

from agentgenome.jobs.reports import read_failure_reports, write_failure_report

GATE_PAYLOAD = {
    "passed": False,
    "failures": [
        {"test": "t::a", "message": "AssertionError: 期望 3 实际 0", "evidence": {"line": 12}}
    ],
    "regressions": [{"test": "t::b", "message": "TypeError: 上一轮还是好的"}],
    "fixed": [{"test": "t::c"}],
}


def test_regressions_come_first(tmp_path: Path) -> None:
    path = write_failure_report(tmp_path, round_=1, title="单元门禁失败", payload=GATE_PAYLOAD)

    body = path.read_text(encoding="utf-8")
    assert body.index("这一轮新出现的失败") < body.index("AssertionError")


def test_the_title_says_you_broke_something(tmp_path: Path) -> None:
    """员工要先看到"你修坏了东西"。"""
    path = write_failure_report(tmp_path, round_=1, title="单元门禁失败", payload=GATE_PAYLOAD)

    assert "引入了 1 个新失败" in path.read_text(encoding="utf-8").splitlines()[0]


def test_no_regressions_means_a_plain_title(tmp_path: Path) -> None:
    """没修坏东西的时候不要吓唬人。"""
    payload = {"passed": False, "failures": [{"message": "挂了"}]}

    path = write_failure_report(tmp_path, round_=1, title="单元门禁失败", payload=payload)

    assert path.read_text(encoding="utf-8").splitlines()[0] == "# 单元门禁失败"


def test_what_got_fixed_is_shown_too(tmp_path: Path) -> None:
    """只说坏消息的话,员工不知道自己上一轮哪部分是对的,可能连对的一起改掉。"""
    path = write_failure_report(tmp_path, round_=1, title="单元门禁失败", payload=GATE_PAYLOAD)

    assert "这一轮修好的" in path.read_text(encoding="utf-8")


def test_a_result_that_claims_failure_without_details_says_so(tmp_path: Path) -> None:
    """悄悄写一份空报告的话,员工会以为上一轮什么都没发生。"""
    path = write_failure_report(tmp_path, round_=1, title="失败", payload={"passed": False})

    assert "没有给出" in path.read_text(encoding="utf-8")


def test_reports_read_back_in_round_order(tmp_path: Path) -> None:
    for round_ in (2, 1, 3):
        write_failure_report(tmp_path, round_=round_, title=f"第 {round_} 轮", payload={})

    assert [item.round for item in read_failure_reports(tmp_path)] == [1, 2, 3]


def test_only_earlier_rounds_come_back(tmp_path: Path) -> None:
    """第三轮要看前两轮,不该看到自己那份。"""
    for round_ in (1, 2, 3):
        write_failure_report(tmp_path, round_=round_, title=f"第 {round_} 轮", payload={})

    assert [item.round for item in read_failure_reports(tmp_path, before_round=3)] == [1, 2]


def test_the_case_name_leads_each_entry(tmp_path: Path) -> None:
    """用例名是这条失败的身份,也是员工唯一能拿去定位的东西。

    只写消息的话,"AssertionError: 期望 3 实际 0" 这种在多个用例里长得一模一样。
    """
    payload = {
        "passed": False,
        "failures": [
            {"test": "tests.t::test_reserve", "message": "挂了", "file": "tests/t.py", "line": 12}
        ],
    }

    path = write_failure_report(tmp_path, round_=1, title="失败", payload=payload)
    body = path.read_text("utf-8")

    assert "tests.t::test_reserve" in body
    assert "tests/t.py:12" in body


def test_a_failure_without_a_position_says_nothing_about_it(tmp_path: Path) -> None:
    """编一个位置出来会让员工跑去改一个无关的文件。"""
    payload = {"passed": False, "failures": [{"test": "t::a", "message": "挂了"}]}

    path = write_failure_report(tmp_path, round_=1, title="失败", payload=payload)
    body = path.read_text("utf-8")

    assert "位置" not in body


def test_the_raw_log_is_appended_at_the_end(tmp_path: Path) -> None:
    """日志是补充材料不是主菜——但没有它,解析器没读懂的那部分失败就彻底不可见了。"""
    payload = {
        "passed": False,
        "failures": [{"test": "t::a", "message": "挂了"}],
        "gates": [{"id": "unit", "passed": False, "log_tail": "E   AssertionError: 预占没实现"}],
    }

    path = write_failure_report(tmp_path, round_=1, title="失败", payload=payload)
    body = path.read_text("utf-8")

    assert "预占没实现" in body
    assert body.index("t::a") < body.index("原始日志"), "日志排到了结构化条目前面"


def test_a_passing_gate_contributes_no_log(tmp_path: Path) -> None:
    payload = {
        "passed": False,
        "failures": [{"test": "t::a", "message": "挂了"}],
        "gates": [{"id": "lint", "passed": True, "log_tail": "一切正常"}],
    }

    path = write_failure_report(tmp_path, round_=1, title="失败", payload=payload)
    body = path.read_text("utf-8")

    assert "一切正常" not in body
