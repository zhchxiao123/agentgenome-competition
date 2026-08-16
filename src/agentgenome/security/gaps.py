"""记录平面之间的缺口检测。

配置是 git 里的普通文件,谁都能直接推一个提交上去,而那样改**不产生任何事件**。此时
"每次操作都有记录"字面上还成立——版本面记到了——但事件面上出现了一个洞,而事件面恰恰是
审计检索唯一的入口。于是:走界面改配置查得到,直接推代码改配置查不到,**而后者恰恰是更
需要被审计的那一种**。

这一层把两个平面对齐:遍历配置文件的提交历史,与事件面上的配置变更事件按 sha 比对,列出
没有对应事件的提交。

## 只报告

直接改仓库是合法的运维手段,所以这里**不拦截、不告警升级、不改任何状态**。`detect` 是一个
纯读函数——连数据库都不会顺手建出来;那条"查过了"的事件由 `record_scan` 另外写。分开是为了
让"检测不改变状态"这句话可以被直接测出来,而不是靠人读代码确认。

## 不做定时任务

定时跑出来的结果没人看,却会制造"已经在监控了"的错觉——那比没有检测更糟。需要定时的话由
外部调度调这条命令。作为补偿,**导审计包时会顺带跑一次**并把结果放进包里:一个只报告、不
拦截、又不定时跑的检测,很容易变成一个从来没人调用的接口;接进导出至少保证它在每次真正需要
审计的时候一定被执行过。

## 比不了的地方要说出来

一份"零条缺口"的报告有三种可能:真的对得上、根本没法比、或者只比了一部分。后两种与第一种
长得一模一样,而它们是零信息。所以凡是让这次比对不完整的情况——浅克隆、历史被改写、旧版
审计日志、没纳入比对的路径——一律进 `notes`,而不是悄悄按"没问题"处理。

## 为什么不走 Forge

`space.forge` 收的是**外部网络依赖**(托管平台的 PR 操作),而这里读的是本地仓库的提交历史
——确定性的、跑得快的、测试里就该用真的那一类。把它塞进 Forge 会让那个窄口不再只关于网络。

## 看哪些路径

只看**有入口会为它写事件**的路径。员工定义、工序、规则同样是版本化资产,但系统里目前没有
任何一条路径会为它们写配置变更事件,把它们直接列进 `WATCHED` 的结果是每一次合法的 PR 都
变成一条缺口——而一份全是已知噪音的报告,没有人会看第二遍。

**但也不能装作它们不存在**:那样"报告里没有"就与"那里没事发生"分不开了。所以它们进
`UNWATCHED`——报告会说"这些路径上有 N 个提交,没有比对",把一个已知的盲区摆在明面上。
等它们也有了带事件的写入路径,再挪进 `WATCHED`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentgenome import paths
from agentgenome.core.events import SYSTEM_SUBJECT, Event, EventLog, LogKind
from agentgenome.space.gitcmd import git, is_repo_root

#: 纳入比对的路径。判据是"有入口会为它写事件",理由见模块说明。
WATCHED: tuple[str, ...] = (str(paths.ROOT_CONFIG), str(paths.GATES))

#: 已知的盲区:是配置、但还没有带事件的写入路径。**列出来,不比对。**
UNWATCHED: tuple[str, ...] = (str(paths.EMPLOYEES),)

#: 提交头行里各字段的分隔符。给 `git log --format` 用。
_SEP = "\x1f"


@dataclass(frozen=True)
class Gap:
    """一个改了受监视路径、却没有对应事件的提交。"""

    rev: str
    author: str
    at: str
    #: 这个提交改动的**受监视文件**。不是它改的全部文件——比对的对象是配置。
    files: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"rev": self.rev, "author": self.author, "at": self.at, "files": list(self.files)}


@dataclass(frozen=True)
class GapReport:
    watched: tuple[str, ...] = ()
    #: 受监视路径上一共看了多少个提交。
    commits: int = 0
    gaps: tuple[Gap, ...] = ()
    #: 让这次比对不完整的已知情况。见模块说明——**空报告与"只比了一部分"必须能分辨**。
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: 完全没法比对时说明原因(不是 git 仓库、读不到历史)。
    unavailable: str = ""

    @property
    def clean(self) -> bool:
        """**比对过的那部分**对得上。

        它不表示"没有问题":盲区与不完整之处在 `notes` 里,而那些地方压根没参与比对。所以
        渲染时 notes 一定跟着一起出——只报一个"对得上"、把保留意见留在结构体里,读到的人
        会当成一句全称的结论。
        """
        return not self.gaps and not self.unavailable

    def as_dict(self) -> dict[str, Any]:
        return {
            "watched": list(self.watched),
            "commits": self.commits,
            "gaps": [item.as_dict() for item in self.gaps],
            "notes": list(self.notes),
            "unavailable": self.unavailable,
        }


def detect(workspace_root: Path, watched: tuple[str, ...] = WATCHED) -> GapReport:
    """比对两个平面。**纯读**:不写事件、不建库、不改文件、不拦截任何东西。"""
    root = Path(workspace_root)
    if not is_repo_root(root):
        # **问的是"它自己是不是仓库"。** 往上找的话,一个放在别人仓库里的 Workspace 会拿到
        # 外层的历史,于是报出一堆与本 Workspace 无关的"缺口"。
        return GapReport(watched=watched, unavailable="这个 Workspace 不是 git 仓库,没有版本面可比")

    log = _log(root, watched)
    if log is None:
        return GapReport(
            watched=watched,
            unavailable=_why_no_history(root),
        )

    recorded = _recorded_revs(root)
    commits = _parse_log(log)
    gaps = tuple(commit for commit in commits if commit.rev not in recorded)
    return GapReport(
        watched=watched,
        commits=len(commits),
        gaps=gaps,
        notes=_notes(root, recorded),
    )


def record_scan(workspace_root: Path, report: GapReport, actor: str) -> Event:
    """把"什么时候查过、查出什么"记进事件面。

    与 `detect` 分开:检测本身必须是纯读的,而"查过没查过"又确实需要留痕。合成一个函数的话,
    "检测不改变任何状态"这句承诺就没有办法被测出来。

    **只记数量,不记缺口明细。** 明细在报告里(以及审计包里),事件面记的是动作;两处各存
    一份同样的内容,只会随保留期慢慢分叉。
    """
    return EventLog(workspace_root).append(
        SYSTEM_SUBJECT,
        actor=actor,
        kind=LogKind.GAP_SCAN,
        payload={
            "watched": list(report.watched),
            "commits": report.commits,
            "gaps": len(report.gaps),
            "notes": list(report.notes),
            "unavailable": report.unavailable,
        },
    )


def render(report: GapReport) -> str:
    """给人看的那一份。审计包里放的是它旁边那份 JSON。"""
    if report.unavailable:
        return f"没法比对:{report.unavailable}"
    lines = [
        f"受监视路径:{', '.join(report.watched)}",
        f"看过 {report.commits} 个提交,其中 {len(report.gaps)} 个没有对应事件。",
    ]
    if report.clean:
        # **不写"两个平面对得上"。** 下面还跟着盲区与不完整之处,一句全称的结论会把它们
        # 盖过去——而读的人只会记住第一句。
        lines.append("比对过的这部分对得上。")
    for gap in report.gaps:
        lines.append(f"  {gap.rev[:8]}  {gap.at}  {gap.author}")
        lines.append(f"    改了:{', '.join(gap.files) or '(合并提交,按第一父提交比)'}")
    if report.gaps:
        # 说清楚这不是指控。不说的话,第一份报告出来时会有人去追责一次合法的运维操作。
        lines.append("直接改仓库是合法的,这份清单只是说它没有走带记录的入口。")
    lines += [f"注意:{note}" for note in report.notes]
    return "\n".join(lines)


def _log(root: Path, watched: tuple[str, ...]) -> str | None:
    """受监视路径上的提交历史。读不出来返回 `None`。

    三个开关各自堵一个洞:

    - `--all`:**只看 HEAD 等于只看当前分支。** 往另一个 ref 上推一个改配置的提交就能完全
      绕过检测,而那正是这个模块要抓的动作。
    - `--full-history`:默认的历史简化会把与第一父提交同树的合并侧支剪掉,于是分支上那次
      修改在报告里消失。
    - `--diff-merges=first-parent`:合并提交默认不出 diff,文件清单是空的——而"在解冲突时
      顺手改了配置"恰恰是最该被看见的一种。
    - `core.quotePath=false`:非 ASCII 路径默认被转义成八进制,人对不上是哪个文件。
    """
    found = git(
        root,
        "-c",
        "core.quotePath=false",
        "log",
        "--all",
        "--full-history",
        "--diff-merges=first-parent",
        f"--format=%H{_SEP}%an{_SEP}%aI",
        "--name-only",
        "--",
        *watched,
        check=False,
    )
    return found.stdout if found.returncode == 0 else None


def _why_no_history(root: Path) -> str:
    """`git log` 失败的原因。

    **带上 git 自己的话。** 一个刚 init、还没有任何提交的仓库会走到这里,但对象库坏掉、
    仓库属主可疑、pathspec 不合法也会——统一写成"还没有提交"的话,一个坏掉的仓库看起来
    就跟一个崭新的仓库一模一样。
    """
    detail = git(root, "log", "-1", check=False).stderr.strip()
    return f"读不到提交历史(仓库里可能还没有提交):{detail}" if detail else "读不到提交历史"


def _recorded_revs(root: Path) -> frozenset[str]:
    """事件面上记过的那些 sha。

    **数据库不在就当没有记录,不要顺手把它建出来。** `EventLog(...)` 的构造会建目录、建表,
    而这个函数的调用方承诺了自己是纯读的——在一个别人的目录里跑一次检测,不该留下一个空的
    Workspace 骨架。
    """
    if not (root / paths.DATABASE).is_file():
        return frozenset()
    return EventLog(root).payload_values(LogKind.CONFIG_CHANGED, "rev")


def _notes(root: Path, recorded: frozenset[str]) -> tuple[str, ...]:
    """这次比对有哪些地方不完整。理由见模块说明。"""
    found: list[str] = []
    if git(root, "rev-parse", "--is-shallow-repository", check=False).stdout.strip() == "true":
        found.append("这是一个浅克隆,只比对了拉下来的那一段历史")
    orphaned = _unreachable(root, recorded)
    if orphaned:
        found.append(
            f"有 {orphaned} 条事件指向的提交已经不在任何分支上了(历史被改写过),"
            "它们记录过的那些改动会以缺口的形式出现"
        )
    legacy = root / paths.TASKS / "settings-audit.jsonl"
    if legacy.is_file():
        found.append("这个 Workspace 有旧版设置审计日志,那时的记录不带 sha,无法参与比对")
    skipped = _count_commits(root, UNWATCHED)
    if skipped:
        found.append(
            f"{', '.join(UNWATCHED)} 上有 {skipped} 个提交没有比对——系统还没有为它们写事件的入口"
        )
    return tuple(found)


def _unreachable(root: Path, revs: frozenset[str]) -> int:
    """记过、但已经不在任何分支上的 sha 有几个。rebase / force-push 之后会出现。

    **问"还在不在某个分支上",不问"对象还在不在"。** amend 过的提交在对象库里躺着,
    `cat-file` 照样找得到它——而它已经不属于任何历史了,于是那次走了正门的改动会以缺口的
    形式出现,报告指认的"嫌疑人"是编排器自己。

    一个 rev 一次 git 调用:配置变更的条数是人手动改配置的次数,这个量级不值得为它写批处理。
    """
    return sum(1 for rev in sorted(revs) if not _reachable(root, rev))


def _reachable(root: Path, rev: str) -> bool:
    found = git(root, "for-each-ref", "--contains", rev, "--count=1", check=False)
    return found.returncode == 0 and bool(found.stdout.strip())


def _count_commits(root: Path, watched: tuple[str, ...]) -> int:
    found = git(root, "log", "--all", "--full-history", "--format=%H", "--", *watched, check=False)
    return len(found.stdout.split()) if found.returncode == 0 else 0


def _parse_log(raw: str) -> list[Gap]:
    """把 `git log --name-only` 的输出切成一条条提交。

    按**头行**切而不是按空行切:一个提交的文件清单可以为空,按空行切会把它与下一个提交并成
    一条。

    头行按"第一段是 sha、最后一段是时间、中间全是名字"拆,而不是直接解包三个字段:`%an`
    是提交者自己写的字符串,里面完全可以有分隔符——而**能控制那个字段的人正是这个模块要抓
    的人**。直接解包的话,他只要把分隔符写进自己的名字,整个检测就抛异常。
    """
    found: list[Gap] = []
    files: list[str] = []
    rev = author = at = ""
    for line in raw.splitlines():
        parts = line.split(_SEP)
        if len(parts) >= 3:
            if rev:
                found.append(Gap(rev=rev, author=author, at=at, files=tuple(files)))
            rev, at = parts[0], parts[-1]
            author = _SEP.join(parts[1:-1])
            files = []
        elif line.strip():
            files.append(line.strip())
    if rev:
        found.append(Gap(rev=rev, author=author, at=at, files=tuple(files)))
    return found


__all__ = [
    "UNWATCHED",
    "WATCHED",
    "Gap",
    "GapReport",
    "detect",
    "record_scan",
    "render",
]
