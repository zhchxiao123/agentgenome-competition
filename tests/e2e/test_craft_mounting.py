"""手艺物化走完整派发路径:走 CLI 入口,断言员工工作区里真的出现了那份 SKILL.md。

单测证明了复制语义对(`tests/unit/test_craft_materialization.py`);这里证明的是**它确实接在
派发路径上**——物化写对了但没人调用,单测照样全绿。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.genome import craft
from tests.fixtures.procedures import RESULT, write_procedure

runner = CliRunner()

OPERATOR = """\
id: operator
runtime: claude-code
prompt: prompts/operator.md
procedures: [code-develop, survey]
tools:
  allow: [Bash, Read]
permissions:
  write_paths: ["**"]
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
    (tmp_path / "global").mkdir()
    (tmp_path / "lib").mkdir()
    root = tmp_path / "ws"
    (root / "genome" / "procedures").mkdir(parents=True)
    (root / "agentgenome.yaml").write_text("platform: {git_host: local}\n")
    (root / "employees" / "prompts").mkdir(parents=True)
    (root / "employees" / "prompts" / "operator.md").write_text("你是操作员。\n", encoding="utf-8")
    (root / "employees" / "operator.yaml").write_text(OPERATOR, encoding="utf-8")
    for args in (("init", "--initial-branch=main"), ("commit", "--allow-empty", "-m", "base")):
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@t.test", *args],
            cwd=root,
            check=True,
            capture_output=True,
        )
    return root


def _recording(library: Path, procedure_id: str) -> None:
    directory = library / f"operator__{procedure_id}__r1"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps(RESULT), encoding="utf-8")


def _with_craft(procedures_root: Path, procedure_id: str, crafts: dict[str, str]) -> None:
    write_procedure(procedures_root, procedure_id, "agentic", prompt="干活\n", script=None)
    for name, body in crafts.items():
        directory = procedures_root / procedure_id / craft.CRAFT_DIR / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / craft.CRAFT_MANIFEST).write_text(body, encoding="utf-8")


def _run(workspace: Path, procedure_id: str):
    return runner.invoke(
        app,
        [
            "procedure",
            "run",
            procedure_id,
            "--workspace",
            str(workspace),
            "--runtime",
            "replay",
            "--json",
        ],
    )


def _mounted(workspace: Path) -> Path:
    """CLI 把工作区设在 workspace 根上,所以物化目标就在那儿。"""
    return workspace / craft.MOUNT_SUBPATH


def test_the_craft_reaches_the_workspace_the_agent_runs_in(workspace: Path, tmp_path: Path) -> None:
    _with_craft(
        workspace / "genome" / "procedures",
        "code-develop",
        {"failure-diagnosis": "# 失败诊断\n\n先读报告,再动手。\n"},
    )
    _recording(tmp_path / "lib", "code-develop")

    result = _run(workspace, "code-develop")

    assert result.exit_code == 0, result.output
    landed = _mounted(workspace) / "failure-diagnosis" / craft.CRAFT_MANIFEST
    assert landed.is_file()
    assert "先读报告" in landed.read_text(encoding="utf-8")


def test_the_dispatch_result_records_which_crafts_were_mounted(
    workspace: Path, tmp_path: Path
) -> None:
    """审计要能复现"当时带着哪版手艺干的活"——`procedure@version` 只说清了契约那一半。"""
    _with_craft(
        workspace / "genome" / "procedures",
        "code-develop",
        {"failure-diagnosis": "# a\n", "output-discipline": "# b\n"},
    )
    _recording(tmp_path / "lib", "code-develop")

    result = _run(workspace, "code-develop")

    payload = json.loads(result.output)
    assert payload["crafts"] == ["failure-diagnosis", "output-discipline"]


def test_a_procedure_without_craft_still_runs(workspace: Path, tmp_path: Path) -> None:
    """手艺是增强不是前置依赖。冷启动时工序只靠 prompt.md 也能跑。"""
    write_procedure(
        workspace / "genome" / "procedures", "code-develop", "agentic", prompt="干活\n", script=None
    )
    _recording(tmp_path / "lib", "code-develop")

    result = _run(workspace, "code-develop")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["crafts"] == []


def test_the_previous_procedures_craft_does_not_linger(workspace: Path, tmp_path: Path) -> None:
    """换一个工序跑,上一个的手艺必须消失——否则角色定制在第二个 Job 之后就不成立。"""
    procedures = workspace / "genome" / "procedures"
    _with_craft(procedures, "survey", {"codebase-survey": "# 勘察\n"})
    _with_craft(procedures, "code-develop", {"failure-diagnosis": "# 诊断\n"})
    _recording(tmp_path / "lib", "survey")
    _recording(tmp_path / "lib", "code-develop")

    assert _run(workspace, "survey").exit_code == 0
    assert _run(workspace, "code-develop").exit_code == 0

    assert sorted(p.name for p in _mounted(workspace).iterdir()) == ["failure-diagnosis"]


def test_a_tampered_mount_is_restored_on_the_next_run(workspace: Path, tmp_path: Path) -> None:
    """员工改了挂载副本不会持久化:它不入库,而且每次重新物化。"""
    _with_craft(workspace / "genome" / "procedures", "code-develop", {"one": "原文\n"})
    _recording(tmp_path / "lib", "code-develop")
    assert _run(workspace, "code-develop").exit_code == 0

    mounted = _mounted(workspace) / "one" / craft.CRAFT_MANIFEST
    mounted.write_text("员工偷偷改的\n", encoding="utf-8")

    assert _run(workspace, "code-develop").exit_code == 0

    assert mounted.read_text(encoding="utf-8") == "原文\n"


def test_a_craft_without_a_manifest_is_refused_at_load_time(workspace: Path) -> None:
    """缺 SKILL.md 的目录不是一个手艺包,挂上去也没用——加载期就拒绝。"""
    procedures = workspace / "genome" / "procedures"
    write_procedure(procedures, "code-develop", "agentic", prompt="干活\n", script=None)
    (procedures / "code-develop" / craft.CRAFT_DIR / "broken").mkdir(parents=True)

    result = runner.invoke(app, ["procedure", "validate", str(procedures / "code-develop")])

    assert result.exit_code != 0
    assert craft.CRAFT_MANIFEST in result.output


def test_the_mount_directory_is_ignored_by_git(tmp_path: Path) -> None:
    """派生视图不进版本面。入库的话员工对挂载副本的改动就持久化了。"""
    from agentgenome.genome.scaffold import GITIGNORE

    assert f"{craft.MOUNT_SUBPATH}/" in GITIGNORE


ARCHITECT = """\
id: architect
runtime: claude-code
prompt: prompts/operator.md
procedures: [survey]
crafts: [codebase-survey]
tools:
  allow: [Read]
permissions:
  write_paths: ["**"]
"""

DEVELOPER = """\
id: developer
runtime: claude-code
prompt: prompts/operator.md
procedures: [code-develop]
crafts: [failure-diagnosis]
tools:
  allow: [Read]
permissions:
  write_paths: ["**"]
"""


def _common_craft(workspace: Path, *names: str) -> None:
    root = workspace / "genome" / "procedures" / craft.COMMON_DIR / craft.CRAFT_DIR
    for name in names:
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / craft.CRAFT_MANIFEST).write_text(f"# {name}\n", encoding="utf-8")


def _run_as(workspace: Path, employee: str, procedure_id: str, runtime: str = "replay"):
    return runner.invoke(
        app,
        [
            "procedure",
            "run",
            procedure_id,
            "--workspace",
            str(workspace),
            "--employee",
            employee,
            "--runtime",
            runtime,
            "--json",
        ],
    )


def test_each_role_only_gets_its_own_crafts(workspace: Path, tmp_path: Path) -> None:
    """开发员工不会看到架构员工的手艺清单——这是角色定制要保证的事。"""
    procedures = workspace / "genome" / "procedures"
    write_procedure(procedures, "survey", "agentic", prompt="勘察\n", script=None)
    write_procedure(procedures, "code-develop", "agentic", prompt="干活\n", script=None)
    _common_craft(workspace, "codebase-survey", "failure-diagnosis")
    (workspace / "employees" / "architect.yaml").write_text(ARCHITECT, encoding="utf-8")
    (workspace / "employees" / "developer.yaml").write_text(DEVELOPER, encoding="utf-8")
    for employee, procedure_id in (("architect", "survey"), ("developer", "code-develop")):
        directory = tmp_path / "lib" / f"{employee}__{procedure_id}__r1"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(json.dumps(RESULT), encoding="utf-8")

    arch = _run_as(workspace, "architect", "survey")
    assert arch.exit_code == 0, arch.output
    assert json.loads(arch.output)["crafts"] == ["codebase-survey"]

    dev = _run_as(workspace, "developer", "code-develop")
    assert dev.exit_code == 0, dev.output
    assert json.loads(dev.output)["crafts"] == ["failure-diagnosis"]
    # 换了员工之后,上一个角色的手艺必须已经从工作区消失。
    assert sorted(p.name for p in _mounted(workspace).iterdir()) == ["failure-diagnosis"]


def test_a_runtime_without_the_mechanism_gets_the_craft_inlined(
    workspace: Path, tmp_path: Path
) -> None:
    """降级是**内联摘要,不是不给**——手艺内容只写一份、运行时无关,可插拔承诺不能因为
    多了手艺层就破掉。

    这里用回放跑,但把工作区的手艺挂载能力按 qwen-code 那档处理:断言工作区里没有挂载
    目录,而手艺全文进了上下文包。
    """
    import agentgenome.agents.capabilities as caps

    _with_craft(
        workspace / "genome" / "procedures", "code-develop", {"failure-diagnosis": "# 诊断纪律\n"}
    )
    _recording(tmp_path / "lib", "code-develop")

    downgraded = caps.RuntimeCapabilities(
        name="replay", tools=dict(caps.CLAUDE_CODE.tools), craft_mounting=False
    )
    caps.register(downgraded)
    try:
        result = _run(workspace, "code-develop")
    finally:
        caps.register(caps.REPLAY)

    assert result.exit_code == 0, result.output
    # 没有挂载目录 —— 这个运行时读不到它。
    assert not _mounted(workspace).exists()
    # 但手艺全文进了上下文包,员工照样看得到。
    bundles = list((workspace / "tasks").rglob("context-attempt-0.md")) or list(
        tmp_path.rglob("context-attempt-0.md")
    )
    assert bundles, "找不到上下文包"
    assert "诊断纪律" in bundles[0].read_text(encoding="utf-8")
