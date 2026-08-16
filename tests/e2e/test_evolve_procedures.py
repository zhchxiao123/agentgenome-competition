"""`agctl evolve procedures`:给 L3 分级一个生产调用方。

这颗切片此前是"有测试、零调用方"——分级函数、阈值、渲染全在,但系统里没有任何东西调它。
测试绿只证明"这段逻辑本身没错",不证明"它真的被接进了系统里跑",这是两件不同的事。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.core.events import ActorKind, EventLog, LogKind
from tests.fixtures.procedures import write_procedure

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    root = tmp_path / "ws"
    (root / "genome" / "procedures").mkdir(parents=True)
    (root / "agentgenome.yaml").write_text("platform: {git_host: local}\n", encoding="utf-8")
    write_procedure(
        root / "genome" / "procedures", "code-develop", "agentic", prompt="干活\n", script=None
    )
    return root


def _failed(root: Path, task_id: str, detail: str, times: int = 1) -> None:
    log = EventLog(root)
    for _ in range(times):
        log.append(
            task_id,
            actor="dev-employee",
            actor_kind=ActorKind.EMPLOYEE,
            kind=LogKind.JOB_FINISHED,
            payload={
                "ok": False,
                "procedure_ref": "code-develop@1.0.0",
                "failure_detail": detail,
            },
        )


def _run(workspace: Path, *extra: str):
    return runner.invoke(
        app, ["evolve", "procedures", "--workspace", str(workspace), *extra]
    )


def test_a_procedure_failing_the_same_way_shows_up(workspace: Path) -> None:
    _failed(workspace, "ag-1", "result.json 缺 changed_files", times=3)

    result = _run(workspace, "--json")

    assert result.exit_code == 0, result.output
    [found] = json.loads(result.output)
    assert found["procedure_id"] == "code-develop"
    assert "changed_files" in found["failure_pattern"][0]


def test_occasional_failures_do_not_count(workspace: Path) -> None:
    """偶发失败没有进化价值。反复以完全相同的方式失败才说明是能力本身的问题。"""
    _failed(workspace, "ag-1", "网络抖了一下", times=2)

    result = _run(workspace, "--json")

    assert json.loads(result.output) == []


def test_the_pattern_is_found_across_tasks_not_within_one(workspace: Path) -> None:
    """失败模式恰恰是「在不同任务里以同一种方式挂」——单看一个任务看不出来。"""
    for index in range(3):
        _failed(workspace, f"ag-{index}", "同一个原因", times=1)

    result = _run(workspace, "--json")

    assert len(json.loads(result.output)) == 1


def test_a_proposal_without_a_diff_defaults_to_the_stricter_lane(workspace: Path) -> None:
    """没有改动就没有分级依据。默认走严的那档,不默认放行。"""
    _failed(workspace, "ag-1", "老是忘了写产物", times=3)

    [found] = json.loads(_run(workspace, "--json").output)

    assert found["level"] == "L3a"
    assert found["needs_human"] is True


def test_it_says_out_loud_that_it_does_not_write_the_fix(workspace: Path) -> None:
    """**声称它自动进化会是一句假话。** 生成改进内容需要一次 agentic 步骤,而那一步
    现在不存在;这条命令交付的是证据,不是改动。
    """
    _failed(workspace, "ag-1", "老是忘了写产物", times=3)

    result = _run(workspace)

    assert "不是改动" in result.output


def test_nothing_to_report_is_said_plainly(workspace: Path) -> None:
    result = _run(workspace)

    assert result.exit_code == 0
    assert "没有工序" in result.output


def test_the_threshold_is_adjustable(workspace: Path) -> None:
    _failed(workspace, "ag-1", "两次就够了吗", times=2)

    assert json.loads(_run(workspace, "--json").output) == []
    assert len(json.loads(_run(workspace, "--json", "--threshold", "2").output)) == 1
