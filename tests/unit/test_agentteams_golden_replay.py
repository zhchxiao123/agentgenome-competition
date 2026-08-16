"""黄金回放:用录自真实 AgentTeams 平台的素材驱动适配器全链路。

素材来历见 `tests/fixtures/golden/agentteams/README.md`。这条测试的价值是
**真机形状**:假传输的剧情是我们想象的平台,这份素材是平台真实说过的话——
两者对适配器的约束并不相同,漂移时先怀疑想象的那份。
"""

from __future__ import annotations

import json
from pathlib import Path

from agentgenome.agents.agentteams import AgentTeamsRuntime
from agentgenome.agents.agentteams.recording import ReplayTransport
from agentgenome.agents.runtime import JobSpec

GOLDEN = Path(__file__).resolve().parent.parent / "fixtures" / "golden"


async def test_the_recorded_real_platform_exchange_drives_the_adapter(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "README.md").write_text("# 冒烟工作区\n这里是初始现场。\n", encoding="utf-8")
    context = tmp_path / "context.md"
    context.write_text("# 上下文包\n", encoding="utf-8")
    spec = JobSpec(
        task_id="smoke-002",
        employee_id="agentgenome-smoke",
        procedure_id="smoke",
        procedure_version="0.0.1",
        round=1,
        workdir=workdir,
        context_file=context,
        output_dir=tmp_path / "out",
        timeout_s=10,
    )

    result = await AgentTeamsRuntime(ReplayTransport(GOLDEN)).run_job(spec)

    assert result.ok is True
    assert (workdir / "hello.txt").read_text() == "hello from agentgenome adapter\n", (
        "真机 Worker 的代码改动要落回本地工作区"
    )
    assert result.result_path is not None
    assert json.loads(result.result_path.read_text())["passed"] is True
    assert result.tokens_available is False, "平台无逐任务计量——素材如实录着 null"
