"""按代码路径把一次改动路由到功能点。

现在的切片按**模块**过滤:员工要改订单服务的退款流程,拿到的是这个模块全部功能点的全部知识。
模块粒度在两个模块的项目里够用,在一个有五十个功能点的模块里毫无意义——真正相关的那几十行
被淹没在里面。

**路由是纯函数**:输入进、命中出。不做 I/O、不看任务状态,所以它能被直接测。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentgenome import paths
from agentgenome.genome.loader import load_tree
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.routing import RouteInputs, RouteRecord, route
from agentgenome.genome.tree import MODULE_MAP, write_tree

BASE = {
    "version": 1,
    "project": {"name": "mall"},
    "modules": [{"id": "order-service", "path": "repos/order-service/"}],
}

CARD = "---\nid: {id}\nsummary: {summary}\n---\n细节。\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / paths.KNOWLEDGE).mkdir(parents=True)
    for relative in ("reserve", "refund", "payment"):
        (tmp_path / "repos/order-service" / "src" / "order" / relative).mkdir(parents=True)
    write_tree(tmp_path, ProjectMap.model_validate(BASE))
    _features(
        tmp_path,
        _feature("reserve-flow", "下单预占库存流程", "repos/order-service/src/order/reserve/**"),
        _feature("refund-flow", "退款流程", "repos/order-service/src/order/refund/**"),
        {
            "id": "payment-glue",
            "summary": "支付胶水",
            "scope": ["repos/order-service/src/order/payment/**"],
            "no_card": "纯转发,无隐含约定",
        },
    )
    for feature_id, summary in (("reserve-flow", "下单预占"), ("refund-flow", "退款")):
        _card(tmp_path, feature_id, summary)
    return tmp_path


def _feature(feature_id: str, summary: str, *scope: str) -> dict[str, object]:
    return {
        "id": feature_id,
        "summary": summary,
        "scope": list(scope),
        "card": f"features/{feature_id}.md",
    }


def _features(root: Path, *entries: dict[str, object]) -> None:
    target = root / paths.MODULES / "order-service" / MODULE_MAP
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["features"] = list(entries)
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def _card(root: Path, feature_id: str, summary: str) -> None:
    target = root / paths.MODULES / "order-service" / "features" / f"{feature_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(CARD.format(id=feature_id, summary=summary), encoding="utf-8")


def _ids(record: RouteRecord) -> list[str]:
    return sorted(hit.feature.id for hit in record.hits)


# --- 按路径命中 ---------------------------------------------------------------


def test_a_changed_file_finds_the_feature_covering_it(workspace: Path) -> None:
    tree = load_tree(workspace)

    record = route(tree, RouteInputs(paths=("repos/order-service/src/order/reserve/service.py",)))

    assert _ids(record) == ["reserve-flow"]


def test_an_unrelated_file_finds_nothing(workspace: Path) -> None:
    tree = load_tree(workspace)

    record = route(tree, RouteInputs(paths=("repos/order-service/README.md",)))

    assert record.hits == ()
    assert {item.id for item in record.missed} == {"reserve-flow", "refund-flow"}


def test_several_files_accumulate_their_hits(workspace: Path) -> None:
    tree = load_tree(workspace)

    record = route(
        tree,
        RouteInputs(
            paths=(
                "repos/order-service/src/order/reserve/a.py",
                "repos/order-service/src/order/refund/b.py",
            ),
        ),
    )

    assert _ids(record) == ["refund-flow", "reserve-flow"]


def test_a_declared_no_card_feature_never_shows_up(workspace: Path) -> None:
    """它已经声明过「这里不需要知识」,再给一行目录是噪声。"""
    tree = load_tree(workspace)

    record = route(tree, RouteInputs(paths=("repos/order-service/src/order/payment/gateway.py",)))

    assert record.hits == ()
    assert "payment-glue" not in {item.id for item in record.missed}


# --- 关键词兜底 ---------------------------------------------------------------


def test_a_keyword_matches_a_feature_id(workspace: Path) -> None:
    """第一轮还没有任何文件路径,只能靠需求解析给的关键词。"""
    tree = load_tree(workspace)

    record = route(tree, RouteInputs(keywords=("refund-flow",)))

    assert _ids(record) == ["refund-flow"]


def test_a_keyword_matches_a_summary(workspace: Path) -> None:
    tree = load_tree(workspace)

    record = route(tree, RouteInputs(keywords=("预占",)))

    assert _ids(record) == ["reserve-flow"]


# --- 三个输入来源 -------------------------------------------------------------


def test_the_failure_report_paths_widen_the_hits(workspace: Path) -> None:
    """**这一层最大的实际收益。** 门禁告诉你哪个文件炸了,路由就把覆盖那个文件的知识
    捞到员工面前。"""
    tree = load_tree(workspace)
    before = route(tree, RouteInputs(paths=("repos/order-service/src/order/reserve/a.py",)))

    after = route(
        tree,
        RouteInputs(
            paths=("repos/order-service/src/order/reserve/a.py",),
            failure_paths=("repos/order-service/src/order/refund/b.py",),
        ),
    )

    assert _ids(before) == ["reserve-flow"]
    assert _ids(after) == ["refund-flow", "reserve-flow"]


def test_suspect_files_from_the_itest_report_count_too(workspace: Path) -> None:
    tree = load_tree(workspace)

    record = route(tree, RouteInputs(failure_paths=("repos/order-service/src/order/refund/b.py",)))

    assert _ids(record) == ["refund-flow"]


def test_the_three_sources_are_merged_and_deduplicated(workspace: Path) -> None:
    """它们指向的是同一个问题的不同侧面,不分优先级。"""
    tree = load_tree(workspace)

    record = route(
        tree,
        RouteInputs(
            paths=("repos/order-service/src/order/reserve/a.py",),
            failure_paths=("repos/order-service/src/order/reserve/a.py",),
            keywords=("reserve-flow",),
        ),
    )

    assert _ids(record) == ["reserve-flow"]


# --- 路由记录:为什么没拿到那张卡片 --------------------------------------------


def test_the_record_says_which_path_matched_which_glob(workspace: Path) -> None:
    tree = load_tree(workspace)

    record = route(tree, RouteInputs(paths=("repos/order-service/src/order/reserve/service.py",)))

    reason = record.hits[0].reason
    assert "repos/order-service/src/order/reserve/service.py" in reason
    assert "repos/order-service/src/order/reserve/**" in reason


def test_the_record_lists_what_was_missed(workspace: Path) -> None:
    tree = load_tree(workspace)

    record = route(tree, RouteInputs(paths=("repos/order-service/src/order/reserve/a.py",)))

    assert [item.id for item in record.missed] == ["refund-flow"]


def test_an_empty_input_is_not_the_same_as_a_missed_input(workspace: Path) -> None:
    """两者都表现为零命中,但前者是正常的第一轮,后者是路由或 scope 写错了。
    不分开的话,一次静默失败长得和正常情况一模一样。"""
    tree = load_tree(workspace)

    nothing_to_route = route(tree, RouteInputs())
    routed_but_missed = route(tree, RouteInputs(paths=("repos/order-service/README.md",)))

    assert nothing_to_route.had_no_input
    assert not routed_but_missed.had_no_input


def test_a_path_that_is_not_workspace_relative_is_called_out(workspace: Path) -> None:
    """绝对路径会静默零命中,而零命中和「确实没有相关知识」在结果上一模一样。"""
    tree = load_tree(workspace)

    record = route(tree, RouteInputs(paths=("/abs/repos/order-service/src/order/reserve/a.py",)))

    assert record.suspicious_paths == ("/abs/repos/order-service/src/order/reserve/a.py",)


def test_the_record_serialises_for_the_artifact_directory(workspace: Path) -> None:
    """「为什么这个任务没拿到那张卡片」的唯一答案来源,要能落盘。"""
    tree = load_tree(workspace)

    payload = route(
        tree, RouteInputs(paths=("repos/order-service/src/order/reserve/a.py",))
    ).as_dict()

    assert payload["hits"][0]["feature_id"] == "reserve-flow"
    assert payload["hits"][0]["module_id"] == "order-service"
    assert payload["missed"] == ["refund-flow"]
    assert payload["had_no_input"] is False
