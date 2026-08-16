"""knowledge-deepen 工序:单功能点三源深化(PRD 41)。

这一组钉四条缝:

- **队列是纯函数**:churn 降序、`no_card` 不入队、人工定稿不入队——输入是树与计数,
  不做 I/O,与 `routing.route` 同一个测试哲学;
- **staging 只许一张卡**:多写的文件按越界拒绝(同 PRD 34 focus 语义),锚点校验与
  行数预算走 staging 同一套规则;
- **应用尊重人工编辑**:带标记的卡不被覆盖;
- **提示词上下文有界**:只有该功能点的 scope、测试与提交历史指引,无全库内容。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome import paths
from agentgenome.genome.deepen import (
    apply_deepened,
    build_prompt,
    deepen_output_check,
    deepen_queue,
    validate_deepen_staging,
)
from agentgenome.genome.loader import load_tree
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.staging import HUMAN_EDITED_MARKER
from agentgenome.genome.tree import module_dir, write_tree

BASE = {
    "version": 1,
    "project": {"name": "mall"},
    "modules": [
        {"id": "order-service", "path": "repos/order-service/"},
        {"id": "inventory-service", "path": "repos/inventory-service/"},
    ],
}

FEATURES = {
    "order-service": [
        {
            "id": "place-order",
            "summary": "下单",
            "scope": ["repos/order-service/src/order/**"],
            "card": "features/place-order.md",
        },
        {
            "id": "refund",
            "summary": "退款",
            "scope": ["repos/order-service/src/refund/**"],
            "card": "features/refund.md",
        },
        {
            "id": "migrations",
            "summary": "迁移脚本",
            "scope": ["repos/order-service/migrations/**"],
            "no_card": "读代码比读卡片快",
        },
    ]
}

CARD = "---\nid: {id}\nconfidence: high\n---\n\n{body}\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / paths.KNOWLEDGE).mkdir(parents=True)
    for repo, sub in (
        ("order-service", "src/order"),
        ("order-service", "src/refund"),
        ("order-service", "migrations"),
        ("inventory-service", "src"),
    ):
        (tmp_path / "repos" / repo / sub).mkdir(parents=True, exist_ok=True)
    write_tree(tmp_path, ProjectMap.model_validate(BASE), features=dict(FEATURES))
    directory = module_dir(tmp_path, "order-service") / "features"
    directory.mkdir(parents=True, exist_ok=True)
    for feature_id in ("place-order", "refund"):
        (directory / f"{feature_id}.md").write_text(
            CARD.format(id=feature_id, body="薄卡。"), encoding="utf-8"
        )
    return tmp_path


# --- 队列 ---------------------------------------------------------------------


def test_the_queue_is_churn_descending_and_skips_no_card(workspace: Path) -> None:
    counts = {
        "repos/order-service/src/order/app.py": 1,
        "repos/order-service/src/refund/flow.py": 5,
        "repos/order-service/migrations/0001.sql": 99,
    }

    queue = deepen_queue(load_tree(workspace), counts)

    assert [(item.module_id, item.feature_id) for item in queue] == [
        ("order-service", "refund"),
        ("order-service", "place-order"),
    ]
    assert queue[0].churn == 5


def test_a_human_edited_card_is_not_queued(workspace: Path) -> None:
    """人工定稿的卡深化了也不会被应用,入队只会白烧一个作业。"""
    target = module_dir(workspace, "order-service") / "features" / "refund.md"
    target.write_text(
        CARD.format(id="refund", body=f"{HUMAN_EDITED_MARKER}\n人定的稿。"), encoding="utf-8"
    )

    queue = deepen_queue(load_tree(workspace), {"repos/order-service/src/refund/flow.py": 5})

    assert [(item.module_id, item.feature_id) for item in queue] == [
        ("order-service", "place-order"),
    ]


# --- staging 校验 --------------------------------------------------------------


def _staging(workspace: Path) -> Path:
    target = workspace / "tasks" / "gn-1" / "artifacts" / "staging"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write(staging: Path, relative: str, text: str) -> None:
    target = staging / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_a_single_deepened_card_passes(workspace: Path) -> None:
    staging = _staging(workspace)
    _write(
        staging,
        "modules/order-service/features/place-order.md",
        CARD.format(id="place-order", body="- **承重不变量**:`src/order/app.py:1` 不为负。"),
    )
    (workspace / "repos/order-service/src/order/app.py").write_text("x = 1\n", encoding="utf-8")

    issues = validate_deepen_staging(
        workspace, staging, "order-service", "place-order", "repos/order-service/"
    )

    assert issues == []


def test_a_second_feature_file_is_refused_as_out_of_bounds(workspace: Path) -> None:
    staging = _staging(workspace)
    _write(
        staging,
        "modules/order-service/features/place-order.md",
        CARD.format(id="place-order", body="不变量。"),
    )
    _write(
        staging,
        "modules/order-service/features/refund.md",
        CARD.format(id="refund", body="越界的卡。"),
    )

    issues = validate_deepen_staging(
        workspace, staging, "order-service", "place-order", "repos/order-service/"
    )

    assert len(issues) == 1
    assert "refund.md" in issues[0].file
    assert "越界" in issues[0].message


def test_an_empty_staging_is_no_output(workspace: Path) -> None:
    issues = validate_deepen_staging(
        workspace, _staging(workspace), "order-service", "place-order", "repos/order-service/"
    )

    assert any("没有产物" in item.message for item in issues)


def test_a_fake_anchor_in_a_deepened_card_is_refused(workspace: Path) -> None:
    """深化的卡走 staging 同一套锚点规则——通道不同,纪律相同。"""
    staging = _staging(workspace)
    _write(
        staging,
        "modules/order-service/features/place-order.md",
        CARD.format(id="place-order", body="- 不变量:`src/order/ghost.py:9` 恒真。"),
    )

    issues = validate_deepen_staging(
        workspace, staging, "order-service", "place-order", "repos/order-service/"
    )

    assert any("引用的仓内路径不存在" in item.message for item in issues)


def test_the_output_check_closure_renders_refusals(workspace: Path) -> None:
    check = deepen_output_check(workspace, "order-service", "place-order", "repos/order-service/")

    verdict = check(workspace / "tasks" / "gn-1" / "artifacts")

    assert verdict is not None
    assert "没有产物" in verdict


# --- 应用 ---------------------------------------------------------------------


def test_apply_writes_the_card_and_reports_the_path(workspace: Path) -> None:
    outcome = apply_deepened(
        workspace,
        "order-service",
        "place-order",
        CARD.format(id="place-order", body="深化后的不变量。"),
    )

    assert outcome.written
    target = workspace / outcome.path
    assert "深化后的不变量" in target.read_text(encoding="utf-8")


def test_apply_preserves_a_human_edited_card(workspace: Path) -> None:
    target = module_dir(workspace, "order-service") / "features" / "place-order.md"
    human = CARD.format(id="place-order", body=f"{HUMAN_EDITED_MARKER}\n人定的稿。")
    target.write_text(human, encoding="utf-8")

    outcome = apply_deepened(
        workspace, "order-service", "place-order", CARD.format(id="place-order", body="机写的。")
    )

    assert not outcome.written
    assert target.read_text(encoding="utf-8") == human


# --- 提示词 --------------------------------------------------------------------


def test_the_prompt_is_bounded_to_one_feature(workspace: Path) -> None:
    tree = load_tree(workspace)
    entry = next(f for f in tree.features("order-service") if f.id == "place-order")

    prompt = build_prompt(
        module_id="order-service",
        module_path="repos/order-service/",
        feature=entry,
        current_card=CARD.format(id="place-order", body="薄卡。"),
        workspace_root=workspace,
        output_dir=workspace / "tasks" / "gn-1" / "artifacts",
        test_cmd="pytest -q",
    )

    assert "repos/order-service/src/order/**" in prompt
    assert "log -p -10" in prompt
    assert "只允许" in prompt and "place-order.md" in prompt
    # 上下文有界:另一个模块、另一个功能点都不出现。
    assert "inventory-service" not in prompt
    assert "refund" not in prompt


def test_the_prompt_carries_the_card_discipline(workspace: Path) -> None:
    tree = load_tree(workspace)
    entry = next(f for f in tree.features("order-service") if f.id == "place-order")

    prompt = build_prompt(
        module_id="order-service",
        module_path="repos/order-service/",
        feature=entry,
        current_card="",
        workspace_root=workspace,
        output_dir=workspace / "tasks" / "gn-1" / "artifacts",
        test_cmd="pytest -q",
    )

    assert "承重不变量" in prompt
    assert "描述行为的句子删掉" in prompt
    assert "路径:行号" in prompt
