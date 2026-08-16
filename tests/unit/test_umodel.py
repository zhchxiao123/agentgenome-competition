"""语义图谱同步与告警反查。

排障从「人读日志猜模块」变成「图谱定位」。这一层最该钉死的是**定位不到时的行为**——
瞎猜一个模块比不建任务更糟。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentgenome.genome.models import ProjectMap
from agentgenome.umodel.graph import Entity, EntityKind, InMemoryGraph, Relation, RelationKind
from agentgenome.umodel.sync import Alert, AlertGate, localise, sync_project_map, sync_task

MAP = ProjectMap.model_validate(
    {
        "version": 1,
        "project": {"name": "mall"},
        "modules": [
            {
                "id": "order-service",
                "path": "repos/order-service/",
                "depends_on": ["inventory-service"],
            },
            {"id": "inventory-service", "path": "repos/inventory-service/"},
        ],
        "interfaces": [
            {
                "id": "order-to-inventory",
                "kind": "http",
                "provider": "inventory-service",
                "consumers": ["order-service"],
                "schema": "repos/inventory-service/api/reserve.yaml",
            }
        ],
    }
)


def _graph() -> InMemoryGraph:
    graph = InMemoryGraph()
    sync_project_map(graph, MAP)
    return graph


def _with_production(graph: InMemoryGraph) -> InMemoryGraph:
    """运维侧写入的那两类实体。我们只读它们,不产它们。"""
    graph.upsert(
        [Entity(EntityKind.SERVICE, "order-api"), Entity(EntityKind.ALERT, "AL-1")],
        [
            Relation(RelationKind.DEPLOYED_FROM, "service:order-api", "module:order-service"),
            Relation(RelationKind.FIRES_ON, "alert:AL-1", "service:order-api"),
        ],
    )
    return graph


# --- 同步 -------------------------------------------------------------------


def test_modules_and_interfaces_land_on_the_graph() -> None:
    keys = _graph().all_keys()

    assert "module:order-service" in keys
    assert "interface:order-to-inventory" in keys


def test_the_provider_and_consumer_edges_point_the_right_way() -> None:
    """方向是有意义的。反过来的话,「谁提供这个接口」会给出消费者。"""
    graph = _graph()

    assert graph.neighbors("module:inventory-service", RelationKind.PROVIDES) == [
        "interface:order-to-inventory"
    ]
    assert graph.neighbors("module:order-service", RelationKind.CONSUMES) == [
        "interface:order-to-inventory"
    ]


def test_syncing_twice_does_not_duplicate_anything() -> None:
    """同步会被反复触发。重复写产生重复节点的话,图很快就不可读了。"""
    graph = _graph()
    before = len(graph.all_keys())

    changed = sync_project_map(graph, MAP)

    assert changed == 0
    assert len(graph.all_keys()) == before


def test_a_merged_task_records_which_modules_it_touched() -> None:
    """这条边是告警反查的最后一跳。"""
    graph = _graph()

    sync_task(graph, "ag-1", ["order-service"], merged_at="2026-08-09T10:00:00Z")

    assert graph.neighbors("task:ag-1", RelationKind.CHANGED) == ["module:order-service"]


# --- 告警反查 ---------------------------------------------------------------


def test_an_alert_walks_to_the_module_and_the_recent_task() -> None:
    graph = _with_production(_graph())
    sync_task(graph, "ag-1", ["order-service"], merged_at="2026-08-09T10:00:00Z")

    found = localise(graph, Alert(id="AL-1", service="order-api", summary="下单接口 5xx 飙升"))

    assert found.modules == ("order-service",)
    assert found.recent_tasks == ("ag-1",)
    assert found.located


def test_the_most_recent_task_comes_first() -> None:
    """排障第一个要看的就是「上一次动它是什么时候」。"""
    graph = _with_production(_graph())
    sync_task(graph, "ag-old", ["order-service"], merged_at="2026-08-01T10:00:00Z")
    sync_task(graph, "ag-new", ["order-service"], merged_at="2026-08-09T10:00:00Z")

    found = localise(graph, Alert(id="AL-1", service="order-api", summary="x"))

    assert found.recent_tasks[0] == "ag-new"


def test_an_unmapped_service_locates_nothing() -> None:
    """**定位不到就不建任务。**

    瞎猜一个模块会让数字员工去改一段与故障无关的代码,而人还以为系统在处理这次告警。
    """
    found = localise(_graph(), Alert(id="AL-9", service="没在图上的服务", summary="x"))

    assert found.modules == ()
    assert not found.located


def test_the_requirement_carries_the_alert_context() -> None:
    """需求解析阶段就要看到告警上下文,不必再查一遍图谱。"""
    graph = _with_production(_graph())
    sync_task(graph, "ag-1", ["order-service"], merged_at="2026-08-09T10:00:00Z")

    text = localise(graph, Alert(id="AL-1", service="order-api", summary="下单 5xx")).requirement()

    assert "下单 5xx" in text
    assert "order-service" in text
    assert "ag-1" in text


def test_an_unlocated_alert_says_so_in_the_requirement() -> None:
    text = localise(_graph(), Alert(id="AL-9", service="未知", summary="x")).requirement()

    assert "定位不到" in text


# --- 自动建任务的闸 ---------------------------------------------------------


def _alert(service: str, at: datetime) -> Alert:
    return Alert(id=f"AL-{at.timestamp()}", service=service, summary="x", fired_at=at)


def test_an_alert_storm_only_creates_one_task() -> None:
    """**一次告警风暴能创建几百个任务,把预算烧光。**"""
    gate = AlertGate(window_s=900)
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)

    verdicts = [
        gate.admit(_alert("order-api", now + timedelta(seconds=i * 30)))[0] for i in range(6)
    ]

    assert verdicts == [True, False, False, False, False, False]


def test_dedup_is_by_service_not_by_alert_id() -> None:
    """同一个故障会产生一串 id 各不相同的告警。按 id 去重等于没有去重。"""
    gate = AlertGate(window_s=900)
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    gate.admit(_alert("order-api", now))

    admitted, reason = gate.admit(_alert("order-api", now + timedelta(seconds=1)))

    assert admitted is False
    assert "去重" in reason


def test_a_different_service_is_not_deduped() -> None:
    gate = AlertGate(window_s=900)
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    gate.admit(_alert("order-api", now))

    assert gate.admit(_alert("inventory-api", now))[0] is True


def test_the_window_expires() -> None:
    gate = AlertGate(window_s=60)
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    gate.admit(_alert("order-api", now))

    assert gate.admit(_alert("order-api", now + timedelta(seconds=61)))[0] is True


def test_the_master_switch_stops_everything() -> None:
    gate = AlertGate(enabled=False)

    admitted, reason = gate.admit(_alert("order-api", datetime.now(UTC)))

    assert admitted is False
    assert "总闸" in reason


@pytest.mark.parametrize("kind", list(RelationKind))
def test_every_relation_kind_is_queryable(kind: RelationKind) -> None:
    assert _graph().neighbors("module:order-service", kind) is not None
