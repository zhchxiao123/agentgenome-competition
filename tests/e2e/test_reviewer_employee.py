"""评审员工:只批不改,而且是**手里没有写入工具**那种不改。

批判者与生成者共用一个头脑时,批判会不自觉地迁就自己刚写的东西。职责分离是提质的机制
本身,不是分工洁癖——所以这里验的是机制:工具集、可写范围、越权检查,以及意见小票的形状。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from agentgenome.agents.contract import check_result_contract
from agentgenome.cli import app
from agentgenome.employees import load_employees
from agentgenome.genome.procedures import load_procedure
from tests.fixtures.mall import materialize_mall

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        app,
        [
            "init",
            "--local-only",
            str(root),
            "--name",
            "example-mall",
            "--repo",
            mall["order-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    return root


def reviewer(workspace: Path) -> dict[str, Any]:
    payload: dict[str, Any] = yaml.safe_load(
        (workspace / "employees" / "reviewer-employee.yaml").read_text(encoding="utf-8")
    )
    return payload


def critique_schema(workspace: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        (workspace / "genome" / "procedures" / "code-critique" / "schemas" / "out.json").read_text(
            encoding="utf-8"
        )
    )
    return payload


def test_the_workspace_gets_a_reviewer_and_its_procedure(workspace: Path) -> None:
    assert (workspace / "employees" / "reviewer-employee.yaml").is_file()
    assert (workspace / "employees" / "prompts" / "reviewer.md").is_file()
    spec = load_procedure(workspace / "genome" / "procedures" / "code-critique")
    assert spec.id == "code-critique"


def test_the_reviewer_has_no_writing_tools(workspace: Path) -> None:
    """ "只读"不是一句承诺,是它手里根本没有那几把工具。"""
    allowed = set(reviewer(workspace)["tools"]["allow"])

    assert allowed == {"Read", "Grep", "Glob"}
    assert not allowed & {"Write", "Edit", "Bash"}


def test_the_reviewer_may_not_write_code_or_the_genome(workspace: Path) -> None:
    permissions = reviewer(workspace)["permissions"]

    assert permissions["write_paths"] == ["tasks/{task_id}/**"]
    assert "genome/**" in permissions["forbid_paths"]
    assert any("repos" in item for item in permissions["forbid_paths"])


def test_the_reviewer_may_only_run_code_critique(workspace: Path) -> None:
    """员工能跑哪些工序是白名单。空着或多给都不是"暂时没配"。"""
    assert reviewer(workspace)["procedures"] == ["code-critique"]


def test_the_declared_craft_is_actually_written_out(workspace: Path) -> None:
    """声明了手艺就必须把它写出来。

    否则每一个新工作区的员工校验都会以"声明的通用手艺不存在"失败——一个只有初始化过的人
    才看得见、而且看不出是哪里配错的错误。
    """
    declared = reviewer(workspace)["crafts"]
    craft_root = workspace / "genome" / "procedures" / "_common" / "craft"

    for name in declared:
        assert (craft_root / name / "SKILL.md").is_file()

    body = (craft_root / "rule-compliance" / "SKILL.md").read_text(encoding="utf-8")
    # 手艺要教"怎么把问题问具体",不是"请检查规则"。判据:它给了反例。
    assert "❌" in body and "✅" in body


def test_the_employee_registry_accepts_the_reviewer(workspace: Path) -> None:
    registry = load_employees(workspace / "employees")

    assert "reviewer-employee" in registry
    assert not registry.rejected


def test_the_critique_receipt_is_flat_and_capped(workspace: Path) -> None:
    """意见是小票,不是散文。20 条上限写进 schema,不只写在提示词里。"""
    schema = critique_schema(workspace)
    findings = schema["properties"]["findings"]

    assert "approved" in schema["required"] and "findings" in schema["required"]
    assert findings["maxItems"] == 20
    assert set(findings["items"]["required"]) == {"file", "severity", "issue", "suggestion"}


def test_a_receipt_with_too_many_findings_is_refused(workspace: Path, tmp_path: Path) -> None:
    """只写在提示词里的话,超限的产物照样过契约,而"最多 20 条"就等于没有。"""
    output = tmp_path / "out"
    output.mkdir()
    finding = {
        "file": "repos/order-service/src/a.py",
        "severity": "minor",
        "issue": "x",
        "suggestion": "y",
    }
    (output / "result.json").write_text(
        json.dumps(
            {
                "task_id": "ag-1",
                "producer": "reviewer-employee",
                "created_at": "2026-09-01T10:00:00Z",
                "passed": True,
                "approved": False,
                "findings": [finding] * 21,
            }
        ),
        encoding="utf-8",
    )

    check = check_result_contract(output, critique_schema(workspace))

    assert not check.ok
    assert check.detail is not None and "findings" in check.detail


def test_approved_cannot_coexist_with_a_blocking_finding(workspace: Path, tmp_path: Path) -> None:
    """一张小票不能一边说可合并，一边留下 major/blocker。"""
    output = tmp_path / "out"
    output.mkdir()
    (output / "result.json").write_text(
        json.dumps(
            {
                "task_id": "ag-1",
                "producer": "reviewer-employee",
                "created_at": "2026-09-01T10:00:00Z",
                "passed": True,
                "approved": True,
                "findings": [
                    {
                        "file": "repos/order-service/src/a.py",
                        "severity": "major",
                        "issue": "会丢数据",
                        "suggestion": "先修复再合并",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    check = check_result_contract(output, critique_schema(workspace))

    assert not check.ok


def test_scaffolding_twice_does_not_overwrite_local_edits(workspace: Path) -> None:
    """使用者改过的员工定义不该被一次重新初始化抹掉——那正是这些东西做成文件的全部理由。"""
    target = workspace / "employees" / "reviewer-employee.yaml"
    target.write_text(target.read_text(encoding="utf-8") + "\n# 本地改动\n", encoding="utf-8")

    from agentgenome.genome.roster import scaffold_roster

    scaffold_roster(workspace)

    assert "# 本地改动" in target.read_text(encoding="utf-8")
