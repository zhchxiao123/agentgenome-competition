"""agentteams 适配器的事务性工作区同步。

核心不变量:**返回时本地工作区即为最终现场**——越权检查、后续工序接力、
提交流水线的正确性都押在这一条上。取回失败判过程失败,工作区保持开工前状态。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from agentgenome.agents.agentteams import AgentTeamsRuntime, TransportOutcome
from agentgenome.agents.pool import AgentPool
from agentgenome.agents.runtime import FailureKind, JobSpec
from agentgenome.core.scope import ScopePolicy
from tests.fixtures.fake_agentteams import FakeTransport
from tests.fixtures.git import commit_all, git
from tests.unit.test_agentteams_runtime import GOOD_RESULT, _ok_outcome


def _spec(tmp_path: Path, **overrides: Any) -> JobSpec:
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    context = tmp_path / "context.md"
    context.write_text("# 上下文包\n", encoding="utf-8")
    spec = JobSpec(
        task_id="ag-1",
        employee_id="dev-employee",
        procedure_id="code-develop",
        procedure_version="1.0.0",
        round=1,
        workdir=workdir,
        context_file=context,
        output_dir=tmp_path / "out",
        timeout_s=5,
    )
    return replace(spec, **overrides) if overrides else spec


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


# --- 推送:Worker 看到的现场 -------------------------------------------------


async def test_the_workspace_travels_with_the_job(tmp_path: Path) -> None:
    """容器员工要看到与本地员工同一个现场——包括上一个员工刚写下的未提交改动。"""
    transport = FakeTransport([_ok_outcome()])
    spec = _spec(tmp_path)
    (spec.workdir / "src").mkdir()
    (spec.workdir / "src" / "app.py").write_text("print('旧')\n", encoding="utf-8")
    (spec.workdir / "留给下个员工.md").write_text("上一轮的成果\n", encoding="utf-8")

    await AgentTeamsRuntime(transport).run_job(spec)

    workspace = transport.jobs[0].workspace
    assert workspace is not None
    assert workspace["src/app.py"] == "print('旧')\n"
    assert workspace["留给下个员工.md"] == "上一轮的成果\n"


async def test_git_internals_stay_out_of_the_snapshot(tmp_path: Path) -> None:
    transport = FakeTransport([_ok_outcome()])
    spec = _spec(tmp_path)
    (spec.workdir / "app.py").write_text("print(1)\n", encoding="utf-8")
    (spec.workdir / ".git").mkdir()
    (spec.workdir / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    await AgentTeamsRuntime(transport).run_job(spec)

    workspace = transport.jobs[0].workspace
    assert workspace is not None
    assert "app.py" in workspace
    assert not any(name.startswith(".git") for name in workspace)


# --- 取回:落地与事务性 ------------------------------------------------------


async def test_worker_changes_land_in_the_local_workdir(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    (spec.workdir / "app.py").write_text("print('旧')\n", encoding="utf-8")
    outcome = _ok_outcome(
        changed_files={"app.py": "print('新')\n", "src/new.py": "print(2)\n"}
    )

    result = await AgentTeamsRuntime(FakeTransport([outcome])).run_job(spec)

    assert result.ok is True
    assert (spec.workdir / "app.py").read_text() == "print('新')\n"
    assert (spec.workdir / "src" / "new.py").read_text() == "print(2)\n"


async def test_a_deletion_by_the_worker_is_applied(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    (spec.workdir / "老文件.py").write_text("要删掉\n", encoding="utf-8")

    result = await AgentTeamsRuntime(
        FakeTransport([_ok_outcome(changed_files={"老文件.py": None})])
    ).run_job(spec)

    assert result.ok is True
    assert not (spec.workdir / "老文件.py").exists()


async def test_a_path_escape_fails_the_job_and_leaves_the_workdir_untouched(
    tmp_path: Path,
) -> None:
    """半份改动不落地:任一路径越界,整个取回作废,工作区逐字节保持开工前状态。"""
    spec = _spec(tmp_path)
    (spec.workdir / "app.py").write_text("print('旧')\n", encoding="utf-8")
    before = _snapshot(spec.workdir)
    outcome = _ok_outcome(
        changed_files={"app.py": "print('新')\n", "../外面.txt": "越界写入\n"}
    )

    result = await AgentTeamsRuntime(FakeTransport([outcome])).run_job(spec)

    assert result.ok is False
    assert result.failure_kind is FailureKind.PROCESS
    assert "越出" in (result.failure_detail or "")
    assert _snapshot(spec.workdir) == before, "工作区必须保持开工前状态"
    assert not (tmp_path / "外面.txt").exists()


async def test_overwriting_a_binary_file_still_rolls_back_cleanly(tmp_path: Path) -> None:
    """原样快照存字节不存文本:被覆盖的可能是二进制文件,按 UTF-8 读它会炸——
    炸在快照那一步意味着前面的写入没人回滚,恰好击穿事务性承诺。"""
    spec = _spec(tmp_path)
    binary = spec.workdir / "assets" / "logo.png"
    binary.parent.mkdir()
    binary.write_bytes(b"\x89PNG\x00\xff\xfe")
    outcome = _ok_outcome(
        changed_files={"assets/logo.png": "覆盖成文本\n", "src/new.py": "print(1)\n"}
    )

    result = await AgentTeamsRuntime(FakeTransport([outcome])).run_job(_spec(tmp_path))

    assert result.ok is True, "覆盖二进制文件是合法改动,不该炸"
    assert (spec.workdir / "assets" / "logo.png").read_text() == "覆盖成文本\n"
    assert (spec.workdir / "src" / "new.py").read_text() == "print(1)\n"


async def test_mixed_fleet_hands_the_workdir_between_local_and_container_employees(
    tmp_path: Path,
) -> None:
    """同一任务先后混跑本地员工与容器员工:本地子进程写下的现场要被容器员工
    看到,容器员工的改动要留给后续本地工序。"""
    import sys

    from agentgenome.agents.subprocess_runtime import SubprocessRuntime
    from tests.fixtures import fake_agent
    from tests.fixtures.fake_agent import SCRIPT_ENV

    spec_local = _spec(
        tmp_path,
        output_schema={},
        credentials={
            SCRIPT_ENV: json.dumps(
                {
                    "result": {"task_id": "ag-1"},
                    "files": {"src/from_local.py": "print('本地员工写的')\n"},
                    "workdir": str(tmp_path / "work"),
                    "output_dir": str(tmp_path / "out"),
                }
            )
        },
    )
    transport = FakeTransport(
        [_ok_outcome(changed_files={"src/from_container.py": "print('容器员工写的')\n"})]
    )
    pool = AgentPool(
        {
            "subprocess": SubprocessRuntime(argv=[sys.executable, fake_agent.__file__]),
            "agentteams": AgentTeamsRuntime(transport),
        }
    )

    local = await pool.submit(spec_local, "subprocess")
    container = await pool.submit(_spec(tmp_path, output_dir=tmp_path / "out2"), "agentteams")

    assert local.ok is True and container.ok is True
    pushed = transport.jobs[0].workspace
    assert pushed is not None
    assert pushed["src/from_local.py"] == "print('本地员工写的')\n", "容器员工要看到本地现场"
    assert (tmp_path / "work" / "src" / "from_container.py").exists(), "容器改动要留给后续工序"


async def test_writing_into_git_internals_is_refused(tmp_path: Path) -> None:
    """改 .git 能让后续的 diff 说谎——越权检查、提交流水线全建立在 git 可信之上。"""
    spec = _spec(tmp_path)
    before = _snapshot(spec.workdir)

    result = await AgentTeamsRuntime(
        FakeTransport([_ok_outcome(changed_files={".git/config": "[core]\n"})])
    ).run_job(spec)

    assert result.ok is False
    assert result.failure_kind is FailureKind.PROCESS
    assert _snapshot(spec.workdir) == before


# --- 越权检查照常生效 --------------------------------------------------------


async def test_scope_violation_by_a_container_employee_is_caught_and_rolled_back(
    tmp_path: Path,
) -> None:
    """隔离手段升级不等于放松管束:越权照样改判、照样回滚。检查本身零改动。"""
    spec = _spec(tmp_path, scope=ScopePolicy(write_paths=("src/**",)))
    git(spec.workdir, "init", "-q")
    (spec.workdir / "src").mkdir()
    (spec.workdir / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    commit_all(spec.workdir, "chore: 初始现场")
    outcome = _ok_outcome(
        changed_files={"secrets.txt": "越权写入\n", "src/app.py": "print(2)\n"}
    )
    pool = AgentPool({"agentteams": AgentTeamsRuntime(FakeTransport([outcome]))})

    result = await pool.submit(spec, "agentteams")

    assert result.ok is False
    assert result.failure_kind is FailureKind.SCOPE
    assert not (spec.workdir / "secrets.txt").exists(), "越权文件要被回滚"


async def test_in_scope_changes_by_a_container_employee_survive_the_scope_check(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path, scope=ScopePolicy(write_paths=("src/**",)))
    git(spec.workdir, "init", "-q")
    (spec.workdir / "src").mkdir()
    (spec.workdir / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    commit_all(spec.workdir, "chore: 初始现场")
    result_json = json.dumps(GOOD_RESULT, ensure_ascii=False)
    outcome = TransportOutcome(
        ok=True,
        artifacts={"result.json": result_json},
        changed_files={"src/app.py": "print(2)\n"},
    )
    pool = AgentPool({"agentteams": AgentTeamsRuntime(FakeTransport([outcome]))})

    result = await pool.submit(spec, "agentteams")

    assert result.ok is True
    assert (spec.workdir / "src" / "app.py").read_text() == "print(2)\n"
