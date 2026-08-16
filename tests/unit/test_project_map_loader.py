"""项目地图加载器：文本进，模型或可读错误出。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from agentgenome.genome.loader import GenomeValidationError, load_project_map
from tests.fixtures.tree import write_flat_as_tree

VALID = """\
version: 3
updated_at: 2026-09-01T10:00:00Z
project:
  name: example-mall
  summary: 电商中台,订单/库存双子模块
modules:
  - id: order-service
    path: repos/order-service/
    lang: python
    summary: 订单域,依赖 inventory 的预占接口
    entrypoints: [src/order/app.py]
    test_cmd: "pytest -q"
    build_cmd: "make build"
    depends_on: [inventory-service]
    doc: genome/knowledge/modules/order-service/overview.md
  - id: inventory-service
    path: repos/inventory-service/
    lang: python
    summary: 库存域
    test_cmd: "pytest -q"
    doc: genome/knowledge/modules/inventory-service/overview.md
interfaces:
  - id: reserve-api
    kind: http
    provider: inventory-service
    consumers: [order-service]
    schema: repos/inventory-service/api/reserve.yaml
datastores:
  - id: order-db
    kind: postgres
    owner: order-service
    migrations: repos/order-service/migrations/
"""


def _workspace(tmp_path: Path, project_map: str) -> Path:
    """摆一个最小 Workspace：地图 + 它引用到的那些文件真实存在。"""
    root = tmp_path / "ws"
    for relative in (
        "genome/knowledge/modules/order-service/overview.md",
        "genome/knowledge/modules/inventory-service/overview.md",
        "repos/inventory-service/api/reserve.yaml",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# stub\n")
    (root / "repos/order-service" / "migrations").mkdir(parents=True, exist_ok=True)

    write_flat_as_tree(root, yaml.safe_load(textwrap.dedent(project_map)))
    return root


def test_loads_a_valid_project_map(tmp_path: Path) -> None:
    root = _workspace(tmp_path, VALID)

    project_map = load_project_map(root)

    assert project_map.version == 3
    assert project_map.project.name == "example-mall"
    assert [m.id for m in project_map.modules] == ["order-service", "inventory-service"]
    assert project_map.modules[0].depends_on == ["inventory-service"]
    assert project_map.interfaces[0].consumers == ["order-service"]
    assert project_map.datastores[0].migrations == "repos/order-service/migrations/"


def test_optional_sections_default_to_empty(tmp_path: Path) -> None:
    """新 Workspace 还没有契约与数据存储，这是正常状态而非错误。"""
    root = _workspace(
        tmp_path,
        """\
        version: 1
        project: {name: bare, summary: 空项目}
        modules:
          - id: only
            path: repos/order-service/
        """,
    )

    project_map = load_project_map(root)

    assert project_map.interfaces == []
    assert project_map.datastores == []
    assert project_map.modules[0].depends_on == []


def test_module_lookup_by_id(tmp_path: Path) -> None:
    root = _workspace(tmp_path, VALID)

    project_map = load_project_map(root)

    assert project_map.module("order-service").path == "repos/order-service/"
    with pytest.raises(KeyError):
        project_map.module("nope")


def test_missing_project_map_is_a_readable_error(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    with pytest.raises(GenomeValidationError) as excinfo:
        load_project_map(root)

    assert "project-map.yaml" in str(excinfo.value)
