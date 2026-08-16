"""控制面能不能把配置变成运行时——**这条路以前一次都没被走过**。

会话平面在真实部署里曾经完全不可用:`create_app` 把运行时注册表初始化成空字典,生产代码里
没有任何地方往里写,于是任何 `POST /sessions` 都以 409 告终。而 1859 个测试全绿,因为会话
e2e 直接把回放替身塞进应用状态——**那是装配之后的位置**,装配因此从没被执行过。

所以这个文件里的测试有一条共同的硬性约束:**绝不碰 `app.state.session_runtimes`**。替身一律
经真实装配路径(根配置 + `AGENTGENOME_RECORDINGS`)选中。碰了它,这些测试就退化成它们要防的
那种测试。见 PRD 29。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentgenome.agents.recording import session_key
from agentgenome.cli import app as cli_app
from agentgenome.server.app import create_app
from agentgenome.sessions.store import SessionStore
from tests.fixtures.git import commit_all
from tests.fixtures.mall import materialize_mall

runner = CliRunner()

REPLAY_EMPLOYEE = """\
id: architect
runtime: replay
prompt: prompts/architect.md
procedures: [code-develop]
tools:
  allow: [Read]
permissions:
  write_paths: ["**"]
"""

#: qwen-code 没有 `--resume` 的等价物,能力矩阵里 `sessions=False`。
NO_SESSION_EMPLOYEE = """\
id: qwen-hand
runtime: qwen-code
prompt: prompts/architect.md
procedures: [code-develop]
tools:
  allow: [Read]
permissions:
  write_paths: ["**"]
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
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
    (root / "employees" / "architect.yaml").write_text(REPLAY_EMPLOYEE, encoding="utf-8")
    (root / "employees" / "qwen-hand.yaml").write_text(NO_SESSION_EMPLOYEE, encoding="utf-8")
    commit_all(root, "chore: 员工")
    return root


def test_a_plain_server_can_create_a_session(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**本文件最重要的一条。**

    构造一个真实应用,**不往运行时注册表塞任何东西**,断言会话建得出来。它断言的内容很少,
    价值全在于它必须走过装配——这是唯一能挡住"控制面攥着一个空注册表"复发的测试。
    """
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
    client = TestClient(create_app(workspace))

    response = client.post("/sessions", json={"employee": "architect", "mode": "consult"})

    assert response.status_code == 201, response.text


def test_a_missing_runtime_says_it_is_not_configured_not_that_it_is_unregistered(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「没有注册」对用户毫无意义——他从没被要求注册过任何东西。

    这个员工声明的是 replay,而回放库没配,所以它真的开不出来。错误信息要指向根配置。
    """
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    client = TestClient(create_app(workspace))

    response = client.post("/sessions", json={"employee": "architect", "mode": "consult"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "没有配置" in detail
    assert "没有注册" not in detail


def test_a_runtime_that_cannot_do_sessions_says_so_instead(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**这条测的就是两句话不该被合并。**

    qwen-code 配好了、能跑 Job,只是开不了会话。用户该做的是换一个员工,不是去改根配置——
    而合成一句「没有注册」会把他支使到完全错误的地方,改完当然没用。
    """
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
    config = workspace / "agentgenome.yaml"
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    payload["runtime"]["qwen-code"] = {"cmd": "qwen", "max_turns": 40}
    config.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    client = TestClient(create_app(workspace))

    response = client.post("/sessions", json={"employee": "qwen-hand", "mode": "consult"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "开不了会话" in detail
    assert "没有配置" not in detail


def test_a_failed_creation_leaves_no_session_behind(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**只断言状态码的话,这个 bug 会原样复发。**

    早先会话先落盘、运行时后解析,于是失败路径留下一条 `active` 的僵尸:列表里看着健康,
    点进去永远发不出消息,而且每失败一次多攒一条。所以这里连着失败三次再看列表——
    "有没有留下"和"留了几条"都要能看出来。
    """
    monkeypatch.delenv("AGENTGENOME_RECORDINGS", raising=False)
    client = TestClient(create_app(workspace))

    for _ in range(3):
        assert (
            client.post("/sessions", json={"employee": "architect", "mode": "consult"}).status_code
            == 409
        )

    assert client.get("/sessions").json()["items"] == []


def _record(tmp_path: Path, session_id: str, index: int, text: str) -> None:
    directory = tmp_path / "lib" / session_key("architect", session_id, index)
    directory.mkdir(parents=True, exist_ok=True)
    events = [
        {"kind": "text", "text": text},
        {"kind": "usage", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    (directory / "stream.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8"
    )


class TestSurvivingARestart:
    """**从外部行为进入,不去戳内存里的句柄字典。**

    句柄字典是实现细节;用户关心的是"服务重启之后,昨天那个会话还能不能接着聊"。所以这里
    构造第二个应用实例(同一个工作区)来模拟重启,只用 REST 断言。
    """

    def test_a_session_still_takes_messages_after_a_restart(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """早先重启会让**全部**历史会话变成僵尸:句柄只在内存里,而续接标识从没落盘,
        于是没有任何恢复路径。用户看到的是"它把我们聊过的都忘了",列表里那些会话却还在。
        """
        monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
        before = TestClient(create_app(workspace))
        session = before.post("/sessions", json={"employee": "architect", "mode": "consult"}).json()
        _record(tmp_path, session["id"], 1, "第一轮的答案")
        before.post(f"/sessions/{session['id']}/messages", json={"message": "问"})

        # 重启:同一个工作区,全新的应用实例(内存里什么都没有)。
        after = TestClient(create_app(workspace))
        _record(tmp_path, session["id"], 2, "重启之后的答案")
        response = after.post(f"/sessions/{session['id']}/messages", json={"message": "再问"})

        assert response.status_code == 200, response.text
        assert "重启之后的答案" in response.text

    def test_the_restarted_session_keeps_its_history(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """续接的是同一场对话,不是新开一场——所以之前的消息要还在。"""
        monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
        before = TestClient(create_app(workspace))
        session = before.post("/sessions", json={"employee": "architect", "mode": "consult"}).json()
        _record(tmp_path, session["id"], 1, "第一轮的答案")
        before.post(f"/sessions/{session['id']}/messages", json={"message": "问"})

        after = TestClient(create_app(workspace))
        page = after.get(f"/sessions/{session['id']}/messages").json()

        assert "第一轮的答案" in [item["text"] for item in page["items"]]

    def test_a_session_that_truly_cannot_be_recovered_says_so(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**「重启后无法恢复」与「还没开起来」必须是两句话。**

        前者只能新开一个,后者是等它开好。合成一句等于让用户去等一件永远不会发生的事。
        """
        monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
        before = TestClient(create_app(workspace))
        session = before.post("/sessions", json={"employee": "architect", "mode": "consult"}).json()

        # 人为抹掉运行时标识,模拟一条真的重建不出来的记录。
        store = SessionStore(workspace)
        store.save(store.get(session["id"]).evolve(native_session_id=None))

        after = TestClient(create_app(workspace))
        response = after.post(f"/sessions/{session['id']}/messages", json={"message": "问"})

        assert response.status_code == 409
        assert "无法恢复" in response.json()["detail"]


class TestEmployeeList:
    """选之前就知道谁开得了会话,而不是选完提交再吃一个 409。"""

    def test_it_lists_the_employees_in_the_roster(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
        client = TestClient(create_app(workspace))

        items = client.get("/employees").json()["items"]

        assert {"architect", "qwen-hand"} <= {item["id"] for item in items}

    def test_it_says_who_can_open_a_session_and_who_cannot(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
        client = TestClient(create_app(workspace))

        by_id = {item["id"]: item for item in client.get("/employees").json()["items"]}

        assert by_id["architect"]["can_session"] is True
        # qwen-code 没有 `--resume` 的等价物。
        assert by_id["qwen-hand"]["can_session"] is False

    def test_a_blocked_employee_says_why(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**置灰要说明原因**,否则用户只会以为是自己没权限。"""
        monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
        client = TestClient(create_app(workspace))

        by_id = {item["id"]: item for item in client.get("/employees").json()["items"]}

        assert by_id["qwen-hand"]["session_blocked_reason"]
        assert by_id["architect"]["session_blocked_reason"] == ""

    def test_it_does_not_leak_tool_allowlists_or_write_scopes(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**安全相关的否定断言要显式写。**

        不写的话,以后有人给 `EmployeeSummary` 加字段时没人会想起这里有一条边界——而
        「这个员工能碰哪些路径」漏给任何能打开控制台的人,是一次真实的信息泄露。
        """
        monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
        client = TestClient(create_app(workspace))

        body = client.get("/employees").text

        for leaked in ("tools", "allow", "permissions", "write_paths", "credentials", "procedures"):
            assert leaked not in body, f"员工列表不该带 {leaked}"


class TestListDensity:
    """列表要认得出「是哪一场」——PRD 30 issue 01。"""

    def test_the_display_name_falls_back_to_the_id(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**兜底不能少。** 现有的员工定义里都没有这个字段,不兜底会让界面上那一列突然空掉。"""
        monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
        client = TestClient(create_app(workspace))

        by_id = {item["id"]: item for item in client.get("/employees").json()["items"]}

        # architect.yaml 没写 name。
        assert by_id["architect"]["name"] == "architect"

    def test_a_named_employee_shows_that_name(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
        named = (workspace / "employees" / "architect.yaml").read_text(encoding="utf-8")
        (workspace / "employees" / "architect.yaml").write_text(
            named.replace("id: architect\n", "id: architect\nname: 架构员工\n"), encoding="utf-8"
        )
        client = TestClient(create_app(workspace))

        by_id = {item["id"]: item for item in client.get("/employees").json()["items"]}

        assert by_id["architect"]["name"] == "架构员工"

    def test_the_preview_is_the_last_question_the_user_asked(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """取用户那条而不是员工那条——用户想认出的是"我当时问的是什么"。"""
        monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
        client = TestClient(create_app(workspace))
        session = client.post("/sessions", json={"employee": "architect", "mode": "consult"}).json()
        _record(tmp_path, session["id"], 1, "员工的回答不该被当成预览")
        client.post(f"/sessions/{session['id']}/messages", json={"message": "补偿逻辑是什么?"})

        listed = client.get("/sessions").json()["items"][0]

        assert listed["last_question"] == "补偿逻辑是什么?"

    def test_counts_cover_every_state_not_just_the_filtered_one(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**筛选器上的计数回答的是"别处有没有积压"。**

        拿筛完的结果去数的话,选中「进行中」之后别的几项会一起变成 0——而那正是这些数字
        要回答的问题,方向相反。
        """
        monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
        client = TestClient(create_app(workspace))
        keep = client.post("/sessions", json={"employee": "architect", "mode": "consult"}).json()
        done = client.post("/sessions", json={"employee": "architect", "mode": "consult"}).json()
        client.post(f"/sessions/{done['id']}/end")

        filtered = client.get("/sessions", params={"state": "active"}).json()

        assert [item["id"] for item in filtered["items"]] == [keep["id"]]
        # 只筛出一条,但计数要把已结束那条也算上。
        assert filtered["counts"]["active"] == 1
        assert filtered["counts"]["ended"] == 1
        assert filtered["total"] == 2
