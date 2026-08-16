"""拆分提案:决策员工说"这不是一个任务能交付的"之后发生什么。

PRD 48 issue 01。三件事在这里验:提案让任务停在 CREATED 等人(不发 PLAN_DONE、不进
DEVELOPING);拒绝带反馈走既有解析重试路径且反馈进下一轮上下文;不合法的提案(环、
引用批外)按解析失败处理,压根不去打扰人。

走真实的库、真实的状态机、真实的待办表,唯独 Agent 那一段是回放的——与
`test_orchestrator` 同一条纪律。
"""

from __future__ import annotations

from pathlib import Path

from agentgenome.core.states import TaskState
from agentgenome.jobs.reports import read_failure_reports
from agentgenome.todo import service as todo_service
from agentgenome.todo.store import DONE, PENDING, SPLIT, TodoStore
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    PLAN,
    _orchestrator,
    _record,
    _submit,
    library,
    workspace,
)

__all__ = ["library", "workspace"]

#: 一份合法的拆分提案:三个子需求,一条依赖链加一条并行边。
SPLIT_PROPOSAL = {
    "task_id": "",
    "producer": "decision-employee",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
    "split": {
        "children": [
            {"title": "预占接口", "text": "实现库存预占接口。验收:预占后可查询余量。"},
            {
                "title": "下单接线",
                "text": "下单流程调用预占。验收:下单成功后库存减少。",
                "blocked_by": [0],
            },
            {"title": "对账报表", "text": "预占对账报表。验收:报表与流水一致。"},
        ],
        "rationale": "单任务交付不可审:三块各自有独立的验收面。",
    },
}


def _split_recorded(library: Path, task_id: str, proposal: dict | None = None) -> None:
    _record(
        library,
        "decision-employee",
        "requirement-analysis",
        1,
        (proposal or SPLIT_PROPOSAL) | {"task_id": task_id},
        {},
    )


def _the_split_todo(workspace: Path, task_id: str):
    found = [item for item in TodoStore(workspace).open_todos(task_id=task_id)]
    assert len(found) == 1, f"该有且只有一张拆分待办,拿到 {found}"
    return found[0]


# --- 提案停在待确认 ----------------------------------------------------------


async def test_a_split_proposal_parks_the_task_awaiting_confirmation(
    workspace: Path, library: Path
) -> None:
    """提案不是计划:任务不进 DEVELOPING,停在 CREATED 挂一张 split 待办。"""
    task_id = _submit(workspace, requirement="做一个兼容 SQLite 的 SQL 引擎")
    _split_recorded(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.CREATED
    assert task.pending_todo_id, "任务该在等一张待办"
    todo = _the_split_todo(workspace, task_id)
    assert todo.id == task.pending_todo_id
    assert todo.kind == SPLIT
    assert todo.state == PENDING


async def test_a_parked_task_cannot_be_pushed_again(workspace: Path, library: Path) -> None:
    """挂着待办的任务再推是 no-op:不重复投待办,也不烧钱。"""
    task_id = _submit(workspace)
    _split_recorded(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    again = await orchestrator.advance(task_id)

    assert again.state is TaskState.CREATED
    assert len(TodoStore(workspace).open_todos(task_id=task_id)) == 1


# --- 拒绝走解析重试 ----------------------------------------------------------


async def test_rejecting_the_split_retries_the_parse_with_the_feedback(
    workspace: Path, library: Path
) -> None:
    """拒绝带反馈 → 解析重试一次,反馈进下一轮上下文;第二轮产出 plan 就照常往下走。"""
    task_id = _submit(workspace)
    _split_recorded(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    todo = _the_split_todo(workspace, task_id)

    submission = todo_service.submit(
        workspace,
        todo.id,
        {"approved": False, "feedback": "先别拆,这个需求一个任务就能交付"},
        actor=todo.assignee,
    )
    assert submission.ok, submission.detail
    orchestrator.resume(task_id)
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.CREATED
    assert task.plan_retries == 1
    reports = read_failure_reports(orchestrator.store.task_dir(task_id), before_round=1)
    assert reports, "拒绝的反馈必须以失败报告回注下一轮"
    assert "先别拆" in reports[0].body

    # 第二轮产出一份正常计划 → 照常进 DEVELOPING。
    _record(
        library, "decision-employee", "requirement-analysis", 2, PLAN | {"task_id": task_id}, {}
    )
    task = await orchestrator.advance(task_id)
    assert task.state is TaskState.DEVELOPING
    assert TodoStore(workspace).get(todo.id).state == DONE


async def test_rejecting_the_second_proposal_escalates(workspace: Path, library: Path) -> None:
    """重试额度只有一次:第二份提案再被拒,走既有升级路径、原因是既有措辞。"""
    task_id = _submit(workspace)
    _split_recorded(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    first = _the_split_todo(workspace, task_id)
    todo_service.submit(
        workspace, first.id, {"approved": False, "feedback": "拆法不对"}, actor=first.assignee
    )
    orchestrator.resume(task_id)
    await orchestrator.advance(task_id)

    _record(
        library,
        "decision-employee",
        "requirement-analysis",
        2,
        SPLIT_PROPOSAL | {"task_id": task_id},
        {},
    )
    await orchestrator.advance(task_id)
    second = _the_split_todo(workspace, task_id)
    todo_service.submit(
        workspace, second.id, {"approved": False, "feedback": "还是不对"}, actor=second.assignee
    )
    orchestrator.resume(task_id)
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.ESCALATED
    assert "需求解析重试次数已达上限" in task.escalate_reason


# --- 不合法的提案不打扰人 ----------------------------------------------------


async def test_a_split_with_a_dependency_cycle_is_a_parse_failure(
    workspace: Path, library: Path
) -> None:
    """环在提案这一刻就拒,不等人来发现:消耗解析重试,不投待办。"""
    task_id = _submit(workspace)
    cyclic = SPLIT_PROPOSAL | {
        "split": {
            "children": [
                {"title": "甲", "text": "甲。", "blocked_by": [1]},
                {"title": "乙", "text": "乙。", "blocked_by": [0]},
            ],
            "rationale": "环",
        }
    }
    _split_recorded(library, task_id, cyclic)
    orchestrator = _orchestrator(workspace, library)

    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.CREATED
    assert task.plan_retries == 1
    assert not TodoStore(workspace).open_todos(task_id=task_id), "非法提案不该打扰人"


async def test_a_split_referencing_outside_the_batch_is_a_parse_failure(
    workspace: Path, library: Path
) -> None:
    task_id = _submit(workspace)
    outside = SPLIT_PROPOSAL | {
        "split": {
            "children": [
                {"title": "甲", "text": "甲。"},
                {"title": "乙", "text": "乙。", "blocked_by": [7]},
            ],
            "rationale": "引用批外",
        }
    }
    _split_recorded(library, task_id, outside)
    orchestrator = _orchestrator(workspace, library)

    task = await orchestrator.advance(task_id)

    assert task.plan_retries == 1
    assert not TodoStore(workspace).open_todos(task_id=task_id)


# --- 存量工作区的迁移(R2) ---------------------------------------------------


def test_migration_refreshes_an_untouched_legacy_procedure(workspace: Path) -> None:
    """1.0.0 原样的工作区跑一次迁移,schema/manifest/prompt 升到当前版。"""
    import json

    from agentgenome.genome.roster_legacy import (
        REQUIREMENT_ANALYSIS_V1_MANIFEST,
        REQUIREMENT_ANALYSIS_V1_PROMPT,
        REQUIREMENT_ANALYSIS_V1_SCHEMA,
    )
    from agentgenome.genome.roster_migrate import run_migration

    directory = workspace / "genome" / "procedures" / "requirement-analysis"
    # 把工作区拨回 1.0.0——内容逐字来自 git 历史,不是从当前代码反推的。
    (directory / "procedure.yaml").write_text(REQUIREMENT_ANALYSIS_V1_MANIFEST, encoding="utf-8")
    (directory / "prompt.md").write_text(REQUIREMENT_ANALYSIS_V1_PROMPT, encoding="utf-8")
    (directory / "schemas" / "out.json").write_text(
        json.dumps(REQUIREMENT_ANALYSIS_V1_SCHEMA, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    migration = run_migration(workspace)

    assert not migration.kept
    schema = json.loads((directory / "schemas" / "out.json").read_text(encoding="utf-8"))
    assert "split" in schema["properties"], "刷新后 schema 该认识 split 变体"
    assert "oneOf" in schema
    assert "version: 1.1.0" in (directory / "procedure.yaml").read_text(encoding="utf-8")
    assert "split" in (directory / "prompt.md").read_text(encoding="utf-8")


def test_migration_keeps_a_customized_procedure_and_says_so(workspace: Path) -> None:
    """使用者改过的文件迁移不碰,但要指名道姓——静默跳过会被读成"升上去了"。"""
    from agentgenome.genome.roster_migrate import run_migration

    directory = workspace / "genome" / "procedures" / "requirement-analysis"
    customized = "# 我们自己的解析提示词\n照我们的口径来。\n"
    (directory / "prompt.md").write_text(customized, encoding="utf-8")

    migration = run_migration(workspace)

    assert "genome/procedures/requirement-analysis/prompt.md" in migration.kept
    assert (directory / "prompt.md").read_text(encoding="utf-8") == customized


# --- 到期梯子 ----------------------------------------------------------------


async def test_a_split_todo_nobody_answers_escalates_through_the_ladder(
    workspace: Path, library: Path
) -> None:
    """没人裁决的提案走既有三段梯子,最终经既有升级路径进 ESCALATED。

    不为 split 另写一条到期路——另写的那条多半会漏掉封存与蒸馏,而漏掉的那一次
    恰好是最需要证据的那一次。
    """
    from datetime import timedelta

    from agentgenome.config import Config, HumanConfig
    from agentgenome.core.task import TaskStore
    from agentgenome.todo.store import ESCALATED as TODO_ESCALATED
    from agentgenome.todo.sweep import sweep

    task_id = _submit(workspace)
    _split_recorded(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    todo = _the_split_todo(workspace, task_id)

    report = sweep(workspace, Config(human=HumanConfig()), now=todo.created_at + timedelta(days=30))

    assert report.escalated == [todo.id]
    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.ESCALATED
    assert task.pending_todo_id == ""
    assert TodoStore(workspace).get(todo.id).state == TODO_ESCALATED


# --- REST 面 ----------------------------------------------------------------


async def test_the_split_todo_surfaces_on_the_rest_face(workspace: Path, library: Path) -> None:
    """网页那一侧要能不翻产物目录就看到:提案内容、裁决的形状、以及"任务在等人"。"""
    import httpx

    from agentgenome.jobs.split import VERDICT_SCHEMA
    from agentgenome.server.app import create_app

    # 决策员工切到回放运行时——服务端装配用的是员工声明的运行时,与
    # `test_task_run_wiring` 同一手法:装配路径不变,变的只是这个工作区的声明。
    from tests.fixtures.git import commit_all

    decision_yaml = workspace / "employees" / "decision-employee.yaml"
    decision_yaml.write_text(
        decision_yaml.read_text("utf-8").replace("runtime: claude-code", "runtime: replay"),
        encoding="utf-8",
    )
    commit_all(workspace, "chore: 决策员工切到回放运行时")

    task_id = _submit(workspace)
    _split_recorded(library, task_id)
    # 拒绝之后驱动循环会立刻跑第二轮解析:摆好第二份提案,让整条链停在一个确定的地方。
    _record(
        library,
        "decision-employee",
        "requirement-analysis",
        2,
        SPLIT_PROPOSAL | {"task_id": task_id},
        {},
    )

    app = create_app(workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        run = await client.post(f"/tasks/{task_id}/run")
        assert run.status_code == 202, run.text
        await app.state.task_runs[f"{workspace}|{task_id}"]

        listed = (await client.get("/todos")).json()["items"]
        assert len(listed) == 1
        card = listed[0]
        assert card["kind"] == "split"
        assert [child["title"] for child in card["proposal"]["children"]] == [
            "预占接口",
            "下单接线",
            "对账报表",
        ]

        shown = (await client.get(f"/tasks/{task_id}")).json()
        assert shown["state"] == "CREATED"
        assert shown["can_run"] is False, "挂着待办的任务不该还能推"
        assert shown["pending_todo"] == {
            "id": card["id"],
            "kind": "split",
            "assignee": card["assignee"],
        }, "任务详情必须说清楚机器正在等哪张人工待办"
        task_card = next(
            item for item in (await client.get("/tasks")).json() if item["id"] == task_id
        )
        assert task_card["pending_todo"] == shown["pending_todo"], "工作台也要能把它计入待确认"

        detail = (await client.get(f"/todos/{card['id']}")).json()
        assert detail["schema"] == VERDICT_SCHEMA, "拆分待办给人看的契约是裁决的形状,不是工序产物"

        rejected = await client.post(
            f"/todos/{card['id']}/submit",
            json={"result": {"approved": False, "feedback": "第一刀切错了"}},
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["ok"] is True
        # 拒绝 → 解析重试 → 第二份提案 → 重新停在待确认:整条环在一次响应里走完。
        assert rejected.json()["task_state"] == "CREATED"
        after = (await client.get("/todos")).json()["items"]
        assert len(after) == 1
        assert after[0]["id"] != card["id"], "第二轮的提案是一张新待办,不是旧的复活"
