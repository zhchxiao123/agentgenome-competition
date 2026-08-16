"""knowledge-deepen:架构员工按三源法深化单个功能点(PRD 41)。

走回放运行时 + mall 夹具——真实的 git、真实的知识树加载器、真实的 staging 校验,
唯独 Agent 那一段是确定性的。钉三条:深化全程走通且只写那一张卡;越界产出被拒;
人工定稿不被覆盖。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.core.genome_task import GenomeTaskStore
from agentgenome.genome.staging import HUMAN_EDITED_MARKER
from tests.fixtures.knowledge_staging import build_staging, card, receipt_json
from tests.fixtures.mall import materialize_mall

runner = CliRunner()

DEEPENED = """\
---
id: place-order
confidence: high
---

下单主链路的入口。

- **承重不变量**:`src/order/app.py` 里的余额扣减先于库存预占。
- **测试证据**:见模块测试对下单顺序的断言。
- **坑点**:曾因先预占后扣减造成超卖(修复见提交历史)。
"""


def _record_init(library: Path) -> None:
    staging = build_staging(
        [
            {
                "id": "order-service",
                "summary": "订单域",
                "map": {"entrypoints": ["src/order/app.py"]},
                "overview": "# order-service\n\n订单域。\n",
                "features": [
                    {
                        "id": "place-order",
                        "summary": "下单",
                        "scope": ["repos/order-service/src/**"],
                        "card_text": card("place-order", "薄卡:时序与坑点。"),
                    }
                ],
            },
            {
                "id": "inventory-service",
                "summary": "库存域",
                "overview": "# inventory-service\n\n库存域。\n",
                "features": [
                    {
                        "id": "reserve",
                        "summary": "预占",
                        "scope": ["repos/inventory-service/src/**"],
                        "no_card": "薄壳,读代码更快",
                    }
                ],
            },
        ]
    )
    directory = library / "arch-employee__knowledge-init__r1"
    if directory.is_dir():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for relative, content in staging.items():
        target = directory / "outputs" / "staging" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (directory / "result.json").write_text(receipt_json(), encoding="utf-8")


def _record_deepen(library: Path, files: dict[str, str]) -> None:
    directory = library / "arch-employee__knowledge-deepen__order-service.place-order__r1"
    if directory.is_dir():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for relative, content in files.items():
        target = directory / "outputs" / "staging" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (directory / "result.json").write_text(
        receipt_json(task_id="knowledge-deepen"), encoding="utf-8"
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
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
    _record_init(tmp_path / "lib")
    result = runner.invoke(
        app, ["knowledge", "init", "--workspace", str(root), "--runtime", "replay"]
    )
    assert result.exit_code == 0, result.output
    return root


def _deepen(workspace: Path, *extra: str):
    return runner.invoke(
        app,
        [
            "knowledge",
            "deepen",
            "--workspace",
            str(workspace),
            "--feature",
            "order-service/place-order",
            "--runtime",
            "replay",
            *extra,
        ],
    )


def test_deepen_thickens_the_card_end_to_end(workspace: Path, tmp_path: Path) -> None:
    _record_deepen(tmp_path / "lib", {"modules/order-service/features/place-order.md": DEEPENED})

    result = _deepen(workspace)

    assert result.exit_code == 0, result.output
    target = workspace / "genome/knowledge/modules/order-service/features/place-order.md"
    text = target.read_text(encoding="utf-8")
    assert "承重不变量" in text
    assert "薄卡" not in text
    # 深化建的是一条真正的基因组任务,种类是 deepen。
    kinds = {record.kind.value for record in GenomeTaskStore(workspace).all_tasks()}
    assert "deepen" in kinds


def test_an_out_of_bounds_second_card_fails_the_job(workspace: Path, tmp_path: Path) -> None:
    _record_deepen(
        tmp_path / "lib",
        {
            "modules/order-service/features/place-order.md": DEEPENED,
            "modules/order-service/features/refund.md": card("refund", "越界的卡。"),
        },
    )

    result = _deepen(workspace)

    assert result.exit_code != 0
    assert "越界" in result.output
    # 树上那张薄卡原样未动——失败的深化一个字节都不该写进基因组。
    target = workspace / "genome/knowledge/modules/order-service/features/place-order.md"
    assert "薄卡" in target.read_text(encoding="utf-8")


def test_deepen_preserves_a_human_edited_card(workspace: Path, tmp_path: Path) -> None:
    target = workspace / "genome/knowledge/modules/order-service/features/place-order.md"
    human = f"---\nid: place-order\nconfidence: high\n---\n\n{HUMAN_EDITED_MARKER}\n人定的稿。\n"
    target.write_text(human, encoding="utf-8")
    _record_deepen(tmp_path / "lib", {"modules/order-service/features/place-order.md": DEEPENED})

    result = _deepen(workspace)

    assert result.exit_code == 0, result.output
    assert "保留人工编辑" in result.output
    assert target.read_text(encoding="utf-8") == human


def test_a_successful_deepen_clears_the_card_suspects(workspace: Path, tmp_path: Path) -> None:
    """改卡是可疑账的另一种响应:深化应用成功,这张卡的可疑一起清,留痕带任务号。"""
    from agentgenome.genome.suspects import (
        Suspect,
        SuspectKind,
        pending_suspects,
        record_suspects,
        resolutions,
    )

    record_suspects(
        workspace,
        (
            Suspect(
                kind=SuspectKind.STALE,
                task_id="ag-earlier",
                card="order-service/place-order",
                changed=("repos/order-service/src/order/app.py",),
            ),
        ),
    )
    _record_deepen(tmp_path / "lib", {"modules/order-service/features/place-order.md": DEEPENED})

    result = _deepen(workspace)

    assert result.exit_code == 0, result.output
    assert pending_suspects(workspace) == ()
    trace = resolutions(workspace)
    assert trace and trace[0]["action"] == "updated"
    assert "深化于 gn-" in trace[0]["note"]
