"""`agctl topology validate`:拆图时的反馈回路。

校验器要能在**没有任何执行器**的情况下被单独跑一遍——否则"派发前全绿"这条承诺会退化成
"跑起来才知道"。这个命令就是它独立成立的那一端。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from agentgenome.cli import app

runner = CliRunner()


def write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    target = tmp_path / "topology.yaml"
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return target


def validate(path: Path, *extra: str):
    return runner.invoke(app, ["topology", "validate", str(path), *extra])


GOOD = {
    "id": "diamond",
    "nodes": [
        {"id": "plan", "employee": "arch", "procedure": "p", "produces": ["spec.md"]},
        {
            "id": "order",
            "employee": "dev",
            "procedure": "d",
            "needs": ["spec.md"],
            "produces": ["order.diff"],
            "write_scope": ["repos/order/**"],
        },
        {
            "id": "inventory",
            "employee": "dev",
            "procedure": "d",
            "needs": ["spec.md"],
            "produces": ["inventory.diff"],
            "write_scope": ["repos/inventory/**"],
        },
    ],
    "edges": [["plan", "order"], ["plan", "inventory"]],
}


def test_a_legal_graph_exits_zero(tmp_path: Path) -> None:
    result = validate(write(tmp_path, GOOD))

    assert result.exit_code == 0, result.output
    assert "3" in result.output and "2" in result.output


def test_json_output_reports_the_essentials(tmp_path: Path) -> None:
    payload = json.loads(validate(write(tmp_path, GOOD), "--json").output)

    assert payload == {"id": "diamond", "ok": True, "nodes": 3, "edges": 2}


def test_a_fake_edge_exits_nonzero_and_names_the_edge(tmp_path: Path) -> None:
    """报错的第一读者是下一次拆图的 LLM:它要能直接改图。"""
    bad = json.loads(json.dumps(GOOD))
    bad["nodes"][1]["needs"] = ["nothing-upstream.md"]

    result = validate(write(tmp_path, bad))

    assert result.exit_code == 1
    assert "fake-edge" in result.output
    assert "plan→order" in result.output
    assert "nothing-upstream.md" in result.output
    assert "Traceback" not in result.output


def test_a_write_conflict_names_the_globs(tmp_path: Path) -> None:
    bad = json.loads(json.dumps(GOOD))
    bad["nodes"][2]["write_scope"] = ["repos/order/src/*.py"]

    result = validate(write(tmp_path, bad))

    assert result.exit_code == 1
    assert "write-conflict" in result.output
    assert "repos/order/src/*.py" in result.output


def test_a_cycle_prints_the_path(tmp_path: Path) -> None:
    bad = json.loads(json.dumps(GOOD))
    bad["nodes"][0]["needs"] = ["order.diff"]
    bad["edges"].append(["order", "plan"])

    result = validate(write(tmp_path, bad))

    assert result.exit_code == 1
    assert "cycle" in result.output


def test_json_output_lists_the_issues(tmp_path: Path) -> None:
    bad = json.loads(json.dumps(GOOD))
    bad["nodes"][1]["needs"] = ["nothing-upstream.md"]

    payload = json.loads(validate(write(tmp_path, bad), "--json").output)

    assert payload["ok"] is False
    assert payload["issues"][0]["code"] == "fake-edge"
    assert payload["issues"][0]["where"] == "plan→order"


def test_an_unknown_field_is_refused_with_the_field_name(tmp_path: Path) -> None:
    """静默吞掉写错的键,等于让一个笔误变成"这个节点没声明写集"。"""
    bad = json.loads(json.dumps(GOOD))
    bad["nodes"][1]["write_scopes"] = ["repos/order/**"]

    result = validate(write(tmp_path, bad))

    assert result.exit_code == 1
    assert "write_scopes" in result.output
    assert "Traceback" not in result.output


def test_a_missing_file_fails_cleanly(tmp_path: Path) -> None:
    result = validate(tmp_path / "nope.yaml")

    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_a_checker_with_a_write_scope_is_refused(tmp_path: Path) -> None:
    """检查节点产出判定,不产出资产——它不该碰版本面。"""
    bad = json.loads(json.dumps(GOOD))
    bad["nodes"][2] = {
        "id": "review",
        "kind": "checker",
        "employee": "reviewer",
        "procedure": "code-critique",
        "needs": ["order.diff"],
        "produces": ["critique.json"],
        "write_scope": ["repos/order/**"],
    }
    bad["edges"] = [["plan", "order"], ["order", "review"]]

    result = validate(write(tmp_path, bad))

    assert result.exit_code == 1
    assert "checker-writes" in result.output
    assert "review" in result.output
