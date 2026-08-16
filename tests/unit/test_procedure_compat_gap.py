"""切到容器运行时之前,哪些工序还没声明兼容——以及一键补上。

**补声明是显式动作,不自动发生。** 兼容闸的语义是"只在一个运行时上验证过的工序不该
悄悄换台跑";界面替人自动补等于把闸门变成摆设。这一层负责把差距列清楚,判断归人。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from agentgenome.employees import EmployeeNotFound
from agentgenome.genome.dispatch import runtime_compatible
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.procedures import load_workspace_registry
from agentgenome.server.employees_edit import compat_gap, declare_compat
from agentgenome.server.rbac import Principal, Role
from tests.fixtures.git import commit_all, git

ARCH = """\
id: arch-employee
runtime: claude-code
prompt: prompts/arch.md
procedures: [requirement-analysis, itest-decide]
"""


def _procedure(
    root: Path, procedure_id: str, runtimes: list[str] | None, kind: str = "agentic"
) -> None:
    directory = root / "genome" / "procedures" / procedure_id
    directory.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = {
        "id": procedure_id,
        "version": "1.0.0",
        "kind": kind,
        "trigger": {"states": []},
        "outputs": {"schema_ref": "schemas/out.json"},
    }
    if kind == "deterministic":
        (directory / "scripts").mkdir(exist_ok=True)
        (directory / "scripts" / "run.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    else:
        body["prompt"] = "prompt.md"
        (directory / "prompt.md").write_text("干活。\n", encoding="utf-8")
    if runtimes is not None:
        body["compat"] = {"runtimes": runtimes}
    (directory / "procedure.yaml").write_text(
        yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (directory / "schemas").mkdir(exist_ok=True)
    (directory / "schemas" / "out.json").write_text('{"type": "object"}', encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    root = tmp_path / "ws"
    prompts = root / "employees" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "arch.md").write_text("你负责项目认知。\n", encoding="utf-8")
    (root / "employees" / "arch-employee.yaml").write_text(textwrap.dedent(ARCH), encoding="utf-8")
    (root / "agentgenome.yaml").write_text("platform: {git_host: local}\n", encoding="utf-8")
    _procedure(root, "requirement-analysis", ["claude-code"])
    _procedure(root, "itest-decide", ["claude-code", "agentteams"])
    git(root, "init", "-q")
    commit_all(root, "chore: 初始")
    return root


def _employee_procedures(root: Path, procedure_ids: list[str]) -> None:
    """改这个员工声明的工序列表。缺口是按员工算的,所以测新工序要先让他认领。"""
    (root / "employees" / "arch-employee.yaml").write_text(
        textwrap.dedent(ARCH).replace(
            "procedures: [requirement-analysis, itest-decide]",
            f"procedures: [{', '.join(procedure_ids)}]",
        ),
        encoding="utf-8",
    )


def _principal() -> Principal:
    return Principal(subject="alice", roles=frozenset({Role.ADMIN}))


def _runtimes_of(root: Path, procedure_id: str) -> list[str]:
    payload = yaml.safe_load(
        (root / "genome" / "procedures" / procedure_id / "procedure.yaml").read_text("utf-8")
    )
    return list(payload.get("compat", {}).get("runtimes", []))


# --- 差距 -------------------------------------------------------------------


def test_the_gap_lists_only_the_procedures_that_are_missing_the_runtime(
    workspace: Path,
) -> None:
    gap = compat_gap(workspace, "arch-employee", "agentteams")

    assert gap == ["requirement-analysis"], "已经声明过的那道不该出现在差距里"


def test_a_runtime_everything_already_declares_yields_an_empty_gap(workspace: Path) -> None:
    assert compat_gap(workspace, "arch-employee", "claude-code") == []


def test_a_deterministic_procedure_declaring_none_is_not_a_gap(workspace: Path) -> None:
    """**`none` 说的是"我不碰任何运行时"**,不是"我只兼容一个叫 none 的运行时"。

    派发闸对它明确放行(`dispatch._check_runtime_compat`)。把它列成缺口的代价不只是
    噪声:界面上那个一键补声明的按钮会把 `agentteams` 写进一份**根本不跑在运行时上**的
    版本化资产里,而那份声明从此是假的。
    """
    _procedure(workspace, "unit-gate", ["none"], kind="deterministic")
    _employee_procedures(workspace, ["requirement-analysis", "itest-decide", "unit-gate"])

    assert "unit-gate" not in compat_gap(workspace, "arch-employee", "agentteams")


def test_a_gap_asks_the_same_question_the_dispatch_gate_asks(workspace: Path) -> None:
    """**判据只有一份。** 界面提示与派发闸各判一次的话,两者迟早给出两个答案——而人
    看到的是"补完了还是被挡下"或者"没提示却挡下了",两种都无从解释。
    """
    _procedure(workspace, "unit-gate", ["none"], kind="deterministic")
    _employee_procedures(workspace, ["requirement-analysis", "itest-decide", "unit-gate"])
    registry = load_workspace_registry(workspace)

    gap = compat_gap(workspace, "arch-employee", "agentteams")

    for procedure_id in ("requirement-analysis", "itest-decide", "unit-gate"):
        refused = not runtime_compatible(registry.get(procedure_id), "agentteams")
        assert (procedure_id in gap) is refused, f"{procedure_id} 上两处判断分叉了"


def test_the_human_runtime_never_produces_a_gap(workspace: Path) -> None:
    """人对兼容性透明——人正是这套系统里唯一什么都能干的执行者。"""
    assert compat_gap(workspace, "arch-employee", "human") == []


def test_a_procedure_without_any_compat_declaration_is_not_a_gap(workspace: Path) -> None:
    """空的兼容声明表示"不限制"——那不是缺口,补它反而是收窄。"""
    _procedure(workspace, "open-procedure", None)
    (workspace / "employees" / "arch-employee.yaml").write_text(
        textwrap.dedent(ARCH).replace(
            "procedures: [requirement-analysis, itest-decide]",
            "procedures: [requirement-analysis, itest-decide, open-procedure]",
        ),
        encoding="utf-8",
    )

    assert "open-procedure" not in compat_gap(workspace, "arch-employee", "agentteams")


# --- 一键补声明 -------------------------------------------------------------


def test_declaring_adds_the_runtime_and_keeps_the_existing_ones(workspace: Path) -> None:
    declare_compat(workspace, _principal(), ["requirement-analysis"], "agentteams")

    assert _runtimes_of(workspace, "requirement-analysis") == ["claude-code", "agentteams"]


def test_declaring_is_idempotent(workspace: Path) -> None:
    declare_compat(workspace, _principal(), ["itest-decide"], "agentteams")

    assert _runtimes_of(workspace, "itest-decide") == ["claude-code", "agentteams"]


def test_nothing_happens_without_an_explicit_call(workspace: Path) -> None:
    """**否定断言**:光是问差距不该改任何东西——自动补声明等于把兼容闸变成摆设。"""
    before = git(workspace, "rev-parse", "HEAD")

    compat_gap(workspace, "arch-employee", "agentteams")

    assert git(workspace, "rev-parse", "HEAD") == before
    assert _runtimes_of(workspace, "requirement-analysis") == ["claude-code"]


def test_declaring_lands_a_commit(workspace: Path) -> None:
    before = git(workspace, "rev-parse", "HEAD")

    declare_compat(workspace, _principal(), ["requirement-analysis"], "agentteams")

    assert git(workspace, "rev-parse", "HEAD") != before


def test_an_unknown_procedure_stops_the_whole_batch(workspace: Path) -> None:
    """**否定断言**:一批里有一个不存在,前面那些也不该留在盘上。

    "三道补了两道"这种半截状态没有任何界面能表达——人看到的是"保存失败",而盘上已经变了。
    """
    with pytest.raises(EmployeeNotFound):
        declare_compat(
            workspace, _principal(), ["requirement-analysis", "no-such-procedure"], "agentteams"
        )

    assert _runtimes_of(workspace, "requirement-analysis") == ["claude-code"]


def test_a_declaration_that_leaves_the_procedure_unloadable_is_rolled_back(
    workspace: Path,
) -> None:
    """**否定断言**:补完声明它得还能加载,不能加载就把盘上改回去。

    这一条守的是注册表**不抛**这个事实:一道写坏的工序进 `rejected`,加载照样"成功"。
    只看有没有异常的话,这次保存会放行一道从此静默不可用的工序,而没有任何报错指向它。
    """
    target = workspace / "genome" / "procedures" / "requirement-analysis" / "procedure.yaml"
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    del payload["version"]  # 少一个必填字段:注册表会拒它,但只记进 rejected
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    before = target.read_text(encoding="utf-8")

    with pytest.raises(GenomeValidationError):
        declare_compat(workspace, _principal(), ["requirement-analysis"], "agentteams")

    assert target.read_text(encoding="utf-8") == before, "校验没过,但盘上那份被改了"
