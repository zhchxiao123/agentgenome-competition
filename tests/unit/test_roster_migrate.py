"""花名册迁移:补齐缺的员工,从旧主人手里摘掉已移交的工序。

**只改白名单那一个键。** 脚手架不覆盖已存在的文件——那是刻意的,使用者改过的员工定义不该
被一次重新初始化抹掉。于是存量工作区里新员工会被补进来、而旧主人的白名单不会被改,
排他校验会当场拒绝加载。这条迁移就是那个缺口的唯一补法,所以它自己必须极其克制:
碰了限额或提示词的话,人下次就不敢跑它了。
"""

from __future__ import annotations

from pathlib import Path

from agentgenome.genome.roster_migrate import drop_procedures, plan_migration, run_migration

FLOW = """\
# 架构数字员工。
id: arch-employee
name: 架构员工
prompt: prompts/arch.md

procedures: [requirement-analysis, itest-decide, experience-distill]

limits:
  job_timeout_s: 1800
"""

BLOCK = """\
id: arch-employee
procedures:
  - requirement-analysis   # 我加的注释
  - itest-decide
  - experience-distill
limits:
  job_timeout_s: 1800
"""


def test_a_flow_style_whitelist_loses_only_the_moved_procedures() -> None:
    changed = drop_procedures(FLOW, ("requirement-analysis", "itest-decide"))

    assert changed is not None
    assert "procedures: [experience-distill]" in changed
    assert "requirement-analysis" not in changed


def test_everything_else_survives_byte_for_byte() -> None:
    """限额、提示词指针、注释、名字——使用者调过的东西一个字都不能动。"""
    changed = drop_procedures(FLOW, ("requirement-analysis", "itest-decide"))

    assert changed is not None
    untouched = (
        "# 架构数字员工。",
        "name: 架构员工",
        "prompt: prompts/arch.md",
        "  job_timeout_s: 1800",
    )
    for line in untouched:
        assert line in changed


def test_a_block_style_whitelist_works_too() -> None:
    """两种写法都要认。只认一种的话,改过格式的工作区会静默地迁移不动。"""
    changed = drop_procedures(BLOCK, ("requirement-analysis", "itest-decide"))

    assert changed is not None
    assert "experience-distill" in changed
    assert "requirement-analysis" not in changed
    assert "itest-decide" not in changed
    assert "job_timeout_s: 1800" in changed


def test_an_already_migrated_definition_is_left_alone() -> None:
    """幂等:跑第二遍什么都不该发生。返回 None 表示"没有改动"。"""
    assert drop_procedures("id: x\nprocedures: [experience-distill]\n", ("itest-decide",)) is None


def test_a_definition_without_a_whitelist_is_left_alone() -> None:
    assert drop_procedures("id: x\nruntime: claude-code\n", ("itest-decide",)) is None


def test_dropping_everything_leaves_an_empty_list_not_a_broken_file() -> None:
    """白名单被清空是合法状态(什么都不能调用),不是可以省略的键。"""
    changed = drop_procedures("id: x\nprocedures: [itest-decide]\n", ("itest-decide",))

    assert changed == "id: x\nprocedures: []\n"


# --- 工作区级 ---------------------------------------------------------------


def _workspace(tmp_path: Path) -> Path:
    """一个"旧形状"的工作区:只有四个员工,plan 类工序还在架构员工名下。"""
    root = tmp_path / "ws"
    employees = root / "employees"
    (employees / "prompts").mkdir(parents=True)
    (employees / "prompts" / "arch.md").write_text("# 架构\n", encoding="utf-8")
    (employees / "arch-employee.yaml").write_text(
        "id: arch-employee\nruntime: claude-code\nprompt: prompts/arch.md\n"
        "procedures: [requirement-analysis, itest-decide, experience-distill]\n"
        "limits:\n  job_timeout_s: 999\n",
        encoding="utf-8",
    )
    return root


def test_the_plan_says_what_would_change_before_anything_happens(tmp_path: Path) -> None:
    """动手前要能看到 diff。看不到的话,人只能在"信它"和"不跑它"之间二选一。"""
    root = _workspace(tmp_path)

    plan = plan_migration(root)

    assert "decision-employee.yaml" in plan.added
    assert "arch-employee.yaml" in plan.rewritten
    assert plan.diff, "diff 是这条命令唯一的说服力"
    assert "-procedures: [requirement-analysis, itest-decide, experience-distill]" in plan.diff
    assert "+procedures: [experience-distill]" in plan.diff
    # 只是看一眼,盘上不该有任何变化。
    assert not (root / "employees" / "decision-employee.yaml").exists()


def test_running_it_converges_the_ownership(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    run_migration(root)

    arch = (root / "employees" / "arch-employee.yaml").read_text(encoding="utf-8")
    decision = (root / "employees" / "decision-employee.yaml").read_text(encoding="utf-8")
    assert "requirement-analysis" not in arch
    assert "experience-distill" in arch
    assert "requirement-analysis" in decision
    assert "job_timeout_s: 999" in arch, "使用者调过的限额被抹掉的话,这条命令就没人敢跑了"


def test_it_is_idempotent(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    run_migration(root)
    before = (root / "employees" / "arch-employee.yaml").read_text(encoding="utf-8")

    second = plan_migration(root)

    assert second.is_empty
    run_migration(root)
    assert (root / "employees" / "arch-employee.yaml").read_text(encoding="utf-8") == before


def test_a_migrated_workspace_loads_and_dispatches_to_the_decision_employee(
    tmp_path: Path,
) -> None:
    """迁移的验收不是"文件长得对",是"加载器不再拒收"。"""
    from agentgenome.employees import load_employees
    from agentgenome.genome.roster import PLAN_PROCEDURES

    root = _workspace(tmp_path)
    run_migration(root)

    registry = load_employees(root / "employees", exclusive=PLAN_PROCEDURES)

    assert registry.get("decision-employee").procedures == list(PLAN_PROCEDURES)
    assert "requirement-analysis" not in registry.get("arch-employee").procedures
