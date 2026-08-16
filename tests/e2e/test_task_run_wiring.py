"""`POST /tasks/{id}/run` 能不能把配置变成真的执行——这条路以前不存在。

同 `test_session_wiring.py` 一条底线:**不往应用状态塞任何替身运行时**,装配走真实的
`build_runtimes(load_config(root))`,回放靠 `AGENTGENOME_RECORDINGS` 走真实的那条选择
逻辑。这里测的价值不在断言的内容,在于它必须走过整条装配——`_task_pool` 那道口子如果
被悄悄短路成一个测试专用替身,这条测试就会在装配本身坏掉时照样全绿,而这正是 PRD 29
记录过的那类"绕过装配的测试查不出装配坏没坏"的教训。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskMode, TaskStore
from agentgenome.jobs.orchestrator import Orchestrator
from agentgenome.server import notify_prefs
from agentgenome.server.app import create_app
from tests.fixtures.git import commit_all
from tests.fixtures.mall import materialize_mall
from tests.fixtures.tree import module_ids, patch_module_map

runner = CliRunner()

#: 只推 CREATED → DEVELOPING 这一步,所以只需要决策员工的 `requirement-analysis` 产物。
PLAN = {
    "task_id": "",
    "producer": "decision-employee",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
    "modules": ["order-service"],
    "cross_module": False,
    "acceptance": ["下单时调用预占接口"],
    "risks": [],
}


def _record(library: Path, employee: str, procedure: str, round_: int, result: dict) -> None:
    directory = library / f"{employee}__{procedure}__r{round_}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        cli_app,
        [
            "init",
            "--local-only",
            str(root),
            "--name",
            "example-mall",
            "--repo",
            mall["order-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    for module_id in module_ids(root):
        patch_module_map(root, module_id, test_cmd="python -m pytest -q tests")

    # 决策员工切到回放运行时——`agctl init` 默认生成的是 `runtime: claude-code`,
    # 而这条测试不该真的去拉起一个 Claude 子进程。装配路径本身不变,变的只是
    # 这个工作区自己声明要用哪个运行时,与生产完全同一条代码。
    decision_yaml = root / "employees" / "decision-employee.yaml"
    decision_yaml.write_text(
        decision_yaml.read_text("utf-8").replace("runtime: claude-code", "runtime: replay"),
        encoding="utf-8",
    )
    commit_all(root, "chore: 决策员工切到回放运行时")
    return root


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "lib"
    path.mkdir()
    # `build_runtimes` 见到这个环境变量才会注册 `replay`——与命令行、会话平面同一条路。
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(path))
    return path


def _submit(workspace: Path, requirement: str = "下单时要预占库存") -> str:
    result = runner.invoke(
        cli_app,
        ["task", "submit", "--requirement", requirement, "--workspace", str(workspace), "--json"],
    )
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["id"])


async def test_the_run_endpoint_actually_advances_the_task_through_real_assembly(
    workspace: Path, library: Path
) -> None:
    """本文件最重要的一条:不塞替身,证明"配置能不能变成真的执行"这条装配路径真的通。"""
    task_id = _submit(workspace)
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(mode=TaskMode.INTERACTIVE))
    _record(library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id})

    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/tasks/{task_id}/run")
        assert response.status_code == 202
        # 返回的是**推进前**的快照——这一步这时候大概率还没跑完。
        assert response.json()["state"] == "CREATED"

        key = f"{workspace}|{task_id}"
        running = app.state.task_runs.get(key)
        assert running is not None, "没有把后台任务记下来,重复点击挡不住重复派发"
        await running

        after = await client.get(f"/tasks/{task_id}")

    assert after.json()["state"] == "DEVELOPING"
    assert after.json()["can_run"] is False
    notices = app.state.bus.since(0, task_id)
    assert {notice.kind for notice in notices} >= {
        "run_started",
        "job_started",
        "job_finished",
        "task_changed",
        "run_finished",
    }
    assert {notice.workspace for notice in notices} == {"default"}


async def test_one_start_stops_when_the_plan_requests_human_clarification(
    workspace: Path, library: Path
) -> None:
    task_id = _submit(workspace)
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(mode=TaskMode.INTERACTIVE))
    _record(
        library,
        "decision-employee",
        "requirement-analysis",
        1,
        PLAN | {"task_id": task_id, "passed": False},
    )

    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/tasks/{task_id}/run")
        assert response.status_code == 202
        await app.state.task_runs[f"{workspace}|{task_id}"]
        after = await client.get(f"/tasks/{task_id}")

    assert after.json()["state"] == "ESCALATED"
    assert after.json()["plan_retries"] == 1
    assert "澄清" in after.json()["escalate_reason"]


async def test_a_second_call_while_the_first_is_in_flight_is_refused(
    workspace: Path, library: Path
) -> None:
    task_id = _submit(workspace)
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(mode=TaskMode.INTERACTIVE))
    _record(library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id})

    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(f"/tasks/{task_id}/run")
        assert first.status_code == 202

        second = await client.post(f"/tasks/{task_id}/run")
        assert second.status_code == 409
        assert "推进中" in second.json()["detail"]

        await app.state.task_runs[f"{workspace}|{task_id}"]


async def test_a_failed_step_is_recorded_not_swallowed(workspace: Path, library: Path) -> None:
    """没有录好回放时,`advance` 会抛。后台任务必须自己把这件事记下来——异常不能无声消失,
    否则界面上「点了没反应」和「真的挂了」长得一模一样。"""
    task_id = _submit(workspace)
    # 故意不 `_record` 任何东西:回放运行时找不到录好的产物会报错。

    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/tasks/{task_id}/run")
        assert response.status_code == 202

        await app.state.task_runs[f"{workspace}|{task_id}"]

        events = await client.get(f"/tasks/{task_id}/events")
        after = await client.get(f"/tasks/{task_id}")

    notes = [item for item in events.json()["items"] if item["kind"] == "note"]
    assert notes, "推进失败之后,任务的事件流里应该有一条能看见的记录"
    assert "推进失败" in notes[-1]["payload"]["note"]
    assert after.json()["execution_status"] == "interrupted"


# --- REST 的 IM 推送:escalated/completed 走后台的 `_run_one_step`,不是 `_notify_im` --
#
# 与 `test_prd12_console.py` 里 CLI 那两条同一个回归:`_notify_im` 只在请求本身同步的
# 那一刻调用,`advance` 真正落地终态的时候早就不在那次请求里了。这里不需要真的走回放,
# monkeypatch 掉 `Orchestrator.advance`/`drain_evolution` 就够——测的是"推进到终态之后
# 有没有人去推",不是编排器本身对不对。


async def test_run_notifies_im_on_escalation(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _submit(workspace, "卡住的任务")
    store = TaskStore(workspace)

    async def fake_advance(self: Orchestrator, advancing_task_id: str) -> object:
        return store.save(store.get(advancing_task_id).evolve(state=TaskState.ESCALATED))

    async def fake_drain(self: Orchestrator) -> None:
        return None

    pushed: list[tuple[str, str, str]] = []

    def fake_push(root: Path, tid: str, title: str, event: str) -> None:
        pushed.append((tid, title, event))

    monkeypatch.setattr(Orchestrator, "advance", fake_advance)
    monkeypatch.setattr(Orchestrator, "drain_evolution", fake_drain)
    monkeypatch.setattr(notify_prefs, "push", fake_push)

    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/tasks/{task_id}/run")
        assert response.status_code == 202
        await app.state.task_runs[f"{workspace}|{task_id}"]

    assert pushed == [(task_id, "卡住的任务", "escalated")]


async def test_run_does_not_notify_when_state_is_unchanged(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _submit(workspace)
    store = TaskStore(workspace)

    async def fake_advance(self: Orchestrator, advancing_task_id: str) -> object:
        return store.get(advancing_task_id)  # 没有任何转移

    async def fake_drain(self: Orchestrator) -> None:
        return None

    pushed: list[object] = []
    monkeypatch.setattr(Orchestrator, "advance", fake_advance)
    monkeypatch.setattr(Orchestrator, "drain_evolution", fake_drain)
    monkeypatch.setattr(notify_prefs, "push", lambda *a, **kw: pushed.append(a))

    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/tasks/{task_id}/run")
        assert response.status_code == 202
        await app.state.task_runs[f"{workspace}|{task_id}"]

    assert pushed == []
