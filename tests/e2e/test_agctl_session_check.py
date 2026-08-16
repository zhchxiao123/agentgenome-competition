"""`agctl session check`:部署完能不能验会话链路。

这条命令回答的问题以前只能靠点界面来回答,而它曾经在生产里是断的(见 PRD 29)。
**默认路径不烧一个 token**——装配通没通、能力判断对不对,这两件事不需要 token 就能验完;
要烧 token 的检查进不了冒烟,那等于没有。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from tests.fixtures.git import commit_all
from tests.fixtures.mall import materialize_mall

runner = CliRunner()

REPLAY_EMPLOYEE = """\
id: architect
runtime: replay
prompt: prompts/architect.md
procedures: [code-develop]
tools:
  allow: [Read]
permissions:
  write_paths: ["**"]
"""

NO_SESSION_EMPLOYEE = """\
id: qwen-hand
runtime: qwen-code
prompt: prompts/architect.md
procedures: [code-develop]
tools:
  allow: [Read]
permissions:
  write_paths: ["**"]
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    (tmp_path / "lib").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        cli_app,
        [
            "init",
            "--local-only",
            str(root),
            "--name",
            "mall",
            "--repo",
            mall["order-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    (root / "employees" / "prompts" / "architect.md").write_text("你是架构员工。\n", "utf-8")
    (root / "employees" / "architect.yaml").write_text(REPLAY_EMPLOYEE, encoding="utf-8")
    (root / "employees" / "qwen-hand.yaml").write_text(NO_SESSION_EMPLOYEE, encoding="utf-8")
    # qwen-code **配好了**——这样"开不了会话"才是它真正的成因,而不是"根本没配"。
    config = root / "agentgenome.yaml"
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    payload["runtime"]["qwen-code"] = {"cmd": "qwen", "max_turns": 40}
    config.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    commit_all(root, "chore: 员工")
    return root


def _check(workspace: Path, as_json: bool = True):
    argv = ["session", "check", "--workspace", str(workspace)]
    return runner.invoke(cli_app, argv + (["--json"] if as_json else []))


def test_it_reports_which_employees_can_open_a_session(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))

    result = _check(workspace)

    assert result.exit_code == 0, result.output
    by_id = {row["id"]: row for row in json.loads(result.output)["employees"]}
    assert by_id["architect"]["can_session"] is True
    assert by_id["qwen-hand"]["can_session"] is False


def test_a_blocked_employee_comes_with_a_reason(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))

    result = _check(workspace)

    by_id = {row["id"]: row for row in json.loads(result.output)["employees"]}
    assert "开不了会话" in by_id["qwen-hand"]["reason"]


def test_it_exits_non_zero_when_nobody_can_open_a_session(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**冒烟检查要能靠退出码判断。**

    打印一堆 ✗ 然后退出码 0 的话,它在流水线里会静默通过——而这条命令存在的全部理由就是
    在部署之后发现"对话功能是断的"。

    构造一个全员 qwen-code 的工作区:`agctl init` 铺的默认员工都用 claude-code,留着它们中的
    **任何一个**这条都不会红——所以删的是全部,不是记忆里的那三个(队伍会长)。
    """
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    for default in sorted((workspace / "employees").glob("*-employee.yaml")):
        default.unlink()
    (workspace / "employees" / "architect.yaml").unlink()

    result = _check(workspace, as_json=False)

    assert result.exit_code != 0
    assert "没有任何员工能开会话" in result.output


def test_the_human_output_names_the_usable_runtimes(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))

    result = _check(workspace, as_json=False)

    assert result.exit_code == 0, result.output
    assert "replay" in result.output
    assert "architect" in result.output
