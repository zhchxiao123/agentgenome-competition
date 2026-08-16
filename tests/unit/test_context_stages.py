"""分阶段切片:目录加已命中内容,不是全量灌入。

**员工的工作区里本来就有完整的知识树。** 它随时能自己去读。所以真正需要塞进包里的,只有
「我该知道有哪些知识存在」——未命中的卡片给一行目录就够,命中的才给正文。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentgenome import paths
from agentgenome.context import Stage, load_genome_slice
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.routing import RouteInputs
from agentgenome.genome.tree import MODULE_MAP, write_tree

BASE = {
    "version": 1,
    "project": {"name": "mall", "summary": "电商中台"},
    "modules": [
        {"id": "order-service", "path": "repos/order-service/", "summary": "订单域"},
        {"id": "inventory-service", "path": "repos/inventory-service/", "summary": "库存域"},
    ],
}

CARD = "---\nid: {id}\nsummary: {summary}\n---\n这里是{id}的细节正文。\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / paths.KNOWLEDGE).mkdir(parents=True)
    for relative in ("reserve", "refund"):
        (tmp_path / "repos/order-service" / "src" / "order" / relative).mkdir(parents=True)
    (tmp_path / "repos/inventory-service").mkdir(parents=True, exist_ok=True)
    (tmp_path / paths.RULES).mkdir(parents=True, exist_ok=True)
    write_tree(tmp_path, ProjectMap.model_validate(BASE))
    target = tmp_path / paths.MODULES / "order-service" / MODULE_MAP
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["test_cmd"] = "pytest -q"
    payload["features"] = [
        {
            "id": "reserve-flow",
            "summary": "下单预占库存流程",
            "scope": ["repos/order-service/src/order/reserve/**"],
            "card": "features/reserve-flow.md",
        },
        {
            "id": "refund-flow",
            "summary": "退款流程",
            "scope": ["repos/order-service/src/order/refund/**"],
            "card": "features/refund-flow.md",
        },
    ]
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    for feature_id, summary in (("reserve-flow", "下单预占"), ("refund-flow", "退款")):
        card = tmp_path / paths.MODULES / "order-service" / "features" / f"{feature_id}.md"
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text(CARD.format(id=feature_id, summary=summary), encoding="utf-8")
    return tmp_path


def _text(fragments) -> str:
    return "\n".join(f"{item.title}\n{item.body}" for item in fragments)


# --- 需求解析阶段 -------------------------------------------------------------


def test_planning_gets_the_whole_root_index(workspace: Path) -> None:
    """此阶段还不知道会碰哪里,给的是全貌。"""
    text = _text(load_genome_slice(workspace, stage=Stage.PLAN))

    assert "order-service" in text
    assert "inventory-service" in text


def test_planning_carries_no_feature_cards(workspace: Path) -> None:
    """还没有任何路径信息可用,路由无从谈起——给卡片只是在猜。"""
    text = _text(load_genome_slice(workspace, stage=Stage.PLAN))

    assert "这里是reserve-flow的细节正文" not in text
    assert "这里是refund-flow的细节正文" not in text


# --- 开发阶段:命中给正文,未命中给目录行 --------------------------------------


def test_a_hit_card_brings_its_body(workspace: Path) -> None:
    fragments = load_genome_slice(
        workspace,
        modules=("order-service",),
        route_inputs=RouteInputs(paths=("repos/order-service/src/order/reserve/a.py",)),
    )

    assert "这里是reserve-flow的细节正文" in _text(fragments)


def test_a_missed_card_is_only_a_directory_line(workspace: Path) -> None:
    """**员工能自己去读。** 塞进正文只是把真正相关的那几十行淹掉。"""
    fragments = load_genome_slice(
        workspace,
        modules=("order-service",),
        route_inputs=RouteInputs(paths=("repos/order-service/src/order/reserve/a.py",)),
    )
    text = _text(fragments)

    assert "这里是refund-flow的细节正文" not in text
    assert "refund-flow" in text
    assert "退款流程" in text


def test_the_directory_line_says_where_to_find_it(workspace: Path) -> None:
    """看到这行就知道「这里有知识存在」,需要时自己去读——所以得说清楚去哪读。"""
    fragments = load_genome_slice(
        workspace,
        modules=("order-service",),
        route_inputs=RouteInputs(paths=("repos/order-service/src/order/reserve/a.py",)),
    )

    assert "modules/order-service/features/refund-flow.md" in _text(fragments)


def test_how_the_module_runs_is_still_there(workspace: Path) -> None:
    fragments = load_genome_slice(workspace, modules=("order-service",))

    assert "pytest -q" in _text(fragments)


def test_rules_still_ride_along(workspace: Path) -> None:
    """既有行为不回退:约束和知识一起到位。"""
    (workspace / paths.ARCHITECTURE_RULES).write_text(
        "```rules\nforbidden_deps: []\nlayering: [api 层不得直接访问 db 层]\n```\n",
        encoding="utf-8",
    )

    fragments = load_genome_slice(workspace, modules=("order-service",))

    assert any(item.trust.name == "RULE" for item in fragments)


def test_without_routing_the_old_behaviour_is_kept(workspace: Path) -> None:
    """三十处消费方一行不改就得继续工作:不给路由输入时不该凭空冒出卡片。"""
    text = _text(load_genome_slice(workspace, modules=("order-service",)))

    assert "这里是reserve-flow的细节正文" not in text


def test_a_cross_module_task_gets_both_module_maps(workspace: Path) -> None:
    text = _text(load_genome_slice(workspace, modules=("order-service", "inventory-service")))

    assert "order-service" in text
    assert "inventory-service" in text


# --- 接线：光有路由函数不算数 -------------------------------------------------


def test_the_failure_report_persists_its_paths_as_data(tmp_path: Path) -> None:
    """报告正文是给员工读的散文。从散文里再抠一遍路径是件必然会写错的事——门禁改一次
    措辞，路由就静默零命中。"""
    from agentgenome.jobs.reports import failure_paths, write_failure_report

    write_failure_report(
        tmp_path,
        round_=1,
        title="第 1 轮失败",
        payload={
            "gates": [
                {
                    "id": "unit",
                    "failures": [
                        {
                            "test": "t",
                            "message": "m",
                            "file": "repos/order-service/src/order/refund/b.py",
                        }
                    ],
                }
            ]
        },
    )

    assert failure_paths(tmp_path, before_round=2) == ("repos/order-service/src/order/refund/b.py",)


def test_itest_suspect_files_are_persisted_too(tmp_path: Path) -> None:
    from agentgenome.jobs.reports import failure_paths, write_failure_report

    write_failure_report(
        tmp_path,
        round_=1,
        title="集测失败",
        payload={
            "failures": [
                {
                    "case": "c",
                    "message": "m",
                    "suspect_files": ["repos/order-service/src/order/reserve/a.py"],
                }
            ]
        },
    )

    assert failure_paths(tmp_path, before_round=2) == (
        "repos/order-service/src/order/reserve/a.py",
    )


def test_the_dispatch_path_actually_accepts_routing() -> None:
    """**结构做出来 ≠ 功能接上了。** 前几轮评审三次命中同一个形状：配置项没人读、
    门禁没接进写入路径、预算字段没传下去。这条断言守的是这次别再来一遍。"""
    import inspect

    from agentgenome.genome import dispatch
    from agentgenome.jobs import orchestrator

    assert "route_inputs" in inspect.signature(dispatch.dispatch_procedure).parameters
    assert "route_inputs=self._route_inputs(task)" in inspect.getsource(orchestrator)
