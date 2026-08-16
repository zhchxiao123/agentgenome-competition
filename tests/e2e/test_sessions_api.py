"""会话平面走 REST。

驱动入口取最高点:走公开的 REST 端点,不直接 new `SessionService`。底下跑真实的库、
真实的文件系统与真实的回放运行时——**唯独 Agent 那一段是确定性的**,与全仓其余测试同一条缝。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentgenome.agents.recording import session_key
from agentgenome.cli import app as cli_app
from agentgenome.config import INITIAL_TOKEN_LIMIT
from agentgenome.server.app import create_app
from tests.fixtures.git import commit_all
from tests.fixtures.mall import materialize_mall

runner = CliRunner()

ARCHITECT = """\
id: architect
runtime: replay
prompt: prompts/architect.md
procedures: [code-develop]
tools:
  allow: [Bash, Read, Write, Edit]
permissions:
  write_paths: ["**"]
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    # 回放替身经**真实装配**选中,不手工塞进应用状态——见 `client` fixture。
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
    (tmp_path / "global").mkdir()
    (tmp_path / "lib").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        cli_app,
        [
            "init",
            "--local-only",
            str(root),
            "--name",
            "mall",
            "--repo",
            mall["order-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    (root / "employees" / "prompts" / "architect.md").write_text("你是架构员工。\n", "utf-8")
    (root / "employees" / "architect.yaml").write_text(ARCHITECT, encoding="utf-8")
    commit_all(root, "chore: 架构员工")
    return root


@pytest.fixture
def client(workspace: Path) -> TestClient:
    """**不往 `session_runtimes` 里塞任何东西。**

    替身由 `AGENTGENOME_RECORDINGS`(见 `workspace` fixture)经真实装配选中,于是"服务端
    怎么把配置变成运行时"这一步在测试里照跑。早先这里手工塞一个 `ReplayRuntime` 进应用
    状态——那是装配**之后**的位置,装配因此从没被执行过,而它在生产里是断的。见 PRD 29。
    """
    return TestClient(create_app(workspace))


def _record(tmp_path: Path, session_id: str, index: int, text: str) -> None:
    directory = tmp_path / "lib" / session_key("architect", session_id, index)
    directory.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "kind": "tool_use",
            "text": "genome/knowledge/project-map.yaml",
            "detail": {"name": "Read"},
        },
        {"kind": "text", "text": text},
        {"kind": "usage", "usage": {"input_tokens": 40, "output_tokens": 20}},
    ]
    (directory / "stream.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8"
    )


def _create(client: TestClient, **extra) -> dict:
    """缺省开一个最普通的会话:只读、不关任务。**两个自由度都不给是正常形态。**"""
    response = client.post("/sessions", json={"employee": "architect", **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _blocks(response) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


class TestLifecycle:
    def test_a_consult_session_answers_a_question(self, client, tmp_path: Path) -> None:
        session = _create(client)
        _record(tmp_path, session["id"], 1, "会动到 order-service")

        response = client.post(
            f"/sessions/{session['id']}/messages", json={"message": "这个需求动哪些模块?"}
        )

        assert response.status_code == 200
        kinds = [block["kind"] for block in _blocks(response)]
        # 第一块是用户自己那句回声——`attach` 补齐从起跑前的 seq 之后开始,用户那条
        # 消息也落在这个区间里。前端拿它替掉乐观更新的那一条,拿到的是权威的 seq。
        assert kinds == ["text", "tool-step", "text"]

    def test_the_tool_step_names_what_it_read(self, client, tmp_path: Path) -> None:
        """工具调用过程可视化是信任的来源——它必须显示具体在读什么。"""
        session = _create(client)
        _record(tmp_path, session["id"], 1, "答案")

        response = client.post(f"/sessions/{session['id']}/messages", json={"message": "问"})

        [_question, step, _answer] = _blocks(response)
        assert "project-map.yaml" in step["text"]

    def test_history_matches_what_was_streamed(self, client, tmp_path: Path) -> None:
        session = _create(client)
        _record(tmp_path, session["id"], 1, "答案")
        streamed = _blocks(
            client.post(f"/sessions/{session['id']}/messages", json={"message": "问"})
        )

        page = client.get(f"/sessions/{session['id']}/messages").json()

        # SSE 是持久日志的 tail:两条路读到的必须逐字对得上,包括用户那条回声。
        assert [item["text"] for item in page["items"]] == [b["text"] for b in streamed]

    def test_reconnecting_after_a_sequence_gets_only_the_tail(self, client, tmp_path: Path) -> None:
        """断线补齐:前端记住最后收到的序号,重连后从那儿续上。"""
        session = _create(client)
        _record(tmp_path, session["id"], 1, "答案")
        client.post(f"/sessions/{session['id']}/messages", json={"message": "问"})
        whole = client.get(f"/sessions/{session['id']}/messages").json()

        tail = client.get(
            f"/sessions/{session['id']}/messages", params={"after": whole["items"][0]["seq"]}
        ).json()

        assert tail["items"] == whole["items"][1:]

    def test_listing_filters_by_permission(self, client) -> None:
        readonly = _create(client)
        _create(client, writable=True)

        found = client.get("/sessions", params={"writable": "false"}).json()

        assert [item["id"] for item in found["items"]] == [readonly["id"]]
        # 计数从全量算,不从筛选结果算——筛选器上那些数字回答的是"别的档有没有积压"。
        assert found["total"] == 2

    def test_an_unknown_session_is_a_404(self, client) -> None:
        assert client.get("/sessions/nope").status_code == 404


class TestBackgroundTurns:
    """一轮问答跑在后台任务里,不再绑定某一次连接。

    真正"关页面还接着跑"的时序需要一个会卡住的运行时,单元测试(`test_session_service.
    py::TestBackgroundTurns`)已经用一个可遥控的替身把这条路径连同并发、去重都过了一遍。
    这里只验证走公开 REST 端点时**接口和字段对得上**——真实回放运行时没有人工延迟,一次
    `POST` 打完这轮基本已经跑完了,不适合在这一层赌时序。
    """

    def test_a_fresh_session_is_not_generating(self, client) -> None:
        session = _create(client)

        assert client.get(f"/sessions/{session['id']}").json()["generating"] is False

    def test_generating_is_false_again_once_a_turn_has_finished(
        self, client, tmp_path: Path
    ) -> None:
        session = _create(client)
        _record(tmp_path, session["id"], 1, "答案")

        client.post(f"/sessions/{session['id']}/messages", json={"message": "问"})

        assert client.get(f"/sessions/{session['id']}").json()["generating"] is False

    def test_the_stream_endpoint_replays_full_history_once_the_turn_is_done(
        self, client, tmp_path: Path
    ) -> None:
        """专给"重新打开页面"用的接口,对一轮已经问完的会话该表现得和普通历史一样。"""
        session = _create(client)
        _record(tmp_path, session["id"], 1, "答案")
        client.post(f"/sessions/{session['id']}/messages", json={"message": "问"})

        page = client.get(f"/sessions/{session['id']}/messages").json()
        replayed = _blocks(client.get(f"/sessions/{session['id']}/messages/stream"))

        assert [item["text"] for item in page["items"]] == [b["text"] for b in replayed]

    def test_the_stream_endpoint_is_also_incremental(self, client, tmp_path: Path) -> None:
        session = _create(client)
        _record(tmp_path, session["id"], 1, "答案")
        client.post(f"/sessions/{session['id']}/messages", json={"message": "问"})
        whole = client.get(f"/sessions/{session['id']}/messages").json()

        tail = _blocks(
            client.get(
                f"/sessions/{session['id']}/messages/stream",
                params={"after": whole["items"][0]["seq"]},
            )
        )

        assert [b["text"] for b in tail] == [item["text"] for item in whole["items"][1:]]

    def test_stopping_when_nothing_is_running_is_not_an_error(self, client) -> None:
        """用户点两下"停止"很正常——第二下它已经如愿了,不该报 404/409。"""
        session = _create(client)

        response = client.post(f"/sessions/{session['id']}/stop")

        assert response.status_code == 200
        assert response.json()["generating"] is False

    def test_stopping_an_unknown_session_is_a_404(self, client) -> None:
        assert client.post("/sessions/nope/stop").status_code == 404


class TestBudget:
    """会话的 token 上限:**缺省不限,由项目配置决定**。"""

    def test_a_new_session_starts_with_the_generous_initial_budget(self, client) -> None:
        """新项目先保证会话能完成，再由管理员按真实用量收紧。"""
        assert _create(client)["max_tokens"] == INITIAL_TOKEN_LIMIT

    def test_the_project_setting_reaches_a_new_session(self, client) -> None:
        client.put(
            "/settings",
            json={
                "section": "budgets",
                "value": {
                    "per_task_tokens": 1_500_000,
                    "per_job_tokens": 300_000,
                    "session_tokens": 200_000,
                },
            },
        )

        assert _create(client)["max_tokens"] == 200_000

    def test_resuming_retakes_the_budget_so_raising_it_actually_helps(self, client) -> None:
        """**这条是配置面板存在的理由。**

        被预算挂起的会话,`tokens_used` 已经压在旧上限之上。恢复时不按当前配置重取的话,
        下一轮结账会原地再挂一次——用户看到"点了恢复,只能再说一句就又停了",而他刚做的
        事正是去把上限调高。
        """
        client.put(
            "/settings",
            json={
                "section": "budgets",
                "value": {
                    "per_task_tokens": 1_500_000,
                    "per_job_tokens": 300_000,
                    "session_tokens": 1,
                },
            },
        )
        session = _create(client)
        assert session["max_tokens"] == 1
        client.post(f"/sessions/{session['id']}/suspend", json={"reason": "budget"})
        # 上限调回不限。
        client.put(
            "/settings",
            json={
                "section": "budgets",
                "value": {
                    "per_task_tokens": 1_500_000,
                    "per_job_tokens": 300_000,
                    "session_tokens": 0,
                },
            },
        )

        resumed = client.post(f"/sessions/{session['id']}/resume").json()

        assert resumed["state"] == "active"
        assert resumed["max_tokens"] == 0


class TestGuards:
    def test_there_is_no_endpoint_that_changes_a_session_mode(self, client) -> None:
        """模式不可变是权限边界。**界面不该暗示一个后端拒绝执行的操作**,API 也一样——
        所以这条路径根本不存在,而不是存在但会拒绝。
        """
        paths = client.app.openapi()["paths"]
        mutating = [
            (path, method)
            for path, ops in paths.items()
            if path.startswith("/sessions")
            for method in ops
            if method in {"patch", "put"}
        ]
        assert mutating == []

    def test_a_session_needs_neither_a_task_nor_write_access(self, client) -> None:
        """**两个都不给是完全正常的一种**:在项目根上只读地聊。

        早先"没有任务的质询"会被拒,而那条校验的前提是"质询"作为一种模式存在。
        """
        response = client.post("/sessions", json={"employee": "architect"})

        assert response.status_code == 201
        assert response.json()["writable"] is False
        assert response.json()["task_id"] is None

    def test_a_legacy_mode_still_works_for_one_more_version(self, client) -> None:
        """`mode` 已废弃,但还没切过来的调用方不该在同一次升级里一起断。"""
        response = client.post("/sessions", json={"employee": "architect", "mode": "pair"})

        assert response.status_code == 201
        assert response.json()["writable"] is True

    def test_an_unknown_legacy_mode_is_refused_with_the_allowed_set(self, client) -> None:
        response = client.post("/sessions", json={"employee": "architect", "mode": "结对"})

        assert response.status_code == 400
        assert "consult" in response.json()["detail"]

    def test_a_contradiction_between_writable_and_mode_is_refused(self, client) -> None:
        """**不静默挑一个。** 静默挑的那次会让调用方以为自己传的参数生效了。"""
        response = client.post(
            "/sessions", json={"employee": "architect", "writable": True, "mode": "consult"}
        )

        assert response.status_code == 400
        assert "矛盾" in response.json()["detail"]

    def test_an_unknown_employee_is_a_404(self, client) -> None:
        response = client.post("/sessions", json={"employee": "nobody", "mode": "consult"})

        assert response.status_code == 404

    def test_an_ended_session_refuses_more_messages_and_says_why(
        self, client, tmp_path: Path
    ) -> None:
        session = _create(client)
        client.post(f"/sessions/{session['id']}/end")

        response = client.post(f"/sessions/{session['id']}/messages", json={"message": "问"})

        assert response.status_code == 409
        assert "已结束" in response.json()["detail"]

    def test_an_ended_session_cannot_be_resumed(self, client) -> None:
        """已结束才是不可恢复的那一档。"""
        session = _create(client)
        client.post(f"/sessions/{session['id']}/end")

        assert client.post(f"/sessions/{session['id']}/resume").status_code == 409


class TestConsultToTask:
    def test_a_writable_session_hands_over_its_branch(self, client) -> None:
        """**转成任务是可写会话唯一的出口**,而草稿是这条路上唯一的交接物。

        不带分支的话,接手的人只拿到一段需求描述,已经写好的代码没有任何线索能找回来
        ——它既不在主线上,也不在任何任务分支上。
        """
        session = client.post("/sessions", json={"employee": "architect", "writable": True}).json()

        draft = client.post(f"/sessions/{session['id']}/escalate").json()

        assert draft["source_branch"] == f"session/{session['id']}"

    def test_a_read_only_session_hands_over_no_branch(self, client) -> None:
        """它没改过任何东西,给一条分支名是在说一件不成立的事。"""
        session = _create(client)

        draft = client.post(f"/sessions/{session['id']}/escalate").json()

        assert draft["source_branch"] == ""

    def test_escalate_returns_a_draft_and_creates_nothing(self, client, tmp_path: Path) -> None:
        """**确认前不建任务。** 自动建任务会让「随口问一句」变成「莫名多了个任务」。"""
        session = _create(client)
        _record(tmp_path, session["id"], 1, "会动到 order-service")
        client.post(f"/sessions/{session['id']}/messages", json={"message": "加一个审计字段"})
        before = len(client.get("/tasks").json())

        draft = client.post(f"/sessions/{session['id']}/escalate").json()

        assert draft["source_session_id"] == session["id"]
        assert "加一个审计字段" in draft["requirement"]
        assert len(client.get("/tasks").json()) == before, "escalate 不该建任务"

    def test_the_draft_can_be_confirmed_into_a_real_task(self, client, tmp_path: Path) -> None:
        session = _create(client)
        _record(tmp_path, session["id"], 1, "答案")
        client.post(f"/sessions/{session['id']}/messages", json={"message": "加一个审计字段"})
        draft = client.post(f"/sessions/{session['id']}/escalate").json()

        created = client.post("/tasks", json={"requirement": draft["requirement"]})

        assert created.status_code == 201

    def test_any_session_can_escalate(self, client) -> None:
        """**任何会话都能转任务。** 早先只放行咨询,那是三选一模式下的产物。

        对可写会话来说这是它**唯一**的出口:改动要进主线,只能由一个任务接管后走门禁
        与审批——会话不提供任何直接进主线的路径。
        """
        session = _create(client, task_id="ag-1")

        response = client.post(f"/sessions/{session['id']}/escalate")

        assert response.status_code == 200


class TestTaskLinked:
    def test_a_read_only_session_can_be_linked_to_a_task(self, client) -> None:
        session = _create(client, task_id="ag-20260810-001")

        assert session["task_id"] == "ag-20260810-001"
        assert session["writable"] is False

    def test_inquiring_about_a_task_whose_worktree_is_gone_still_works(
        self, client, tmp_path: Path, workspace: Path
    ) -> None:
        """**这条直接对应 `--resume` 路径绑定的实测发现。**

        质询的典型场景就是复盘已完成或已升级人工的任务,而那些任务的 worktree 早在
        `MERGING → COMPLETED` 时被清理了。会话 workdir 若绑了 worktree,这个场景会失败。
        """
        worktrees = tmp_path / "worktrees"
        assert not (worktrees / "ag-gone").exists(), "前提:这个任务的工作区不存在"

        session = _create(client, task_id="ag-gone")
        _record(tmp_path, session["id"], 1, "当时是这么改的")

        response = client.post(
            f"/sessions/{session['id']}/messages", json={"message": "为什么这么改?"}
        )

        assert response.status_code == 200
        assert [b["kind"] for b in _blocks(response)] == ["text", "tool-step", "text"]

    def test_a_conclusion_can_be_injected_as_an_approval_comment(
        self, client, tmp_path: Path, workspace: Path
    ) -> None:
        session = _create(client, task_id="ag-1")

        response = client.post(
            f"/sessions/{session['id']}/inject",
            json={"decision": "approve", "comment": "理由已核实,同意合并。"},
        )

        assert response.status_code == 200
        from agentgenome.sessions.drafts import injections

        [recorded] = injections(workspace, session["id"])
        assert recorded["task_id"] == "ag-1"
        assert recorded["session_id"] == session["id"], "回注要带来源会话 id"

    def test_a_consult_session_cannot_inject(self, client) -> None:
        session = _create(client)

        assert (
            client.post(f"/sessions/{session['id']}/inject", json={"comment": "x"}).status_code
            == 400
        )


class TestPairIsReachable:
    """**结对会话得真的能从 API 建出来。**

    这一整套逻辑(越权检查、finish_pair、全链路)此前只有直接调编排器的测试在走,
    而 API 这条路上 `task_worktree` 写死 `None`,`workdir_for` 对 PAIR 直接抛。
    逻辑齐全但路径不通,比缺一块更难发现。
    """

    def _interactive_task(self, client: TestClient) -> str:
        response = client.post("/tasks", json={"requirement": "边聊边改", "mode": "interactive"})
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    def test_rest_can_create_an_interactive_task(self, client) -> None:
        """CLI 的 `--interactive` 与 REST 的 `mode` 是同一件事——两条路只走一套语义。"""
        task_id = self._interactive_task(client)

        assert client.get(f"/tasks/{task_id}").json()["mode"] == "interactive"

    def test_a_writable_session_without_a_task_gets_its_own_worktree(self, client) -> None:
        """改动要有地方落,而那个地方**不能是主线的工作树**。

        早先"可写"必然要求一个任务,于是想随手改点什么就得先建一个任务——而界面上又没有
        地方选任务(见 PRD 45 issue 01),这条路整个是断的。
        """
        response = client.post("/sessions", json={"employee": "architect", "writable": True})

        assert response.status_code == 201
        assert response.json()["writable"] is True

    def test_a_writable_session_changes_never_touch_the_main_worktree(
        self, client, workspace: Path
    ) -> None:
        """**这是"会话不能绕过门禁把变更合进主线"那条线在目录层面的形态。**

        会话在自己的 `session/<id>` 分支上改,主线的工作树一个字节都不动;出口是转成任务,
        由任务接管后照常过门禁与审批。
        """
        from agentgenome.sessions.store import SessionStore
        from agentgenome.space.git_ws import SESSION_BRANCH_PREFIX
        from agentgenome.space.gitcmd import git_out

        session_id = client.post(
            "/sessions", json={"employee": "architect", "writable": True}
        ).json()["id"]
        workdir = Path(SessionStore(workspace).get(session_id).workdir)

        assert workdir.is_dir() and workdir != workspace
        # 分支前缀与任务分支分开:按前缀认任务分支的地方不该把它扫进去。
        assert (
            git_out(workdir, "rev-parse", "--abbrev-ref", "HEAD")
            == f"{SESSION_BRANCH_PREFIX}/{session_id}"
        )
        # 主线工作树干净——会话的存在本身不该在上面留下任何痕迹。
        assert git_out(workspace, "status", "--porcelain") == ""

    def test_pairing_on_an_autonomous_task_is_refused(self, client) -> None:
        """结对要在 `--interactive` 建的任务上开——自主任务的 DEVELOPING 由机器占着。"""
        task_id = client.post("/tasks", json={"requirement": "普通需求"}).json()["id"]

        response = client.post(
            "/sessions", json={"employee": "architect", "writable": True, "task_id": task_id}
        )

        assert response.status_code == 400
        assert "交互式" in response.json()["detail"]

    def test_pairing_before_the_worktree_exists_says_so(self, client) -> None:
        """还在 CREATED 态时工作区还没开出来。说清楚"先推进到开发态",而不是一句
        「结对会话必须给出任务工作区」——后者是内部实现的说法,用户看不懂。
        """
        task_id = self._interactive_task(client)

        response = client.post(
            "/sessions", json={"employee": "architect", "writable": True, "task_id": task_id}
        )

        assert response.status_code == 409
        assert "开发态" in response.json()["detail"]

    def test_a_pair_session_opens_on_the_task_worktree(self, client, workspace: Path) -> None:
        task_id = self._interactive_task(client)
        # 把工作区开出来,模拟任务已经进了 DEVELOPING。
        from agentgenome.space.git_ws import GitWorkspace

        worktree = GitWorkspace(workspace).checkout_isolated(task_id)

        session = client.post(
            "/sessions", json={"employee": "architect", "writable": True, "task_id": task_id}
        )

        assert session.status_code == 201, session.text
        assert session.json()["writable"] is True
        # workdir 就是那个 worktree —— 会话寿命因此等于任务,这是有意为之。
        from agentgenome.sessions.store import SessionStore

        assert SessionStore(workspace).get(session.json()["id"]).workdir == str(worktree)


class TestProvenance:
    def test_a_task_created_from_a_session_remembers_where_it_came_from(
        self, client, workspace: Path
    ) -> None:
        """回头看一个任务时能找到当初把它讲清楚的那次会话,而不是只剩一段誊写过的需求。"""
        response = client.post(
            "/tasks", json={"requirement": "从对话来的需求", "source_session_id": "sess-1"}
        )
        task_id = response.json()["id"]

        events = client.get(f"/tasks/{task_id}/events").json()["items"]

        [created] = [e for e in events if e["kind"] == "task_created"]
        assert created["payload"]["source_session_id"] == "sess-1"

    def test_a_task_created_without_a_session_carries_no_empty_field(self, client) -> None:
        """没有来源就不写这个键,而不是写一个空串——空串会让"查不到"与"没有"分不开。"""
        task_id = client.post("/tasks", json={"requirement": "手工提的"}).json()["id"]

        events = client.get(f"/tasks/{task_id}/events").json()["items"]

        [created] = [e for e in events if e["kind"] == "task_created"]
        assert "source_session_id" not in created["payload"]


class TestContextBar:
    """上下文条要有真数据可渲染。

    此前它是一句静态文案——不是前端偷懒,是后端没有"这次装载了什么"这份清单。
    """

    def test_a_session_reports_what_it_loaded(self, client) -> None:
        session = _create(client, task_id="ag-20260810-001")

        assert "task:ag-20260810-001" in session["context_items"]

    def test_pinning_marks_an_item(self, client) -> None:
        """钉住的不参与截断:预算紧张时先砍没钉的。"""
        session = _create(client, task_id="ag-1")

        pinned = client.post(
            f"/sessions/{session['id']}/pin", json={"item": "task:ag-1", "pinned": True}
        ).json()

        assert pinned["pinned"] == ["task:ag-1"]

    def test_unpinning_takes_it_off_again(self, client) -> None:
        session = _create(client, task_id="ag-1")
        client.post(f"/sessions/{session['id']}/pin", json={"item": "task:ag-1"})

        unpinned = client.post(
            f"/sessions/{session['id']}/pin", json={"item": "task:ag-1", "pinned": False}
        ).json()

        assert unpinned["pinned"] == []

    def test_dropping_an_item_also_unpins_it(self, client) -> None:
        """留一条钉在不存在条目上的记录只会让下次截断算错。"""
        session = _create(client, task_id="ag-1")
        client.post(f"/sessions/{session['id']}/pin", json={"item": "task:ag-1"})

        dropped = client.delete(f"/sessions/{session['id']}/context/task:ag-1").json()

        assert "task:ag-1" not in dropped["context_items"]
        assert dropped["pinned"] == []


class TestFeedback:
    """「对话本身成为知识自然选择的输入源」唯一的落地点。"""

    def test_saying_useful_credits_the_cards_that_were_loaded(
        self, client, workspace: Path
    ) -> None:
        session = _create(client, task_id="ag-1")
        # 手工塞一张卡片进装载清单——这里测的是反馈那一段,不是路由。
        from agentgenome.sessions.store import SessionStore

        store = SessionStore(workspace)
        store.save(
            store.get(session["id"]).evolve(context_items=("card:order-service/reserve-flow",))
        )

        response = client.post(f"/sessions/{session['id']}/feedback", json={"useful": True})

        assert response.json()["credited"] == ["order-service/reserve-flow"]

    def test_saying_not_useful_credits_nothing_and_does_not_deduct(
        self, client, workspace: Path
    ) -> None:
        """**不倒扣。** 一次没帮上忙不等于这张卡片是错的,而倒扣会让少数几次不满意
        把一张长期有用的卡片打下去。
        """
        from agentgenome.genome.hits import pending_credits
        from agentgenome.sessions.store import SessionStore

        session = _create(client, task_id="ag-1")
        store = SessionStore(workspace)
        store.save(store.get(session["id"]).evolve(context_items=("card:order/x",)))

        response = client.post(f"/sessions/{session['id']}/feedback", json={"useful": False})

        assert response.json()["credited"] == []
        assert pending_credits(workspace) == ()

    def test_feedback_lands_on_the_event_plane(self, client, workspace: Path) -> None:
        session = _create(client, task_id="ag-1")
        client.post(f"/sessions/{session['id']}/feedback", json={"useful": True})

        from agentgenome.core.events import EventLog

        actions = [e.payload.get("action") for e in EventLog(workspace).events("ag-1")]
        assert "feedback" in actions

    def test_feedback_on_an_unknown_session_is_a_404(self, client) -> None:
        assert client.post("/sessions/nope/feedback", json={"useful": True}).status_code == 404
