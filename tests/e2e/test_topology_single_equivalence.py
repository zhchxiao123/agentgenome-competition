"""`single` 等价性:泛化层什么都没改变。

**这一层的价值全部来自"它什么都没改变"。** 处理器从"派一个 Job"变成"解析模板 → 执行拓扑
→ 聚合 → 抛既有事件"之后,同一个夹具任务的事件序列、产物布局、状态迁移必须逐条相等——
一旦泛化层顺手改了点什么,后面每一次拓扑扩展都要连带怀疑它。

黄金序列是**改造前**跑出来的:先在上一个提交上跑这个文件拿到序列,再回到改造后跑一遍。
写死在这里而不是"跑两遍比一比",是因为改造后的代码里已经没有改造前那条路可跑了。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentgenome.core.events import LogKind
from agentgenome.core.states import TaskState
from agentgenome.core.task import TaskStore
from agentgenome.core.topology import SINGLE, UnknownTopology
from agentgenome.jobs.orchestrator import Orchestrator
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    PASSING_TEST,
    _arm,
    _orchestrator,
    _submit,
    library,
    workspace,
)

__all__ = ["library", "workspace"]

#: 改造前那条链路留下的事件序列(`(kind, 关键字段)`)。**拓扑事件不在里面**——它是新增的
#: 记录,不是对既有记录的改写,所以断言时把它单独摘出来。
GOLDEN = [
    ('task_created', ''),
    ('job_started', 'requirement-analysis@1.1.0'),
    ('job_finished', 'requirement-analysis@1.1.0'),
    ('transition', 'plan_done'),
    ('note', ''),
    ('job_started', 'code-develop@1.0.0'),
    ('job_finished', 'code-develop@1.0.0'),
    ('transition', 'dev_done'),
    ('job_started', 'unit-gate@1.0.0'),
    ('job_finished', 'unit-gate@1.0.0'),
    ('gate_result', ''),
    ('job_started', 'itest-decide@1.0.0'),
    ('job_finished', 'itest-decide@1.0.0'),
    ('itest_decision', ''),
    ('transition', 'gate_pass'),
]


def _sequence(orchestrator: Orchestrator, task_id: str) -> list[tuple[str, str]]:
    """事件序列,拓扑事件摘掉。"""
    rows = []
    for event in orchestrator.log.events(task_id):
        if event.kind is LogKind.TOPOLOGY:
            continue
        payload = event.payload or {}
        key = str(payload.get("procedure_ref") or payload.get("event") or "")
        rows.append((event.kind.value, key))
    return rows


async def _walk(workspace: Path, library: Path) -> tuple[Orchestrator, str]:
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(3):
        await orchestrator.advance(task_id)
    return orchestrator, task_id


async def test_the_event_sequence_is_unchanged(workspace: Path, library: Path) -> None:
    orchestrator, task_id = await _walk(workspace, library)

    assert _sequence(orchestrator, task_id) == GOLDEN


async def test_the_artifact_layout_is_unchanged(workspace: Path, library: Path) -> None:
    """`single` 塌缩掉节点维度:目录名里不许出现节点后缀。"""
    _, task_id = await _walk(workspace, library)

    names = sorted(item.name for item in (workspace / "tasks" / task_id / "artifacts").iterdir())

    assert names == ["01-plan", "02-develop", "03-unit-gate", "04-itest-decide"]


async def test_the_task_lands_in_the_same_state(workspace: Path, library: Path) -> None:
    _, task_id = await _walk(workspace, library)

    assert TaskStore(workspace).get(task_id).state is TaskState.READY_TO_COMMIT


async def test_the_manifest_keeps_naming_the_employee_and_procedure(
    workspace: Path, library: Path
) -> None:
    """血缘清单的 producer 从"处理器上写死的员工"改成"节点声明的员工",值必须没变。"""
    _, task_id = await _walk(workspace, library)

    manifest = json.loads(
        (workspace / "tasks" / task_id / "artifacts" / "02-develop" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["producer"] == "dev-employee"
    assert manifest["summary"].startswith("code-develop")
    assert manifest["node"] == "" and manifest["variant"] == ""


# --- 拓扑层自己的那几条 -------------------------------------------------------


async def test_the_topology_instance_lands_in_the_event_plane(
    workspace: Path, library: Path
) -> None:
    """图是数据不是黑箱:前端画图的数据源就是它,不另造一份。"""
    orchestrator, task_id = await _walk(workspace, library)

    instances = [
        event.payload
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.TOPOLOGY
    ]

    # 三个 Procedure 状态**全部**经模板路径执行,没有"直接派一个 Job"的旁路。
    # `itest-decide` 不在其中:它不是状态处理器,是编排器在门禁通过之后就地问的一个判定,
    # 走的是同一个 `dispatch` 但不属于任何状态的活——如实说明,不假装它也在图上。
    assert [item["stage"] for item in instances] == ["plan", "develop", "unit-gate"]
    first = instances[0]["template"]
    assert first["id"] == SINGLE
    assert len(first["nodes"]) == 1
    assert first["nodes"][0]["employee"] == "decision-employee"
    assert first["nodes"][0]["procedure"] == "requirement-analysis"
    assert first["edges"] == []


async def test_an_explicit_single_default_behaves_identically(
    workspace: Path, library: Path
) -> None:
    """显式配 `topology.default: single` 与不配一模一样。"""
    config = workspace / "agentgenome.yaml"
    config.write_text(
        config.read_text(encoding="utf-8") + "topology: {default: single}\n", encoding="utf-8"
    )

    orchestrator, task_id = await _walk(workspace, library)

    assert _sequence(orchestrator, task_id) == GOLDEN


async def test_a_template_without_an_executor_is_refused_not_silently_downgraded(
    workspace: Path, library: Path
) -> None:
    """回退到 single 的话,一个配成 dag 的部署会安安静静按单节点跑。

    表现是"并行怎么没生效",而原因在三层之外的一张注册表里。
    """
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    store = TaskStore(workspace)
    # 四种模板都有执行器了,所以这里用一个**没注册过的名字**——这条测试问的是
    # "配错名字会怎样",而不是"哪个模板还没做"。
    store.save(store.get(task_id).evolve(topology="swarm"))
    orchestrator = _orchestrator(workspace, library)

    try:
        await orchestrator.advance(task_id)
    except UnknownTopology as error:
        assert "swarm" in str(error)
    else:  # pragma: no cover - 走到这里说明静默降级了
        raise AssertionError("配了一个没有执行器的模板却照跑了")


async def test_a_task_level_override_of_single_still_runs(workspace: Path, library: Path) -> None:
    """任务级覆盖的通路本身要能走通(本期只有一个合法值,验的是通路不是第二个模板)。"""
    task_id = _submit(workspace)
    _arm(library, task_id, PASSING_TEST)
    store = TaskStore(workspace)
    store.save(store.get(task_id).evolve(topology=SINGLE))
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    assert store.get(task_id).state is TaskState.READY_TO_COMMIT
