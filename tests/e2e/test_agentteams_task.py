"""容器员工的端到端:一个研发任务全程跑在 agentteams 运行时上,从需求推进到门禁。

真实的编排器、真实的 git 隔离工作区、真实的 pytest 门禁,唯独平台那一段是
回放的传输素材(issue 06)——这正是 CI 驱动这个运行时的标准姿势。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentgenome.agents.agentteams import AgentTeamsRuntime
from agentgenome.agents.agentteams.recording import SUBDIR, ReplayTransport
from agentgenome.agents.pool import AgentPool
from agentgenome.core.states import TaskState
from agentgenome.jobs.orchestrator import Orchestrator

# 复用编排器 e2e 的工作区脚手架与产物样本——同一条直通链路,换一个运行时。
from tests.e2e.test_orchestrator import (  # noqa: F401
    DEV_RESULT,
    ITEST_DECIDE_RESULT,
    PASSING_TEST,
    PLAN,
    _submit,
    workspace,
)
from tests.fixtures.git import commit_all


def _declare_agentteams_compat(root: Path) -> None:
    """工作区管理员的动作:把工序显式声明为兼容 agentteams。

    兼容闸的语义是"只在一个运行时上验证过的工序不该悄悄换台跑"——对容器
    运行时它照常生效,启用是一次**显式的资产变更**,不是适配器的特权。
    """
    patched = 0
    for path in (root / "genome" / "procedures").rglob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        if "runtimes: [claude-code]" in text:
            path.write_text(
                text.replace("runtimes: [claude-code]", "runtimes: [claude-code, agentteams]"),
                encoding="utf-8",
            )
            patched += 1
    assert patched, "脚手架工序里找不到 compat 声明——启用动作没生效"
    commit_all(root, "chore: 工序声明兼容 agentteams")


def _arm_transport(
    library: Path,
    employee: str,
    procedure: str,
    round_: int,
    result: dict[str, object],
    changed: dict[str, str] | None = None,
) -> None:
    """手写一份传输素材。素材形状见 `agents.agentteams.recording`。"""
    directory = library / SUBDIR / f"{employee}__{procedure}__r{round_}"
    directory.mkdir(parents=True, exist_ok=True)
    outcome = {
        "ok": True,
        "detail": None,
        "artifacts": {"result.json": json.dumps(result, ensure_ascii=False)},
        "changed_files": changed,
        "tokens_used": None,
        "events": [{"kind": "text", "text": f"{employee} 在容器里干完了 {procedure}"}],
    }
    (directory / "outcome.json").write_text(
        json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def test_a_dev_task_walks_to_ready_to_commit_on_container_employees(
    workspace: Path,  # noqa: F811 —— pytest 夹具按名字注入,与顶部的夹具导入同名是刻意的
    tmp_path: Path,
) -> None:
    """直通链路 plan → dev → gate,全部 Job 派给容器员工。编排器零改动。"""
    _declare_agentteams_compat(workspace)
    task_id = _submit(workspace)
    library = tmp_path / "transport-lib"
    _arm_transport(
        library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id}
    )
    _arm_transport(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        changed={"repos/order-service/tests/test_reserve.py": PASSING_TEST},
    )
    _arm_transport(
        library, "decision-employee", "itest-decide", 1, ITEST_DECIDE_RESULT | {"task_id": task_id}
    )
    pool = AgentPool({"agentteams": AgentTeamsRuntime(ReplayTransport(library))})
    orchestrator = Orchestrator(workspace, pool=pool, runtime_name="agentteams")

    assert (await orchestrator.advance(task_id)).state is TaskState.DEVELOPING
    assert (await orchestrator.advance(task_id)).state is TaskState.UNIT_TESTING
    assert (await orchestrator.advance(task_id)).state is TaskState.READY_TO_COMMIT
