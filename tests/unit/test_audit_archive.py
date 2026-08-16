"""审计包落在哪。

审计包存在的唯一理由,是在原始日志被清理之后仍然留得住。所以它不能住在任务目录里面——
那样清理任务目录会把它一起清掉,而它恰恰是为了这一刻准备的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome import paths
from agentgenome.security.audit import TaskNotArchivable, archive_root, export


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    task = tmp_path / paths.TASKS / "ag-20260901-001"
    (task / "logs").mkdir(parents=True)
    (task / "task.json").write_text('{"id": "ag-20260901-001"}', encoding="utf-8")
    (task / "logs" / "job-01.jsonl").write_text('{"kind":"note"}\n', encoding="utf-8")
    return tmp_path


def test_the_bundle_lands_outside_the_task_directory(workspace: Path) -> None:
    package = export(workspace, "ag-20260901-001")

    task = workspace / paths.TASKS / "ag-20260901-001"
    assert task not in package.path.parents
    assert package.path.is_file()


def test_the_bundle_lands_under_the_archive_directory_keyed_by_task(workspace: Path) -> None:
    package = export(workspace, "ag-20260901-001")

    assert package.path.parent == workspace / paths.ARCHIVE / "ag-20260901-001"


def test_the_archive_directory_can_be_moved_elsewhere(workspace: Path, tmp_path: Path) -> None:
    """指向有备份的挂载点是这个配置项存在的理由。"""
    elsewhere = tmp_path / "backed-up"

    package = export(workspace, "ag-20260901-001", archive=elsewhere)

    assert package.path.parent == elsewhere / "ag-20260901-001"


def test_an_explicit_target_still_wins(workspace: Path, tmp_path: Path) -> None:
    target = tmp_path / "somewhere" / "mine.zip"

    package = export(workspace, "ag-20260901-001", target=target)

    assert package.path == target


def test_the_bundle_still_packs_the_whole_task_directory(workspace: Path) -> None:
    package = export(workspace, "ag-20260901-001")

    assert set(package.entries) == {"task.json", str(Path("logs") / "job-01.jsonl")}


def test_a_missing_task_still_says_so(workspace: Path) -> None:
    with pytest.raises(TaskNotArchivable):
        export(workspace, "ag-nope")


def test_the_archive_root_defaults_to_a_sibling_of_tasks(tmp_path: Path) -> None:
    assert archive_root(tmp_path) == tmp_path / paths.ARCHIVE
    assert archive_root(tmp_path, "elsewhere") == tmp_path / "elsewhere"


def test_a_sealed_bundle_is_not_rebuilt(workspace: Path) -> None:
    """固化过的包是那一刻的快照。日志清掉之后再导一次会打出一个缺东西的包——而它会盖掉
    好的那一份。"""
    first = export(workspace, "ag-20260901-001", manifest={"state": "COMPLETED"})
    (workspace / paths.TASKS / "ag-20260901-001" / "logs" / "job-01.jsonl").unlink()

    second = export(workspace, "ag-20260901-001")

    assert second.already_sealed
    assert second.path == first.path
    assert str(Path("logs") / "job-01.jsonl") in second.entries, "原快照被重打后缺了材料"


def test_an_explicit_target_still_writes_even_when_sealed(workspace: Path, tmp_path: Path) -> None:
    """不覆盖只保护那一个封存路径,人显式指定落点时该导还是导。"""
    export(workspace, "ag-20260901-001")

    package = export(workspace, "ag-20260901-001", target=tmp_path / "copy.zip")

    assert not package.already_sealed


def test_a_truncated_bundle_does_not_count_as_sealed(workspace: Path) -> None:
    """归档盘写到一半断掉会留下一个打不开的 zip。按"文件在不在"判定的话它算已封存,
    于是日志被删——正是这条规则要防的那种静默证据丢失。"""
    import zipfile

    sealed = archive_root(workspace) / "ag-20260901-001" / "ag-20260901-001-audit.zip"
    sealed.parent.mkdir(parents=True)
    sealed.write_bytes(b"")

    assert not zipfile.is_zipfile(sealed)
    package = export(workspace, "ag-20260901-001")
    assert not package.already_sealed, "坏包被当成了好包"
