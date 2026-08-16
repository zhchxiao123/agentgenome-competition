"""critique-loop 端到端:命中策略才进环,进了环也停得住。

**critique 省的是门禁往返,不动门禁的裁决地位。** 门禁只能拦"错的",拦不住"能跑但差的"
——绕远的实现、违背约定卡片的写法,门禁全绿照样进 PR,把成本转嫁给人工审批。这组测试验的
是:该进的时候进、不该进的时候一个字节都不变、进了之后停得住而且不偷任务的修复轮次。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from agentgenome.core.events import LogKind
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskStore
from agentgenome.core.topology import (
    CRITIQUE_LOOP,
    SINGLE,
    STOPPED_BUDGET,
    STOPPED_CHECKER_FAILED,
    STOPPED_CONVERGED,
    STOPPED_MAX_ROUNDS,
)
from agentgenome.jobs.orchestrator import Orchestrator
from agentgenome.space.git_ws import GitWorkspace
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    DEV_RESULT,
    PASSING_TEST,
    _arm,
    _orchestrator,
    _record,
    _submit,
    library,
    workspace,
)
from tests.fixtures.git import commit_all

__all__ = ["library", "workspace"]

CRITIQUE_ROUND_1 = {
    "task_id": "",
    "producer": "reviewer-employee",
    "created_at": "2026-09-01T10:06:00Z",
    "passed": True,
    "approved": False,
    "findings": [
        {
            "file": "repos/order-service/src/order/reserve.py",
            "line": 12,
            "severity": "major",
            "issue": "直接 raise RuntimeError,而 rules/coding.md 要求领域错误用专用异常",
            "suggestion": "改成 InventoryShortage",
        }
    ],
}
CRITIQUE_APPROVED = {**CRITIQUE_ROUND_1, "approved": True, "findings": []}
#: 一份不合契约的批判产物:缺 `approved` 与 `findings`。契约校验会判这次 Job 失败。
BROKEN_CRITIQUE = {
    "task_id": "",
    "producer": "reviewer-employee",
    "created_at": "2026-09-01T10:06:00Z",
    "passed": True,
}


def enable_critique(workspace: Path, **overrides: object) -> None:
    """打开策略。判据放宽到"计划命中一个模块就进环",夹具才走得到环里。"""
    config = workspace / "agentgenome.yaml"
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    payload["topology"] = {
        "default": "single",
        "critique": {
            "enabled": True,
            "on_protected": False,
            "min_modules": 1,
            "min_changed_files": 0,
            **overrides,
        },
    }
    config.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def record_node(
    library: Path,
    employee: str,
    procedure: str,
    subject: str,
    result: dict[str, Any],
    files: dict[str, str] | None = None,
) -> None:
    """按 `(employee, procedure, subject, round)` 摆一份录制。

    环里同一轮会派两次 `code-develop`(生成、精化)与两次 `code-critique`(第一、第二轮
    批判),任务轮次却始终是 1。**不带 `subject` 的话它们全撞在一个键上**,回放给每一次
    返回同一份产出——于是"第二轮批判通过了"这件事根本录不出来,而测试照样是绿的。
    """
    directory = library / f"{employee}__{procedure}__{subject}__r1"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    for relative, content in (files or {}).items():
        target = directory / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


#: 一段**能过门禁但违反项目约定**的实现:测试跑得通,错误却是裸 RuntimeError。
#: 门禁只能拦"错的",拦不住"能跑但差的"——这正是环要抓的那一类。
SLOPPY = "def reserve():\n    raise RuntimeError('库存不足')\n"
#: 精化那一轮按意见改成的样子。
POLISHED = "def reserve():\n    raise InventoryShortage('库存不足')\n"

TARGET = "repos/order-service/src/order/reserve.py"


def arm_critique(
    library: Path, task_id: str, *verdicts: dict[str, Any], refine_fixes: bool = True
) -> None:
    """摆好环里每一步的回放:生成、逐轮批判、精化。

    生成那一轮写下一段违反约定的实现,精化那一轮按意见改掉——**对照组**(不进环)因此能
    断言那段实现原样留在了 diff 里。
    """
    dev = DEV_RESULT | {"task_id": task_id}
    green = {"repos/order-service/tests/test_reserve.py": PASSING_TEST}
    record_node(
        library, "dev-employee", "code-develop", "generate.1", dev, {**green, TARGET: SLOPPY}
    )
    record_node(
        library,
        "dev-employee",
        "code-develop",
        "refine.1",
        dev,
        {**green, TARGET: POLISHED if refine_fixes else SLOPPY},
    )
    for round_, verdict in enumerate(verdicts, start=1):
        record_node(
            library,
            "reviewer-employee",
            "code-critique",
            f"critique.{round_}",
            verdict | {"task_id": task_id},
        )


def declare_tokens(library: Path, key: str, tokens: int) -> None:
    """给一份录制声明用量。回放缺省报不出用量,而环级预算判据靠它。"""
    meta = library / key / "meta.yaml"
    meta.write_text(yaml.safe_dump({"tokens_used": tokens}), encoding="utf-8")


def worktree_file(workspace: Path, task_id: str, relative: str) -> str:
    """任务隔离工作区里那份文件现在长什么样。**产物是最强证据,自述是最弱证据。**"""
    return (GitWorkspace(workspace).worktree_path(task_id) / relative).read_text(encoding="utf-8")


async def walk(workspace: Path, library: Path, steps: int = 3) -> tuple[Orchestrator, str]:
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(steps):
        await orchestrator.advance(task_id)
    return orchestrator, task_id


def topology_events(orchestrator: Orchestrator, task_id: str) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.TOPOLOGY
    ]


def develop_slots(workspace: Path, task_id: str) -> list[str]:
    root = workspace / "tasks" / task_id / "artifacts"
    return sorted(item.name for item in root.iterdir() if "develop" in item.name)


# --- 不命中 -----------------------------------------------------------------


async def test_a_task_that_misses_the_policy_runs_exactly_as_before(
    workspace: Path, library: Path
) -> None:
    """缺省不开。一个默认开启的增值步骤会让所有人在看到收益之前先看到账单。"""
    orchestrator, task_id = await walk(workspace, library)

    develop = [
        item for item in topology_events(orchestrator, task_id) if item["stage"] == "develop"
    ]

    assert develop[0]["template"]["id"] == SINGLE
    assert develop[0]["why"] == ""
    assert develop_slots(workspace, task_id) == ["02-develop"]


async def test_enabling_the_policy_but_missing_its_thresholds_still_runs_single(
    workspace: Path, library: Path
) -> None:
    enable_critique(workspace, min_modules=99, min_changed_files=99)

    orchestrator, task_id = await walk(workspace, library)

    develop = [
        item for item in topology_events(orchestrator, task_id) if item["stage"] == "develop"
    ]
    assert develop[0]["template"]["id"] == SINGLE


# --- 命中 -------------------------------------------------------------------


async def test_a_hit_runs_generate_critique_refine_and_then_the_gate(
    workspace: Path, library: Path
) -> None:
    """批判抓到一条,精化改掉,门禁照跑——**门禁的裁决地位一个字没变**。"""
    enable_critique(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_ROUND_1, CRITIQUE_APPROVED)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    assert develop_slots(workspace, task_id) == [
        "02-develop.generate",
        "03-develop.critique",
        "04-develop.refine",
        "05-develop.critique",
    ]
    assert TaskStore(workspace).get(task_id).state is TaskState.READY_TO_COMMIT


async def test_the_event_plane_answers_why_in_why_out_and_how_much(
    workspace: Path, library: Path
) -> None:
    """为什么进环、每轮结论、为什么停、花了多少——四个问题都要能回答。"""
    enable_critique(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_ROUND_1, CRITIQUE_APPROVED)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    events = topology_events(orchestrator, task_id)
    chosen = [item for item in events if item.get("template", {}).get("id") == CRITIQUE_LOOP]
    ran = [item for item in events if item.get("template_id") == CRITIQUE_LOOP]

    assert chosen and chosen[0]["why"] == "modules"
    assert ran and ran[0]["stopped_because"] == STOPPED_CONVERGED
    assert ran[0]["rounds"] == 2
    assert [node["kind"] for node in ran[0]["nodes"]] == ["work", "checker", "work", "checker"]
    assert "tokens_used" in ran[0]


async def test_two_rejections_hit_the_round_cap_and_still_reach_the_gate(
    workspace: Path, library: Path
) -> None:
    """达上限即停,带最后一版意见送门禁,**不升级人工**——环不是闸门。"""
    enable_critique(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_ROUND_1, CRITIQUE_ROUND_1)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    ran = [
        item for item in topology_events(orchestrator, task_id)
        if item.get("template_id") == CRITIQUE_LOOP
    ]
    assert ran[0]["stopped_because"] == STOPPED_MAX_ROUNDS
    assert TaskStore(workspace).get(task_id).state is TaskState.READY_TO_COMMIT


async def test_the_loop_does_not_spend_the_tasks_fix_rounds(
    workspace: Path, library: Path
) -> None:
    """精化轮次由环内终止判据管。两个环混淆的后果是悄悄绕过其中一条上限。"""
    enable_critique(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_ROUND_1, CRITIQUE_ROUND_1)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    assert TaskStore(workspace).get(task_id).fix_rounds == 0


async def test_the_findings_are_handed_to_the_refine_round_in_full(
    workspace: Path, library: Path
) -> None:
    """回注的必须是原文。摘要会把"第 37 行没判空"压成"有几处健壮性问题"。"""
    enable_critique(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_ROUND_1, CRITIQUE_APPROVED)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    bundle = (
        workspace / "tasks" / task_id / "artifacts" / "04-develop.refine" / "context-attempt-0.md"
    ).read_text(encoding="utf-8")

    assert "InventoryShortage" in bundle
    assert "rules/coding.md 要求领域错误用专用异常" in bundle


async def test_the_critique_actually_changes_the_code_and_the_control_group_shows_it(
    workspace: Path, library: Path
) -> None:
    """**验证产物,不验证自述。** 环跑完之后,工作区里那份文件必须真的被改过了。"""
    enable_critique(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_ROUND_1, CRITIQUE_APPROVED)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    assert "InventoryShortage" in worktree_file(workspace, task_id, TARGET)


async def test_without_the_loop_the_same_implementation_reaches_the_gate_untouched(
    workspace: Path, library: Path
) -> None:
    """对照组:策略不命中,门禁照样通过,而那段违反约定的写法一个字没改。

    它等着人工审批去发现——那正是这个环要省掉的成本。
    """
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        {"repos/order-service/tests/test_reserve.py": PASSING_TEST, TARGET: SLOPPY},
    )
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    assert TaskStore(workspace).get(task_id).state is TaskState.READY_TO_COMMIT
    assert "RuntimeError" in worktree_file(workspace, task_id, TARGET)


async def test_a_failed_critique_blocks_the_gate_without_discarding_the_work(
    workspace: Path, library: Path
) -> None:
    """批判失败保留代码现场，但不允许本轮继续进入门禁。

    这里让批判交出一份**不合契约的产物**(缺 approved 与 findings):它走的是编排器真实的
    契约失败路径,不是替身里的一个开关。
    """
    enable_critique(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, BROKEN_CRITIQUE)
    declare_tokens(library, "dev-employee__code-develop__generate.1__r1", 12_345)

    orchestrator = _orchestrator(workspace, library)
    for _ in range(2):
        await orchestrator.advance(task_id)

    ran = [
        item
        for item in topology_events(orchestrator, task_id)
        if item.get("template_id") == CRITIQUE_LOOP
    ]
    assert ran[0]["stopped_because"] == STOPPED_CHECKER_FAILED
    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.DEVELOPING
    assert task.fix_rounds == 1
    assert task.tokens_used == 12_345
    assert "RuntimeError" in worktree_file(workspace, task_id, TARGET)
    report = (workspace / "tasks" / task_id / "failures" / "round-1.md").read_text(
        encoding="utf-8"
    )
    assert "critique" in report
    assert "contract" in report


async def test_a_reviewer_scope_violation_escalates_without_another_attempt(
    workspace: Path, library: Path
) -> None:
    """越权是安全事件；重试相同 reviewer 只会扩大风险与费用。"""
    enable_critique(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_APPROVED)
    meta = library / "reviewer-employee__code-critique__critique.1__r1" / "meta.yaml"
    meta.write_text(
        yaml.safe_dump(
            {
                "failure_kind": "scope",
                "failure_detail": "",
            }
        ),
        encoding="utf-8",
    )

    orchestrator = _orchestrator(workspace, library)
    for _ in range(2):
        await orchestrator.advance(task_id)

    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.ESCALATED
    assert task.fix_rounds == 0
    assert "越出授权范围" in (task.escalate_reason or "")


async def test_a_reviewer_runtime_failure_escalates_with_the_original_reason(
    workspace: Path, library: Path
) -> None:
    """认证或进程环境坏了不是代码返工；重跑 reviewer 不会改变外部环境。"""
    enable_critique(workspace)
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_APPROVED)
    meta = library / "reviewer-employee__code-critique__critique.1__r1" / "meta.yaml"
    meta.write_text(
        yaml.safe_dump(
            {
                "failure_kind": "process",
                "failure_detail": (
                    "运行时报告失败(api_error): Not logged in · Please run /login"
                ),
            }
        ),
        encoding="utf-8",
    )

    orchestrator = _orchestrator(workspace, library)
    for _ in range(2):
        await orchestrator.advance(task_id)

    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.ESCALATED
    assert task.fix_rounds == 0
    assert "api_error" in (task.escalate_reason or "")
    assert "Not logged in" in (task.escalate_reason or "")
    assert develop_slots(workspace, task_id) == [
        "02-develop.generate",
        "03-develop.critique",
    ]


async def test_a_tight_loop_budget_stops_the_loop_and_says_so(
    workspace: Path, library: Path
) -> None:
    """轮次封顶管得住次数,管不住单轮的花费。

    用量由录制的 meta 声明——**回放缺省报不出用量**,不声明的话这条判据在端到端里永远不
    生效,而测试会以"没触发"的形式静默通过。
    """
    enable_critique(workspace, budget_share=0.001, max_rounds=5)
    config = workspace / "agentgenome.yaml"
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    payload["budgets"]["per_task_tokens"] = 1_000_000
    payload["budgets"]["per_job_tokens"] = 300_000
    config.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_ROUND_1, CRITIQUE_ROUND_1, CRITIQUE_APPROVED)
    for round_ in (1, 2, 3):
        declare_tokens(library, f"reviewer-employee__code-critique__critique.{round_}__r1", 9_000)

    orchestrator = _orchestrator(workspace, library, enforce_budget=True)
    for _ in range(3):
        await orchestrator.advance(task_id)

    ran = [
        item
        for item in topology_events(orchestrator, task_id)
        if item.get("template_id") == CRITIQUE_LOOP
    ]
    assert ran[0]["stopped_because"] == STOPPED_BUDGET
    assert ran[0]["rounds"] == 1


async def test_a_protected_path_inside_a_module_triggers_the_loop(
    workspace: Path, library: Path
) -> None:
    """受保护路径**在模块之内**才是常态,而这条策略默认开着。

    只判"模块整个落在受保护路径下"的话,它在真实项目里永远不命中——一条默认开启却从不生效
    的策略,比没有这条策略更糟:它让人以为高风险改动已经被多看了一眼。
    """
    protected = workspace / "genome" / "rules" / "protected.yaml"
    payload = yaml.safe_load(protected.read_text(encoding="utf-8"))
    payload["protected_paths"].append(
        {
            "path": "repos/order-service/src/order/**",
            # 这条用例验“高风险路径触发额外评审”，不是“禁止开发修改”。
            "writable_by": ["dev-employee"],
        }
    )
    protected.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    commit_all(workspace, "chore: 把订单核心路径标为受保护")
    enable_critique(workspace, on_protected=True, min_modules=99, min_changed_files=99)

    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_APPROVED)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    chosen = [
        item
        for item in topology_events(orchestrator, task_id)
        if item.get("template", {}).get("id") == CRITIQUE_LOOP
    ]
    assert chosen and chosen[0]["why"] == "protected-paths"


async def test_a_task_level_override_forces_the_loop_without_the_policy(
    workspace: Path, library: Path
) -> None:
    """任务级把模板直接点成环:那是一个人做出的决定,不再问策略。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_APPROVED)
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(topology=CRITIQUE_LOOP))
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    chosen = [
        item
        for item in topology_events(orchestrator, task_id)
        if item.get("template", {}).get("id") == CRITIQUE_LOOP
    ]
    assert chosen and chosen[0]["why"] == "task-override"
    assert store.get(task_id).state is TaskState.READY_TO_COMMIT


# --- 从界面拧开它 -----------------------------------------------------------


async def test_turning_the_loop_on_from_the_console_actually_puts_a_task_in_it(
    workspace: Path, library: Path
) -> None:
    """**配置落盘不等于生效。**

    这一条走的是人真会走的那条路:从 REST 面拧开精化环,然后跑一个任务,断言它真的进了环
    ——而不是只断言 YAML 里多了一行。少了这一条,一次把开关写进只读副本的改动照样是绿的。
    """
    from fastapi.testclient import TestClient

    from agentgenome.server.app import create_app
    from agentgenome.server.rbac import Principal, Role

    client = TestClient(
        create_app(workspace, principals={"root": Principal("root", frozenset({Role.ADMIN}))})
    )
    section = client.get("/settings", headers={"x-actor": "root"}).json()["topology"]
    section["critique"] = {
        **section["critique"],
        "enabled": True,
        "on_protected": False,
        "min_modules": 1,
        "min_changed_files": 0,
    }
    written = client.put(
        "/settings",
        json={"section": "topology", "value": section},
        headers={"x-actor": "root"},
    )
    assert written.status_code == 200, written.text

    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    arm_critique(library, task_id, CRITIQUE_ROUND_1, CRITIQUE_APPROVED)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(3):
        await orchestrator.advance(task_id)

    develop = [
        item for item in topology_events(orchestrator, task_id) if item["stage"] == "develop"
    ]
    assert develop[0]["template"]["id"] == CRITIQUE_LOOP
    assert "03-develop.critique" in develop_slots(workspace, task_id)
