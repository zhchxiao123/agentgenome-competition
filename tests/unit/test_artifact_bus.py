"""ArtifactBus:产物目录的分配、血缘与检索。

**员工之间不直接通信,只通过产物目录交换信息**(黑板模式)。每次交接都天然留痕——
"上一阶段到底给了下一阶段什么"永远是一个能被回答的问题。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentgenome.jobs.artifacts import MANIFEST, ArtifactBus


@pytest.fixture
def bus(tmp_path: Path) -> ArtifactBus:
    return ArtifactBus(tmp_path / "tasks" / "ag-1")


def test_allocating_creates_a_numbered_stage_directory(bus: ArtifactBus) -> None:
    slot = bus.allocate("develop")

    assert slot.path.is_dir()
    assert slot.path.name == "01-develop"


def test_numbers_increase_and_are_never_reused(bus: ArtifactBus) -> None:
    """复用序号的话,同一个目录名在事件流里指向两份不同的产物。"""
    names = [bus.allocate(stage).path.name for stage in ("plan", "develop", "unit-gate")]

    assert names == ["01-plan", "02-develop", "03-unit-gate"]


def test_re_entering_a_stage_gets_a_fresh_directory(bus: ArtifactBus) -> None:
    """修复循环会多次进同一个 stage。覆盖上一轮的产物等于把失败现场擦掉。"""
    first = bus.allocate("develop")
    (first.path / "result.json").write_text("{}")

    second = bus.allocate("develop")

    assert second.path != first.path
    assert (first.path / "result.json").is_file()


def test_a_manifest_records_the_lineage(bus: ArtifactBus) -> None:
    """少了它,三个月后看到一个 gate-report.json 只能猜它是哪一轮的。"""
    slot = bus.allocate("develop")

    slot.write_manifest(
        producer="dev-employee",
        inputs=["01-plan/plan.yaml"],
        outputs=["result.json"],
        summary="实现预占调用",
    )

    payload = json.loads((slot.path / MANIFEST).read_text(encoding="utf-8"))
    assert payload["producer"] == "dev-employee"
    assert payload["inputs"] == ["01-plan/plan.yaml"]
    assert payload["outputs"] == ["result.json"]
    assert payload["stage"] == "develop"
    assert payload["summary"]


def test_a_slot_without_a_manifest_is_visible_as_such(bus: ArtifactBus) -> None:
    """ "忘了写清单"与"这一阶段没产物"要分得开。"""
    slot = bus.allocate("develop")

    assert slot.manifest() is None


def test_stages_can_be_retrieved_by_name_in_order(bus: ArtifactBus) -> None:
    bus.allocate("develop")
    bus.allocate("unit-gate")
    bus.allocate("develop")

    found = [slot.path.name for slot in bus.by_stage("develop")]

    assert found == ["01-develop", "03-develop"]


def test_the_latest_slot_of_a_stage_is_the_last_one(bus: ArtifactBus) -> None:
    bus.allocate("develop")
    latest = bus.allocate("develop")

    assert bus.latest("develop") is not None
    assert bus.latest("develop").path == latest.path


def test_a_stage_that_never_ran_has_no_latest(bus: ArtifactBus) -> None:
    assert bus.latest("itest") is None


def test_all_slots_come_back_in_allocation_order(bus: ArtifactBus) -> None:
    for stage in ("plan", "develop", "unit-gate"):
        bus.allocate(stage)

    assert [slot.stage for slot in bus.all()] == ["plan", "develop", "unit-gate"]


def test_a_second_bus_sees_what_the_first_allocated(tmp_path: Path) -> None:
    """崩溃恢复时新进程要能看见旧进程分配过的目录,不然序号会从头再来。"""
    first = ArtifactBus(tmp_path / "tasks" / "ag-1")
    first.allocate("plan")
    first.allocate("develop")

    third = ArtifactBus(tmp_path / "tasks" / "ag-1").allocate("unit-gate")

    assert third.path.name == "03-unit-gate"


def test_stage_names_with_odd_characters_are_refused(bus: ArtifactBus) -> None:
    """stage 名进路径。放任 `../` 进去等于让产物写到任务目录之外。"""
    with pytest.raises(ValueError):
        bus.allocate("../escape")


# --- 编址扩维:(stage, node, variant, attempt) --------------------------------
#
# 节点字段冻了没用,并行执行器真正会撞上的是**产物编址**。一个 stage 底下要能住下一张图。


def test_a_node_slot_carries_the_node_in_its_directory_name(bus: ArtifactBus) -> None:
    slot = bus.allocate("develop", node="order")

    assert slot.path.name == "01-develop.order"
    assert (slot.stage, slot.node, slot.variant) == ("develop", "order", "")


def test_a_variant_slot_carries_both(bus: ArtifactBus) -> None:
    slot = bus.allocate("develop", node="order", variant="minimal")

    assert slot.path.name == "01-develop.order.minimal"
    assert (slot.node, slot.variant) == ("order", "minimal")


def test_a_variant_without_a_node_is_refused(bus: ArtifactBus) -> None:
    """变体属于某个节点。没有节点的变体在编址上没有位置,也在语义上说不通。"""
    with pytest.raises(ValueError):
        bus.allocate("develop", variant="minimal")


def test_node_and_variant_names_with_odd_characters_are_refused(bus: ArtifactBus) -> None:
    """它们同样进路径,所以受同一套白名单约束。"""
    with pytest.raises(ValueError):
        bus.allocate("develop", node="../escape")
    with pytest.raises(ValueError):
        bus.allocate("develop", node="order", variant="../escape")


def test_slots_are_retrievable_by_node(bus: ArtifactBus) -> None:
    bus.allocate("develop", node="order")
    bus.allocate("develop", node="inventory")
    bus.allocate("develop", node="order")

    found = [slot.path.name for slot in bus.by_node("develop", "order")]

    assert found == ["01-develop.order", "03-develop.order"]


def test_by_node_separates_variants(bus: ArtifactBus) -> None:
    bus.allocate("develop", node="dev", variant="minimal")
    bus.allocate("develop", node="dev", variant="perf")

    assert [slot.variant for slot in bus.by_node("develop", "dev", "minimal")] == ["minimal"]


def test_by_node_with_no_node_is_the_plain_stage_slots(bus: ArtifactBus) -> None:
    """`single` 下节点维度取空,于是它退化成今天那条线。"""
    bus.allocate("develop")
    bus.allocate("develop", node="order")

    assert [slot.path.name for slot in bus.by_node("develop")] == ["01-develop"]


def test_attempt_is_derived_from_disk_not_remembered(bus: ArtifactBus) -> None:
    """真相在磁盘上。记在内存里的话,重启之后轮次会从头再来。"""
    bus.allocate("develop", node="order")
    second = bus.allocate("develop", node="order")

    assert second.attempt == 2
    reopened = ArtifactBus(bus.root.parent).by_node("develop", "order")
    assert [slot.attempt for slot in reopened] == [1, 2]


def test_attempt_counts_per_node_not_per_stage(bus: ArtifactBus) -> None:
    """两个并行节点各自数自己的轮次。按 stage 数的话,一个节点的重试会被算到另一个头上。"""
    bus.allocate("develop", node="order")
    bus.allocate("develop", node="inventory")
    third = bus.allocate("develop", node="inventory")

    assert third.attempt == 2


def test_the_manifest_records_the_new_dimensions(bus: ArtifactBus) -> None:
    slot = bus.allocate("develop", node="order", variant="minimal")

    slot.write_manifest(producer="dev-employee")

    payload = json.loads((slot.path / MANIFEST).read_text(encoding="utf-8"))
    assert payload["node"] == "order"
    assert payload["variant"] == "minimal"
    assert payload["attempt"] == 1


def test_a_plain_slot_manifest_keeps_the_dimensions_empty(bus: ArtifactBus) -> None:
    """不带这三维时字段缺省,不污染既有断言。"""
    slot = bus.allocate("develop")

    slot.write_manifest(producer="dev-employee")

    payload = json.loads((slot.path / MANIFEST).read_text(encoding="utf-8"))
    assert payload["node"] == ""
    assert payload["variant"] == ""


def test_concurrent_allocation_never_hands_out_the_same_directory(bus: ArtifactBus) -> None:
    """并发分配必须是安全的。

    改动前是"扫盘取 max+1"加 `mkdir(exist_ok=True)`:两个并行节点会拿到同一个号、写进
    同一个目录、后写的赢,而且**没有任何报错或事件能指向这里**。
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=16) as pool:
        slots = list(pool.map(lambda index: bus.allocate("develop", node=f"n{index}"), range(100)))

    numbers = [slot.number for slot in slots]
    assert len(set(numbers)) == 100
    assert len({slot.path for slot in slots}) == 100
    assert len(bus.all()) == 100


def test_the_reservation_marks_stay_out_of_the_artifacts_directory(bus: ArtifactBus) -> None:
    """产物目录是证据面。取号标记是机械装置,混进去的话每个枚举产物的调用方都要先学会跳过它。"""
    bus.allocate("develop")

    assert [entry.name for entry in bus.root.iterdir()] == ["01-develop"]
