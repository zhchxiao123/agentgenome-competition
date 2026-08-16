"""系统设置的读写与审计。

## 内容进版本面,动作进事件面

设置页能改预算、并发、审批人名单、集成配置——这几样的影响面比大多数代码变更都大,所以
"谁改的、什么时候、改成了什么"必须事后查得到。两个平面各记各能记准的那一半:

**内容归 git。** 配置本来就是版本化资产,`git show` 一条命令就能答"改成了什么",而且不会
撒谎。所以这里**不记前值后值**——理由不是省事:前端读到配置与人按下保存之间,别人可能已经
改过一轮,那时写进记录的"前值"是前端读到的旧值,不是真实的前值。它看起来跟真的一模一样,
出事时没有任何办法分辨。git 的 diff 不会犯这个错。

**真人身份归事件面。** 提交用的是编排器的机器人身份(见 `space.gitcmd`),版本面记不下
"是谁在界面上点的保存"——而追责需要的恰恰是这一条。发起入口同理。

## 两个平面要么都写,要么都不写

先写文件再提交,提交失败就把文件**改回去**。少了这一步,一次失败的保存会留下一份已经生效
但**两个平面上都没有记录**的配置——那是最坏的一种状态:系统按新值在跑,而任何一种查法都
查不到它是什么时候、被谁改成这样的。

## 配置写回去会丢注释

`yaml.safe_dump` 重排整份文件,人手写的注释留不下来。知道这件事仍然这么做:引一个保注释的
YAML 实现是一整个依赖,而配置的解释权本来就该在文档与提交信息里,不在文件里的行末。**代价
写在这里**,以免下一个人以为它是个 bug 顺手"修"成手工拼字符串。

## 设置的真相仍然在文件里

这一层不是第二份配置存储,它只是**带审计的文件写入**。做成数据库里的一份副本的话,会出现
"文件里写着 3、界面上显示 5"这种谁都说不清的状态——而基因组资产是版本化的,那正是它的价值。
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from agentgenome import paths
from agentgenome.config import Config
from agentgenome.core.events import SYSTEM_SUBJECT, EventLog, LogKind
from agentgenome.genome.loader import parse_yaml_model
from agentgenome.server.rbac import Action, Principal
from agentgenome.space.gitcmd import ORCHESTRATOR_IDENTITY, git, git_out, is_repo_root

#: 设置变更的旧审计日志。**保留它只为读回历史**:新的变更进事件面,见 `update`。
SETTINGS_AUDIT = Path("tasks") / "settings-audit.jsonl"

#: 读—改—写的互斥文件。**整体互斥,不是只锁提交。** 两个人同时改不同的段时,后写的那个人
#: 手里是他读到配置那一刻的整份内容,于是他的写入会把前一个人的改动原样盖掉——而两个人都
#: 会看到"保存成功"。git 的 index.lock 挡不住这个,它只挡同时提交。
SETTINGS_LOCK = Path("tasks") / "settings.lock"

#: 一次读回多少条配置变更。**有上限就会有被截掉的记录**,所以它不能悄悄发生:超出时
#: `history` 会在最早那条上标 `truncated`,由调用方决定是提示还是翻页。
HISTORY_LIMIT = 1000

#: 允许从界面改的顶层段。**白名单而不是黑名单**:加一个新段时,默认是不可改的。
#:
#: 每一项都必须是 `config.Config` 上真实存在的字段——白名单里放一个模型不认的段名,写进去
#: 的配置会通过校验前的每一道关卡,然后在下一次 `load_config` 时整个 Workspace 起不来。
#:
#: `topology` 与 `quality_line` 在这里,而配置层**没有把它们合并**:评审那一档仍然住在
#: `topology.critique`,搬进质量线配置节是一次破坏性迁移(配置模型拒绝未知键,存量文件会
#: 直接加载失败)。要一起看是**界面**的事,员工管理页那张卡片已经解决了它。
#:
#: `runtime` 在这里是 **PRD 33 修订过的一条决策**。这里原先写着"运行时与员工定义是版本化
#: 资产,改它们要走 git 评审",于是启用容器运行时必须离开界面去编辑文件——而失败要等到
#: 派发任务时才暴露。修订理由有二:其一,**这条写入路径本身就提交进 git 并记事件**,
#: "走 git"这条保证不因入口是界面而失效;其二,员工定义早已可以从界面改(执行档位那条
#: 走的就是这条路),把运行时排除在外只是历史顺序,不是原则。
#:
#: **凭证仍然不在这里。** 运行时段存的是令牌的**环境变量名**,值只在服务端进程里——
#: 所以放开这一段不等于把密钥端上界面。
EDITABLE = frozenset(
    {
        "concurrency",
        "budgets",
        "limits",
        "itest",
        "genome_tasks",
        "approval",
        "topology",
        "quality_line",
        "runtime",
    }
)


class NotEditable(PermissionError):
    """这一段不允许从界面改。"""


class Entrance(StrEnum):
    """这次变更从哪儿进来的。

    **有限集合而不是自由文本**:审计问的是"走界面改的还是脚本改的",而自由文本的第一个后果
    是同一个入口出现三种写法,于是按入口筛选这件事直接失效。
    """

    WEB = "web"
    CLI = "cli"
    API = "api"

    @classmethod
    def parse(cls, value: object, fallback: Entrance) -> Entrance:
        """认不出来的入口退回 `fallback`,**不抛**。

        历史记录里出现一个没见过的值(手改过的文件、更新版本写的新入口),那一条读不出来是
        可以接受的;因为它而整个历史页面打不开不可以。
        """
        try:
            return cls(str(value))
        except ValueError:
            return fallback


@dataclass(frozen=True)
class Change:
    """一次设置变更。

    `before` / `after` **只服务于读回旧审计日志**,新记录一律留空——见模块说明。
    """

    actor: str
    section: str
    at: str
    entrance: Entrance = Entrance.WEB
    #: 结果提交。不是 git 仓库、或者这次写入没有改变内容时为空。
    rev: str = ""
    before: Any = None
    after: Any = None
    #: 这条之前还有记录,只是没读回来。见 `HISTORY_LIMIT`。
    truncated: bool = False

    @property
    def is_legacy(self) -> bool:
        """来自旧审计日志。判据是它带了新记录不会有的前值后值。"""
        return self.before is not None or self.after is not None

    def as_dict(self) -> dict[str, Any]:
        found = {
            "actor": self.actor,
            "section": self.section,
            "at": self.at,
            "entrance": self.entrance.value,
            "rev": self.rev,
            "truncated": self.truncated,
        }
        if self.is_legacy:
            # 旧记录里确实有这两样,读回来就得给出去——否则解析它们的那段代码没有任何读者。
            # 新记录**不带**这两个键,`update` 里那条否定断言守着。
            found |= {"before": self.before, "after": self.after}
        return found


def update(
    workspace_root: Path,
    principal: Principal,
    section: str,
    value: dict[str, Any],
    entrance: Entrance = Entrance.API,
) -> Change:
    """改一段配置:校验 → 写文件 → 提交进版本面 → 在事件面记这次动作。

    **权限校验在这里,不在前端。** 前端藏起表单只是展示裁剪。

    **提交在写事件之前。** 事件带的 sha 必须指向一个真实存在、且真的改了配置的提交;
    反过来先记事件的话,提交失败会留下一条"发生过"的记录指向一个不存在的提交——而那种记录
    比没有记录更糟,它会让查的人相信自己已经查到了。

    提交失败时文件会被改回去,见模块说明。抛 `GitError`,调用方要把"没改成"告诉人。
    """
    principal.ensure(Action.EDIT_SETTINGS)
    if section not in EDITABLE:
        raise NotEditable(
            f"{section} 不允许从界面改(可改: {', '.join(sorted(EDITABLE))})。"
            "白名单之外的段是版本化资产,改它们要走 git 评审。"
        )

    root = Path(workspace_root)
    target = root / paths.ROOT_CONFIG
    with _exclusive(root):
        original = target.read_text(encoding="utf-8") if target.is_file() else None
        payload = yaml.safe_load(original) if original else {}
        payload = payload if isinstance(payload, dict) else {}
        payload[section] = value
        # **先校验再落盘。** 提交一份加载不了的配置,等于把整个 Workspace 的下一次启动
        # 押在没人会去看的 git 历史上;而这一层恰恰是唯一能在写之前拦住它的地方。
        parse_yaml_model(Config, payload, str(paths.ROOT_CONFIG))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

        try:
            rev = _commit(root, section, principal.subject)
        except Exception:
            _restore(root, target, original)
            raise

        event = EventLog(root).append(
            SYSTEM_SUBJECT,
            actor=principal.subject,
            kind=LogKind.CONFIG_CHANGED,
            # 这里**只有动作,没有内容**:section 说改的是哪一段,rev 说改成了什么。
            payload={"section": section, "entrance": entrance.value, "rev": rev},
        )
    return Change(
        actor=principal.subject,
        section=section,
        at=event.ts.isoformat(),
        entrance=entrance,
        rev=rev,
    )


def history(workspace_root: Path, limit: int = HISTORY_LIMIT) -> list[Change]:
    """读回设置变更历史:事件面的新记录 + 旧审计日志里的老记录,按时间排。

    旧记录不迁移也不丢。迁移要重写它们的 `at` 语义与身份来源,而那是拿"当时到底怎么记的"
    去换整齐——审计日志恰恰不该被后来的人修饰。
    """
    root = Path(workspace_root)
    events = EventLog(root).all_events(kind=LogKind.CONFIG_CHANGED, limit=limit + 1)
    truncated = len(events) > limit
    found = [
        Change(
            actor=event.actor,
            section=str(event.payload.get("section", "")),
            at=event.ts.isoformat(),
            entrance=Entrance.parse(event.payload.get("entrance"), Entrance.API),
            rev=str(event.payload.get("rev", "")),
        )
        for event in events[:limit]
    ]
    found += _legacy(root)
    found.sort(key=_at)
    if truncated and found:
        # 截断标在**最早**那条上:被砍掉的都比它更早,而看历史的人正是从那一头往回找。
        found[0] = replace(found[0], truncated=True)
    return found


def _at(change: Change) -> datetime:
    """排序用的时间。

    **按解析出来的时刻排,不按字符串排。** 旧日志里的偏移量不一定是 `+00:00`,而
    `2020-01-01T08:00:00+08:00` 按字符串排会跑到同一时刻的 UTC 写法后面。
    """
    try:
        return datetime.fromisoformat(change.at).astimezone(UTC)
    except ValueError:
        # 时间戳坏掉的记录排到最前面,而不是让整份历史读不出来。
        return datetime.min.replace(tzinfo=UTC)


def _legacy(root: Path) -> list[Change]:
    audit = root / SETTINGS_AUDIT
    if not audit.is_file():
        return []
    found = []
    for line in audit.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        found.append(
            Change(
                actor=payload.get("actor", ""),
                section=payload.get("section", ""),
                at=payload.get("at", ""),
                entrance=Entrance.parse(payload.get("entrance"), Entrance.WEB),
                rev=payload.get("rev", ""),
                before=payload.get("before"),
                after=payload.get("after"),
            )
        )
    return found


@contextmanager
def _exclusive(root: Path) -> Iterator[None]:
    """把整个读—改—写—提交串起来。理由见 `SETTINGS_LOCK`。"""
    lock = root / SETTINGS_LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _restore(root: Path, target: Path, original: str | None) -> None:
    """把配置文件改回提交之前的样子,顺带把索引也还原。

    **尽力而为**:恢复本身再失败的话,原来那个异常比它更值得让人看到。
    """
    try:
        if original is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(original, encoding="utf-8")
        git(root, "reset", "-q", "--", str(paths.ROOT_CONFIG), check=False)
    except OSError:
        pass


def _commit(root: Path, section: str, actor: str) -> str:
    """把配置提交进版本面,返回结果提交的 sha。

    **不是 git 仓库时返回空而不是报错。** 一个还没 git init 的 Workspace 照样要能改配置,
    它只是没有版本面可写;事件照记,rev 留空说明这次没有可追的提交。**内容没变时也返回空**
    ——没有新提交可指,而随手指向 HEAD 会让审计以为那个提交改过配置。

    只提交配置文件这一条路径(`commit -- <path>`),不扫周围的改动:一次改并发数不该顺手把
    别人正在改的东西一起提交掉。
    """
    relative = str(paths.ROOT_CONFIG)
    if not is_repo_root(root):
        return ""
    git(root, "add", "--", relative)
    if git(root, "diff", "--cached", "--quiet", "--", relative, check=False).returncode == 0:
        return ""
    git(
        root,
        *ORCHESTRATOR_IDENTITY,
        "commit",
        "-m",
        f"chore(settings): {actor} 改了 {section}",
        "--",
        relative,
    )
    return git_out(root, "rev-parse", "HEAD")


__all__ = [
    "EDITABLE",
    "HISTORY_LIMIT",
    "SETTINGS_AUDIT",
    "SETTINGS_LOCK",
    "Change",
    "Entrance",
    "NotEditable",
    "history",
    "update",
]
