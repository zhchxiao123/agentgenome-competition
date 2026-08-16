"""集成测试报告与复现命令。

这一层是纯的:给定失败用例集与日志,输出报告结构。不跑子进程、不碰 docker。

本片交付的是**诊断字段为空的完整报告**——`suspect_files` 与 `suggestion` 那半是 Agent
的活,在 issue 05 补上。形状先定下来,降级形态先测掉。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentgenome.itest.env import EnvPlan
from agentgenome.itest.report import (
    LOG_TAIL_LINES,
    REPORT_FILE,
    EnvSnapshot,
    ItestFailure,
    ItestReport,
    archive_logs,
    repro_command,
    tail_of,
)
from agentgenome.itest.task_itest import capture_pointers

PLAN = EnvPlan(
    project="ag-ag-1",
    compose_file=Path("/ws/itest/compose.yaml"),
    modules=("inventory-service", "order-service"),
)

SNAPSHOT = EnvSnapshot(
    project=PLAN.project,
    compose_file="itest/compose.yaml",
    built_modules=PLAN.modules,
    submodule_pointers={"repos/order-service": "a" * 40, "repos/inventory-service": "b" * 40},
)


def _report(failures: list[ItestFailure] | None = None) -> ItestReport:
    return ItestReport(
        task_id="ag-1",
        created_at="2026-09-01T10:00:00Z",
        failures=failures or [],
        env=SNAPSHOT,
    )


# --- 日志尾部 ---------------------------------------------------------------


def test_a_long_log_is_cut_from_the_end() -> None:
    """要的是"最后发生了什么",不是"最先发生了什么"。"""
    text = "\n".join(f"line-{index}" for index in range(200))

    tail, truncated = tail_of(text, lines=5)

    assert tail.splitlines() == ["line-195", "line-196", "line-197", "line-198", "line-199"]
    assert truncated is True


def test_a_short_log_is_kept_whole() -> None:
    tail, truncated = tail_of("只有一行\n", lines=5)

    assert tail == "只有一行\n"
    assert truncated is False


def test_truncation_is_visible_in_the_report() -> None:
    """静默截断读起来像"日志就这么多",而那正好是排查时最误导人的一句话。"""
    failure = ItestFailure(
        case="test_reserve", message="预占失败", log_tail="...", log_truncated=True, repro_cmd="x"
    )

    assert _report([failure]).as_dict()["failures"][0]["log_truncated"] is True


def test_the_default_tail_length_is_not_the_whole_log() -> None:
    """几个容器的日志混在一起动辄上万行,原样注入会把上下文包挤爆。"""
    assert 0 < LOG_TAIL_LINES < 500


# --- 复现命令 ---------------------------------------------------------------


def test_the_repro_command_is_one_pasteable_line() -> None:
    command = repro_command(PLAN, service="order-service", argv=["pytest", "-q", "itest/test_a.py"])

    assert "\n" not in command
    assert command.startswith("docker compose")


def test_the_repro_command_pins_the_project_and_the_compose_file() -> None:
    """不带项目名的话,粘过去那条命令跑的是另一套环境。"""
    command = repro_command(PLAN, service="order-service", argv=["pytest", "-q"])

    assert f"-p {PLAN.project}" in command
    assert str(PLAN.compose_file) in command


def test_the_repro_command_names_the_service_and_the_case() -> None:
    command = repro_command(PLAN, service="order-service", argv=["pytest", "-q", "itest/test_a.py"])

    assert "order-service" in command
    assert "itest/test_a.py" in command


def test_arguments_with_spaces_survive_the_round_trip() -> None:
    """拼出来的命令要能真的跑。带空格的参数不加引号的话,粘过去就是另一条命令。"""
    import shlex

    command = repro_command(PLAN, service="order-service", argv=["pytest", "-k", "a or b"])

    assert "a or b" in shlex.split(command)


def test_the_repro_command_rebuilds_before_running() -> None:
    """环境已经销毁了。不重建的话,粘过去那条命令在一台干净机器上必然报"找不到服务"。"""
    command = repro_command(PLAN, service="order-service", argv=["pytest"], with_build=True)

    assert "build" in command
    assert " && " in command


# --- 环境快照 ---------------------------------------------------------------


def test_the_env_section_records_which_version_combination_was_tested() -> None:
    """跨模块任务里同一份代码在不同指针组合下结果可能不同。没有这个字段,历史报告
    是不可解释的。"""
    payload = _report().as_dict()["env"]

    assert payload["submodule_pointers"] == {
        "repos/order-service": "a" * 40,
        "repos/inventory-service": "b" * 40,
    }
    assert payload["built_modules"] == ["inventory-service", "order-service"]
    assert payload["project"] == "ag-ag-1"


# --- 报告结构 ---------------------------------------------------------------


def test_a_clean_run_still_produces_a_report() -> None:
    payload = _report().as_dict()

    assert payload["passed"] is True
    assert payload["failures"] == []


def test_any_failure_makes_the_whole_report_fail() -> None:
    payload = _report([ItestFailure(case="a", message="b", log_tail="", repro_cmd="c")]).as_dict()

    assert payload["passed"] is False


def test_the_report_satisfies_the_common_result_contract() -> None:
    """状态机读的是公共字段。少一个的话下游读它时才炸,而那时已经离现场很远。"""
    payload = _report().as_dict()

    assert {"task_id", "producer", "created_at", "passed"} <= set(payload)


def test_the_diagnosis_fields_are_present_but_empty_before_the_agent_runs() -> None:
    """形状先定下来:下一片补 Agent 那一半时不该再改契约。"""
    failure = _report([ItestFailure(case="a", message="b", log_tail="", repro_cmd="c")]).as_dict()[
        "failures"
    ][0]

    assert failure["suspect_files"] == []
    assert failure["suggestion"] == ""


def test_the_report_lands_on_disk_where_the_state_machine_looks(tmp_path: Path) -> None:
    path = _report().write(tmp_path)

    assert path == tmp_path / REPORT_FILE
    assert json.loads(path.read_text(encoding="utf-8"))["task_id"] == "ag-1"


# --- 完整日志归档 -----------------------------------------------------------


def test_full_logs_are_archived_next_to_the_report(tmp_path: Path) -> None:
    """需要深挖时材料还在——报告里只有尾部,不等于其余的可以丢。"""
    archived = archive_logs(tmp_path, {"order-service": "第一行\n第二行\n"})

    assert archived == {"order-service": "logs/order-service.log"}
    landed = tmp_path / "logs" / "order-service.log"
    assert landed.read_text(encoding="utf-8") == "第一行\n第二行\n"


def test_a_service_name_cannot_escape_the_logs_directory(tmp_path: Path) -> None:
    """服务名来自编排文件,而编排文件是项目自己的资产——不是可信输入。"""
    with pytest.raises(ValueError):
        archive_logs(tmp_path, {"../../etc/passwd": "x"})


def test_the_report_points_at_the_archived_logs(tmp_path: Path) -> None:
    report = _report()
    report.log_paths = archive_logs(tmp_path, {"order-service": "x"})

    assert report.as_dict()["env"]["logs"] == {"order-service": "logs/order-service.log"}


def test_a_run_with_no_logs_at_all_still_produces_a_report(tmp_path: Path) -> None:
    """服务根本没起来时一条日志都收不到。这时更需要报告,不是更不需要。"""
    report = _report([ItestFailure(case="a", message="服务没起来", log_tail="", repro_cmd="c")])
    report.log_paths = archive_logs(tmp_path, {})

    assert report.write(tmp_path).is_file()
    assert report.as_dict()["env"]["logs"] == {}


# --- 子模块指针快照 ---------------------------------------------------------


def test_a_directory_that_is_not_a_repo_yields_no_pointers(tmp_path: Path) -> None:
    """手工在任意目录里跑重跑命令时不该炸——拿不到指针就是拿不到。"""
    assert capture_pointers(tmp_path) == {}


def test_a_repo_without_submodules_yields_no_pointers(tmp_path: Path) -> None:
    from tests.fixtures.git import git as run_git

    run_git(tmp_path, "init", "--initial-branch=main")

    assert capture_pointers(tmp_path) == {}
