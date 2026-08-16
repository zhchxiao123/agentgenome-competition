"""② 模块边界草案与人工闸门。

一个存量项目的模块划分往往**不等于目录划分**。这个判断只有熟悉项目的人做得了——而划错了，
后面所有的知识都长在歪的划分上。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.config import load_config
from agentgenome.core.genome_gate import read_answer, read_draft
from agentgenome.core.genome_task import GenomeTaskKind, GenomeTaskState, GenomeTaskStore
from agentgenome.core.task_ids import Lane, lane_of
from tests.fixtures.git import fake_checkout
from tests.fixtures.mall import materialize_mall

runner = CliRunner()


@pytest.fixture
def mall(tmp_path: Path):
    return materialize_mall(tmp_path / "upstream")


@pytest.fixture
def workspace(tmp_path: Path, mall, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    root = tmp_path / "ws"
    result = runner.invoke(
        app,
        [
            "init", "--local-only",
            str(root),
            "--name",
            "example-mall",
            "--repo",
            mall["order-service"].remote_url,
            "--repo",
            mall["inventory-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output
    return root


def _unready(workspace: Path, mount: str) -> None:
    """把一个挂载点弄成"声明了但没 checkout"。

    判据就是挂载点下的 `.git`,所以移走它即可——比构造一次真实的非递归 clone 便宜得多,
    而且更能说明判据本身是什么。
    """
    (workspace / mount / ".git").rename(workspace / f"{mount.replace('/', '_')}.git-stash")


def _plan(workspace: Path) -> dict:
    result = runner.invoke(app, ["knowledge", "plan", "--workspace", str(workspace), "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# --- 初始化是一条真正的基因组任务 ---------------------------------------------


def test_planning_creates_a_genome_task(workspace: Path) -> None:
    payload = _plan(workspace)

    record = GenomeTaskStore(workspace).get(payload["task_id"])
    assert record.kind is GenomeTaskKind.INIT
    assert record.state is GenomeTaskState.AWAITING_CONFIRMATION


def test_it_lands_on_the_genome_lane(workspace: Path) -> None:
    """**这修的是 21-04 留下的已知问题。** 此前初始化以字符串当 id 跑，落在研发泳道、
    走研发预算——而它恰恰是「基因组任务要有自己的泳道」这条设计的旗舰场景。"""
    payload = _plan(workspace)

    assert lane_of(payload["task_id"]) is Lane.GENOME


def test_it_draws_on_the_genome_budget(workspace: Path) -> None:
    payload = _plan(workspace)

    record = GenomeTaskStore(workspace).get(payload["task_id"])
    assert record.budget_tokens == load_config(workspace).genome_tasks.per_task_tokens


# --- 草案 --------------------------------------------------------------------


def test_the_draft_is_based_on_the_scan(workspace: Path) -> None:
    payload = _plan(workspace)

    draft = read_draft(workspace, payload["task_id"])
    # 顺序按挂载路径排,不按 `--repo` 的给出顺序——候选来自挂载声明,而那是一个集合。
    # 要紧的是它**确定**(同一份 Workspace 扫两次结果一致),不是它等于命令行的顺序。
    assert [item["path"] for item in draft["modules"]] == [
        "repos/inventory-service/",
        "repos/order-service/",
    ]


def test_every_candidate_says_why_it_is_one(workspace: Path) -> None:
    """只给一个模块列表的话，人无从判断该不该改它——「为什么这么分」是他唯一能复核的东西。"""
    payload = _plan(workspace)

    draft = read_draft(workspace, payload["task_id"])
    assert all(item["rationale"] for item in draft["modules"])


def test_the_scan_result_is_kept_next_to_it(workspace: Path) -> None:
    payload = _plan(workspace)

    assert (workspace / "tasks" / payload["task_id"] / "scan.json").is_file()


# --- 闸门:看、改、答 ---------------------------------------------------------


def test_a_human_can_read_the_draft_from_the_command_line(workspace: Path) -> None:
    payload = _plan(workspace)

    result = runner.invoke(
        app, ["genome", "confirm", payload["task_id"], "--workspace", str(workspace)]
    )

    assert result.exit_code == 0
    assert "repos/order-service" in result.output


def test_merging_two_modules_is_just_a_shorter_list(workspace: Path) -> None:
    """合并、拆分、改名、剔除在数据上都是「给出最终列表」——不为每种动作定义单独的指令。"""
    payload = _plan(workspace)
    answer = workspace / "answer.json"
    answer.write_text(
        json.dumps(
            {
                "modules": [{"id": "mall", "path": "repos/order-service/"}],
                "note": "两个域其实是一个",
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "genome",
            "confirm",
            payload["task_id"],
            "--answer",
            str(answer),
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    assert GenomeTaskStore(workspace).get(payload["task_id"]).state is GenomeTaskState.DEEP_READ


def test_the_original_draft_survives_the_answer(workspace: Path) -> None:
    """「系统建议了什么」与「人改成了什么」永远都能各自读出来。"""
    payload = _plan(workspace)
    answer = workspace / "answer.json"
    answer.write_text(
        json.dumps({"modules": [{"id": "mall", "path": "repos/order-service/"}]}), encoding="utf-8"
    )
    runner.invoke(
        app,
        [
            "genome",
            "confirm",
            payload["task_id"],
            "--answer",
            str(answer),
            "--workspace",
            str(workspace),
        ],
    )

    draft = read_draft(workspace, payload["task_id"])
    assert len(draft["modules"]) == 2
    assert len(read_answer(workspace, payload["task_id"])["modules"]) == 1


def test_a_bad_answer_leaves_the_task_at_the_gate(workspace: Path) -> None:
    payload = _plan(workspace)
    answer = workspace / "answer.json"
    answer.write_text(json.dumps({"modules": []}), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "genome",
            "confirm",
            payload["task_id"],
            "--answer",
            str(answer),
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code != 0
    record = GenomeTaskStore(workspace).get(payload["task_id"])
    assert record.state is GenomeTaskState.AWAITING_CONFIRMATION


def test_closing_the_terminal_does_not_lose_the_gate(workspace: Path) -> None:
    """**全异步。** 人可以关掉终端，隔天再回来——那是这个闸门存在的形态前提。"""
    payload = _plan(workspace)

    reopened = GenomeTaskStore(workspace)

    assert [item.id for item in reopened.awaiting_confirmation()] == [payload["task_id"]]


# --- ⑤ 按模块重建 -------------------------------------------------------------


def test_rebuilding_a_module_creates_a_scoped_genome_task(workspace: Path) -> None:
    """跳过扫描、划分与闸门——边界已经拍过板了。"""
    result = runner.invoke(
        app,
        ["genome", "reinit", "--module", "order-service", "--workspace", str(workspace), "--json"],
    )

    assert result.exit_code == 0, result.output
    task_id = json.loads(result.output)["tasks"][0]["task_id"]
    record = GenomeTaskStore(workspace).get(task_id)
    assert record.kind is GenomeTaskKind.REINIT
    assert record.subject == "order-service"
    assert record.state is GenomeTaskState.SCANNING


def test_rebuilding_an_unknown_module_is_refused(workspace: Path) -> None:
    """凭空发明模块会让下游的影响判定失去依据。"""
    result = runner.invoke(
        app, ["genome", "reinit", "--module", "ghost", "--workspace", str(workspace)]
    )

    assert result.exit_code != 0
    assert "ghost" in result.output


def test_two_rebuilds_of_one_module_cannot_run_together(workspace: Path) -> None:
    """两次重建并行跑会互相覆盖：后写的那次把前一次的产出抹掉，而两次都报成功。"""
    runner.invoke(
        app, ["genome", "reinit", "--module", "order-service", "--workspace", str(workspace)]
    )

    again = runner.invoke(
        app, ["genome", "reinit", "--module", "order-service", "--workspace", str(workspace)]
    )

    assert again.exit_code != 0
    assert "已经有一个基因组任务在跑" in again.output


def test_several_modules_can_be_rebuilt_at_once(workspace: Path) -> None:
    result = runner.invoke(
        app,
        [
            "genome",
            "reinit",
            "--module",
            "order-service",
            "--module",
            "inventory-service",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)["tasks"]) == 2


# --- 挂载点没就绪时,闸门不该问人问题 -------------------------------------------


def test_planning_refuses_while_a_mount_point_is_not_checked_out(workspace: Path) -> None:
    """对着一堆空目录规划模块边界不产生任何价值。

    **让它一路走到深读才炸掉,中间会烧掉真金白银的 token**;而这里根本不是"绕过界面",
    是一个纯粹的环境未就绪,该当场被看见。
    """
    _unready(workspace, "repos/order-service")

    result = runner.invoke(app, ["knowledge", "plan", "--workspace", str(workspace)])

    assert result.exit_code != 0


def test_the_refusal_names_the_repos_and_the_command_to_run(workspace: Path) -> None:
    """人要能照着做,而不是自己去查子模块的用法。"""
    _unready(workspace, "repos/order-service")

    result = runner.invoke(app, ["knowledge", "plan", "--workspace", str(workspace)])

    assert "repos/order-service" in result.output
    assert "git submodule update --init" in result.output


def test_a_ready_workspace_still_plans(workspace: Path) -> None:
    """只测拦得住不测放得过,等于没测。"""
    result = runner.invoke(app, ["knowledge", "plan", "--workspace", str(workspace), "--json"])

    assert result.exit_code == 0, result.output


def test_a_repo_with_no_code_yet_does_not_block_planning(workspace: Path) -> None:
    """绿地新仓是**正常状态**,不是错误。挡住它等于让绿地项目根本走不到闸门。"""
    fake_checkout(workspace, "repos/greenfield")
    with (workspace / ".gitmodules").open("a", encoding="utf-8") as handle:
        handle.write(
            '[submodule "repos/greenfield"]\n\tpath = repos/greenfield\n\turl = ../gf.git\n'
        )

    result = runner.invoke(app, ["knowledge", "plan", "--workspace", str(workspace), "--json"])

    assert result.exit_code == 0, result.output
    draft = read_draft(workspace, json.loads(result.output)["task_id"])
    greenfield = next(item for item in draft["modules"] if item["id"] == "greenfield")
    assert "还没有代码" in greenfield["rationale"]
