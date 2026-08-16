"""配置变更:动作进事件面,内容进版本面。

**这一组盯的是分工。** 一次改并发数会在两个平面上各留一半记录,而每一半都只在自己那个平面
上记得准:真人身份 git 记不下(提交用的是机器人身份),前值后值事件面记不准(前端读到配置到
人按下保存之间,别人可能已经改过一轮)。所以这里既断言"该在的在",也断言**"不该在的不在"**
——前值后值那条是否定断言,单独一条用例。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentgenome import paths
from agentgenome.cli import app as cli_app
from agentgenome.core.events import ORCHESTRATOR, SYSTEM_SUBJECT, Event, EventLog, LogKind
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.server.app import create_app
from agentgenome.server.rbac import Principal, Role
from agentgenome.server.settings import Entrance, update
from agentgenome.space.gitcmd import ORCHESTRATOR_IDENTITY, GitError, git, git_out

runner = CliRunner()

#: 浏览器一定会发、页面脚本改不了的那组头。curl 不会发。
BROWSER = {"sec-fetch-mode": "cors", "sec-fetch-site": "same-origin"}

#: `-c user.name=AgentGenome` 里的那个值。
ROBOT_NAME = ORCHESTRATOR_IDENTITY[1].split("=", 1)[1]

ROOT = Principal("root", frozenset({Role.ADMIN}))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """一个最小的 git 仓库 Workspace。

    不走 `agctl init`:这一组只关心配置文件与 git,而 init 会拉远端、建骨架,把用例的失败
    原因摊到一整条链路上。
    """
    root = tmp_path / "ws"
    root.mkdir()
    (root / paths.ROOT_CONFIG).write_text("concurrency: {global_jobs: 3}\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return root


def _app(workspace: Path) -> FastAPI:
    return create_app(workspace, principals={"root": ROOT})


def _config_events(workspace: Path) -> tuple[Event, ...]:
    return EventLog(workspace).all_events(kind=LogKind.CONFIG_CHANGED)


def test_a_settings_change_lands_on_the_event_plane(workspace: Path) -> None:
    """不是那份独立 JSONL——进事件面才能被 `/activity` 与审计检索看到。"""
    response = TestClient(_app(workspace)).put(
        "/settings",
        json={"section": "concurrency", "value": {"global_jobs": 8}},
        headers={"x-actor": "root", **BROWSER},
    )

    assert response.status_code == 200, response.text
    (event,) = _config_events(workspace)
    assert event.kind is LogKind.CONFIG_CHANGED
    assert event.task_id == SYSTEM_SUBJECT
    assert event.payload["section"] == "concurrency"


def test_the_event_names_the_human_and_the_commit_names_the_robot(workspace: Path) -> None:
    """两个平面各记各能记准的那一半。

    追责要的是"谁在界面上点的保存",而提交的作者是编排器——如果只看 git,查到的永远是机器人。
    """
    TestClient(_app(workspace)).put(
        "/settings",
        json={"section": "budgets", "value": {"per_task_tokens": 99}},
        headers={"x-actor": "root", **BROWSER},
    )

    (event,) = _config_events(workspace)
    assert event.actor == "root"
    assert event.actor != ORCHESTRATOR
    author = git_out(workspace, "log", "-1", "--format=%an", event.payload["rev"])
    # 精确相等:`in` 对 "AgentGenome Employee" 也成立,而员工身份与编排器身份混掉正是
    # `space.gitcmd` 说必须一眼分辨的那件事。
    assert author == ROBOT_NAME


def test_the_entrance_is_read_off_the_request_not_self_reported(workspace: Path) -> None:
    """浏览器发 `Sec-Fetch-*`,脚本默认不发。

    **这条判据只在一个方向上硬**:没这组头基本可以断定不是从界面来的,反过来不成立
    (谁都能手工加上)。它省掉的是"body 里填什么就信什么"这种连痕迹都不留的自报。
    """
    client = TestClient(_app(workspace))

    client.put(
        "/settings",
        json={"section": "concurrency", "value": {"global_jobs": 4}},
        headers={"x-actor": "root", **BROWSER},
    )
    client.put(
        "/settings",
        json={"section": "concurrency", "value": {"global_jobs": 5}},
        headers={"x-actor": "root"},
    )

    entrances = [event.payload["entrance"] for event in _config_events(workspace)]
    # `all_events` 按 seq 倒序,最近的在前。
    assert entrances == [Entrance.API.value, Entrance.WEB.value]


def test_the_command_line_is_its_own_entrance(workspace: Path) -> None:
    """三个入口都要有真的产出者,否则"按入口筛选"是个永远筛不出东西的字段。"""
    result = runner.invoke(
        cli_app,
        [
            "settings",
            "set",
            "limits",
            '{"max_fix_rounds": 5}',
            "--as",
            "alice",
            "-w",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    (event,) = _config_events(workspace)
    assert event.payload["entrance"] == Entrance.CLI.value
    assert event.actor == "alice"


def test_the_event_points_at_a_commit_that_really_changed_the_config(workspace: Path) -> None:
    """指向一个存在的提交还不够——它得**确实改了配置文件**,否则这个 sha 只是装饰。"""
    change = update(workspace, ROOT, "concurrency", {"global_jobs": 7})

    assert git(workspace, "cat-file", "-e", change.rev, check=False).returncode == 0
    touched = git_out(workspace, "show", "--name-only", "--format=", change.rev).splitlines()
    assert str(paths.ROOT_CONFIG) in touched


def test_the_event_carries_no_before_and_after(workspace: Path) -> None:
    """否定断言:前值后值不进事件面。

    **不是省事。** 前端读到配置到人按下保存之间,配置可能已被别人改过;那时写进事件的"前值"
    是前端读到的旧值,而它看起来跟真的一模一样,出事时没有任何办法分辨。git 的 diff 不会
    犯这个错,所以内容一律去版本面查。
    """
    update(workspace, ROOT, "budgets", {"per_task_tokens": 1234567})

    (event,) = _config_events(workspace)
    assert not {"before", "after", "old", "new", "value", "payload"} & set(event.payload)
    # 换个字段名藏进去也算带了内容。
    assert "1234567" not in json.dumps(event.payload, ensure_ascii=False)


def _refuse_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentgenome.server import settings as settings_module

    real = settings_module.git

    def refuse(cwd: Path, *args: str, **kwargs: Any) -> Any:
        if "commit" in args:
            raise GitError(args, 1, "磁盘满了")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(settings_module, "git", refuse)


def test_a_failed_commit_leaves_neither_a_record_nor_a_changed_config(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交失败要**回到原样**,两个平面一起空着。

    只做到"不写事件"是不够的:文件已经改了的话,系统正按新值在跑,而事件面没有它、版本面
    也没有它——那是最坏的一种状态,任何一种查法都查不出这个值是谁什么时候改的。
    """
    before = (workspace / paths.ROOT_CONFIG).read_text(encoding="utf-8")
    _refuse_commit(monkeypatch)

    with pytest.raises(GitError):
        update(workspace, ROOT, "concurrency", {"global_jobs": 9})

    assert _config_events(workspace) == ()
    assert (workspace / paths.ROOT_CONFIG).read_text(encoding="utf-8") == before
    # 索引也得还原:留着一份暂存的新值,下一次别人提交时它会顺带被带进去。
    assert git(workspace, "diff", "--cached", "--quiet", check=False).returncode == 0


def test_the_api_says_the_change_was_rolled_back(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交挂了要告诉人"没改成"。不说的话,他会以为保存成功了,而配置还是旧值。"""
    _refuse_commit(monkeypatch)

    response = TestClient(_app(workspace)).put(
        "/settings",
        json={"section": "concurrency", "value": {"global_jobs": 9}},
        headers={"x-actor": "root", **BROWSER},
    )

    assert response.status_code == 503
    assert "回滚" in response.json()["detail"]


def test_a_workspace_nested_inside_someone_elses_repo_has_no_version_plane(
    tmp_path: Path,
) -> None:
    """外层是仓库、Workspace 自己不是——**不能提交进外层**。

    `rev-parse --git-dir` 会一路往上找,照它的答案走的话:事件里的 sha 指向一段与本
    Workspace 无关的历史,而外层仓库凭空多出一堆没人认领的提交。
    """
    outer = tmp_path / "outer"
    (outer / "ws").mkdir(parents=True)
    git(outer, "init", "-q")
    root = outer / "ws"
    (root / paths.ROOT_CONFIG).write_text("concurrency: {global_jobs: 3}\n", encoding="utf-8")

    change = update(root, ROOT, "concurrency", {"global_jobs": 5})

    assert change.rev == ""
    assert git(outer, "log", "--oneline", check=False).stdout.strip() == ""


def test_a_config_the_loader_would_reject_never_reaches_the_file(workspace: Path) -> None:
    """校验在落盘之前。

    提交一份加载不了的配置,等于把下一次启动押在没人会看的 git 历史上——而症状出现时,
    界面上明明显示"保存成功"。
    """
    before = (workspace / paths.ROOT_CONFIG).read_text(encoding="utf-8")

    with pytest.raises(GenomeValidationError):
        update(workspace, ROOT, "concurrency", {"global_jobs": -1})

    assert (workspace / paths.ROOT_CONFIG).read_text(encoding="utf-8") == before
    assert _config_events(workspace) == ()


def test_config_events_stay_out_of_task_timelines(workspace: Path) -> None:
    """挂在保留 id 下,**不混进任何一个真任务的时间线**。

    用空 task_id 的话,每一处按任务查询都要各自记得排除它;漏掉的那一处会让一个任务的
    历史里凭空出现别人改配置的记录。
    """
    log = EventLog(workspace)
    log.append("ag-0001", actor="root", kind=LogKind.NOTE, payload={})
    update(workspace, ROOT, "concurrency", {"global_jobs": 4})

    kinds = [event.kind for event in log.events("ag-0001")]

    assert LogKind.CONFIG_CHANGED not in kinds
    assert [event.kind for event in log.events(SYSTEM_SUBJECT)] == [LogKind.CONFIG_CHANGED]


def test_the_config_event_is_written_to_both_the_database_and_the_jsonl(workspace: Path) -> None:
    """双写对得上。JSONL 是前端 tail 的那一份,少写一条就是界面上少一条。"""
    update(workspace, ROOT, "budgets", {"per_task_tokens": 42})

    lines = EventLog(workspace).jsonl_path(SYSTEM_SUBJECT).read_text(encoding="utf-8").splitlines()

    (event,) = _config_events(workspace)
    # 整条比,不只比 payload:加一个字段却只写进数据库的话,前端 tail 到的那一份会少一块,
    # 而"两边对得上"正是这一层的重点测试。
    assert [json.loads(line) for line in lines] == [event.as_dict()]


def test_a_workspace_without_git_still_records_the_change(tmp_path: Path) -> None:
    """还没 git init 的 Workspace 照样要能改配置,它只是没有版本面可写。"""
    root = tmp_path / "bare"
    root.mkdir()
    (root / paths.ROOT_CONFIG).write_text("concurrency: {global_jobs: 3}\n", encoding="utf-8")

    change = update(root, ROOT, "concurrency", {"global_jobs": 6})

    assert change.rev == ""
    (event,) = _config_events(root)
    assert event.payload["rev"] == ""


def test_a_write_that_changes_nothing_points_at_no_commit(workspace: Path) -> None:
    """同一个值保存两次,第二次没有新提交可指。

    **随手指向 HEAD 会让审计以为那个提交改过配置**,而它改的是别的东西。动作照记——人确实
    点了保存,只是这一次版本面上什么都没发生。
    """
    first = update(workspace, ROOT, "concurrency", {"global_jobs": 3})
    second = update(workspace, ROOT, "concurrency", {"global_jobs": 3})

    assert first.rev  # 第一次要落一个提交:写出来的 YAML 与手写的排版未必一致。
    assert second.rev == ""
    assert len(_config_events(workspace)) == 2


def test_config_changes_can_be_filtered_by_person_and_time(workspace: Path) -> None:
    """按人、按时间范围筛选——审计检索走的是事件面那个统一入口,不是设置页专用的接口。"""
    update(workspace, ROOT, "concurrency", {"global_jobs": 4})
    update(
        workspace, Principal("mallory", frozenset({Role.ADMIN})), "limits", {"max_fix_rounds": 9}
    )

    client = TestClient(_app(workspace))
    found = client.get(
        "/audit/events",
        params={"kind": LogKind.CONFIG_CHANGED.value, "actor": "mallory"},
        headers={"x-actor": "root"},
    ).json()["items"]

    assert [item["payload"]["section"] for item in found] == ["limits"]
    since = found[0]["ts"]
    later = client.get(
        "/audit/events",
        params={"kind": LogKind.CONFIG_CHANGED.value, "since": since},
        headers={"x-actor": "root"},
    ).json()["items"]
    assert [item["actor"] for item in later] == ["mallory"]


def test_the_settings_history_still_shows_new_changes(workspace: Path) -> None:
    """改了记录平面之后,`/settings/history` 不能变成一个永远空着的页面。"""
    client = TestClient(_app(workspace))
    client.put(
        "/settings",
        json={"section": "concurrency", "value": {"global_jobs": 8}},
        headers={"x-actor": "root", **BROWSER},
    )

    history = client.get("/settings/history", headers={"x-actor": "root"}).json()

    assert history[-1]["actor"] == "root"
    assert history[-1]["entrance"] == Entrance.WEB.value
    assert history[-1]["rev"]


def test_the_settings_history_needs_audit_permission(workspace: Path) -> None:
    """谁调的并发数,跟事件检索是同一类问题——匿名不该看得到。"""
    response = TestClient(_app(workspace)).get("/settings/history")

    assert response.status_code == 403


def test_a_truncated_history_says_so(workspace: Path) -> None:
    """有上限就会有被砍掉的记录,**不能悄悄发生**。

    一份从中间开始的审计历史,看起来跟一份完整的一模一样——而看的人正是从最早那一头往回找。
    """
    from agentgenome.server.settings import history

    for jobs in (4, 5, 6):
        update(workspace, ROOT, "concurrency", {"global_jobs": jobs})

    found = history(workspace, limit=2)

    assert len(found) == 2
    assert found[0].truncated
    assert not found[-1].truncated


# --- 配置读得回来 -----------------------------------------------------------


def test_the_current_settings_can_be_read_back(workspace: Path) -> None:
    """写得进去读不回来的话,界面上的旋钮只能从空白开始——那不是"改",那是"重填"。"""
    payload = TestClient(_app(workspace)).get("/settings", headers={"x-actor": "root"}).json()

    assert payload["concurrency"]["global_jobs"] == 3
    assert payload["runtime"]["default"] == "claude-code"
    assert payload["genome_tasks"]["per_task_tokens"] == 400_000
    assert payload["topology"]["default"] == "single"
    assert payload["quality_line"]["tester"] == "dev"


def test_reading_gives_back_exactly_what_writing_takes(workspace: Path) -> None:
    """读写同形:读回来改一个字段再写回去是一次数据往返,不是一次翻译。"""
    client = TestClient(_app(workspace))
    before = client.get("/settings", headers={"x-actor": "root"}).json()

    section = before["topology"]
    section["critique"]["enabled"] = True
    written = client.put(
        "/settings",
        json={"section": "topology", "value": section},
        headers={"x-actor": "root", **BROWSER},
    )

    assert written.status_code == 200, written.text
    after = client.get("/settings", headers={"x-actor": "root"}).json()
    assert after["topology"]["critique"]["enabled"] is True


def test_the_settings_view_carries_exactly_the_editable_sections(workspace: Path) -> None:
    """多返回一段就是配置全文接口,少一段就是一个改不了的旋钮——两种都不是这里要的。"""
    from agentgenome.server.models import SettingsView
    from agentgenome.server.settings import EDITABLE

    assert set(SettingsView.model_fields) - {"can_edit"} == set(EDITABLE)


def test_the_quality_line_dials_can_be_changed_from_the_api(workspace: Path) -> None:
    response = TestClient(_app(workspace)).put(
        "/settings",
        json={"section": "quality_line", "value": {"tester": "dedicated", "adversary": "always"}},
        headers={"x-actor": "root", **BROWSER},
    )

    assert response.status_code == 200, response.text
    from agentgenome.config import load_config

    config = load_config(workspace)
    assert config.quality_line.tester.value == "dedicated"
    assert config.quality_line.adversary.value == "always"


def test_runtime_and_genome_limits_can_be_changed_from_the_same_settings_api(
    workspace: Path,
) -> None:
    client = TestClient(_app(workspace))
    runtime = {
        "default": "claude-code",
        "runtimes": {"claude-code": {"cmd": "claude", "max_turns": 999}},
    }

    runtime_response = client.put(
        "/settings",
        json={"section": "runtime", "value": runtime},
        headers={"x-actor": "root", **BROWSER},
    )
    genome_response = client.put(
        "/settings",
        json={"section": "genome_tasks", "value": {"per_task_tokens": 888}},
        headers={"x-actor": "root", **BROWSER},
    )

    assert runtime_response.status_code == 200, runtime_response.text
    assert genome_response.status_code == 200, genome_response.text
    current = client.get("/settings", headers={"x-actor": "root"}).json()
    assert current["runtime"]["runtimes"]["claude-code"]["max_turns"] == 999
    assert current["genome_tasks"]["per_task_tokens"] == 888


def test_a_section_outside_the_whitelist_is_still_refused(workspace: Path) -> None:
    """白名单还是白名单:放开几段不等于放开全部。

    拿 `platform` 举例而不是 `runtime`:后者在 PRD 33 里被有意放进了白名单(容器运行时
    那一段要能从界面配),而这条测试守的是"白名单本身还在",不是某一段的去留。
    """
    response = TestClient(_app(workspace)).put(
        "/settings",
        json={"section": "platform", "value": {}},
        headers={"x-actor": "root", **BROWSER},
    )

    assert response.status_code == 400


def test_the_view_says_whether_this_caller_may_edit(workspace: Path) -> None:
    """前端不重新猜权限矩阵——猜的话,加一个角色时界面与后端会给出两个答案。"""
    app = create_app(
        workspace,
        principals={
            "root": ROOT,
            "reader": Principal("reader", frozenset({Role.REQUESTER})),
        },
    )
    client = TestClient(app)

    assert client.get("/settings", headers={"x-actor": "root"}).json()["can_edit"] is True
    assert client.get("/settings", headers={"x-actor": "reader"}).json()["can_edit"] is False


def test_someone_without_permission_is_refused_by_the_server_not_the_form(
    workspace: Path,
) -> None:
    """前端禁用是体验,不是闸门。"""
    app = create_app(
        workspace,
        principals={"reader": Principal("reader", frozenset({Role.REQUESTER}))},
    )

    response = TestClient(app).put(
        "/settings",
        json={"section": "topology", "value": {"default": "single"}},
        headers={"x-actor": "reader", **BROWSER},
    )

    assert response.status_code == 403


def test_the_cli_can_read_the_same_settings_back(workspace: Path) -> None:
    result = runner.invoke(cli_app, ["settings", "show", "--workspace", str(workspace), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["topology"]["default"] == "single"


def test_the_cli_can_change_the_topology_section(workspace: Path) -> None:
    """命令行改配置的命令早就在,放开白名单它就自动可用——这条盯的就是那件事。"""
    result = runner.invoke(
        cli_app,
        [
            "settings",
            "set",
            "topology",
            json.dumps({"critique": {"enabled": True}}),
            "--as",
            "root",
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    from agentgenome.config import load_config

    assert load_config(workspace).topology.critique.enabled is True


def test_single_machine_development_can_still_change_settings(workspace: Path) -> None:
    """身份表为空时放行——**而"放行"要一路走到底。**

    此前这条路在门口说了放行,却在写入层被同一个匿名身份挡住:界面上旋钮是亮的,一按
    保存 403。两处对"没配账号"的答案不一样,而不一样的那两处谁都不会自己发现。
    """
    client = TestClient(create_app(workspace))

    response = client.put(
        "/settings",
        json={"section": "concurrency", "value": {"global_jobs": 5}},
        headers=BROWSER,
    )

    assert response.status_code == 200, response.text
    assert client.get("/settings").json()["can_edit"] is True
