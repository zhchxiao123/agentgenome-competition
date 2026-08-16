"""项目地图的校验约束:每条约束一个用例。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from agentgenome import paths
from agentgenome.genome.loader import GenomeValidationError, load_project_map
from tests.fixtures.tree import write_flat_as_tree


def _write(tmp_path: Path, project_map: str) -> Path:
    """摆一棵树。**校验用例要摆的正是非法的树**,所以夹具按键的归属分发,不做校验。"""
    root = tmp_path / "ws"
    text = textwrap.dedent(project_map)
    try:
        flat = yaml.safe_load(text)
    except yaml.YAMLError:
        flat = None
    if not isinstance(flat, dict):
        # YAML 本身就坏掉的用例:原样写进根索引,让加载器去报解析失败。
        target = root / paths.PROJECT_MAP
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return root
    return write_flat_as_tree(root, flat)


def _load_expecting_failure(tmp_path: Path, project_map: str) -> GenomeValidationError:
    with pytest.raises(GenomeValidationError) as excinfo:
        load_project_map(_write(tmp_path, project_map))
    return excinfo.value


def test_unknown_top_level_field_is_rejected(tmp_path: Path) -> None:
    """拼写错误静默失效比报错难查得多,所以未知字段一律拒绝。"""
    error = _load_expecting_failure(
        tmp_path,
        """\
        version: 1
        project: {name: p}
        moduls:
          - id: typo
            path: repos/order-service/
        """,
    )

    assert "moduls" in error.render()


def test_unknown_module_field_is_rejected(tmp_path: Path) -> None:
    error = _load_expecting_failure(
        tmp_path,
        """\
        version: 1
        project: {name: p}
        modules:
          - id: order
            path: repos/order-service/
            test_command: "pytest -q"
        """,
    )

    assert "test_command" in error.render()


def test_missing_required_field_is_rejected(tmp_path: Path) -> None:
    error = _load_expecting_failure(
        tmp_path,
        """\
        version: 1
        project: {name: p}
        modules:
          - id: order
        """,
    )

    assert "modules.0.path" in error.render()


def test_depends_on_must_reference_an_existing_module(tmp_path: Path) -> None:
    error = _load_expecting_failure(
        tmp_path,
        """\
        version: 1
        project: {name: p}
        modules:
          - id: order
            path: repos/order-service/
            depends_on: [ghost]
        """,
    )

    rendered = error.render()
    assert "ghost" in rendered
    assert "modules.0.depends_on" in rendered


def test_doc_must_reference_an_existing_file(tmp_path: Path) -> None:
    error = _load_expecting_failure(
        tmp_path,
        """\
        version: 1
        project: {name: p}
        modules:
          - id: order
            path: repos/order-service/
            doc: genome/knowledge/modules/order/missing.md
        """,
    )

    assert "genome/knowledge/modules/order/missing.md" in error.render()


def test_interface_schema_must_reference_an_existing_file(tmp_path: Path) -> None:
    error = _load_expecting_failure(
        tmp_path,
        """\
        version: 1
        project: {name: p}
        modules:
          - id: order
            path: repos/order-service/
        interfaces:
          - id: reserve-api
            kind: http
            provider: order
            schema: repos/inventory-service/api/gone.yaml
        """,
    )

    assert "repos/inventory-service/api/gone.yaml" in error.render()


def test_interface_provider_and_consumers_must_be_known_modules(tmp_path: Path) -> None:
    error = _load_expecting_failure(
        tmp_path,
        """\
        version: 1
        project: {name: p}
        modules:
          - id: order
            path: repos/order-service/
        interfaces:
          - id: reserve-api
            kind: http
            provider: ghost-provider
            consumers: [ghost-consumer]
        """,
    )

    rendered = error.render()
    assert "ghost-provider" in rendered
    assert "ghost-consumer" in rendered


def test_datastore_owner_and_migrations_are_checked(tmp_path: Path) -> None:
    error = _load_expecting_failure(
        tmp_path,
        """\
        version: 1
        project: {name: p}
        modules:
          - id: order
            path: repos/order-service/
        datastores:
          - id: order-db
            kind: postgres
            owner: ghost
            migrations: repos/order-service/gone/
        """,
    )

    rendered = error.render()
    assert "ghost" in rendered
    assert "repos/order-service/gone/" in rendered


def test_all_reference_problems_are_reported_at_once(tmp_path: Path) -> None:
    """修一条跑一次的反馈回路太长,一次把问题报全。"""
    error = _load_expecting_failure(
        tmp_path,
        """\
        version: 1
        project: {name: p}
        modules:
          - id: order
            path: repos/order-service/
            depends_on: [ghost-a, ghost-b]
            doc: genome/knowledge/modules/order/missing.md
        """,
    )

    assert len(error.issues) == 3


def test_malformed_yaml_is_a_readable_error(tmp_path: Path) -> None:
    error = _load_expecting_failure(tmp_path, "version: 1\n  bad indent: [\n")

    assert "project-map.yaml" in error.render()


def test_every_issue_names_the_offending_file(tmp_path: Path) -> None:
    error = _load_expecting_failure(
        tmp_path,
        """\
        version: 1
        project: {name: p}
        modules:
          - id: order
            path: repos/order-service/
            depends_on: [ghost]
        """,
    )

    assert all(issue.file == "genome/knowledge/project-map.yaml" for issue in error.issues)
