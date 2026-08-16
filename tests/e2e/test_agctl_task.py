"""`agctl task submit/status/list`:任务的提交与查看。

这一层是需求方唯一会直接碰的入口,所以断言的是"我提交之后能不能查到、查到的是不是我提的"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskStore

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "genome" / "rules").mkdir(parents=True)
    (root / "agentgenome.yaml").write_text("platform: {git_host: local}\n")
    return root


def _submit(workspace: Path, *extra: str):
    return runner.invoke(
        app,
        [
            "task",
            "submit",
            "--requirement",
            "下单时要预占库存",
            "--workspace",
            str(workspace),
            *extra,
        ],
    )


def _status(workspace: Path, task_id: str, *extra: str):
    return runner.invoke(app, ["task", "status", task_id, "--workspace", str(workspace), *extra])


def test_submitting_returns_a_readable_task_id(workspace: Path) -> None:
    result = _submit(workspace, "--json")

    assert result.exit_code == 0, result.output
    task_id = json.loads(result.output)["id"]
    assert task_id.startswith("ag-")


def test_the_requirement_is_kept_verbatim(workspace: Path) -> None:
    """需求原文要原样存着——后面注入上下文时给员工看的就是它。"""
    task_id = json.loads(_submit(workspace, "--json").output)["id"]

    payload = json.loads(_status(workspace, task_id, "--json").output)

    assert payload["requirement"] == "下单时要预占库存"


def test_a_requirement_can_come_from_a_file(workspace: Path, tmp_path: Path) -> None:
    """真实需求经常有好几段,塞进命令行参数不现实。"""
    spec = tmp_path / "requirement.md"
    spec.write_text("# 预占库存\n\n下单时要先向库存申请预占。\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "task",
            "submit",
            "--requirement-file",
            str(spec),
            "--workspace",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    task_id = json.loads(result.output)["id"]
    assert "预占" in json.loads(_status(workspace, task_id, "--json").output)["requirement"]


def test_submitting_without_a_requirement_is_refused(workspace: Path) -> None:
    result = runner.invoke(app, ["task", "submit", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_a_new_task_starts_in_created(workspace: Path) -> None:
    task_id = json.loads(_submit(workspace, "--json").output)["id"]

    payload = json.loads(_status(workspace, task_id, "--json").output)

    assert payload["state"] == "CREATED"
    assert payload["fix_rounds"] == 0
    assert payload["tokens_used"] == 0
    assert payload["needs_itest"] == "undecided"


def test_status_of_an_unknown_task_says_so_without_a_traceback(workspace: Path) -> None:
    result = _status(workspace, "ag-20260101-999")

    assert result.exit_code != 0
    assert "ag-20260101-999" in result.output
    assert "Traceback" not in result.output


def test_list_shows_open_tasks_in_priority_order(workspace: Path) -> None:
    low = json.loads(_submit(workspace, "--json", "--priority", "1").output)["id"]
    high = json.loads(_submit(workspace, "--json", "--priority", "9").output)["id"]

    payload = json.loads(
        runner.invoke(app, ["task", "list", "--workspace", str(workspace), "--json"]).output
    )

    assert [row["id"] for row in payload["tasks"]] == [high, low]


def test_list_still_shows_escalated_tasks(workspace: Path) -> None:
    """`agctl task list` 和 `GET /tasks` 是同一个盲区的两个受害者——两边都读 `open_tasks`。
    只修接口的话,命令行提交任务的人依然看不到自己的任务升级到哪去了。"""
    task_id = json.loads(_submit(workspace, "--json").output)["id"]
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(state=TaskState.ESCALATED, escalate_reason="环境缺件"))

    payload = json.loads(
        runner.invoke(app, ["task", "list", "--workspace", str(workspace), "--json"]).output
    )

    assert [row["id"] for row in payload["tasks"]] == [task_id]


def test_list_is_readable_when_there_is_nothing(workspace: Path) -> None:
    result = runner.invoke(app, ["task", "list", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert result.output.strip()


def test_the_task_directory_carries_a_snapshot(workspace: Path) -> None:
    task_id = json.loads(_submit(workspace, "--json").output)["id"]

    snapshot = json.loads((workspace / "tasks" / task_id / "task.json").read_text(encoding="utf-8"))

    assert snapshot["id"] == task_id


def test_submitting_lands_an_event(workspace: Path) -> None:
    """任务从建出来的那一刻起就有历史。"""
    task_id = json.loads(_submit(workspace, "--json").output)["id"]

    payload = json.loads(
        runner.invoke(
            app, ["task", "events", task_id, "--workspace", str(workspace), "--json"]
        ).output
    )

    assert [event["kind"] for event in payload["events"]] == ["task_created"]


def test_events_of_an_unknown_task_are_empty_not_an_error(workspace: Path) -> None:
    """ "这个任务没有事件"和"这个任务不存在"由 status 区分,events 不必再报一次错。"""
    result = runner.invoke(app, ["task", "events", "ag-x", "--workspace", str(workspace)])

    assert result.exit_code == 0


def test_the_diagram_can_be_printed() -> None:
    """图由迁移表生成,贴进文档与 PR 时永远和表一致。"""
    result = runner.invoke(app, ["task", "diagram"])

    assert result.exit_code == 0, result.output
    assert "stateDiagram-v2" in result.output


def test_cancel_marks_the_task_and_keeps_the_directory(workspace: Path) -> None:
    task_id = json.loads(_submit(workspace, "--json").output)["id"]

    result = runner.invoke(
        app, ["task", "cancel", task_id, "--workspace", str(workspace), "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "CANCELLED"
    assert (workspace / "tasks" / task_id / "task.json").is_file()


def test_cancelling_an_unknown_task_says_so(workspace: Path) -> None:
    result = runner.invoke(app, ["task", "cancel", "ag-x", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_running_an_unknown_task_says_so(workspace: Path) -> None:
    result = runner.invoke(app, ["task", "run", "ag-x", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


# --- 集成测试的人工覆盖 -----------------------------------------------------


def test_a_new_task_leaves_the_integration_test_decision_to_the_system(workspace: Path) -> None:
    task_id = json.loads(_submit(workspace, "--json").output)["id"]

    payload = json.loads(_status(workspace, task_id, "--json").output)

    assert payload["itest_override"] == "auto"
    assert payload["needs_itest"] == "undecided"


def test_submitting_can_force_integration_tests(workspace: Path) -> None:
    result = _submit(workspace, "--itest", "always", "--json")

    assert json.loads(result.output)["itest_override"] == "always"


def test_an_unknown_itest_mode_is_refused(workspace: Path) -> None:
    result = _submit(workspace, "--itest", "sometimes")

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_forcing_mid_flight_decides_the_task_right_away(workspace: Path) -> None:
    """人发了话就不必等下一次门禁通过——覆盖的意义就是立刻生效。"""
    task_id = json.loads(_submit(workspace, "--json").output)["id"]

    result = runner.invoke(
        app, ["task", "itest", task_id, "always", "--workspace", str(workspace), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["itest_override"] == "always"
    assert payload["needs_itest"] == "yes"


def test_handing_back_to_auto_clears_the_previous_verdict(workspace: Path) -> None:
    """不清的话,"交回自动判定"只会保留人当初强加的结论,与命令名说的正好相反。"""
    task_id = json.loads(_submit(workspace, "--itest", "never", "--json").output)["id"]

    runner.invoke(app, ["task", "itest", task_id, "auto", "--workspace", str(workspace)])

    payload = json.loads(_status(workspace, task_id, "--json").output)
    assert payload["itest_override"] == "auto"
    assert payload["needs_itest"] == "undecided"


def test_the_override_lands_in_the_event_stream(workspace: Path) -> None:
    """ "这个任务为什么跳过了集成测试"要能被回答,包括答案是"人说的"。"""
    task_id = json.loads(_submit(workspace, "--json").output)["id"]
    runner.invoke(app, ["task", "itest", task_id, "never", "--workspace", str(workspace)])

    result = runner.invoke(app, ["task", "events", task_id, "--workspace", str(workspace)])

    assert "itest_decision" in result.output


def test_flipping_the_switch_on_a_terminal_task_is_refused(workspace: Path) -> None:
    task_id = json.loads(_submit(workspace, "--json").output)["id"]
    runner.invoke(app, ["task", "cancel", task_id, "--workspace", str(workspace)])

    result = runner.invoke(app, ["task", "itest", task_id, "always", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_flipping_the_switch_on_an_unknown_task_says_so(workspace: Path) -> None:
    result = runner.invoke(app, ["task", "itest", "ag-x", "always", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_submitting_can_choose_an_execution_strategy(workspace: Path) -> None:
    result = _submit(workspace, "--topology", "critique-loop", "--json")

    assert json.loads(result.output)["topology"] == "critique-loop"


def test_not_choosing_a_strategy_leaves_it_empty(workspace: Path) -> None:
    """留空是"跟随项目缺省",不是"选了 single"——两者在追因时是两件事。"""
    task_id = json.loads(_submit(workspace, "--json").output)["id"]

    assert TaskStore(workspace).get(task_id).topology == ""


def test_an_unknown_strategy_is_refused_before_the_task_exists(workspace: Path) -> None:
    result = _submit(workspace, "--topology", "critique-lop")

    assert result.exit_code != 0
    assert "critique-loop" in result.output
    assert TaskStore(workspace).all_tasks() == ()


def test_the_strategy_list_says_what_each_one_does(workspace: Path) -> None:
    result = runner.invoke(app, ["topology", "list", "--workspace", str(workspace), "--json"])

    payload = json.loads(result.output)
    assert payload["default"] == "single"
    assert {item["id"] for item in payload["options"]} == {
        "single",
        "critique-loop",
        "assisted",
        "dag",
        "best-of-n",
    }
    assert all(item["summary"] for item in payload["options"])


def test_the_strategy_list_says_how_much_the_expensive_one_costs(workspace: Path) -> None:
    result = runner.invoke(app, ["topology", "list", "--workspace", str(workspace)])

    assert "3 × 单路" in result.output
    # 没有历史就没有绝对值——编一个出来比不说更糟,人会拿它做决定。
    assert "tokens" not in result.output


def test_the_estimate_appears_once_the_project_has_spent_something(workspace: Path) -> None:
    store = TaskStore(workspace)
    for spend in (1000, 3000):
        task = store.create(title="旧任务", requirement="x")
        store.save(task.evolve(state=TaskState.COMPLETED, tokens_used=spend))

    result = runner.invoke(app, ["topology", "list", "--workspace", str(workspace)])

    assert "6,000 tokens" in result.output
