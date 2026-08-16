"""行数预算、分形递归与碎片化告警。

分层解决了"一个文件装不下",没解决"某一层自己膨胀"。一个有两百个功能点的模块,它的模块地图
会把单文件地图的全部问题重演一遍,只是换了个位置。

**预算是门禁项,不是建议。** 超限的知识变更不予通过——而且报错要说清楚是哪个文件超了多少行,
不然收到拒绝的架构员工只知道"不让过",不知道该拆哪里。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentgenome import paths
from agentgenome.config import KnowledgeConfig
from agentgenome.genome.budgets import check_budgets, fragmentation_warnings
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.loader import load_tree
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.tree import MODULE_MAP, write_tree

BASE = {
    "version": 1,
    "project": {"name": "mall"},
    "modules": [{"id": "order-service", "path": "repos/order-service/"}],
}

CARD = """\
---
id: {id}
scope: ["repos/order-service/src/order/reserve/**"]
summary: 下单预占
---
细节。
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / paths.KNOWLEDGE).mkdir(parents=True)
    (tmp_path / "repos/order-service" / "src" / "order" / "reserve").mkdir(parents=True)
    write_tree(tmp_path, ProjectMap.model_validate(BASE))
    return tmp_path


def _module_map(root: Path, *features: dict[str, object], **extra: object) -> None:
    target = root / paths.MODULES / "order-service" / MODULE_MAP
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["features"] = list(features)
    payload.update(extra)
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def _card(root: Path, relative: str, feature_id: str, extra_lines: int = 0) -> None:
    target = root / paths.MODULES / "order-service" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    text = CARD.format(id=feature_id) + "填充。\n" * extra_lines
    target.write_text(text, encoding="utf-8")


def _entry(feature_id: str, card: str) -> dict[str, object]:
    return {
        "id": feature_id,
        "summary": feature_id,
        "scope": ["repos/order-service/src/order/reserve/**"],
        "card": card,
    }


TIGHT = KnowledgeConfig(
    root_index_lines=200, module_map_lines=200, card_lines=200, fragmentation_threshold=3
)


# --- 预算 --------------------------------------------------------------------


def test_a_tree_inside_its_budget_passes(workspace: Path) -> None:
    _module_map(workspace, _entry("reserve-flow", "features/reserve-flow.md"))
    _card(workspace, "features/reserve-flow.md", "reserve-flow")

    assert check_budgets(workspace, load_tree(workspace), TIGHT) == []


def test_an_oversized_card_is_caught_and_says_by_how_much(workspace: Path) -> None:
    """只说"不让过"的话,收到拒绝的人不知道该拆哪里。"""
    _module_map(workspace, _entry("reserve-flow", "features/reserve-flow.md"))
    _card(workspace, "features/reserve-flow.md", "reserve-flow", extra_lines=50)

    issues = check_budgets(
        workspace, load_tree(workspace), TIGHT.model_copy(update={"card_lines": 10})
    )

    assert len(issues) == 1
    assert "reserve-flow.md" in issues[0].file
    assert "10" in issues[0].message
    assert "56" in issues[0].message


def test_an_oversized_root_index_is_caught(workspace: Path) -> None:
    _module_map(workspace, _entry("reserve-flow", "features/reserve-flow.md"))
    _card(workspace, "features/reserve-flow.md", "reserve-flow")

    issues = check_budgets(
        workspace, load_tree(workspace), TIGHT.model_copy(update={"root_index_lines": 1})
    )

    assert any(str(paths.PROJECT_MAP) in issue.file for issue in issues)


def test_an_oversized_module_map_is_caught(workspace: Path) -> None:
    _module_map(workspace, _entry("reserve-flow", "features/reserve-flow.md"))
    _card(workspace, "features/reserve-flow.md", "reserve-flow")

    issues = check_budgets(
        workspace, load_tree(workspace), TIGHT.model_copy(update={"module_map_lines": 1})
    )

    assert any("order-service" in issue.file and MODULE_MAP in issue.file for issue in issues)


def test_sitting_exactly_on_the_limit_passes(workspace: Path) -> None:
    _module_map(workspace, _entry("reserve-flow", "features/reserve-flow.md"))
    _card(workspace, "features/reserve-flow.md", "reserve-flow")
    lines = len(
        (workspace / paths.MODULES / "order-service" / "features" / "reserve-flow.md")
        .read_text(encoding="utf-8")
        .splitlines()
    )

    limits = TIGHT.model_copy(update={"card_lines": lines})

    assert not [
        issue
        for issue in check_budgets(workspace, load_tree(workspace), limits)
        if "features" in issue.file
    ]


# --- 分形 --------------------------------------------------------------------


def test_a_submap_contributes_its_features(workspace: Path) -> None:
    """超大模块继续拆:子目录加一份子地图,规则原样递归,不为它新增例外。"""
    _module_map(workspace, submaps=["features/payment/map.yaml"])
    submap = workspace / paths.MODULES / "order-service" / "features" / "payment"
    submap.mkdir(parents=True)
    (submap / "map.yaml").write_text(
        yaml.safe_dump(
            {"features": [_entry("checkout", "checkout.md")]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    _card(workspace, "features/payment/checkout.md", "checkout")

    tree = load_tree(workspace)

    assert [item.id for item in tree.features("order-service")] == ["checkout"]
    assert tree.card("order-service", "checkout") is not None


def test_completeness_applies_inside_a_submap_too(workspace: Path) -> None:
    _module_map(workspace, submaps=["features/payment/map.yaml"])
    submap = workspace / paths.MODULES / "order-service" / "features" / "payment"
    submap.mkdir(parents=True)
    (submap / "map.yaml").write_text(
        yaml.safe_dump(
            {"features": [{"id": "checkout", "summary": "x", "scope": ["repos/order-service/**"]}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert "checkout" in caught.value.render()


def test_a_card_owned_by_a_submap_is_not_an_orphan(workspace: Path) -> None:
    """孤儿判定按模块整棵子树算。只看顶层的话,分形之后每张卡片都会被误报。"""
    _module_map(workspace, submaps=["features/payment/map.yaml"])
    submap = workspace / paths.MODULES / "order-service" / "features" / "payment"
    submap.mkdir(parents=True)
    (submap / "map.yaml").write_text(
        yaml.safe_dump({"features": [_entry("checkout", "checkout.md")]}, allow_unicode=True),
        encoding="utf-8",
    )
    _card(workspace, "features/payment/checkout.md", "checkout")

    assert load_tree(workspace).features("order-service")


def test_a_dangling_submap_pointer_is_caught(workspace: Path) -> None:
    _module_map(workspace, submaps=["features/gone/map.yaml"])

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert "features/gone/map.yaml" in caught.value.render()


def test_a_submap_is_subject_to_the_module_map_budget(workspace: Path) -> None:
    _module_map(workspace, submaps=["features/payment/map.yaml"])
    submap = workspace / paths.MODULES / "order-service" / "features" / "payment"
    submap.mkdir(parents=True)
    (submap / "map.yaml").write_text(
        yaml.safe_dump({"features": [_entry("checkout", "checkout.md")]}, allow_unicode=True),
        encoding="utf-8",
    )
    _card(workspace, "features/payment/checkout.md", "checkout")

    issues = check_budgets(
        workspace, load_tree(workspace), TIGHT.model_copy(update={"module_map_lines": 1})
    )

    assert any("payment" in issue.file for issue in issues)


# --- 碎片化告警 --------------------------------------------------------------


def test_within_the_threshold_there_is_no_warning(workspace: Path) -> None:
    _module_map(
        workspace,
        *[_entry(f"f{i}", f"features/f{i}.md") for i in range(3)],
    )
    for i in range(3):
        _card(workspace, f"features/f{i}.md", f"f{i}")

    assert fragmentation_warnings(load_tree(workspace), TIGHT) == []


def test_above_the_threshold_a_warning_names_the_path(workspace: Path) -> None:
    """知识被切碎了。切碎有时是对的,但它应该是被注意到的。"""
    _module_map(
        workspace,
        *[_entry(f"f{i}", f"features/f{i}.md") for i in range(4)],
    )
    for i in range(4):
        _card(workspace, f"features/f{i}.md", f"f{i}")

    warnings = fragmentation_warnings(load_tree(workspace), TIGHT)

    assert len(warnings) == 1
    assert warnings[0].scope_prefix == "repos/order-service/src/order/reserve"
    assert warnings[0].count == 4


def test_a_fragmentation_warning_does_not_fail_validation(workspace: Path) -> None:
    """只提示不拦截:切碎有时是对的。"""
    _module_map(
        workspace,
        *[_entry(f"f{i}", f"features/f{i}.md") for i in range(4)],
    )
    for i in range(4):
        _card(workspace, f"features/f{i}.md", f"f{i}")

    assert load_tree(workspace).features("order-service")
    assert check_budgets(workspace, load_tree(workspace), TIGHT) == []


# --- 评审抓到的:预算逃逸、越界子地图、两个碎片化反例 -------------------------


def test_a_submap_outside_the_features_directory_is_still_budgeted(workspace: Path) -> None:
    """**这是门禁的逃逸口。** 靠"扫 features/ 目录"找子地图的话,把内容挪进别处的子地图
    就成了绕过行数门禁的办法——而门禁要拦的正是"某一层膨胀"。"""
    _module_map(workspace, submaps=["sub/map.yaml"])
    sub = workspace / paths.MODULES / "order-service" / "sub"
    sub.mkdir(parents=True)
    (sub / "map.yaml").write_text(
        yaml.safe_dump({"features": [_entry("checkout", "checkout.md")]}, allow_unicode=True)
        + "# 填充\n" * 300,
        encoding="utf-8",
    )
    _card(workspace, "sub/checkout.md", "checkout")

    issues = check_budgets(
        workspace, load_tree(workspace), TIGHT.model_copy(update={"module_map_lines": 10})
    )

    assert any("sub" in issue.file for issue in issues), "子地图逃过了行数预算"


def test_a_submap_pointing_outside_the_module_is_a_validation_error_not_a_crash(
    workspace: Path,
) -> None:
    """这是员工可写的内容。不拦的话报错变成一段堆栈，而不是一条能改的校验问题。"""
    _module_map(workspace, submaps=["../../../../elsewhere/map.yaml"])

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert "模块目录之外" in caught.value.render()


def test_four_cards_on_one_directory_are_reported_even_with_a_file_glob(
    workspace: Path,
) -> None:
    """漏报的那个反例：`src/order/*.py` 匹配不上 `src/order` 本身（它要求 .py 结尾），
    于是四张卡片压在同一个目录上却一声不吭。"""
    scope = ["repos/order-service/src/order/reserve/*.py"]
    _module_map(
        workspace,
        *[
            {"id": f"f{i}", "summary": f"f{i}", "scope": scope, "card": f"features/f{i}.md"}
            for i in range(4)
        ],
    )
    for i in range(4):
        _card(workspace, f"features/f{i}.md", f"f{i}")

    warnings = fragmentation_warnings(load_tree(workspace), TIGHT)

    assert [w.count for w in warnings] == [4]


def test_coarse_and_fine_layering_is_not_reported_as_fragmentation(workspace: Path) -> None:
    """误报的那个反例：一条模块级覆盖加三条叶子覆盖。把模块级卡片并进叶子卡片恰恰是反的，
    而粗细分层正是这棵树鼓励的写法。"""
    _module_map(
        workspace,
        {
            "id": "whole",
            "summary": "模块级",
            "scope": ["repos/order-service/**"],
            "card": "features/whole.md",
        },
        *[
            {
                "id": f"f{i}",
                "summary": f"f{i}",
                "scope": ["repos/order-service/src/order/reserve/**"],
                "card": f"features/f{i}.md",
            }
            for i in range(3)
        ],
    )
    _card(workspace, "features/whole.md", "whole")
    for i in range(3):
        _card(workspace, f"features/f{i}.md", f"f{i}")

    assert fragmentation_warnings(load_tree(workspace), TIGHT) == []


def test_declared_no_card_features_are_not_counted_as_fragmentation(workspace: Path) -> None:
    """声明了「无需卡片」的地方没有知识可切，把它算进来等于在零张卡片上报「切碎了」。"""
    _module_map(
        workspace,
        *[
            {
                "id": f"f{i}",
                "summary": f"f{i}",
                "scope": ["repos/order-service/src/order/reserve/**"],
                "no_card": "纯 CRUD",
            }
            for i in range(5)
        ],
    )

    assert fragmentation_warnings(load_tree(workspace), TIGHT) == []
