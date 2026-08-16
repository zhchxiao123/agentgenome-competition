"""功能点与功能卡片:知识树的第三层,以及「完备」这条不变式。

模块地图能答"订单服务是干什么的",答不了"下单预占库存这个流程怎么走、超时了会发生什么、
以前在哪踩过坑"——而后者才是员工真正需要的东西。

**完备的含义不是写得好,是地图上没有未知区域**(见 ADR-0003)。每个功能点要么有一张卡片,
要么有一条带理由的「无需卡片」声明。「这里没知识」是缺口;「这里不需要知识」是一个被记录
下来的判断。两者必须能分开,而分开的唯一办法就是逼人把后者写出来。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentgenome import paths
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.features import features_covering, no_card_declarations
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
id: reserve-flow
scope: ["repos/order-service/src/order/reserve/**"]
summary: "下单预占:order 调 inventory 的 reserve-api,超时走补偿"
confidence: high
hits: 17
links: [decisions/ADR-0007.md]
updated_at: 2026-09-01
---
时序、坑点、不变量、测试要点。
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / paths.KNOWLEDGE).mkdir(parents=True)
    (tmp_path / "repos/order-service" / "src" / "order" / "reserve").mkdir(parents=True)
    (tmp_path / "repos/order-service" / "src" / "order" / "refund").mkdir(parents=True)
    write_tree(tmp_path, ProjectMap.model_validate(BASE))
    return tmp_path


def _features(root: Path, *entries: dict[str, object]) -> None:
    target = root / paths.MODULES / "order-service" / MODULE_MAP
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["features"] = list(entries)
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def _card(root: Path, name: str, text: str = CARD) -> None:
    target = root / paths.MODULES / "order-service" / "features" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


RESERVE = {
    "id": "reserve-flow",
    "summary": "下单预占库存流程",
    "scope": ["repos/order-service/src/order/reserve/**"],
    "card": "features/reserve-flow.md",
}


# --- 完备性:ADR-0003 的可执行形式 -------------------------------------------


def test_a_feature_with_a_card_loads(workspace: Path) -> None:
    _features(workspace, RESERVE)
    _card(workspace, "reserve-flow.md")

    tree = load_tree(workspace)

    feature = tree.features("order-service")[0]
    assert feature.id == "reserve-flow"
    assert feature.card is not None


def test_a_feature_with_neither_card_nor_declaration_is_refused(workspace: Path) -> None:
    """这是缺口。地图上不允许存在缺口。"""
    _features(
        workspace,
        {
            "id": "refund-flow",
            "summary": "退款",
            "scope": ["repos/order-service/src/order/refund/**"],
        },
    )

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert "refund-flow" in caught.value.render()


def test_a_declaration_without_a_reason_is_refused(workspace: Path) -> None:
    """至少要求写下一句话,不是随手勾一下。"""
    _features(
        workspace,
        {
            "id": "refund-flow",
            "summary": "退款",
            "scope": ["repos/order-service/src/order/refund/**"],
            "no_card": "",
        },
    )

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert "理由" in caught.value.render()


def test_giving_both_a_card_and_a_declaration_is_refused(workspace: Path) -> None:
    """两个都给说明作者自己也没想清楚这里到底有没有知识。"""
    _features(
        workspace,
        RESERVE | {"no_card": "纯 CRUD"},
    )
    _card(workspace, "reserve-flow.md")

    with pytest.raises(GenomeValidationError):
        load_tree(workspace)


def test_a_declared_no_card_feature_loads(workspace: Path) -> None:
    _features(
        workspace,
        {
            "id": "refund-flow",
            "summary": "退款",
            "scope": ["repos/order-service/src/order/refund/**"],
            "no_card": "纯 CRUD,scope 内无隐含约定",
        },
    )

    tree = load_tree(workspace)

    assert tree.features("order-service")[0].no_card


def test_every_no_card_declaration_can_be_listed_for_review(workspace: Path) -> None:
    """校验只挡得住空理由,挡不住「不需要」三个字。人扫一眼是唯一有效的补强。"""
    _features(
        workspace,
        RESERVE,
        {
            "id": "refund-flow",
            "summary": "退款",
            "scope": ["repos/order-service/src/order/refund/**"],
            "no_card": "纯 CRUD",
        },
    )
    _card(workspace, "reserve-flow.md")

    declared = no_card_declarations(load_tree(workspace))

    assert [(item.module_id, item.feature.id) for item in declared] == [
        ("order-service", "refund-flow")
    ]


# --- 引用完整性 --------------------------------------------------------------


def test_a_dangling_card_pointer_is_caught(workspace: Path) -> None:
    _features(workspace, RESERVE)

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert "reserve-flow.md" in caught.value.render()


def test_an_orphan_card_is_caught(workspace: Path) -> None:
    """没人引用的卡片是知识树里的死文件——它不会被路由到,却会被人读到。"""
    _features(
        workspace,
        {
            "id": "refund-flow",
            "summary": "退款",
            "scope": ["repos/order-service/src/order/refund/**"],
            "no_card": "纯 CRUD",
        },
    )
    _card(workspace, "nobody-points-here.md")

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert "nobody-points-here.md" in caught.value.render()


def test_a_scope_pointing_at_code_that_is_gone_is_caught(workspace: Path) -> None:
    _features(workspace, RESERVE | {"scope": ["repos/order-service/src/order/deleted/**"]})
    _card(workspace, "reserve-flow.md")

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert "repos/order-service/src/order/deleted/**" in caught.value.render()


def test_all_problems_are_reported_in_one_go(workspace: Path) -> None:
    _features(
        workspace,
        {"id": "a", "summary": "a", "scope": ["repos/order-service/src/order/reserve/**"]},
        {
            "id": "b",
            "summary": "b",
            "scope": ["repos/order-service/src/order/refund/**"],
            "no_card": "",
        },
    )
    _card(workspace, "orphan.md")

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert len(caught.value.issues) >= 3


# --- 卡片本身 ----------------------------------------------------------------


def test_the_card_carries_its_identity_and_routing_information(workspace: Path) -> None:
    _features(workspace, RESERVE)
    _card(workspace, "reserve-flow.md")

    card = load_tree(workspace).card("order-service", "reserve-flow")

    assert card is not None
    assert card.summary.startswith("下单预占")
    assert card.confidence == "high"
    assert card.hits == 17
    assert card.links == ["decisions/ADR-0007.md"]
    assert "时序、坑点" in card.body


def test_a_card_whose_id_disagrees_with_its_feature_is_caught(workspace: Path) -> None:
    """id 对不上意味着有人搬过文件而忘了改指针,而卡片会被当成另一个功能点的知识用。"""
    _features(workspace, RESERVE)
    _card(workspace, "reserve-flow.md", CARD.replace("id: reserve-flow", "id: something-else"))

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert "something-else" in caught.value.render()


def test_a_card_without_front_matter_is_caught(workspace: Path) -> None:
    _features(workspace, RESERVE)
    _card(workspace, "reserve-flow.md", "# 只有正文\n")

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert "front matter" in caught.value.render()


def test_the_hit_count_survives_a_reload(workspace: Path) -> None:
    """命中计数是自然选择的依据。加载时丢掉它,长期零命中的卡片就永远淘汰不掉。"""
    _features(workspace, RESERVE)
    _card(workspace, "reserve-flow.md")

    assert load_tree(workspace).card("order-service", "reserve-flow").hits == 17


# --- 按路径反查:路由的地基 ---------------------------------------------------


def test_a_changed_file_finds_the_features_covering_it(workspace: Path) -> None:
    _features(workspace, RESERVE)
    _card(workspace, "reserve-flow.md")

    hits = features_covering(
        load_tree(workspace), "repos/order-service/src/order/reserve/service.py"
    )

    assert [item.feature.id for item in hits] == ["reserve-flow"]
    assert hits[0].module_id == "order-service"


def test_a_path_covered_by_several_features_returns_all_of_them(workspace: Path) -> None:
    """挑一张会漏掉关键信息。哪几张值得注入是预算的事,不是这一层的事。"""
    _features(
        workspace,
        RESERVE,
        RESERVE | {"id": "reserve-timeout", "card": "features/reserve-timeout.md"},
    )
    _card(workspace, "reserve-flow.md")
    _card(workspace, "reserve-timeout.md", CARD.replace("id: reserve-flow", "id: reserve-timeout"))

    hits = features_covering(
        load_tree(workspace), "repos/order-service/src/order/reserve/service.py"
    )

    assert {item.feature.id for item in hits} == {"reserve-flow", "reserve-timeout"}


def test_an_uncovered_path_finds_nothing(workspace: Path) -> None:
    _features(workspace, RESERVE)
    _card(workspace, "reserve-flow.md")

    assert (
        features_covering(load_tree(workspace), "repos/order-service/src/order/refund/api.py") == []
    )


# --- 与既有装配的关系 ---------------------------------------------------------


def test_features_survive_a_project_map_write_back(workspace: Path) -> None:
    """项目地图这个类型不带功能点。写回时抹掉它们的话,"知识莫名其妙少了一块"没人查得出。"""
    _features(workspace, RESERVE)
    _card(workspace, "reserve-flow.md")

    write_tree(workspace, load_tree(workspace).project_map)

    assert [item.id for item in load_tree(workspace).features("order-service")] == ["reserve-flow"]


# --- 评审抓到的三条:早退、重读、静默丢弃 -------------------------------------


def test_a_broken_entry_does_not_make_its_card_look_like_an_orphan(workspace: Path) -> None:
    """条目写错和"这张卡片没人要"是两回事。把后者也报出来的话,两条错里有一条是假的,
    而人会照着假的那条把卡片删掉。"""
    _features(workspace, RESERVE | {"no_card": "既给卡片又给声明"})
    _card(workspace, "reserve-flow.md")

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    assert "孤儿卡片" not in caught.value.render()


def test_scope_is_checked_even_when_the_card_pointer_dangles(workspace: Path) -> None:
    """两个问题互相独立。早退会让其中一个在另一个存在时静默逃掉。"""
    _features(workspace, RESERVE | {"scope": ["repos/order-service/src/order/deleted/**"]})

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    rendered = caught.value.render()
    assert "卡片不存在" in rendered
    assert "覆盖的代码路径不存在" in rendered


def test_one_broken_feature_does_not_hide_the_others(workspace: Path) -> None:
    """整份一起过模型的话,一个手写的拼写错误会让那个模块的其余问题全部看不见。"""
    _features(
        workspace,
        {
            "id": "typo",
            "summary": "x",
            "scope": ["repos/order-service/**"],
            "crad": "features/x.md",
        },
        {"id": "gap", "summary": "y", "scope": ["repos/order-service/src/order/refund/**"]},
    )

    with pytest.raises(GenomeValidationError) as caught:
        load_tree(workspace)

    rendered = caught.value.render()
    assert "crad" in rendered
    assert "gap" in rendered


def test_a_feature_with_a_typo_is_not_silently_deleted_on_write_back(workspace: Path) -> None:
    """**「知识莫名其妙少了一块」是最难查的那种故障。** 写回路径上过模型的话,一个拼写
    错误的条目会在下一次改 test_cmd 时被悄悄删掉。"""
    from agentgenome.genome.tree import assemble

    _features(
        workspace,
        {
            "id": "typo",
            "summary": "x",
            "scope": ["repos/order-service/**"],
            "crad": "features/x.md",
        },
    )
    # 这个 Workspace 现在校验不过——但写回不该因此吃掉数据。装配那一层不解析功能点,
    # 所以它读得出来;写回原样带回去。
    write_tree(workspace, assemble(workspace).project_map)

    payload = yaml.safe_load(
        (workspace / paths.MODULES / "order-service" / MODULE_MAP).read_text(encoding="utf-8")
    )
    assert payload["features"][0]["crad"] == "features/x.md", "带拼写错误的功能点被悄悄删掉了"


def test_a_feature_entry_cannot_be_constructed_in_an_invalid_state() -> None:
    """互斥在模型层面就成立。放在校验函数里的话，直接构造出来的实例绕得过去。"""
    from pydantic import ValidationError

    from agentgenome.genome.features import FeatureEntry

    with pytest.raises(ValidationError):
        FeatureEntry(id="x", card="a.md", no_card="也不需要")
    with pytest.raises(ValidationError):
        FeatureEntry(id="x")
    with pytest.raises(ValidationError):
        FeatureEntry(id="x", no_card="   ")


def test_a_broken_card_does_not_kill_a_running_task(workspace: Path) -> None:
    """完备是知识变更要过的门禁，不是每个任务运行时要过的检查。一张卡片里的拼写错误
    不该让一个正在跑的开发任务当场死掉。"""
    from agentgenome.genome.loader import load_project_map

    _features(workspace, RESERVE)  # 卡片指针悬空

    assert load_project_map(workspace).module("order-service").id == "order-service"
    with pytest.raises(GenomeValidationError):
        load_tree(workspace)
