"""知识地图是一棵树,不是一个文件。

单文件在五十个模块的项目上必然失效:要么膨胀到没人愿意打开,要么被整份塞进每个员工的上下文。

这一层只管**装配**:树在磁盘上怎么摆,以及它怎么合成上层那三十处消费方要的那个项目地图。
装配出来的东西不变是硬约束——否则一次知识分层的改动会波及全仓,而那和分层本身毫无关系。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentgenome import paths
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.loader import load_project_map
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.tree import (
    NothingToMigrate,
    assemble,
    is_flat_map,
    migrate_flat_map,
    write_tree,
)

FLAT = {
    "version": 3,
    "project": {"name": "mall", "summary": "电商中台"},
    "modules": [
        {
            "id": "order-service",
            "path": "repos/order-service/",
            "lang": "python",
            "summary": "订单域",
            "entrypoints": ["src/order/app.py"],
            "test_cmd": "pytest -q",
            "build_cmd": "make build",
            "depends_on": ["inventory-service"],
            "confidence": 0.9,
        },
        {"id": "inventory-service", "path": "repos/inventory-service/", "summary": "库存域"},
    ],
    "interfaces": [
        {
            "id": "reserve-api",
            "kind": "http",
            "provider": "inventory-service",
            "consumers": ["order-service"],
            "description": "库存预占接口",
        }
    ],
    "datastores": [{"id": "order-db", "kind": "postgres", "owner": "order-service"}],
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / paths.KNOWLEDGE).mkdir(parents=True)
    (tmp_path / "repos/order-service").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repos/inventory-service").mkdir(parents=True, exist_ok=True)
    write_tree(tmp_path, ProjectMap.model_validate(FLAT))
    return tmp_path


def test_the_tree_assembles_back_into_the_same_project_map(workspace: Path) -> None:
    """**装配出来的东西不变。** 三十处消费方一行不改就得继续工作。"""
    assert assemble(workspace).project_map == ProjectMap.model_validate(FLAT)


def test_the_root_index_only_carries_identity_and_pointers(workspace: Path) -> None:
    """根索引只答"有哪些模块、去哪找它们"。怎么跑属于模块地图。"""
    root = yaml.safe_load((workspace / paths.PROJECT_MAP).read_text(encoding="utf-8"))

    entry = root["modules"][0]
    assert set(entry) <= {"id", "path", "summary", "map"}
    assert "test_cmd" not in entry
    assert "interfaces" not in root


def test_contracts_live_in_their_own_index(workspace: Path) -> None:
    """跨模块契约不属于任何一个模块,所以它不挂在任何一个模块名下。"""
    contracts = yaml.safe_load((workspace / paths.INTERFACES).read_text(encoding="utf-8"))

    assert [item["id"] for item in contracts["interfaces"]] == ["reserve-api"]
    assert [item["id"] for item in contracts["datastores"]] == ["order-db"]


def test_how_a_module_runs_lives_in_its_module_map(workspace: Path) -> None:
    module_map = yaml.safe_load(
        (workspace / paths.MODULES / "order-service" / "map.yaml").read_text(encoding="utf-8")
    )

    assert module_map["test_cmd"] == "pytest -q"
    assert module_map["depends_on"] == ["inventory-service"]


def test_load_project_map_reads_the_tree(workspace: Path) -> None:
    assert load_project_map(workspace).module("order-service").test_cmd == "pytest -q"


def test_a_dangling_module_map_pointer_is_caught(workspace: Path) -> None:
    (workspace / paths.MODULES / "order-service" / "map.yaml").unlink()

    with pytest.raises(GenomeValidationError) as caught:
        load_project_map(workspace)

    assert "order-service" in str(caught.value)


def test_a_missing_contract_index_is_caught(workspace: Path) -> None:
    (workspace / paths.INTERFACES).unlink()

    with pytest.raises(GenomeValidationError):
        load_project_map(workspace)


def test_every_problem_is_reported_in_one_go(workspace: Path) -> None:
    """修一个文件跑一次的反馈回路太长。"""
    (workspace / paths.MODULES / "order-service" / "map.yaml").unlink()
    (workspace / paths.MODULES / "inventory-service" / "map.yaml").unlink()

    with pytest.raises(GenomeValidationError) as caught:
        load_project_map(workspace)

    assert len(caught.value.issues) >= 2


def test_an_unknown_field_is_still_refused(workspace: Path) -> None:
    """拼写错误静默失效比报错难查得多。"""
    target = workspace / paths.MODULES / "order-service" / "map.yaml"
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["tset_cmd"] = "pytest"
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    with pytest.raises(GenomeValidationError):
        load_project_map(workspace)


def test_the_old_single_file_shape_is_recognised(tmp_path: Path) -> None:
    """严格模式对旧文件会报"多了个 interfaces 字段",读起来像拼写错误而不是"该迁移了"。"""
    (tmp_path / paths.KNOWLEDGE).mkdir(parents=True)
    (tmp_path / paths.PROJECT_MAP).write_text(
        yaml.safe_dump(FLAT, allow_unicode=True), encoding="utf-8"
    )

    assert is_flat_map(tmp_path)
    with pytest.raises(GenomeValidationError) as caught:
        load_project_map(tmp_path)
    assert "migrate" in str(caught.value)


def test_writing_the_tree_twice_is_idempotent(workspace: Path) -> None:
    before = {
        path.relative_to(workspace): path.read_bytes()
        for path in sorted((workspace / paths.KNOWLEDGE).rglob("*"))
        if path.is_file()
    }

    write_tree(workspace, assemble(workspace).project_map)

    after = {
        path.relative_to(workspace): path.read_bytes()
        for path in sorted((workspace / paths.KNOWLEDGE).rglob("*"))
        if path.is_file()
    }
    assert before == after


def test_a_card_pointing_outside_the_module_directory_survives_a_round_trip(
    tmp_path: Path,
) -> None:
    """存基名会让装配把目录拼成约定的那一个,于是任何指到别处的认知卡在一次写回之后就
    指向了一个不存在的文件——而这是静默的,直到某个任务拿不到该模块的认知。"""
    (tmp_path / paths.KNOWLEDGE / "shared").mkdir(parents=True)
    (tmp_path / "repos/order-service").mkdir(parents=True, exist_ok=True)
    elsewhere = "genome/knowledge/shared/order.md"
    (tmp_path / elsewhere).write_text("# 共用认知\n", encoding="utf-8")
    original = ProjectMap.model_validate(
        {
            "version": 1,
            "project": {"name": "mall"},
            "modules": [{"id": "order-service", "path": "repos/order-service/", "doc": elsewhere}],
        }
    )

    write_tree(tmp_path, original)

    assert assemble(tmp_path).project_map == original
    assert load_project_map(tmp_path).module("order-service").doc == elsewhere


def test_migration_moves_the_legacy_flat_card_and_leaves_a_valid_tree(tmp_path: Path) -> None:
    (tmp_path / paths.MODULES).mkdir(parents=True)
    (tmp_path / "repos/order-service").mkdir(parents=True, exist_ok=True)
    legacy = tmp_path / paths.MODULES / "order-service.md"
    legacy.write_text("# 订单域\n", encoding="utf-8")
    (tmp_path / paths.PROJECT_MAP).write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "project": {"name": "mall"},
                "modules": [
                    {
                        "id": "order-service",
                        "path": "repos/order-service/",
                        "test_cmd": "pytest -q",
                        "doc": "genome/knowledge/modules/order-service.md",
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    migrated, moved = migrate_flat_map(tmp_path)

    assert moved == ["order-service"]
    assert not legacy.exists()
    assert (tmp_path / paths.MODULES / "order-service" / "overview.md").read_text() == "# 订单域\n"
    assert load_project_map(tmp_path) == migrated


def test_migrating_a_workspace_without_a_map_says_so(tmp_path: Path) -> None:
    """「已经是知识树」是个错误的说法——这里压根没有地图。"""
    with pytest.raises(NothingToMigrate) as caught:
        migrate_flat_map(tmp_path)

    assert "还不是一个 Workspace" in str(caught.value)


def test_migrating_a_tree_refuses_rather_than_rewriting(workspace: Path) -> None:
    with pytest.raises(NothingToMigrate):
        migrate_flat_map(workspace)


def test_the_test_fixture_lays_out_the_same_tree_as_production(tmp_path: Path) -> None:
    """夹具为了摆出**非法**的树而自己做了一遍拆分。合法输入下它必须和产品代码摆出同一组
    文件、装配出同一个地图——否则测试通过的是一个系统里不存在的布局。

    (文件内容不逐字节比:夹具写的是原始 dict,产品代码写的是模型 dump,后者会把默认值
    显式写出来。那是差异,但不是分歧。)
    """
    from tests.fixtures.tree import write_flat_as_tree

    by_fixture = tmp_path / "fixture"
    by_production = tmp_path / "production"
    for root in (by_fixture, by_production):
        (root / paths.KNOWLEDGE).mkdir(parents=True)
        (root / "repos/order-service").mkdir(parents=True, exist_ok=True)
        (root / "repos/inventory-service").mkdir(parents=True, exist_ok=True)

    write_flat_as_tree(by_fixture, FLAT)
    write_tree(by_production, ProjectMap.model_validate(FLAT))

    def layout(root: Path) -> set[Path]:
        return {
            path.relative_to(root) for path in (root / paths.KNOWLEDGE).rglob("*") if path.is_file()
        }

    assert layout(by_fixture) == layout(by_production)
    assert assemble(by_fixture).project_map == assemble(by_production).project_map
