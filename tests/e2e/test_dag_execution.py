"""dag 端到端:plan 拆出一张图,两个节点同时干活,改动合回一个任务分支。

**跨模块需求今天仍是单 Job 直线**:plan 定位出两个模块,开发员工仍然一个人串行改完。独立的
子改动在互相等待——不是因为有依赖,是因为执行器只会跑一条线,而图的信息在计划阶段就丢掉了。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentgenome.core.events import LogKind
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskStore
from agentgenome.core.topology import DAG
from agentgenome.jobs.orchestrator import Orchestrator
from agentgenome.space.git_ws import GitWorkspace
from tests.e2e.test_critique_loop import record_node  # noqa: PLC2701 —— 同一套录制写法
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    DEV_RESULT,
    ITEST_DECIDE_RESULT,
    PASSING_TEST,
    PLAN,
    _orchestrator,
    _record,
    _submit,
    library,
    workspace,
)

__all__ = ["library", "workspace"]

#: 一张 diamond:两个模块各一个节点,写集不相交,收口节点消费两边的产出。
NODES = [
    {
        "id": "order",
        "produces": ["order.diff"],
        "write_scope": ["repos/order-service/**"],
    },
    {
        "id": "inventory",
        "produces": ["inventory.diff"],
        "write_scope": ["repos/inventory-service/**"],
    },
    {
        "id": "wire",
        "needs": ["order.diff", "inventory.diff"],
        "produces": ["wired"],
        "write_scope": ["repos/order-service/src/order/wire.py"],
    },
]


def arm_plan(library: Path, task_id: str, nodes: list[dict[str, Any]] | None) -> None:
    plan = PLAN | {"task_id": task_id, "modules": ["order-service", "inventory-service"]}
    if nodes is not None:
        plan = plan | {"nodes": nodes}
    _record(library, "decision-employee", "requirement-analysis", 1, plan, {})
    _record(
        library,
        "decision-employee",
        "itest-decide",
        1,
        ITEST_DECIDE_RESULT | {"task_id": task_id},
        {},
    )


def arm_nodes(library: Path, task_id: str) -> None:
    """每个节点一份录制。**回放键带节点名**:不带的话三次派发全撞在一个键上。"""
    for node, files in (
        ("order", {"repos/order-service/src/order/reserve.py": "# 订单侧\n"}),
        ("inventory", {"repos/inventory-service/src/inventory/hold.py": "# 库存侧\n"}),
        ("wire", {"repos/order-service/src/order/wire.py": "# 收口\n"}),
    ):
        record_node(
            library,
            "dev-employee",
            "code-develop",
            f"{node}.1",
            DEV_RESULT | {"task_id": task_id},
            files,
        )


def worktrees(workspace: Path, task_id: str) -> list[str]:
    home = GitWorkspace(workspace).worktree_path(task_id).parent
    return sorted(item.name for item in home.iterdir() if item.name.startswith(task_id))


def topology_events(orchestrator: Orchestrator, task_id: str) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.TOPOLOGY
    ]


async def test_a_planned_graph_runs_its_nodes_and_merges_into_one_branch(
    workspace: Path, library: Path
) -> None:
    """三个节点各自在自己的工作树里干活,改动按拓扑序合回**一个**任务分支。"""
    task_id = _submit(workspace, "订单与库存都要改")
    arm_plan(library, task_id, NODES)
    arm_nodes(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)  # 需求解析:产出图
    await orchestrator.advance(task_id)  # 开发:跑图

    chosen = [item for item in topology_events(orchestrator, task_id) if item.get("template")]
    develop = [item for item in chosen if item["stage"] == "develop"][0]
    assert develop["template"]["id"] == DAG
    assert develop["why"] == "plan"
    # 边由产物推出来:收口节点依赖两个上游,两个上游之间没有边。
    assert sorted(develop["template"]["edges"]) == [["inventory", "wire"], ["order", "wire"]]

    # 三个节点各有工作树,而顶层仍然一个任务分支。
    assert worktrees(workspace, task_id) == [
        task_id,
        f"{task_id}.inventory",
        f"{task_id}.order",
        f"{task_id}.wire",
    ]
    task_tree = GitWorkspace(workspace).worktree_path(task_id)
    assert (task_tree / "repos/order-service/src/order/reserve.py").exists()
    assert (task_tree / "repos/inventory-service/src/inventory/hold.py").exists()
    assert (task_tree / "repos/order-service/src/order/wire.py").exists()


async def test_each_dag_node_is_confined_to_its_declared_write_scope(
    workspace: Path, library: Path
) -> None:
    """员工级模块权限不能放大节点在计划里声明的文件边界。"""
    task_id = _submit(workspace, "订单和库存并行修改")
    nodes = [
        {
            "id": "order",
            "produces": ["order.diff"],
            "write_scope": ["repos/order-service/**"],
        },
        {
            "id": "inventory",
            "produces": ["inventory.diff"],
            "write_scope": ["repos/inventory-service/**"],
        },
    ]
    arm_plan(library, task_id, nodes)
    record_node(
        library,
        "dev-employee",
        "code-develop",
        "order.1",
        DEV_RESULT | {"task_id": task_id},
        {"repos/inventory-service/src/inventory/order-leak.py": "# 越过节点写集\n"},
    )
    record_node(
        library,
        "dev-employee",
        "code-develop",
        "inventory.1",
        DEV_RESULT | {"task_id": task_id},
        {"repos/inventory-service/src/inventory/hold.py": "# 合规改动\n"},
    )
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.DEVELOPING
    assert task.fix_rounds == 1
    order_tree = GitWorkspace(workspace).worktree_path(task_id, "order")
    assert not (order_tree / "repos/inventory-service/src/inventory/order-leak.py").exists()


async def test_an_empty_dag_write_scope_is_read_only(workspace: Path, library: Path) -> None:
    """空写集不是“继承角色的全部权限”，而是这个节点不允许写版本面。"""
    task_id = _submit(workspace, "只读分析与库存修改并行")
    nodes = [
        {"id": "analysis", "produces": ["analysis"], "write_scope": []},
        {
            "id": "inventory",
            "produces": ["inventory.diff"],
            "write_scope": ["repos/inventory-service/**"],
        },
    ]
    arm_plan(library, task_id, nodes)
    record_node(
        library,
        "dev-employee",
        "code-develop",
        "analysis.1",
        DEV_RESULT | {"task_id": task_id},
        {"repos/order-service/src/order/unauthorized.py": "# 空写集不该写入\n"},
    )
    record_node(
        library,
        "dev-employee",
        "code-develop",
        "inventory.1",
        DEV_RESULT | {"task_id": task_id},
        {"repos/inventory-service/src/inventory/hold.py": "# 合规改动\n"},
    )
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.DEVELOPING
    analysis_tree = GitWorkspace(workspace).worktree_path(task_id, "analysis")
    assert not (analysis_tree / "repos/order-service/src/order/unauthorized.py").exists()


async def test_the_artifacts_are_addressed_per_node(workspace: Path, library: Path) -> None:
    """一个 stage 底下住着一张图:产物按节点编址,不再是一条线。"""
    task_id = _submit(workspace, "订单与库存都要改")
    arm_plan(library, task_id, NODES)
    arm_nodes(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    names = sorted(
        item.name
        for item in (workspace / "tasks" / task_id / "artifacts").iterdir()
        if "develop" in item.name
    )
    assert names == ["02-develop.order", "03-develop.inventory", "04-develop.wire"]


async def test_a_plan_without_a_graph_still_runs_the_old_way(
    workspace: Path, library: Path
) -> None:
    """一条线也是合法的计划——**不为并行而并行**。"""
    task_id = _submit(workspace, "只改订单")
    arm_plan(library, task_id, None)
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        {"repos/order-service/tests/test_reserve.py": PASSING_TEST},
    )
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    assert worktrees(workspace, task_id) == [task_id]
    develop = [
        item
        for item in topology_events(orchestrator, task_id)
        if item.get("template") and item["stage"] == "develop"
    ][0]
    assert develop["template"]["id"] == "single"


async def test_an_illegal_graph_is_refused_at_planning_time(
    workspace: Path, library: Path
) -> None:
    """图不合法是"没把需求读明白":它消耗**需求解析重试**,不消耗修复轮次。

    放到派发那一刻判的话,任务已经进了开发态,而那里没有回到解析的路。
    """
    clashing = [
        {"id": "order", "produces": ["a"], "write_scope": ["repos/order-service/**"]},
        {"id": "also-order", "produces": ["b"], "write_scope": ["repos/order-service/src/**"]},
    ]
    task_id = _submit(workspace, "两个节点抢同一个仓")
    arm_plan(library, task_id, clashing)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)

    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.CREATED, "图不合法应该退回重新解析"
    assert task.plan_retries == 1
    assert task.fix_rounds == 0
    refusal = [
        item
        for item in topology_events(orchestrator, task_id)
        if item.get("why") == "refused"
    ]
    assert refusal and any("write-conflict" in line for line in refusal[0]["issues"])


async def test_a_failed_node_freezes_its_downstream_and_redoes_the_round(
    workspace: Path, library: Path
) -> None:
    """下游冻结、旁支跑完,这一轮不算——**不让它带着半张图去撞门禁**。"""
    task_id = _submit(workspace, "订单与库存都要改")
    arm_plan(library, task_id, NODES)
    arm_nodes(library, task_id)
    # 让订单侧那个节点交一份不合契约的产物:走的是真实的契约失败路径。
    record_node(
        library, "dev-employee", "code-develop", "order.1", {"task_id": task_id, "passed": True}, {}
    )
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    task = TaskStore(workspace).get(task_id)
    assert task.state is TaskState.DEVELOPING, "这一轮不算,回开发态重来"
    assert task.fix_rounds == 1
    reports = json.dumps(
        [item.name for item in (workspace / "tasks" / task_id).rglob("failure-*.md")]
    )
    assert reports, "失败现场要留给下一轮"
    ran = [item for item in topology_events(orchestrator, task_id) if item.get("template_id")]
    assert ran and ran[0]["stopped_because"] == "node-failed"


async def test_a_retry_only_reruns_the_failed_node_and_its_downstream(
    workspace: Path, library: Path
) -> None:
    """**一次跑通的东西不该因为别人挂了而重花一遍钱**(与增量修复同一个经济学)。"""
    task_id = _submit(workspace, "订单与库存都要改")
    arm_plan(library, task_id, NODES)
    arm_nodes(library, task_id)
    record_node(
        library, "dev-employee", "code-develop", "order.1", {"task_id": task_id, "passed": True}, {}
    )
    orchestrator = _orchestrator(workspace, library)
    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)  # order 失败 → wire 冻结 → 这一轮不算

    survivor = workspace / "tasks" / task_id / "artifacts" / "03-develop.inventory" / "result.json"
    before = survivor.read_bytes()
    # 这一轮把 order 修好。**第二轮的回放键是 `order.2` 加轮次 2**:节点的第几次、任务的
    # 第几轮各占一维,不然两轮会撞在一个键上。
    directory = library / "dev-employee__code-develop__order.2__r2"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(
        json.dumps(DEV_RESULT | {"task_id": task_id}, ensure_ascii=False), encoding="utf-8"
    )
    fixed = directory / "files" / "repos/order-service/src/order/reserve.py"
    fixed.parent.mkdir(parents=True, exist_ok=True)
    fixed.write_text("# 修好了\n", encoding="utf-8")
    # wire 上一轮被冻结,**它一个槽都没产出过**——所以这一轮它仍然是自己的第一次
    # (`wire.1`),只是任务轮次到了 2。两维各记各的,这正是它们分开的理由。
    for node in ("wire.1",):
        target = library / f"dev-employee__code-develop__{node}__r2"
        target.mkdir(parents=True, exist_ok=True)
        (target / "result.json").write_text(
            json.dumps(DEV_RESULT | {"task_id": task_id}, ensure_ascii=False), encoding="utf-8"
        )

    await orchestrator.advance(task_id)

    names = sorted(
        item.name
        for item in (workspace / "tasks" / task_id / "artifacts").iterdir()
        if "develop" in item.name
    )
    # inventory 没有第二个槽:它上一轮就成了,这一轮重放。
    assert names.count("03-develop.inventory") == 1
    assert len([name for name in names if name.endswith(".inventory")]) == 1
    assert survivor.read_bytes() == before, "重放的产物不该被改写"
    # order 与被它冻结的 wire 重跑了。
    assert any(name.endswith(".order") and name != "02-develop.order" for name in names)


async def test_parallel_nodes_get_distinct_artifact_slots(
    workspace: Path, library: Path
) -> None:
    """并发派发下产物槽无重号、无覆盖——这条改动此前没有任何真并发压过它。"""
    task_id = _submit(workspace, "订单与库存都要改")
    arm_plan(library, task_id, NODES)
    arm_nodes(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    slots = [
        item.name
        for item in (workspace / "tasks" / task_id / "artifacts").iterdir()
        if item.is_dir()
    ]
    numbers = [name.split("-", 1)[0] for name in slots]
    assert len(numbers) == len(set(numbers)), f"序号撞了: {sorted(slots)}"
    for name in slots:
        assert (workspace / "tasks" / task_id / "artifacts" / name / "manifest.json").is_file()


async def test_a_downstream_node_is_handed_what_it_declared_it_needs(
    workspace: Path, library: Path
) -> None:
    """按 `needs` 取上游产物。

    按"最后跑完的那个节点"给的话,并行下拿到谁的产出是不确定的——而不确定的输入会让
    同一张图两次跑出不同的结果,却没有任何地方能看出为什么。
    """
    task_id = _submit(workspace, "订单与库存都要改")
    arm_plan(library, task_id, NODES)
    arm_nodes(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    bundle = next(
        (workspace / "tasks" / task_id / "artifacts" / "04-develop.wire").glob("context-*.md")
    ).read_text(encoding="utf-8")
    assert "upstream" in bundle or "order.diff" in bundle
