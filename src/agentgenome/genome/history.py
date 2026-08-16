"""项目地图的历史版本:认知的演进可回溯。

版本历史不是另开一张表——项目地图本来就是一份进 git 的文本文件,它的历史就是这个文件的
`git log`。**真相只有一份**,这里只是把它读出来给界面用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agentgenome import paths
from agentgenome.space.gitcmd import git, git_out

#: 一条记录里各字段的分隔符。用控制字符而不是常见标点,避免提交信息里出现同样的字符导致错位。
_FIELD_SEP = "\x1f"

#: 记录之间的分隔符。**正文是多行的**,靠换行切会把一条提交切成好几条。
_RECORD_SEP = "\x1e"

#: 提交正文里指回来源任务的那一行。写法见 `commit.message`。
_TASK_LINE = re.compile(r"^Task:\s*(\S+)", re.MULTILINE)


@dataclass(frozen=True)
class ProjectMapVersion:
    """项目地图某一次改动。`rev` 是这次改动之后文件的样子。"""

    rev: str
    at: str
    author: str
    subject: str
    #: 哪个任务的经验触发了这次更新。**从提交正文里读**——现有提交规范已经带任务号,沿用它
    #: 比再存一份关联表可靠:那份表会与 git 历史分叉,而分叉之后没法判断哪份是对的。
    source_task_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "rev": self.rev,
            "at": self.at,
            "author": self.author,
            "subject": self.subject,
            "source_task_id": self.source_task_id,
        }


@dataclass(frozen=True)
class ProjectMapDiff:
    from_rev: str
    to_rev: str
    diff: str


#: 「认知」住在哪几处。**地图分层之后这必须是一组路径而不是一个文件**:模块怎么跑、
#: 跨模块契约长什么样,都已经从根索引搬走了——只盯根索引的话,"认知怎么演进的"会在
#: 界面上安静地退化成"只有模块清单变过",而且没有任何症状。
_KNOWLEDGE_PATHS = (
    str(paths.PROJECT_MAP),
    str(paths.INTERFACES),
    str(paths.MODULES),
)

#: 基因组的三层各自住在哪。**三层都要能回溯**:只给知识那一层历史的话,"规则是什么时候
#: 被谁改成这样的"这个问题在界面上无解——而规则层恰恰是唯一能大范围改变系统行为的杠杆。
ASSET_PATHS: dict[str, tuple[str, ...]] = {
    "knowledge": _KNOWLEDGE_PATHS,
    "rules": (str(paths.RULES),),
    "procedures": (str(paths.PROCEDURES),),
}


class UnknownAsset(KeyError):
    """没有这一层基因组资产。"""


def paths_of(asset: str) -> tuple[str, ...]:
    """这一层住在哪几条路径下。认不出来就抛——**默默退回知识那一层**的话,一次拼错的查询
    会得到一份看起来正常、其实答非所问的历史。"""
    try:
        return ASSET_PATHS[asset]
    except KeyError as error:
        raise UnknownAsset(f"没有这一层基因组资产: {asset}(有 {', '.join(ASSET_PATHS)})") from error


def list_versions(
    workspace_root: Path, limit: int = 50, asset: str = "knowledge"
) -> list[ProjectMapVersion]:
    """某一层基因组资产的改动历史,按时间倒序(最新的在前)。

    只跟踪那一层的历史,不是整仓——认知的版本是它自己的事,与仓库其余部分改了多少次无关。
    """
    watched = paths_of(asset)
    if not (workspace_root / paths.GENOME).exists():
        return []
    format_string = _FIELD_SEP.join(("%H", "%aI", "%an", "%s", "%b")) + _RECORD_SEP
    out = git_out(
        workspace_root,
        "log",
        f"--max-count={limit}",
        f"--format={format_string}",
        "--",
        *watched,
    )
    versions = []
    for record in out.split(_RECORD_SEP):
        if not record.strip():
            continue
        fields = record.strip().split(_FIELD_SEP, 4)
        if len(fields) < 4:
            continue
        rev, at, author, subject = fields[:4]
        body = fields[4] if len(fields) > 4 else ""
        found = _TASK_LINE.search(body)
        versions.append(
            ProjectMapVersion(
                rev=rev,
                at=at,
                author=author,
                subject=subject,
                source_task_id=found.group(1) if found else "",
            )
        )
    return versions


def diff(
    workspace_root: Path, from_rev: str, to_rev: str, asset: str = "knowledge"
) -> ProjectMapDiff:
    """两个版本之间某一层资产的文本 diff。

    **只 diff 那一层。** 调用方给的两个 rev 可能是相隔很远的两次提交,仓库其余部分的改动跟
    "认知怎么演进的"这个问题无关,混进来只会让 diff 读不下去。
    """
    result = git(
        workspace_root,
        "diff",
        f"{from_rev}..{to_rev}",
        "--",
        *paths_of(asset),
    )
    return ProjectMapDiff(from_rev=from_rev, to_rev=to_rev, diff=result.stdout)


__all__ = [
    "ASSET_PATHS",
    "ProjectMapDiff",
    "ProjectMapVersion",
    "UnknownAsset",
    "diff",
    "list_versions",
    "paths_of",
]
