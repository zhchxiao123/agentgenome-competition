"""`agctl gate run`:在任务现有的工作区上重跑门禁,不驱动状态机。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from tests.fixtures.git import commit_all
from tests.fixtures.mall import materialize_mall
from tests.fixtures.tree import module_ids, patch_module_map
from tests.fixtures.verification import write_test_verification

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        app,
        [
            "init", "--local-only",
            str(root),
            "--name",
            "mall",
            "--repo",
            mall["order-service"].remote_url,
            "--repo",
            mall["inventory-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output

    for module_id in module_ids(root):
        patch_module_map(root, module_id, test_cmd="python -m pytest -q tests")
    commit_all(root, "chore: 补上 test_cmd")
    return root


def _submit(workspace: Path) -> str:
    result = runner.invoke(
        app,
        ["task", "submit", "--requirement", "改点东西", "--workspace", str(workspace), "--json"],
    )
    return str(json.loads(result.output)["id"])


def _checkout(workspace: Path, task_id: str) -> Path:
    result = runner.invoke(app, ["ws", "checkout", task_id, "--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    from agentgenome.space.git_ws import GitWorkspace

    return GitWorkspace(workspace).worktree_path(task_id)


def _genome_gates(workspace: Path, worktree: Path, module_id: str) -> None:
    """在基因组里给这个模块配一份只有单测的门禁。"""
    del worktree
    write_test_verification(
        workspace, module_id, ("unit", ("python", "-m", "pytest", "-q", "tests"))
    )


def _run(workspace: Path, task_id: str, *extra: str):
    return runner.invoke(
        app, ["gate", "run", task_id, "--workspace", str(workspace), "--json", *extra]
    )


def test_a_passing_module_reports_green(workspace: Path) -> None:
    task_id = _submit(workspace)
    worktree = _checkout(workspace, task_id)
    (worktree / "repos/order-service" / "tests" / "test_new.py").write_text(
        "def test_ok():\n    assert True\n"
    )

    result = _run(workspace, task_id, "--module", "order-service")

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["module"] == "order-service"
    assert report["gates"][0]["id"] == "unit"


def test_a_failing_test_shows_up_as_a_failed_gate(workspace: Path) -> None:
    task_id = _submit(workspace)
    worktree = _checkout(workspace, task_id)
    # 显式配置只留单测这一关:默认推导会带一关 gitleaks,机器上没装的话整份报告
    # 会被判成环境类——那是另一条用例(见下)。
    _genome_gates(workspace, worktree, "order-service")
    (worktree / "repos/order-service" / "tests" / "test_new.py").write_text(
        "def test_broken():\n    assert False, '没实现'\n"
    )

    report = json.loads(_run(workspace, task_id, "--module", "order-service").output)

    assert report["passed"] is False
    assert report["kind"] == "semantic"
    assert report["failures"], "报告说自己失败了却说不出哪儿失败"


def test_only_the_touched_module_runs(workspace: Path) -> None:
    """单模块改动不必等全仓测试。"""
    task_id = _submit(workspace)
    worktree = _checkout(workspace, task_id)
    (worktree / "repos/order-service" / "tests" / "test_new.py").write_text(
        "def test_ok():\n    assert True\n"
    )

    report = json.loads(_run(workspace, task_id).output)

    assert report["module"] == "order-service"


def test_a_change_outside_every_module_verifies_nothing_and_says_so(workspace: Path) -> None:
    """一个模块都没碰到的门禁没有验证任何东西。判它通过等于凭空发一张绿灯。"""
    task_id = _submit(workspace)
    worktree = _checkout(workspace, task_id)
    (worktree / "genome" / "knowledge" / "notes.md").write_text("随手记\n")

    report = json.loads(_run(workspace, task_id).output)

    assert report["passed"] is False
    assert "没有验证任何东西" in json.dumps(report, ensure_ascii=False)


def test_rerunning_does_not_overwrite_the_previous_report(workspace: Path) -> None:
    """手工重跑常常是"我想看看现在还挂不挂",而覆盖掉的那份正是要对比的基线。"""
    task_id = _submit(workspace)
    worktree = _checkout(workspace, task_id)
    (worktree / "repos/order-service" / "tests" / "test_new.py").write_text(
        "def test_ok():\n    assert True\n"
    )

    _run(workspace, task_id, "--module", "order-service")
    _run(workspace, task_id, "--module", "order-service")

    slots = sorted((workspace / "tasks" / task_id / "artifacts").iterdir())
    assert len(slots) == 2


def test_the_gate_run_does_not_move_the_task(workspace: Path) -> None:
    """排查环境问题时我要的是"现在还挂不挂",不是把任务往前推。"""
    task_id = _submit(workspace)
    _checkout(workspace, task_id)

    _run(workspace, task_id, "--module", "order-service")

    status = json.loads(
        runner.invoke(
            app, ["task", "status", task_id, "--workspace", str(workspace), "--json"]
        ).output
    )
    assert status["state"] == "CREATED"


def test_touching_the_gates_file_refuses_to_run(workspace: Path) -> None:
    """一个能改门禁的员工等于没有门禁。"""
    task_id = _submit(workspace)
    worktree = _checkout(workspace, task_id)
    (worktree / "repos/order-service" / "gates.yaml").write_text(
        "gates:\n  - id: unit\n    cmd: echo 我说通过就通过\n", encoding="utf-8"
    )

    report = json.loads(_run(workspace, task_id, "--module", "order-service").output)

    assert report["passed"] is False
    assert report["kind"] == "tampered"


def test_a_missing_scanner_is_an_environment_failure_not_a_code_failure(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """推导出来的密钥扫描关是必需的。机器上没装扫描工具时整份报告判环境类——
    让 AI 反复去修"环境里没装 gitleaks"是最典型的预算浪费方式。"""
    task_id = _submit(workspace)
    worktree = _checkout(workspace, task_id)
    (worktree / "repos/order-service" / "tests" / "test_new.py").write_text(
        "def test_ok():\n    assert True\n"
    )
    real_which = shutil.which
    monkeypatch.setattr(
        "agentgenome.verification.execution.shutil.which",
        lambda command, *, path=None: (
            None if command == "gitleaks" else real_which(command, path=path)
        ),
    )

    report = json.loads(_run(workspace, task_id, "--module", "order-service").output)

    assert report["passed"] is False
    assert report["kind"] == "environment"
    secrets = next(gate for gate in report["gates"] if gate["id"] == "secrets")
    assert secrets["outcome"] == "unavailable"


def test_a_legacy_config_is_visible_but_not_executable(workspace: Path) -> None:
    task_id = _submit(workspace)
    _checkout(workspace, task_id)
    (workspace / "genome/gates/order-service.yaml").write_text(
        "gates:\n  - id: unit\n    cmd: echo legacy-pass\n", encoding="utf-8"
    )

    report = json.loads(_run(workspace, task_id, "--module", "order-service").output)

    assert "bootstrap_verification:order-service" in report["notes"]
    assert all("legacy-pass" not in gate["log_tail"] for gate in report["gates"])
