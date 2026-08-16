"""`agctl employee list` / `show`:"这个员工的有效配置到底是什么"。

覆盖与默认值叠加之后的结果人是算不清的,所以得让机器算——这两条命令的全部意义。
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app

runner = CliRunner()

DEV = """\
id: dev-employee
runtime: claude-code
model: default
prompt: prompts/dev.md
procedures: [code-develop, unit-gate]
tools:
  allow: [Bash, Read, Write]
  deny: [WebFetch]
permissions:
  write_paths: ["repos/**", "tasks/{task_id}/**"]
  forbid_paths: ["genome/rules/**"]
limits:
  job_timeout_s: 1800
"""

ARCH = """\
id: arch-employee
runtime: claude-code
prompt: prompts/arch.md
procedures: [design-review]
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    prompts = root / "employees" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "dev.md").write_text("你是开发数字员工。\n", encoding="utf-8")
    (prompts / "arch.md").write_text("你是架构数字员工。\n", encoding="utf-8")
    for name, body in (("dev-employee", DEV), ("arch-employee", ARCH)):
        (root / "employees" / f"{name}.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    # 根配置:改执行档位要读它(缺省运行时、确认名单都在里面)。
    (root / "agentgenome.yaml").write_text("platform: {git_host: local}\n", encoding="utf-8")
    return root


def _run(workspace: Path, *args: str):
    return runner.invoke(app, [*args, "--workspace", str(workspace)])


def test_list_shows_every_employee_with_its_runtime_and_procedure_count(workspace: Path) -> None:
    result = _run(workspace, "employee", "list")

    assert result.exit_code == 0, result.output
    assert "dev-employee" in result.output
    assert "arch-employee" in result.output
    assert "claude-code" in result.output
    assert "2" in result.output, "要看得到 Procedure 数"


def test_list_json_is_machine_readable(workspace: Path) -> None:
    result = _run(workspace, "employee", "list", "--json")

    payload = json.loads(result.output)
    rows = {row["id"]: row for row in payload["employees"]}
    assert rows["dev-employee"]["procedures"] == 2
    assert rows["arch-employee"]["runtime"] == "claude-code"


def test_a_broken_definition_is_reported_without_hiding_the_others(workspace: Path) -> None:
    """一个手滑写坏的员工不该让整份名单看不了。"""
    (workspace / "employees" / "broken.yaml").write_text("id: mismatch\nruntime: x\n")

    result = _run(workspace, "employee", "list")

    assert result.exit_code == 0, result.output
    assert "dev-employee" in result.output
    assert "broken" in result.output


def test_show_prints_the_effective_configuration(workspace: Path) -> None:
    result = _run(workspace, "employee", "show", "dev-employee")

    assert result.exit_code == 0, result.output
    assert "code-develop" in result.output
    assert "genome/rules/**" in result.output


def test_show_json_carries_the_whole_effective_configuration(workspace: Path) -> None:
    result = _run(workspace, "employee", "show", "dev-employee", "--json")

    payload = json.loads(result.output)
    assert payload["tools"]["deny"] == ["WebFetch"]
    assert payload["limits"]["job_timeout_s"] == 1800
    assert payload["permissions"]["forbid_paths"] == ["genome/rules/**"]


def test_show_expands_the_task_placeholder_when_a_task_is_given(workspace: Path) -> None:
    """员工只能写**本任务**目录。占位符没展开的话看不出这一点。"""
    result = _run(workspace, "employee", "show", "dev-employee", "--json", "--task", "ag-1")

    payload = json.loads(result.output)
    assert "tasks/ag-1/**" in payload["permissions"]["write_paths"]


def test_an_unknown_employee_lists_what_is_defined(workspace: Path) -> None:
    result = _run(workspace, "employee", "show", "ghost")

    assert result.exit_code != 0
    assert "dev-employee" in result.output
    assert "Traceback" not in result.output


def test_the_rung_can_be_changed_from_the_command_line(workspace: Path) -> None:
    """无头环境与界面能力对等,而且两条路走同一个校验。"""
    result = runner.invoke(
        app,
        [
            "employee",
            "execution",
            "dev-employee",
            "manual",
            "--assignee",
            "alice",
            "--as",
            "root",
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    from agentgenome.employees import load_employees, workspace_employees_root

    employee = load_employees(workspace_employees_root(workspace)).get("dev-employee")
    assert employee.runtime == "human"
    assert employee.assignee == "alice"


def test_manual_without_an_assignee_says_so_without_a_traceback(workspace: Path) -> None:
    result = runner.invoke(
        app,
        [
            "employee",
            "execution",
            "dev-employee",
            "manual",
            "--as",
            "root",
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "指派人" in result.output


def test_the_command_line_is_recorded_as_the_command_line(workspace: Path) -> None:
    """审计问的是"走界面改的还是脚本改的"。入口记错了,这个问题就没法筛。"""
    runner.invoke(
        app,
        [
            "employee",
            "execution",
            "dev-employee",
            "manual",
            "--assignee",
            "alice",
            "--as",
            "root",
            "--workspace",
            str(workspace),
        ],
    )

    from agentgenome.server.settings import history

    assert history(workspace)[-1].entrance.value == "cli"


def test_a_broken_definition_next_door_blocks_the_write_and_leaves_the_file_alone(
    workspace: Path,
) -> None:
    """**整册加载**,不只解析这一份:单份合法而整册不合法的坏法同样会让下一次启动失败。"""
    (workspace / "employees" / "broken-employee.yaml").write_text(
        "id: broken-employee\nruntime: claude-code\n", encoding="utf-8"
    )
    target = workspace / "employees" / "dev-employee.yaml"
    before = target.read_bytes()

    result = runner.invoke(
        app,
        [
            "employee",
            "execution",
            "dev-employee",
            "manual",
            "--assignee",
            "alice",
            "--as",
            "root",
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert target.read_bytes() == before
