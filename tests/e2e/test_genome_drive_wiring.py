"""基因组任务的服务端驱动:答完闸门,机器自己往下走。

词表对「待确认」的承诺是"人一答复,机器自己就往下走"——此前这后半句只在 CLI
(`agctl knowledge run`)里成立:网页上答完闸门,任务停在 DEEP_READ 干等一条没人知道
要敲的命令。这一组测的是服务端把这句话补齐:闸门答复接上后台驱动、断了的驱动能从
`POST /genome/tasks/{id}/run` 接回,以及那个端点该拒绝的都拒绝。

与 `test_task_run_wiring.py` 同一条底线:**不往应用状态塞任何替身运行时**。回放的选择
方式与研发任务一致——改员工定义(arch-employee 切到 `runtime: replay`),装配走真实的
`build_runtimes`。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from agentgenome.server.app import create_app
from tests.fixtures.git import commit_all
from tests.fixtures.knowledge_staging import build_staging, receipt_json
from tests.fixtures.mall import materialize_mall

runner = CliRunner()

STAGING = build_staging(
    [{"id": "order-service"}],
    interfaces=[],
    datastores=[],
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    library = tmp_path / "lib"
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(library))
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
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
            "mall",
            "--repo",
            mall["order-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output

    # 深读作业的录制:staging 树 + 小票(PRD 34 的产出形状)。
    directory = library / "arch-employee__knowledge-init__order-service__r1"
    for relative, content in STAGING.items():
        target = directory / "outputs" / "staging" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (directory / "result.json").write_text(receipt_json(), encoding="utf-8")
    (directory / "meta.yaml").write_text("tokens_used: 1200\n", encoding="utf-8")
    # 归一化事件流:回放会把它重放成 `job-attempt-1.jsonl`——执行轨迹读的正是这份。
    (directory / "stream.jsonl").write_text(
        json.dumps({"kind": "text", "text": "先读 order-service 的代码结构。"}, ensure_ascii=False)
        + "\n"
        + json.dumps(
            {"kind": "tool_use", "text": "Read pyproject.toml", "detail": {}}, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )

    # 架构员工切到回放运行时——服务端驱动按员工声明选运行时,与研发任务同一条纪律。
    arch_yaml = root / "employees" / "arch-employee.yaml"
    arch_yaml.write_text(
        arch_yaml.read_text("utf-8").replace("runtime: claude-code", "runtime: replay"),
        encoding="utf-8",
    )
    commit_all(root, "chore: 架构员工切到回放运行时")
    return root


ANSWER = {"modules": [{"id": "order-service", "paths": ["order-service/"]}]}


async def test_gate_confirm_hands_off_to_the_background_driver(workspace: Path) -> None:
    """本文件最重要的一条:网页上答完闸门,任务自己跑到 SUBMITTED,不再等一条 CLI。"""
    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post("/genome/tasks/init")
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]

        confirmed = await client.post(f"/genome/tasks/{task_id}/gate", json=ANSWER)
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["moved"] is True

        driving = app.state.task_runs.get(f"{workspace}|{task_id}")
        assert driving is not None, "答复推动了状态,却没有接上后台驱动"
        await driving

        after = await client.get(f"/genome/tasks/{task_id}")

        # 执行轨迹读得回来:深读的员工干了什么,不必去服务器上翻日志文件。
        trace = await client.get(f"/genome/tasks/{task_id}/trace")
        assert trace.status_code == 200, trace.text
        stages = {item["stage"] for item in trace.json()["stages"]}
        assert "order-service" in stages

    assert after.json()["state"] == "SUBMITTED"
    # 知识真的落了盘,不只是状态好看。
    assert (workspace / "genome" / "knowledge" / "modules" / "order-service").is_dir()


async def test_a_stranded_deep_read_can_be_resumed_from_the_run_endpoint(
    workspace: Path,
) -> None:
    """用户真实撞上的现场:CLI 答的闸门(或驱动中途断了),任务停在 DEEP_READ。
    「继续推进」把它接回来,不必去敲 `agctl knowledge run`。"""
    planned = runner.invoke(cli_app, ["knowledge", "plan", "-w", str(workspace), "--json"])
    assert planned.exit_code == 0, planned.output
    task_id = json.loads(planned.output)["task_id"]
    answer = workspace / "answer.json"
    answer.write_text(json.dumps(ANSWER), encoding="utf-8")
    confirmed = runner.invoke(
        cli_app, ["genome", "confirm", task_id, "--answer", str(answer), "-w", str(workspace)]
    )
    assert confirmed.exit_code == 0, confirmed.output

    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/genome/tasks/{task_id}/run")
        assert response.status_code == 202, response.text
        await app.state.task_runs[f"{workspace}|{task_id}"]
        after = await client.get(f"/genome/tasks/{task_id}")

    assert after.json()["state"] == "SUBMITTED"


async def test_the_run_endpoint_refuses_what_it_should(workspace: Path) -> None:
    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/genome/tasks/gn-nope/run")).status_code == 404

        created = await client.post("/genome/tasks/init")
        task_id = created.json()["id"]
        # 待确认不收:推它等于替人回答,而那个闸门存在的全部理由就是让人看一眼。
        waiting = await client.post(f"/genome/tasks/{task_id}/run")
        assert waiting.status_code == 409
        assert "替人回答" in waiting.json()["detail"]

        confirmed = await client.post(f"/genome/tasks/{task_id}/gate", json=ANSWER)
        assert confirmed.status_code == 200
        await app.state.task_runs[f"{workspace}|{task_id}"]
        # 已经终结的也不收。
        done = await client.post(f"/genome/tasks/{task_id}/run")
        assert done.status_code == 409
        assert "终结" in done.json()["detail"]
