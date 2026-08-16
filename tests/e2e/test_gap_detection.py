"""缺口检测:绕过界面改的配置也要查得到。

配置是 git 里的普通文件,谁都能直接推一个提交上去,而那样改不产生任何事件。走界面改查得到、
直接推代码改查不到——**而后者恰恰是更需要被审计的那一种**。这一组盯的是那个洞被列了出来,
以及它**只被列出来**:不拦截、不改状态、不定时跑。
"""

from __future__ import annotations

import inspect
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentgenome import paths
from agentgenome.cli import app as cli_app
from agentgenome.config import Config
from agentgenome.core.events import SYSTEM_SUBJECT, EventLog, LogKind
from agentgenome.security import gaps
from agentgenome.security.audit import (
    GAP_REPORT,
    archive_root,
    export,
    export_task_bundle,
    gap_snapshot,
    seal_manifest,
)
from agentgenome.server.app import create_app
from agentgenome.server.rbac import Principal, Role
from agentgenome.server.settings import update
from agentgenome.space.gitcmd import git
from tests.fixtures.mall import materialize_mall

runner = CliRunner()

ROOT = Principal("root", frozenset({Role.ADMIN}))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / paths.ROOT_CONFIG).write_text("concurrency: {global_jobs: 3}\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return root


def _push_config_directly(root: Path, jobs: int, who: str = "mallory") -> str:
    """绕过命令层,直接用 git 改配置。"""
    (root / paths.ROOT_CONFIG).write_text(
        f"concurrency: {{global_jobs: {jobs}}}\n", encoding="utf-8"
    )
    git(root, "add", "--", str(paths.ROOT_CONFIG))
    git(
        root,
        "-c",
        f"user.name={who}",
        "-c",
        "user.email=m@m",
        "commit",
        "-q",
        "-m",
        "偷偷调一下并发",
    )
    return git(root, "rev-parse", "HEAD").stdout.strip()


def test_a_commit_that_bypassed_the_command_layer_shows_up(workspace: Path) -> None:
    """事件面上的洞要能被指名道姓地列出来,否则"缺口可检测"只是一句话。"""
    rev = _push_config_directly(workspace, 9)

    report = gaps.detect(workspace)

    assert rev in {item.rev for item in report.gaps}


def test_the_report_says_who_when_and_which_files(workspace: Path) -> None:
    """列出提交号还不够——审计员拿到报告之后的下一步是"去问谁"。"""
    rev = _push_config_directly(workspace, 9, who="mallory")

    (gap,) = [item for item in gaps.detect(workspace).gaps if item.rev == rev]

    assert gap.author == "mallory"
    assert gap.at  # ISO 时间戳
    assert gap.files == (str(paths.ROOT_CONFIG),)


def test_a_change_made_through_the_command_layer_is_not_a_gap(workspace: Path) -> None:
    """走带记录的入口改的不该出现在报告里。**否则报告全是噪音,没人会看第二遍。**"""
    change = update(workspace, ROOT, "concurrency", {"global_jobs": 8})

    report = gaps.detect(workspace)

    assert change.rev not in {item.rev for item in report.gaps}
    assert report.commits == 2  # 建仓那次 + 这次


def test_the_initial_commit_counts_as_a_gap_and_that_is_honest(workspace: Path) -> None:
    """建仓时带进来的配置也没有事件。

    **不特批它。** 一开始就例外一条,下一个人就会问"那这条呢",而例外清单是缺口检测失效
    最常见的方式。它是真的没有事件,如实列出来。
    """
    report = gaps.detect(workspace)

    assert len(report.gaps) == 1


def _tree(root: Path) -> list[str]:
    return sorted(
        str(item.relative_to(root)) for item in root.rglob("*") if ".git/" not in str(item)
    )


def test_detection_changes_nothing(workspace: Path) -> None:
    """**只报告。** 不写事件、不建库、不改文件、不拦截——直接改仓库是合法的运维手段。

    快照整棵树而不是只看几个已知文件:漏掉的正是没想到的那一个。这条曾经真的漏过——
    `EventLog(...)` 的构造会把 `tasks/` 与数据库建出来,而只断言 HEAD 与配置内容的版本
    看不见它。
    """
    _push_config_directly(workspace, 9)
    before_head = git(workspace, "rev-parse", "HEAD").stdout.strip()
    before_tree = _tree(workspace)

    gaps.detect(workspace)

    assert git(workspace, "rev-parse", "HEAD").stdout.strip() == before_head
    assert _tree(workspace) == before_tree


def test_the_scan_itself_leaves_a_record(workspace: Path) -> None:
    """ "什么时候查过、查出什么"也要有记录——否则"我们查过了"这句话没有证据。"""
    _push_config_directly(workspace, 9)

    result = runner.invoke(cli_app, ["gaps", "--as", "auditor", "-w", str(workspace)])

    assert result.exit_code == 0, result.output
    (event,) = EventLog(workspace).all_events(kind=LogKind.GAP_SCAN)
    assert event.actor == "auditor"
    assert event.task_id == SYSTEM_SUBJECT
    assert event.payload["gaps"] == 2  # 建仓那次 + 偷偷改的那次


def test_the_scan_event_carries_counts_not_the_commit_list(workspace: Path) -> None:
    """事件面记动作,明细在报告与审计包里。

    把整份清单塞进事件载荷的话,同一份内容会在两处各存一份、然后随保留期慢慢分叉。
    """
    _push_config_directly(workspace, 9)
    gaps.record_scan(workspace, gaps.detect(workspace), actor="auditor")

    (event,) = EventLog(workspace).all_events(kind=LogKind.GAP_SCAN)

    assert set(event.payload) == {"watched", "commits", "gaps", "notes", "unavailable"}
    assert isinstance(event.payload["gaps"], int)


def test_the_endpoint_reports_the_same_thing_the_command_does(workspace: Path) -> None:
    """一个命令与一个接口。两条路给出不同答案的话,审计员该信哪一个没有答案。"""
    _push_config_directly(workspace, 9)
    client = TestClient(create_app(workspace, principals={"root": ROOT}))

    body = client.post("/audit/gaps", headers={"x-actor": "root"}).json()
    printed = runner.invoke(cli_app, ["gaps", "--as", "auditor", "--json", "-w", str(workspace)])

    assert printed.exit_code == 0, printed.output
    assert [item["rev"] for item in body["gaps"]] == [
        item["rev"] for item in json.loads(printed.stdout)["gaps"]
    ]


def test_the_endpoint_needs_audit_permission(workspace: Path) -> None:
    """它回答的是"谁绕过界面改了配置",跟审计检索是同一类问题。"""
    response = TestClient(create_app(workspace, principals={"root": ROOT})).post("/audit/gaps")

    assert response.status_code == 403


def test_the_audit_bundle_carries_a_fresh_gap_report(workspace: Path) -> None:
    """一个只报告、不拦截、又不定时跑的检测,很容易变成没人调用的接口。

    挂在导出上,它至少在**每次真正需要审计的时候**一定被跑过一次。
    """
    _push_config_directly(workspace, 9)
    (workspace / paths.TASKS / "ag-0001").mkdir(parents=True)
    (workspace / paths.TASKS / "ag-0001" / "task.json").write_text("{}", encoding="utf-8")

    package = export_task_bundle(workspace, "ag-0001", Config())

    with zipfile.ZipFile(package.path) as bundle:
        report = json.loads(bundle.read(GAP_REPORT))
    assert len(report["gaps"]) == 2
    assert GAP_REPORT in package.entries


def test_no_scheduled_job_is_registered(workspace: Path) -> None:
    """否定断言:**不做定时任务。**

    定时跑出来的结果没人看,却会制造"已经在监控了"的错觉——那比没有检测更糟。这条守着的是
    "顺手加个定时器更省事"这类改动:它加起来只要三行,而它会把这份检测的意义反过来。

    断言用**子串**而不是按空白切词:切词版本里,真实的依赖行 `"apscheduler>=3.10",` 不等于
    `apscheduler`,于是这条断言永远为真——一条结构上不可能失败的测试比没有测试更糟,因为
    它读起来像已经守住了。
    """
    dependencies = (
        (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8").lower()
    )
    for library in ("apscheduler", "croniter", "celery", "schedule"):
        assert library not in dependencies

    source = (Path(inspect.getfile(gaps)).parent.parent).rglob("*.py")
    callers = {
        str(path.relative_to(Path(inspect.getfile(gaps)).parents[2]))
        for path in source
        if "gaps.detect" in path.read_text(encoding="utf-8")
    }
    # 调用点是固定的三处:一条命令、一个接口、导审计包时顺带那一次。多出来的那一处就是
    # 有人给它接了个调度器,或者接进了某条会被反复触发的路径。
    assert callers == {
        "agentgenome/cli.py",
        "agentgenome/server/app.py",
        "agentgenome/security/audit.py",
    }


def test_a_workspace_without_git_says_it_cannot_compare(tmp_path: Path) -> None:
    """ "零条缺口"与"比不了"必须能分辨。后者是零信息,而它们长得一模一样。"""
    root = tmp_path / "bare"
    root.mkdir()

    report = gaps.detect(root)

    assert report.gaps == ()
    assert report.unavailable
    assert not report.clean


def test_a_commit_pushed_to_another_branch_is_not_invisible(workspace: Path) -> None:
    """**只看 HEAD 等于只看当前分支。**

    往另一个 ref 上推一个改配置的提交就能完全绕过检测——而那正是这个模块要抓的动作。更糟的
    是它不会报错:报告说"两个平面对得上",那条"查过了"的事件也照写。
    """
    git(workspace, "checkout", "-q", "-b", "sneaky")
    rev = _push_config_directly(workspace, 99)
    git(workspace, "checkout", "-q", "-")

    assert rev in {item.rev for item in gaps.detect(workspace).gaps}


def test_a_merge_that_edited_the_config_says_which_files(workspace: Path) -> None:
    """合并提交默认不出 diff,文件清单会是空的。

    而"在解冲突时顺手改了配置"恰恰是最该被看见的一种——把它渲染成一行没有文件的记录,等于
    把嫌疑最大的那条做成信息量最小的那条。
    """
    git(workspace, "checkout", "-q", "-b", "side")
    _push_config_directly(workspace, 42)
    git(workspace, "checkout", "-q", "-")
    (workspace / paths.ROOT_CONFIG).write_text("concurrency: {global_jobs: 7}\n", encoding="utf-8")
    git(workspace, "add", "-A")
    git(workspace, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "main 侧")
    git(workspace, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "side", check=False)
    (workspace / paths.ROOT_CONFIG).write_text("concurrency: {global_jobs: 1}\n", encoding="utf-8")
    git(workspace, "add", "-A")
    merge = git(
        workspace, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "解冲突"
    )
    assert merge.returncode == 0, merge.stderr

    rev = git(workspace, "rev-parse", "HEAD").stdout.strip()
    (gap,) = [item for item in gaps.detect(workspace).gaps if item.rev == rev]

    assert gap.files == (str(paths.ROOT_CONFIG),)


def test_a_name_carrying_the_field_separator_does_not_break_the_scan(workspace: Path) -> None:
    """提交者自己写的字段里出现分隔符,不该把整个检测炸掉。

    **能控制那个字段的人正是这个模块要抓的人**——直接解包三个字段的版本里,他只要把分隔符
    写进自己的名字,检测就抛异常,而抛异常的检测与不存在的检测没有区别。
    """
    _push_config_directly(workspace, 9, who="ma\x1fllory")

    report = gaps.detect(workspace)

    assert any("llory" in item.author for item in report.gaps)


def test_a_shallow_clone_says_it_only_compared_part_of_the_history(
    workspace: Path, tmp_path: Path
) -> None:
    """浅克隆里"零条缺口"是零信息:被截掉的那段历史里有什么,没有人知道。"""
    _push_config_directly(workspace, 9)
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{workspace}", str(shallow))

    report = gaps.detect(shallow)

    assert any("浅克隆" in note for note in report.notes)


def test_a_rewritten_history_is_called_out_not_reported_as_a_bypass(workspace: Path) -> None:
    """rebase / force-push 之后,记过的 sha 在仓库里找不到了。

    只按 sha 比的话,那次**走了正门**的改动会变成一条缺口,而报告指认的"嫌疑人"是编排器
    自己。缺口照列——它确实对不上——但要说清楚原因,否则第一份报告就会把人送去查一件没
    发生过的事。
    """
    update(workspace, ROOT, "concurrency", {"global_jobs": 8})
    git(
        workspace,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--amend",
        "-m",
        "改写",
    )

    report = gaps.detect(workspace)

    assert any("历史被改写" in note for note in report.notes)


def test_the_blind_spot_is_named_instead_of_silently_skipped(workspace: Path) -> None:
    """`employees/` 没有比对。

    不说的话,"报告里没有"就与"那里没事发生"分不开了——而前者恰恰是这份报告最容易给人的
    错误印象。
    """
    (workspace / paths.EMPLOYEES).mkdir(parents=True, exist_ok=True)
    (workspace / paths.EMPLOYEES / "dev.yaml").write_text("id: dev\n", encoding="utf-8")
    git(workspace, "add", "-A")
    git(workspace, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "加个员工")

    report = gaps.detect(workspace)

    assert any(str(paths.EMPLOYEES) in note for note in report.notes)


def test_the_render_says_a_bypass_is_not_an_accusation(workspace: Path) -> None:
    """直接改仓库是合法的运维手段。

    这句话在报告里不是客套:第一份报告出来时,没有它就会有人拿着清单去追责一次合法操作。
    """
    _push_config_directly(workspace, 9)

    text = gaps.render(gaps.detect(workspace))

    assert "直接改仓库是合法的" in text


def test_the_render_of_an_unusable_report_does_not_read_as_clean(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    root.mkdir()

    text = gaps.render(gaps.detect(root))

    assert "没法比对" in text
    assert "对得上" not in text


def test_a_freshly_initialised_workspace_reports_clean(tmp_path: Path) -> None:
    """`agctl init` 自己带进来的那份配置也补了事件。

    不补的话,每个 Workspace 从建成第一天起就永久带着一条缺口,而那条缺口的「嫌疑人」是
    系统自己——一份从第一行起就有已知噪音的报告,没有人会看第二遍。补记录而不是在检测那边
    开例外:例外清单一旦开头,下一个人就会问「那这条呢」。
    """
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        cli_app,
        [
            "init",
            "--local-only",
            str(root),
            "--name",
            "demo",
            "--repo",
            mall["order-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output

    report = gaps.detect(root)

    assert report.gaps == ()
    assert report.clean
    # 唯一的保留意见是那个已知盲区。多出一条就说明建仓路径上又有东西没被记录。
    assert [note for note in report.notes if str(paths.EMPLOYEES) not in note] == []


def test_the_sealed_bundle_is_the_one_that_carries_the_report(workspace: Path) -> None:
    """终态自动固化的那份包也要带缺口检测。

    **它是原始日志被清掉之后唯一留下来的那一份**,而封存过的包不重打——固化时不带的话,
    事后无论从哪个入口再导一次，拿到的都是没有报告的那一个。
    """
    _push_config_directly(workspace, 9)
    task_dir = workspace / paths.TASKS / "ag-0001"
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text("{}", encoding="utf-8")
    sealed = export(
        workspace,
        "ag-0001",
        archive=archive_root(workspace),
        manifest=seal_manifest("ag-0001", "done", "now"),
        extras=gap_snapshot(workspace)[0],
    )

    reexported = export_task_bundle(workspace, "ag-0001", Config())

    assert reexported.already_sealed
    assert GAP_REPORT in sealed.entries
    with zipfile.ZipFile(reexported.path) as bundle:
        assert GAP_REPORT in bundle.namelist()


def test_a_workspace_without_git_does_not_get_a_clean_looking_manifest(tmp_path: Path) -> None:
    """清单是给人看的那份摘要,`gaps: 0` 在那里读起来就是「没问题」。

    而真相是「根本没比」——那是零信息,不是零缺口。
    """
    root = tmp_path / "bare"
    (root / paths.TASKS / "ag-0001").mkdir(parents=True)
    (root / paths.TASKS / "ag-0001" / "task.json").write_text("{}", encoding="utf-8")

    _, summary = gap_snapshot(root)

    assert summary["gaps"] is None
    assert summary["gaps_unavailable"]
