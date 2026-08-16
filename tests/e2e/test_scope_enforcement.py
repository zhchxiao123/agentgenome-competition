"""越权检查的端到端:回放一次真实的越权写入,断言机制真的兜住了。

走 `agctl procedure run` + 回放运行时 + mall 夹具——真实的 git、真实的子模块、真实的
基因组加载器,唯独 Agent 那一段是确定性的。录制里的 `files/` 是员工**真的写下去的
文件**,不是断言的对象。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.space.git_ws import branch_name
from agentgenome.space.scope_guard import SCOPE_DIFF, SCOPE_REPORT
from tests.fixtures.git import commit_all, write_plan
from tests.fixtures.git import git as _git
from tests.fixtures.mall import materialize_mall
from tests.fixtures.procedures import RESULT, write_procedure

runner = CliRunner()

TASK = "ag-20260901-001"

#: 只能写第一个业务仓。`genome/rules/**` 由脚手架的 protected.yaml 兜底。
DEV = """\
id: dev-employee
runtime: claude-code
prompt: prompts/dev.md
procedures: [code-develop]
permissions:
  write_paths: ["repos/order-service/**"]
"""


def _record(library: Path, files: dict[str, str]) -> None:
    directory = library / "dev-employee__code-develop__r1"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps(RESULT), encoding="utf-8")
    for relative, content in files.items():
        target = directory / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture
def default_roster_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """与下面那个夹具的唯一区别:**不覆盖员工定义**,用 `init` 铺出来的默认花名册。

    上面那份手写的 `DEV` 是为了把授权收窄到一个仓、好验越权机制本身;而它恰恰验不到
    "默认花名册真的被装载、并且它的 glob 真的生效"——那两件事在 `test_roster.py` 里
    只到 `may_write` 为止,没穿过一次真实的 Job。
    """
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
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
    write_procedure(root / "genome" / "procedures", "code-develop", "agentic", prompt="去写代码\n")
    commit_all(root, "chore: 工序")
    write_plan(root, TASK, "order-service", "inventory-service")
    return root


def test_the_default_roster_lets_the_developer_write_every_planned_module(
    default_roster_workspace: Path, tmp_path: Path
) -> None:
    """默认授权是"本次计划命中的模块",而计划命中了两个,所以两个都能写。

    跨两个仓一次写完:只写一个仓的话,"授权一个模块"与"授权两个模块"会给出同样的结果,
    而占位符是不是真的展开成**多条**正是这条要盯的东西。
    """
    _record(
        tmp_path / "lib",
        {
            "repos/order-service/src/order/feature.py": "# 新功能\n",
            "repos/inventory-service/src/inventory/feature.py": "# 新功能\n",
        },
    )

    result = _run(default_roster_workspace)

    assert result.exit_code == 0, result.output
    assert (default_roster_workspace / "repos/inventory-service/src/inventory/feature.py").exists()


def test_the_default_roster_still_stops_the_developer_from_rewriting_the_rules(
    default_roster_workspace: Path, tmp_path: Path
) -> None:
    """放得过不等于拦得住。默认花名册装载之后,禁写那一半也得照样成立。"""
    _record(tmp_path / "lib", {"genome/rules/architecture.md": "# 一切皆可\n"})

    result = _run(default_roster_workspace)

    assert result.exit_code != 0
    assert json.loads(result.output)["failure_kind"] == "scope"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    mall = materialize_mall(tmp_path / "upstream")
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

    (root / "employees" / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "employees" / "prompts" / "dev.md").write_text("你是开发数字员工。\n", encoding="utf-8")
    (root / "employees" / "dev-employee.yaml").write_text(DEV, encoding="utf-8")
    write_procedure(root / "genome" / "procedures", "code-develop", "agentic", prompt="去写代码\n")
    # 员工定义与 Procedure 都是版本化资产。不提交的话它们在 Job 开始时是未跟踪状态,
    # 会被算成"这次 Job 碰的"。
    commit_all(root, "chore: 员工与工序")
    write_plan(root, TASK, "order-service", "inventory-service")
    return root


def _run(workspace: Path):
    return runner.invoke(
        app,
        [
            "procedure",
            "run",
            "code-develop",
            "--workspace",
            str(workspace),
            "--employee",
            "dev-employee",
            "--runtime",
            "replay",
            "--task",
            TASK,
            "--json",
        ],
    )


def _report(workspace: Path) -> dict:
    path = workspace / "tasks" / TASK / "artifacts" / "code-develop" / SCOPE_REPORT
    return json.loads(path.read_text(encoding="utf-8"))


# --- 越权 -------------------------------------------------------------------


def test_a_job_that_rewrites_the_rules_fails(workspace: Path, tmp_path: Path) -> None:
    """员工把架构规则改成对自己有利的样子,然后自己给自己开绿灯——这条路要堵死。"""
    _record(tmp_path / "lib", {"genome/rules/architecture.md": "# 一切皆可\n"})

    result = _run(workspace)

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["failure_kind"] == "scope"
    assert "genome/rules/architecture.md" in payload["failure_detail"]


def test_the_workspace_is_rolled_back(workspace: Path, tmp_path: Path) -> None:
    _record(tmp_path / "lib", {"genome/rules/architecture.md": "# 一切皆可\n"})
    original = (workspace / "genome" / "rules" / "architecture.md").read_text(encoding="utf-8")

    _run(workspace)

    assert (workspace / "genome" / "rules" / "architecture.md").read_text() == original


def test_the_report_says_which_rule_was_crossed(workspace: Path, tmp_path: Path) -> None:
    _record(tmp_path / "lib", {"genome/rules/architecture.md": "# 一切皆可\n"})

    _run(workspace)

    payload = _report(workspace)
    assert payload["ok"] is False
    assert payload["employee_id"] == "dev-employee"
    assert payload["task_id"] == TASK
    violation = payload["violations"][0]
    assert violation["path"] == "genome/rules/architecture.md"
    assert violation["kind"] == "forbidden"
    assert violation["rule"] == "genome/rules/**"


def test_the_protected_paths_come_from_the_project_rules(workspace: Path, tmp_path: Path) -> None:
    """受保护路径是**项目资产**,叠加在员工自己的规则之上——员工定义里一个字都没写它。"""
    _record(tmp_path / "lib", {"genome/rules/architecture.md": "# 一切皆可\n"})

    _run(workspace)

    assert "genome/rules/**" in _report(workspace)["policy"]["forbid_paths"]


def test_the_violating_diff_survives_the_rollback(workspace: Path, tmp_path: Path) -> None:
    """回滚之后现场就没了,这份 diff 是审计与下一轮回注的唯一依据。"""
    _record(tmp_path / "lib", {"genome/rules/architecture.md": "# 一切皆可\n"})

    _run(workspace)

    saved = workspace / "tasks" / TASK / "artifacts" / "code-develop" / SCOPE_DIFF
    assert "一切皆可" in saved.read_text(encoding="utf-8")


def test_a_write_into_an_unauthorized_business_repo_fails(workspace: Path, tmp_path: Path) -> None:
    """员工只被授权了 repos/order-service,写到 repos/inventory-service 同样是越权。"""
    _record(tmp_path / "lib", {"repos/inventory-service/src/inventory/hack.py": "# 不该动这个仓\n"})

    result = _run(workspace)

    assert result.exit_code != 0
    assert (
        "repos/inventory-service/src/inventory/hack.py"
        in json.loads(result.output)["failure_detail"]
    )
    assert not (workspace / "repos/inventory-service" / "src" / "inventory" / "hack.py").exists()


# --- 合规 -------------------------------------------------------------------


def test_an_authorized_change_is_left_alone(workspace: Path, tmp_path: Path) -> None:
    """只测拦得住不测放得过,等于没测。"""
    _record(tmp_path / "lib", {"repos/order-service/src/order/feature.py": "# 新功能\n"})

    result = _run(workspace)

    assert result.exit_code == 0, result.output
    assert (workspace / "repos/order-service" / "src" / "order" / "feature.py").exists()


def test_the_touched_submodule_gets_the_task_branch(workspace: Path, tmp_path: Path) -> None:
    """从 PRD 01 带过来的账:任务分支此前只在 Workspace 上建,子仓靠测试手工造。"""
    _record(tmp_path / "lib", {"repos/order-service/src/order/feature.py": "# 新功能\n"})

    _run(workspace)

    assert branch_name(TASK) in _git(
        workspace / "repos/order-service", "branch", "--list", branch_name(TASK)
    )


def test_an_untouched_submodule_gets_no_branch(workspace: Path, tmp_path: Path) -> None:
    """留空分支等于让一个没改过的仓也去开 PR。"""
    _record(tmp_path / "lib", {"repos/order-service/src/order/feature.py": "# 新功能\n"})

    _run(workspace)

    assert not _git(workspace / "repos/inventory-service", "branch", "--list", branch_name(TASK))


def test_the_branch_list_is_reported(workspace: Path, tmp_path: Path) -> None:
    _record(tmp_path / "lib", {"repos/order-service/src/order/feature.py": "# 新功能\n"})

    _run(workspace)

    assert _report(workspace)["submodule_branches"] == ["repos/order-service"]


# --- 评审员工 ---------------------------------------------------------------


def _record_reviewer(library: Path, files: dict[str, str]) -> None:
    directory = library / "reviewer-employee__code-critique__r1"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(
        json.dumps({**RESULT, "producer": "reviewer-employee", "approved": True, "findings": []}),
        encoding="utf-8",
    )
    for relative, content in files.items():
        target = directory / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _run_critique(workspace: Path):
    return runner.invoke(
        app,
        [
            "procedure",
            "run",
            "code-critique",
            "--workspace",
            str(workspace),
            "--employee",
            "reviewer-employee",
            "--runtime",
            "replay",
            "--task",
            TASK,
            "--state",
            "DEVELOPING",
            "--inputs",
            json.dumps({"requirement": "下单时预占库存"}),
            "--json",
        ],
    )


def test_a_reviewer_that_edits_code_fails_the_job_and_gets_rolled_back(
    default_roster_workspace: Path, tmp_path: Path
) -> None:
    """只批不改靠机制,不靠自觉。

    工具集里没有写入工具已经是第一层,但**第一层是可以被绕过的**(运行时换一个、
    提示注入、单纯的越界)。这条验的是第二层:每个 Job 结束后对着真实 git 工作树核对。
    """
    target = "repos/order-service/src/order/patched_by_reviewer.py"
    _record_reviewer(tmp_path / "lib", {target: "# 评审员工顺手改的\n"})

    result = _run_critique(default_roster_workspace)

    assert result.exit_code != 0
    assert json.loads(result.output)["failure_kind"] == "scope"
    assert not (default_roster_workspace / target).exists(), "越权改动没有被回滚"
