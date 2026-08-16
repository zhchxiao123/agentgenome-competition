"""开发员工跑一次 code-develop:三片装配起来到底能不能干活。

用的是 `agctl init` 真正写进 Workspace 的那份 dev-employee 与那个 code-develop,
不是测试自己捏的——测试捏一份的话,交付出去的资产有没有问题就永远测不到。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.agents.artifacts import context_filename
from agentgenome.cli import app
from agentgenome.space.git_ws import branch_name
from agentgenome.space.scope_guard import SCOPE_REPORT
from tests.fixtures.git import git as _git
from tests.fixtures.git import write_plan
from tests.fixtures.mall import materialize_mall

runner = CliRunner()

TASK = "ag-20260901-007"

#: 合乎 code-develop 产物 schema 的一份结果。
RESULT = {
    "task_id": TASK,
    "producer": "dev-employee",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
    "changed_files": ["repos/order-service/src/order/reserve.py"],
    "self_test": {"command": "pytest -q", "exit_code": 0, "passed": True, "summary": "12 passed"},
    "impact": {
        "modules": ["order-service"],
        "interfaces": ["reserve-api"],
        "rationale": "改的是下单路径上对预占接口的调用方",
    },
    "questions": [
        {
            "question": "预占失败时是重试还是直接失败下单?",
            "decision": "直接失败",
            "rationale": "需求没说,按最小惊讶原则选了不隐藏错误的那个",
        }
    ],
}


def _record(library: Path, files: dict[str, str], result: dict | None = None) -> None:
    directory = library / "dev-employee__code-develop__r1"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(
        json.dumps(result or RESULT, ensure_ascii=False), encoding="utf-8"
    )
    for relative, content in files.items():
        target = directory / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
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
            "example-mall",
            "--repo",
            mall["order-service"].remote_url,
            "--repo",
            mall["inventory-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    write_plan(root, TASK, "order-service")
    return root


def _run(workspace: Path):
    return runner.invoke(
        app,
        [
            "procedure",
            "run",
            "code-develop",
            "--workspace",
            str(workspace),
            "--employee",
            "dev-employee",
            "--runtime",
            "replay",
            "--task",
            TASK,
            "--state",
            "DEVELOPING",
            "--inputs",
            json.dumps({"requirement": "下单时预占库存"}, ensure_ascii=False),
            "--json",
        ],
    )


def _artifacts(workspace: Path) -> Path:
    return workspace / "tasks" / TASK / "artifacts" / "code-develop"


# --- 正常一轮 ---------------------------------------------------------------


def test_a_compliant_run_succeeds(workspace: Path, tmp_path: Path) -> None:
    _record(
        tmp_path / "lib", {"repos/order-service/src/order/reserve.py": "def reserve():\n    ...\n"}
    )

    result = _run(workspace)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True


def test_the_context_bundle_lands_on_disk(workspace: Path, tmp_path: Path) -> None:
    """ "当时它到底看到了什么"必须是能被回答的问题。"""
    _record(
        tmp_path / "lib", {"repos/order-service/src/order/reserve.py": "def reserve():\n    ...\n"}
    )

    _run(workspace)

    bundle = (_artifacts(workspace) / context_filename(0)).read_text(encoding="utf-8")
    assert "开发数字员工" in bundle, "角色提示词要在包里"
    assert "只改授权路径" in bundle, "铁律段要在包里"
    assert "下单时预占库存" in bundle, "本次入参要在包里"
    assert "不要增删或重同步项目虚拟环境" in bundle


def test_the_result_conforms_to_the_procedure_contract(workspace: Path, tmp_path: Path) -> None:
    """契约由回放路径同样校验——不给自己开后门。"""
    _record(
        tmp_path / "lib", {"repos/order-service/src/order/reserve.py": "def reserve():\n    ...\n"}
    )

    _run(workspace)

    payload = json.loads((_artifacts(workspace) / "result.json").read_text(encoding="utf-8"))
    assert payload["changed_files"] == ["repos/order-service/src/order/reserve.py"]
    assert payload["questions"][0]["decision"] == "直接失败"


def test_a_result_missing_the_required_fields_is_refused(workspace: Path, tmp_path: Path) -> None:
    """产物 schema 是硬要求:少一个字段等同这次 Job 失败。"""
    thin = {
        "task_id": TASK,
        "producer": "dev-employee",
        "created_at": "2026-09-01T10:00:00Z",
        "passed": True,
    }
    _record(tmp_path / "lib", {"repos/order-service/src/order/x.py": "...\n"}, result=thin)

    result = _run(workspace)

    assert result.exit_code != 0
    assert json.loads(result.output)["failure_kind"] == "contract"


def test_the_scope_check_passes_for_an_authorized_change(workspace: Path, tmp_path: Path) -> None:
    _record(
        tmp_path / "lib", {"repos/order-service/src/order/reserve.py": "def reserve():\n    ...\n"}
    )

    _run(workspace)

    report = json.loads((_artifacts(workspace) / SCOPE_REPORT).read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["violations"] == []


def test_the_touched_submodule_gets_the_task_branch(workspace: Path, tmp_path: Path) -> None:
    _record(
        tmp_path / "lib", {"repos/order-service/src/order/reserve.py": "def reserve():\n    ...\n"}
    )

    _run(workspace)

    assert branch_name(TASK) in _git(
        workspace / "repos/order-service", "branch", "--list", branch_name(TASK)
    )


# --- 越权那一轮 -------------------------------------------------------------


def test_rewriting_the_rules_fails_the_job_and_rolls_back(workspace: Path, tmp_path: Path) -> None:
    """开发员工禁写 `genome/rules/**`——这一条是靠机制,不是靠它自觉。"""
    original = (workspace / "genome" / "rules" / "architecture.md").read_text(encoding="utf-8")
    _record(
        tmp_path / "lib",
        {
            "repos/order-service/src/order/reserve.py": "def reserve():\n    ...\n",
            "genome/rules/architecture.md": "# 一切皆可\n",
        },
    )

    result = _run(workspace)

    assert result.exit_code != 0
    assert json.loads(result.output)["failure_kind"] == "scope"
    assert (workspace / "genome" / "rules" / "architecture.md").read_text() == original
    assert not (workspace / "repos/order-service" / "src" / "order" / "reserve.py").exists(), (
        "越权的 Job 整份产出都不可信,合规的那部分也一起回滚"
    )
