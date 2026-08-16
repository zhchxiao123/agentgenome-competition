"""审计包导出。

合规检查来的时候,"这个任务到底发生了什么"必须有现成材料,而不是让人现去七个目录里翻。

## 未终结的任务也能导出

出事的时候人不会等任务走完——恰恰相反,最需要审计包的时刻通常是任务卡住或者刚出问题的时候。

## 归档内容

任务快照、完整事件流、全部产物目录(含 Agent 日志与上下文包)。**整个任务目录打包**而不是
挑选文件:挑选意味着有一份"该带什么"的清单,而那份清单会在加了新产物之后悄悄过时,表现为
审计包里缺了正好要查的那一样。

## 归档落在任务目录之外

审计包存在的唯一理由,是在原始日志被清理之后仍然留得住。落在任务目录里面的话,清理任务目录
会把它一起清掉——恰恰是它为之准备的那一刻。所以归档根与 `tasks/` 并列,且可以被指到别处
(有备份的挂载点)。

三个**记录平面**的寿命并不一样,而设计文档里它们是并排列着的、读起来像同样可靠:

| 平面 | 载体 | 寿命 |
|---|---|---|
| 版本面 | git | **永久** |
| 事件面 | `core.events`(数据库为准,JSONL 是可清理副本) | **永久** |
| 日志面 | `tasks/*/logs/` | **有限**,见 `security.retention` |

这个模块存在的意义就是接住日志面那一栏:任务一进终态就把材料固化下来,而不是等人想起来导。

## 已封存的包不被覆盖

固化过的包是**那一刻的快照**。日志被清理之后再手动导出一次,打出来的包会缺掉正是要查的那些
材料——而它会盖掉好的那一份。所以写向已封存路径的导出直接返回现有的包,不重打。
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentgenome import paths
from agentgenome.config import Config
from agentgenome.core.store import task_dir
from agentgenome.core.task import TaskNotFound, TaskStore
from agentgenome.security import gaps


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


#: 归档的缺省文件名。
ARCHIVE_SUFFIX = "-audit.zip"

#: 包里那份说明"这是什么时候、在什么状态下打的"的清单。
#: 没有它的话,一个包是任务跑到一半导的还是终态固化的,事后分不出来。
MANIFEST = "audit-manifest.json"

#: 包里那份缺口检测结果。**导包的那一刻跑的**,不是历史上某次跑的结果。
GAP_REPORT = "gap-report.json"


def archive_root(workspace_root: Path, configured: str | None = None) -> Path:
    """归档根。`configured` 来自根配置,缺省时与 `tasks/` 并列。

    绝对路径的配置值原样使用——把归档指到 Workspace 之外的备份盘是这个配置项存在的理由。
    """
    if not configured:
        return Path(workspace_root) / paths.ARCHIVE
    candidate = Path(configured).expanduser()
    return candidate if candidate.is_absolute() else Path(workspace_root) / candidate


def bundle_path(workspace_root: Path, task_id: str, archive: Path) -> Path:
    """一个任务的审计包该在哪。按任务 id 分目录,一个任务一个包。

    `archive` 收的是**已解析好的归档根**(由 `archive_root` 产出)。收 `str | Path | None`
    的话,相对 `str` 与相对 `Path` 会解析到两个不同的地方,而两者在类型上看不出区别。
    """
    return Path(archive) / task_id / f"{task_id}{ARCHIVE_SUFFIX}"


def seal_manifest(task_id: str, state: str, sealed_at: str, **extra: Any) -> dict[str, Any]:
    """包里那份清单。**手动导出与终态固化用同一个工厂**——各写各的话,两条路会打出结构
    不同的包,而"我拿到的这个包缺不缺东西"就再也没有一个统一的答案。
    """
    return {"task_id": task_id, "state": state, "sealed_at": sealed_at, **extra}


class TaskNotArchivable(FileNotFoundError):
    """这个任务没有可导出的材料。"""


def is_sealed(workspace_root: Path, task_id: str, archive: Path) -> bool:
    """这个任务的包已经封存过了。封存过的不重打,见模块说明。

    单独一个谓词是为了让调用方**在动手准备材料之前**就能知道这次不会打包——不然像缺口检测
    这种"导包时顺带跑一次"的活会白跑一遍,而它跑的时候要读整个仓库的提交历史。
    """
    return zipfile.is_zipfile(bundle_path(workspace_root, task_id, archive))


def gap_snapshot(workspace_root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    """导包时顺带跑一次缺口检测,返回 (要塞进包的文件, 要并进清单的键)。

    **两条封包路径共用它。** 终态自动固化与手动导出各写各的话,自动固化那份包会没有报告
    ——而它恰恰是原始日志被清掉之后唯一留下来的那一份。

    **检测失败不许把导出一起拖垮。** 最需要审计包的时刻,仓库本身也可能正是坏掉的那一样;
    那时报告写"比不了"就够了,包必须导得出来。
    """
    try:
        report = gaps.detect(workspace_root)
    except (OSError, ValueError) as error:
        report = gaps.GapReport(unavailable=f"缺口检测没跑成:{error}")
    summary: dict[str, Any] = {"gaps": len(report.gaps)}
    if report.unavailable:
        # **不写 `gaps: 0`。** 清单是给人看的那一份摘要,而"零条缺口"与"根本没比"在那里
        # 长得一模一样——后者是零信息。
        summary = {"gaps": None, "gaps_unavailable": report.unavailable}
    return {GAP_REPORT: json.dumps(report.as_dict(), ensure_ascii=False, indent=2)}, summary


@dataclass(frozen=True)
class AuditPackage:
    path: Path
    entries: tuple[str, ...]
    #: 这个包本来就在,这次没有重打。已封存的快照不被覆盖。
    already_sealed: bool = False

    @property
    def count(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class TerminalSeal:
    """终态证据固化的统一结果；调用方只负责把结果写进自己的事件流。"""

    package: AuditPackage | None = None
    error: str = ""


def export(
    workspace_root: Path,
    task_id: str,
    target: Path | None = None,
    archive: Path | None = None,
    manifest: Mapping[str, Any] | None = None,
    extras: Mapping[str, str] | None = None,
) -> AuditPackage:
    """把一个任务的全部材料打成一个归档。

    `target` 指定落点时原样使用(手动导出到任意位置);否则落进归档根。
    `manifest` 写进包内的清单文件,记"这是什么时候、在什么状态下打的"。
    `extras` 是任务目录之外、但属于这次审计的材料(文件名 → 内容),例如缺口检测报告。

    **落点是已封存的那个包、而且它已经存在时,直接返回它,不重打。** 日志被清理之后再导
    一次,打出来的包会缺掉正是要查的那些材料——而它会盖掉好的那一份。
    """
    source = task_dir(workspace_root, task_id)
    if not source.is_dir():
        raise TaskNotArchivable(
            f"没有 {task_id} 的任务目录({source})。任务不存在,或者它还没产出任何东西。"
        )

    sealed = bundle_path(workspace_root, task_id, archive or archive_root(workspace_root))
    if target is None and zipfile.is_zipfile(sealed):
        with zipfile.ZipFile(sealed) as existing:
            return AuditPackage(
                path=sealed, entries=tuple(existing.namelist()), already_sealed=True
            )

    destination = Path(target) if target else sealed
    destination.parent.mkdir(parents=True, exist_ok=True)

    entries = []
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for item in sorted(source.rglob("*")):
            if not item.is_file() or item == destination:
                continue
            name = str(item.relative_to(source))
            bundle.write(item, arcname=name)
            entries.append(name)
        if manifest is not None:
            bundle.writestr(MANIFEST, json.dumps(dict(manifest), ensure_ascii=False, indent=2))
            entries.append(MANIFEST)
        for name, content in (extras or {}).items():
            bundle.writestr(name, content)
            entries.append(name)
    return AuditPackage(path=destination, entries=tuple(entries))


def export_task_bundle(
    workspace_root: Path,
    task_id: str,
    config: Config,
    *,
    target: Path | None = None,
) -> AuditPackage:
    """按根配置把一个任务导成审计包。**手动导出与终态固化共用的那一层。**

    各调用方自己拼归档根与清单的话,它们会慢慢长出差异——而"我手上这个包结构对不对"就
    再也没有统一答案。REST 与命令行都走这里。

    任务查不到时仍然打包(材料还在就该导得出来),清单里的状态记成未知。

    **包里顺带带一份缺口检测。** 一个只报告、不拦截、又不定时跑的检测很容易变成没人调用的
    接口;挂在这里,它至少在每次真正需要审计的时候一定被跑过一次。见 `security.gaps`。
    """
    try:
        state = TaskStore(workspace_root).get(task_id).state.value
    except (TaskNotFound, sqlite3.Error):
        state = "unknown"
    archive = archive_root(workspace_root, config.evidence.archive_dir)
    # 已经封存过就不会重打,那么这一次的检测跑了也是白跑——而它要读整个仓库的提交历史。
    # **指定了落点就不算重打**:那是导到别处的一份新包,它一样要带报告。
    skip = target is None and is_sealed(workspace_root, task_id, archive)
    extras, summary = ({}, {}) if skip else gap_snapshot(workspace_root)
    return export(
        workspace_root,
        task_id,
        target=target,
        archive=archive,
        manifest=seal_manifest(task_id, state, _stamp(), **summary),
        extras=extras,
    )


def seal_terminal_evidence(
    workspace_root: Path,
    task_id: str,
    state: str,
    config: Config,
    *,
    manifest_extra: Mapping[str, Any] | None = None,
) -> TerminalSeal:
    """固化任意任务的终态材料；研发与基因组任务共用同一归档契约。"""
    extras, summary = gap_snapshot(workspace_root)
    try:
        package = export(
            workspace_root,
            task_id,
            archive=archive_root(workspace_root, config.evidence.archive_dir),
            manifest=seal_manifest(
                task_id,
                state,
                _stamp(),
                **summary,
                **dict(manifest_extra or {}),
            ),
            extras=extras,
        )
    except (OSError, TaskNotArchivable) as error:
        return TerminalSeal(error=str(error))
    return TerminalSeal(package=package)


__all__ = [
    "ARCHIVE_SUFFIX",
    "GAP_REPORT",
    "MANIFEST",
    "AuditPackage",
    "TaskNotArchivable",
    "TerminalSeal",
    "archive_root",
    "bundle_path",
    "export",
    "export_task_bundle",
    "gap_snapshot",
    "is_sealed",
    "seal_manifest",
    "seal_terminal_evidence",
]
