"""提交流水线与审批:PRD 08 的验收链路。

高风险改动 → 提交前检查 → `REVIEWING` → 驳回并附意见 → 回开发态且意见在上下文里 →
再次走到审批 → 批准 → 双层合并 → `COMPLETED`。

真实的 git 仓、真实的迁移表、真实的产物目录;Agent 那一段回放,PR 与合并走 `LocalForge`
(对本地 bare 仓的真实现,行为真实、无网络)。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.agents.pool import AgentPool
from agentgenome.agents.recording import RecordingLibrary
from agentgenome.agents.replay import ReplayRuntime
from agentgenome.cli import app
from agentgenome.core.events import LogKind
from agentgenome.core.scope_grants import ScopeGrant, append_grants
from agentgenome.core.states import TaskState
from agentgenome.jobs.handlers import STAGE_DEVELOP
from agentgenome.jobs.orchestrator import Orchestrator
from tests.fixtures.git import commit_all, git
from tests.fixtures.mall import materialize_mall
from tests.fixtures.tree import module_ids, patch_contracts, patch_module_map
from tests.fixtures.verification import write_test_verification

runner = CliRunner()

APPROVER = "alice"

PLAN = {
    "task_id": "",
    "producer": "decision-employee",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
    "modules": ["order-service"],
    "cross_module": False,
    "acceptance": ["下单时写入一条审计记录"],
    "risks": [],
}

DEV_RESULT = {
    "task_id": "",
    "producer": "dev-employee",
    "created_at": "2026-09-01T10:05:00Z",
    "passed": True,
    "changed_files": ["repos/order-service/migrations/0002_audit.sql"],
    "self_test": {"command": "pytest -q", "exit_code": 0, "passed": True},
    "impact": {"modules": ["order-service"], "rationale": "加一张审计表"},
    "questions": [],
}

PASSING_TEST = "def test_ok():\n    assert True\n"


def _record(library: Path, employee: str, procedure: str, round_: int, result: dict, files: dict):
    directory = library / f"{employee}__{procedure}__r{round_}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    for relative, content in files.items():
        target = directory / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_WORKTREES_HOME", str(tmp_path / "worktrees"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
    root = tmp_path / "ws"
    result = runner.invoke(
        app,
        [
            "init", "--local-only",
            str(root),
            "--name",
            "mall",
            "--repo",
            mall["order-service"].remote_url,
            "--repo",
            mall["inventory-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output

    modules = module_ids(root)
    for module_id in modules:
        patch_module_map(root, module_id, test_cmd="python -m pytest -q tests")
    patch_contracts(
        root,
        datastores=[
            {
                "id": "orders",
                "kind": "postgres",
                "owner": "order-service",
                "migrations": "repos/order-service/migrations",
            }
        ],
    )

    for module_id in modules:
        write_test_verification(
            root, module_id, ("unit", ("python", "-m", "pytest", "-q", "tests"))
        )

    # 本地 bare 仓走 LocalForge;审批人名单里放一个人。
    config = root / "agentgenome.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\nplatform:\n  git_host: local\n  protected_branch: main\n"
        + f"approval:\n  approvers: [{APPROVER}]\n",
        encoding="utf-8",
    )
    commit_all(root, "chore: 配上门禁、迁移目录与审批人")

    # 顶层也要有远端:双层合并的最后一步是合顶层 PR,而 PR 开在远端上。
    remote = tmp_path / "ws.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "--quiet", "-u", "origin", "main")
    return root


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "lib"
    path.mkdir()
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(path))
    return path


@pytest.fixture
def no_secrets_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    """把密钥扫描换成一个存在且什么都不报的可执行文件。

    真 gitleaks 不一定装了,而这一组测试要验的是**流水线的编排**,不是 gitleaks 的检出能力
    (PRD 明确把后者划到"不测的")。工具缺失那条路单独一条测试验。
    """
    monkeypatch.setattr(
        "agentgenome.security.precheck.SECRETS_CMD", ("true", "detect"), raising=False
    )


def _orchestrator(workspace: Path, library: Path) -> Orchestrator:
    return Orchestrator(
        workspace,
        pool=AgentPool({"replay": ReplayRuntime(RecordingLibrary(library))}),
        runtime_name="replay",
    )


def _submit(workspace: Path) -> str:
    result = runner.invoke(
        app,
        [
            "task",
            "submit",
            "--requirement",
            "下单时写一条审计记录",
            # 迁移目录同时是影响规则与高风险规则的目标。这一组测的是提交流水线,
            # 集成测试那条链路在 PRD 07 的端到端里已经验过——留着它只会把这里的
            # 失败原因搅浑。
            "--itest",
            "never",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["id"])


def _arm(library: Path, task_id: str, round_: int = 1, files: dict | None = None) -> None:
    _record(
        library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id}, {}
    )
    _record(
        library,
        "dev-employee",
        "code-develop",
        round_,
        DEV_RESULT | {"task_id": task_id},
        files
        or {
            "repos/order-service/migrations/0002_audit.sql": (
                f"-- round {round_}\ncreate table audit();\n"
            ),
            "repos/order-service/tests/test_audit.py": PASSING_TEST,
        },
    )
    _record(
        library,
        "decision-employee",
        "itest-decide",
        round_,
        {
            "task_id": task_id,
            "producer": "decision-employee",
            "created_at": "2026-09-01T10:06:00Z",
            "passed": True,
            "needs_itest": False,
            "reason": "只动了订单域",
        },
        {},
    )


# --- 风险评级与审批 ---------------------------------------------------------


async def test_a_migration_change_stops_for_a_human(
    workspace: Path, library: Path, no_secrets_scanner: None
) -> None:
    """加一行日志和改一次数据库迁移,风险差着数量级。重的必须停下来等人。"""
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(4):  # plan → dev → gate → precheck
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.state is TaskState.REVIEWING
    assert task.risk_level == "high"


async def test_a_task_that_widened_its_scope_stops_for_a_human(
    workspace: Path, library: Path, no_secrets_scanner: None
) -> None:
    """**这是扩权自动放行的对价。**

    只有自动放行的话,员工可以自助扩权而没有任何人过目。真正要守的性质是"没有一次跨计划的
    改动能在无人过目的情况下合入"——它就守在这里,而不是守在一个飞行途中的闸门上。
    """
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    append_grants(
        workspace,
        task_id,
        [ScopeGrant(module="inventory-service", reason="预占要改库存扣减", round_=1)],
    )

    for _ in range(4):  # plan → dev → gate → precheck
        await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    assert task.state is TaskState.REVIEWING
    assert task.risk_level == "high"


async def test_the_approval_notification_carries_enough_to_judge_urgency(
    workspace: Path, library: Path, no_secrets_scanner: None
) -> None:
    """没配 webhook 时不发也不报错——通知渠道是可选的。"""
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(4):
        await orchestrator.advance(task_id)

    notes = [
        event.payload["note"]
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.NOTE and "审批通知" in str(event.payload.get("note"))
    ]
    assert notes, "通知这件事本身没有留痕"


async def test_someone_not_on_the_list_cannot_approve(
    workspace: Path, library: Path, no_secrets_scanner: None
) -> None:
    """不校验的话这道关卡就只是个仪式——任何能调 CLI 的人都能自己批准自己。"""
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)

    result = runner.invoke(
        app, ["task", "approve", task_id, "--as", "mallory", "--workspace", str(workspace)]
    )

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert orchestrator.store.get(task_id).state is TaskState.REVIEWING


async def test_a_rejection_needs_a_comment(
    workspace: Path, library: Path, no_secrets_scanner: None
) -> None:
    """没有意见的驳回对下一轮没有任何价值。"""
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)

    result = runner.invoke(
        app,
        [
            "task",
            "reject",
            task_id,
            "--as",
            APPROVER,
            "--comment",
            "  ",
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code != 0
    assert orchestrator.store.get(task_id).state is TaskState.REVIEWING


async def test_a_rejection_comment_reaches_the_next_round(
    workspace: Path, library: Path, no_secrets_scanner: None
) -> None:
    """人的评审意见不会白写——这是人的判断唯一能直接影响下一轮的通道。"""
    task_id = _submit(workspace)
    _arm(library, task_id)
    _arm(library, task_id, round_=2)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)

    result = runner.invoke(
        app,
        [
            "task",
            "reject",
            task_id,
            "--as",
            APPROVER,
            "--comment",
            "迁移脚本没有回滚语句,补上再来",
            "--workspace",
            str(workspace),
        ],
    )
    assert result.exit_code == 0, result.output
    assert orchestrator.store.get(task_id).state is TaskState.DEVELOPING

    await orchestrator.advance(task_id)  # 第二轮开发
    bundle = (
        orchestrator.bus(orchestrator.store.get(task_id)).by_stage(STAGE_DEVELOP)[1].path
        / "context-attempt-0.md"
    ).read_text(encoding="utf-8")
    assert "没有回滚语句" in bundle, "驳回意见没进下一轮的上下文"


async def test_approval_is_recorded_with_who_and_when(
    workspace: Path, library: Path, no_secrets_scanner: None
) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)

    runner.invoke(
        app, ["task", "approve", task_id, "--as", APPROVER, "--workspace", str(workspace)]
    )

    records = [
        event for event in orchestrator.log.events(task_id) if event.kind is LogKind.APPROVAL
    ]
    assert records and records[-1].actor == APPROVER
    assert records[-1].ts is not None


# --- 双层合并 ---------------------------------------------------------------


async def test_an_approved_task_merges_and_completes(
    workspace: Path, library: Path, no_secrets_scanner: None
) -> None:
    """批准之后走完双层合并:子仓先合、顶层指针前移、最后合顶层 PR。"""
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)

    result = runner.invoke(
        app, ["task", "approve", task_id, "--as", APPROVER, "--workspace", str(workspace)]
    )
    assert result.exit_code == 0, result.output
    assert orchestrator.store.get(task_id).state is TaskState.MERGING

    await orchestrator.advance(task_id)

    assert orchestrator.store.get(task_id).state is TaskState.COMPLETED


async def test_the_pr_body_names_the_rule_that_flagged_it(
    workspace: Path, library: Path, no_secrets_scanner: None
) -> None:
    """回归测试:`_pr_text` 此前把 `matched_rules` 硬编码成 `()`——PR 描述永远显示
    "**high**" 却不说为什么,评审者看不到触发高风险评级的具体规则。这次改动命中的是
    迁移目录,顶层 PR 的正文里必须能读到 `migrations`。
    """
    from agentgenome.space.local_forge import LocalForge

    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)

    runner.invoke(
        app, ["task", "approve", task_id, "--as", APPROVER, "--workspace", str(workspace)]
    )
    await orchestrator.advance(task_id)

    merge_state = orchestrator.merge_state(orchestrator.store.get(task_id))
    assert merge_state is not None and merge_state.workspace_pr is not None
    pr = merge_state.workspace_pr
    record = LocalForge()._read_record(Path(pr.repo), pr.number)

    assert "migrations" in record["body"], record["body"]


async def test_merging_twice_does_not_open_a_second_pr(
    workspace: Path, library: Path, no_secrets_scanner: None
) -> None:
    """**平台上重复开 PR 会得到两个。** 这是整套崩溃恢复里最不能马虎的一处:其余状态重跑
    最多浪费 token,这里重跑会在别人的代码库上留下垃圾。"""
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)
    runner.invoke(
        app, ["task", "approve", task_id, "--as", APPROVER, "--workspace", str(workspace)]
    )
    await orchestrator.advance(task_id)
    first = orchestrator.merge_state(orchestrator.store.get(task_id))

    # 重放:任务已经是终态,再推一次不该动任何东西。
    await orchestrator.advance(task_id)
    again = orchestrator.merge_state(orchestrator.store.get(task_id))

    assert first is not None and again is not None
    assert [step.pr for step in first.submodules] == [step.pr for step in again.submodules]


# --- 提交前检查 -------------------------------------------------------------


async def test_a_missing_secret_scanner_escalates_instead_of_looping(
    workspace: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具没装是环境问题。让开发员工去装一个它装不了的东西,只会以完全相同的方式失败三轮。"""
    monkeypatch.setattr(
        "agentgenome.security.precheck.SECRETS_CMD",
        ("definitely-not-installed-scanner",),
        raising=False,
    )
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(5):
        await orchestrator.advance(task_id)

    assert orchestrator.store.get(task_id).state is TaskState.ESCALATED


# --- 审计 -------------------------------------------------------------------


async def test_an_unfinished_task_can_still_be_audited(
    workspace: Path, library: Path, no_secrets_scanner: None
) -> None:
    """出事的时候人不会等任务走完——恰恰相反,最需要审计包的时刻通常是任务卡住的时候。"""
    task_id = _submit(workspace)
    _arm(library, task_id)
    orchestrator = _orchestrator(workspace, library)
    for _ in range(4):
        await orchestrator.advance(task_id)

    result = runner.invoke(app, ["audit", task_id, "--workspace", str(workspace), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["entries"] > 0
    assert Path(payload["path"]).is_file()


def test_auditing_an_unknown_task_says_so(workspace: Path) -> None:
    result = runner.invoke(app, ["audit", "ag-x", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
