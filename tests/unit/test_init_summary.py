"""④ 汇总：契约去重与那份人要看的报告。

初始化跑完之后，人真正要做的只有两件事：复核系统没把握的地方，扫一眼那些被判定为
「不需要知识」的功能点。所以这两节是**单独列出来的**，不是散在各模块产出里等人自己翻。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentgenome import paths
from agentgenome.genome.deep_read import DeepReadResult, ModuleOutcome
from agentgenome.genome.loader import load_tree
from agentgenome.genome.models import Datastore, Interface, ProjectMap
from agentgenome.genome.summary import build_report, merge_contracts, render_report
from agentgenome.genome.tree import MODULE_MAP, write_tree

# --- 契约去重 ----------------------------------------------------------------


def test_the_two_sides_of_one_contract_become_one() -> None:
    """同一条契约会被 provider 与 consumer 两侧各上报一次。"""
    provider_side = Interface(id="reserve-api", kind="http", provider="inventory", consumers=[])
    consumer_side = Interface(
        id="reserve-api", kind="http", provider="inventory", consumers=["order"]
    )

    interfaces, _ = merge_contracts([([provider_side], []), ([consumer_side], [])])

    assert len(interfaces) == 1


def test_consumers_are_unioned_not_overwritten() -> None:
    """**后写覆盖前写的话会丢掉一半消费者**——而集成测试判定正是按契约找关联方的，
    丢了就判不出该跑集测。"""
    first = Interface(id="api", kind="http", provider="p", consumers=["a"])
    second = Interface(id="api", kind="http", provider="p", consumers=["b"])

    interfaces, _ = merge_contracts([([first], []), ([second], [])])

    assert sorted(interfaces[0].consumers) == ["a", "b"]


def test_datastores_are_deduplicated_too() -> None:
    store = Datastore(id="order-db", kind="postgres", owner="order")

    _, datastores = merge_contracts([([], [store]), ([], [store])])

    assert len(datastores) == 1


def test_the_merged_order_is_stable() -> None:
    """两次初始化给出不同顺序的话，知识 PR 的 diff 会充满假变更。"""
    args = [
        ([Interface(id="b", kind="http", provider="p")], []),
        ([Interface(id="a", kind="http", provider="p")], []),
    ]

    assert [item.id for item in merge_contracts(args)[0]] == ["a", "b"]


# --- 报告 --------------------------------------------------------------------

BASE = {
    "version": 1,
    "project": {"name": "mall"},
    "modules": [{"id": "order-service", "path": "repos/order-service/", "confidence": 0.3}],
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / paths.KNOWLEDGE).mkdir(parents=True)
    (tmp_path / "repos/order-service" / "src").mkdir(parents=True)
    write_tree(tmp_path, ProjectMap.model_validate(BASE))
    target = tmp_path / paths.MODULES / "order-service" / MODULE_MAP
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["features"] = [
        {
            "id": "glue",
            "summary": "胶水",
            "scope": ["repos/order-service/src/**"],
            "no_card": "纯转发，无隐含约定",
        }
    ]
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return tmp_path


def test_low_confidence_entries_are_singled_out(workspace: Path) -> None:
    report = build_report(load_tree(workspace), DeepReadResult(done=["order-service"]))

    assert [item.where for item in report.low_confidence] == ["order-service"]


def test_every_no_card_declaration_is_listed(workspace: Path) -> None:
    """校验挡得住空理由，挡不住「不需要」三个字——人扫一眼是唯一有效的补强。"""
    report = build_report(load_tree(workspace), DeepReadResult(done=["order-service"]))

    assert report.no_card_declarations[0]["feature_id"] == "glue"
    assert "纯转发" in report.no_card_declarations[0]["reason"]


def test_failed_modules_say_which_to_rerun(workspace: Path) -> None:
    """人要知道该重跑哪几个，而不是重跑全库。"""
    result = DeepReadResult(
        done=["order-service"], failed=[ModuleOutcome("inventory", ok=False, detail="超时")]
    )

    report = build_report(load_tree(workspace), result)

    assert report.modules_failed == [{"module_id": "inventory", "detail": "超时"}]


def test_the_human_adjustments_are_kept(workspace: Path) -> None:
    """「系统建议了什么」与「人改成了什么」——后者要出现在报告里。"""
    report = build_report(
        load_tree(workspace),
        DeepReadResult(done=["order-service"]),
        human_adjustments={"note": "两个域其实是一个"},
    )

    assert "两个域其实是一个" in render_report(report)


def test_the_two_actionable_sections_come_first(workspace: Path) -> None:
    """人真正要做的只有那两件事。排在模块清单后面的话，长报告里没人翻得到。"""
    text = render_report(build_report(load_tree(workspace), DeepReadResult(done=["order-service"])))

    assert text.index("需要复核") < text.index("## 模块")
    assert text.index("无需卡片") < text.index("## 模块")


def test_low_confidence_does_not_block(workspace: Path) -> None:
    """**低置信不阻塞。** 拦住整条初始化只会让人为了让它跑完而随手把置信度调高。"""
    report = build_report(load_tree(workspace), DeepReadResult(done=["order-service"]))

    assert report.low_confidence
    assert report.modules_done == ["order-service"]
