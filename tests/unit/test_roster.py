"""默认员工队伍:七份定义、七份提示词,以及 code-develop。

这些是**资产**而不是代码,所以测试盯的是两件事:它们能不能通过各自的校验器,
以及权限布局与提示词铁律有没有真的写进去——写漏一条的表现是员工上岗后无声地
多了点权限,或者少了条约束。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome import paths
from agentgenome.config import INITIAL_TOKEN_LIMIT
from agentgenome.core.scope import ScopePolicy
from agentgenome.employees import EmployeeConfig, load_employees
from agentgenome.genome import craft
from agentgenome.genome.procedures import ProcedureKind, load_procedure
from agentgenome.genome.roster import (
    COMMON_RESULT_REQUIRED,
    IRONCLAD,
    NETWORK_TOOLS,
    scaffold_roster,
)
from agentgenome.genome.rules import load_rules
from agentgenome.genome.scaffold import PROTECTED_TEMPLATE

EMPLOYEE_IDS = (
    "adversary-employee",
    "arch-employee",
    "decision-employee",
    "dev-employee",
    "itest-employee",
    "reviewer-employee",
    "tester-employee",
)


@pytest.fixture
def roster(tmp_path: Path) -> dict[str, EmployeeConfig]:
    scaffold_roster(tmp_path)
    return {employee.id: employee for employee in load_employees(tmp_path / "employees").all()}


# --- 七份定义 ---------------------------------------------------------------


def test_every_employee_loads_and_validates(roster: dict[str, EmployeeConfig]) -> None:
    assert sorted(roster) == sorted(EMPLOYEE_IDS)


def test_scaffolded_employees_and_procedures_do_not_reintroduce_small_limits(
    tmp_path: Path,
) -> None:
    """项目配置调大之后，员工或工序不能又在更低一层把额度压回去。"""
    scaffold_roster(tmp_path)

    for employee in load_employees(tmp_path / "employees").all():
        assert employee.limits.job_timeout_s is None
        assert employee.limits.max_tokens_per_job is None

    for directory in (tmp_path / paths.PROCEDURES).iterdir():
        if not directory.is_dir() or directory.name.startswith("_"):
            continue
        declared = load_procedure(directory).budget.max_tokens
        assert declared in (None, INITIAL_TOKEN_LIMIT), directory.name


def test_every_employee_has_a_prompt_with_content(roster: dict[str, EmployeeConfig]) -> None:
    """指错提示词文件的话,员工会带着空人格上岗。"""
    for employee in roster.values():
        assert len(employee.prompt_text) > 200


@pytest.mark.parametrize("employee_id", EMPLOYEE_IDS)
def test_every_employee_is_offline_by_default(
    roster: dict[str, EmployeeConfig], employee_id: str
) -> None:
    """`arch` 也断网——知识蒸馏不需要外网,而它的写权限是三个里最危险的。"""
    deny = roster[employee_id].tools.deny

    for tool in NETWORK_TOOLS:
        assert tool in deny, f"{employee_id} 没有禁掉 {tool}"


# --- 权限布局 ---------------------------------------------------------------


def test_the_permission_globs_follow_the_mount_root_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """架构与集测的业务代码范围都从 `paths.REPOS` 长出来,不是各写各的字符串。

    **开发员工不在这条里**,它按任务收窄(见下一条),已经不再引用挂载根。收缩这条断言而不是
    删掉它:它守的性质对另外两个角色仍然成立,而一条守着真性质的断言不该因为第三个角色改了
    形态就整条消失。

    **这是"挂载根是权限模型的支点"那句话的兑现。** 各写各的话,挪一次挂载根的表现是:
    常量改了、三份定义没改,而所有断言写的都是新值以外的东西——测试照样绿,权限却指向
    一个不存在的目录,于是开发员工一行业务代码都写不进去。
    """
    monkeypatch.setattr(paths, "REPOS", Path("vendor"))
    scaffold_roster(tmp_path)
    moved = {e.id: e for e in load_employees(tmp_path / "employees").all()}

    assert moved["arch-employee"].may_write("vendor/order-service/src/app.py") is False
    assert moved["itest-employee"].may_write("vendor/order-service/itest/case.py") is True
    assert moved["itest-employee"].may_write("vendor/order-service/src/app.py") is False


def test_the_architect_may_write_the_genome(roster: dict[str, EmployeeConfig]) -> None:
    assert roster["arch-employee"].may_write("genome/knowledge/project-map.yaml") is True


def test_the_architect_may_not_touch_business_code(roster: dict[str, EmployeeConfig]) -> None:
    """业务代码由开发员工改,架构员工只补认知。"""
    assert roster["arch-employee"].may_write("repos/order-service/src/app.py") is False


def test_the_developer_may_write_only_the_modules_this_task_touches(
    roster: dict[str, EmployeeConfig],
) -> None:
    """开发员工的可写范围不是"所有业务仓",是"本次计划命中的模块"。

    最小权限的落点:一个只该改订单域的任务,员工顺手改了库存域的话,那次改动此前会一路穿过
    越权检查、门禁、集测判定,直到人在评审里看出来——而评审面对的是一份已经混了两个域的 diff。
    """
    dev = roster["dev-employee"]

    assert dev.may_write("repos/order-service/src/app.py", "ag-1", ["repos/order-service/"]) is True
    assert (
        dev.may_write("repos/inventory-service/src/x.py", "ag-1", ["repos/order-service/"]) is False
    )


def test_the_developer_can_write_nothing_when_the_plan_names_no_module(
    roster: dict[str, EmployeeConfig],
) -> None:
    """空 = 什么都不能写,**不是**空 = 不限制。

    反过来的话,"计划没写清楚"会静默地变成"权限全开"——而那正是最需要它别全开的场合。
    """
    assert roster["dev-employee"].may_write("repos/order-service/src/app.py", "ag-1", []) is False


def test_the_developer_may_not_rewrite_the_rules(roster: dict[str, EmployeeConfig]) -> None:
    """否则它可以把架构规则改成对自己有利的样子,再自己给自己开绿灯。"""
    dev = roster["dev-employee"]

    assert dev.may_write("genome/rules/architecture.md") is False
    assert dev.may_write(".github/workflows/ci.yml") is False
    assert dev.may_write(".gitmodules") is False


def test_the_developer_may_only_write_its_own_task_directory(
    roster: dict[str, EmployeeConfig],
) -> None:
    dev = roster["dev-employee"]

    assert dev.may_write("tasks/ag-1/notes.md", task_id="ag-1") is True
    assert dev.may_write("tasks/ag-2/notes.md", task_id="ag-1") is False


def test_the_itest_employee_may_not_write_business_source(
    roster: dict[str, EmployeeConfig],
) -> None:
    """集成测试员工不改被测代码——不然它可以把测试改成必过。"""
    itest = roster["itest-employee"]

    assert itest.may_write("repos/order-service/src/app.py") is False
    assert itest.may_write("tasks/ag-1/itest/compose.yaml", task_id="ag-1") is True


def test_the_itest_employee_may_write_the_itest_dir_of_any_business_repo(
    roster: dict[str, EmployeeConfig],
) -> None:
    """集测的诊断本来就可能跨模块,所以它的边界是"哪一层",不是"哪个仓"。"""
    itest = roster["itest-employee"]

    assert itest.may_write("repos/order-service/itest/cases/reserve.py") is True
    assert itest.may_write("repos/inventory-service/itest/cases/stock.py") is True


def test_the_mount_root_itself_is_off_limits_to_the_architect(
    roster: dict[str, EmployeeConfig],
) -> None:
    """禁写一个目录必须连目录本身一起禁,否则"把它删掉"或"把它换成一个文件"是漏网的。"""
    assert roster["arch-employee"].may_write("repos") is False


def test_the_itest_prompt_does_not_claim_to_author_test_cases(
    roster: dict[str, EmployeeConfig],
) -> None:
    """`itest-run` 的用例执行由确定性脚本负责(`itest/procedure_entry.py`),Agent 只在
    失败时补诊断字段,从不写新文件。角色提示词曾说"写与跑集成用例",与工序指令(见
    `_ITEST_RUN_PROMPT`)矛盾——而角色提示词在上下文里优先级更高(见 `context.py` 的
    "固定四段,优先级从高到低"),真会诱导 Agent 去写测试文件、撞上 `write_paths` 越权。
    """
    prompt = roster["itest-employee"].prompt_text

    assert "写与跑集成用例" not in prompt
    assert "也不自己写新的测试用例" in prompt
    assert "也不新增测试文件" in prompt


# --- 提示词铁律 -------------------------------------------------------------


@pytest.mark.parametrize("employee_id", EMPLOYEE_IDS)
def test_every_prompt_carries_the_ironclad_clauses(
    roster: dict[str, EmployeeConfig], employee_id: str
) -> None:
    """四份提示词共用同一段铁律。分别手写的话,迟早有一份少一条。"""
    assert IRONCLAD.strip() in roster[employee_id].prompt_text


@pytest.mark.parametrize(
    "clause",
    [
        "只改授权路径",
        "先读失败报告",
        "questions",
        "result.json",
        "密钥",
        "产物文件",
    ],
)
def test_the_ironclad_section_covers_every_required_clause(clause: str) -> None:
    assert clause in IRONCLAD


def test_the_prompts_forbid_an_internal_retry_loop() -> None:
    """修复循环由状态机驱动。员工内部反复试会让上下文随轮次膨胀、目标漂移。"""
    assert "反复尝试" in IRONCLAD
    assert "一轮只做一遍" in IRONCLAD


# --- code-develop -----------------------------------------------------------


def test_code_develop_passes_procedure_validation(tmp_path: Path) -> None:
    scaffold_roster(tmp_path)

    spec = load_procedure(tmp_path / "genome" / "procedures" / "code-develop")

    assert spec.kind is ProcedureKind.AGENTIC
    assert spec.prompt


def test_code_develop_declares_the_four_result_fields(tmp_path: Path) -> None:
    """`questions[]` 现在还没有消费方,但不产出的话,等有消费方时全部历史任务都没有它。"""
    scaffold_roster(tmp_path)

    schema = load_procedure(tmp_path / "genome" / "procedures" / "code-develop").output_schema

    for name in ("changed_files", "self_test", "impact", "questions"):
        assert name in schema["properties"], f"产物 schema 缺 {name}"
    assert set(schema["required"]) >= {"changed_files", "self_test", "impact", "questions"}


def test_code_develop_is_on_the_developer_whitelist(roster: dict[str, EmployeeConfig]) -> None:
    assert "code-develop" in roster["dev-employee"].procedures


def test_the_developer_workflow_is_pinned_in_the_prompt(tmp_path: Path) -> None:
    """员工内部的工作流固化在提示词里,不靠调用方每次讲一遍。"""
    scaffold_roster(tmp_path)

    prompt = load_procedure(tmp_path / "genome" / "procedures" / "code-develop").prompt

    for step in ("先诊断", "小步", "自跑", "result.json"):
        assert step in prompt


# --- 规则文件的角色边界 -----------------------------------------------------
#
# 走的是脚手架真正写出来的 protected.yaml,不是测试自己捏的:这条边界的两半
# (员工的 write_paths、规则的 writable_by)分别住在两个文件里,只有合起来看
# 才知道结果。


def _effective(root: Path, employee: EmployeeConfig) -> ScopePolicy:
    """员工规则叠上项目受保护路径之后的有效范围。"""
    protected = load_rules(root).protected.paths_for(employee.id)
    return employee.scope(task_id="ag-1", protected=protected)


@pytest.fixture
def scaffolded(tmp_path: Path) -> Path:
    """一个带脚手架规则的 Workspace。"""
    (tmp_path / "genome" / "rules").mkdir(parents=True)
    (tmp_path / paths.PROTECTED_RULES).write_text(PROTECTED_TEMPLATE, encoding="utf-8")
    scaffold_roster(tmp_path)
    return tmp_path


def test_the_architect_may_edit_the_rules(scaffolded: Path) -> None:
    """规则文件只有架构员工能动。规则蒸馏(PRD 13)靠的就是这条口子。"""
    arch = load_employees(scaffolded / "employees").get("arch-employee")

    assert _effective(scaffolded, arch).allows("genome/rules/architecture.md") is True


def test_nobody_else_may_edit_the_rules(scaffolded: Path) -> None:
    """能改规则就等于能自己给自己开绿灯。"""
    employees = load_employees(scaffolded / "employees")

    for employee_id in ("dev-employee", "itest-employee"):
        policy = _effective(scaffolded, employees.get(employee_id))
        assert policy.allows("genome/rules/architecture.md") is False, employee_id


def test_the_exemption_is_named_not_implied(scaffolded: Path) -> None:
    """豁免只对点名的那条路径生效——架构员工照样碰不了 CI 与子模块指针。"""
    arch = load_employees(scaffolded / "employees").get("arch-employee")

    policy = _effective(scaffolded, arch)
    assert policy.allows(".github/workflows/ci.yml") is False
    assert policy.allows(".gitmodules") is False


# --- 公共产物约束 -----------------------------------------------------------


@pytest.mark.parametrize("procedure_id", ["requirement-analysis", "code-develop", "unit-gate"])
def test_every_procedure_result_carries_the_common_fields(
    tmp_path: Path, procedure_id: str
) -> None:
    """一份定义,三份 schema 都引用它。各写一遍的话,加一个公共字段会漏掉其中一处。"""
    scaffold_roster(tmp_path)

    schema = load_procedure(tmp_path / "genome" / "procedures" / procedure_id).output_schema

    for name in COMMON_RESULT_REQUIRED:
        assert name in schema["properties"], f"{procedure_id} 缺公共字段 {name}"
        assert name in schema["required"]
    assert "failures" in schema["properties"], f"{procedure_id} 没法表达失败细节"


# --- 随附的通用手艺 ----------------------------------------------------------
#
# 仓库自己的 `genome/procedures/_common/craft/` 有一组守卫盯着(见 tests/drift)。
# **脚手架写出来的那几份是 Python 字符串,那组守卫扫不到它们**——一份没人检查的手艺会慢慢
# 退化成一段散文,而它每次都会被物化进新工作区。


def craft_bodies(tmp_path: Path) -> dict[str, str]:
    scaffold_roster(tmp_path)
    root = tmp_path / paths.PROCEDURES / "_common" / "craft"
    return {
        item.parent.name: item.read_text(encoding="utf-8") for item in root.glob("*/SKILL.md")
    }


def test_the_scaffolded_crafts_are_the_ones_the_roster_declares(tmp_path: Path) -> None:
    """声明了手艺就必须把它写出来,否则新工作区的员工校验直接失败。"""
    bodies = craft_bodies(tmp_path)
    declared = {
        name
        for employee in load_employees(tmp_path / "employees").all()
        for name in employee.crafts
    }

    assert declared and declared <= set(bodies)


def test_every_scaffolded_craft_fits_the_line_budget(tmp_path: Path) -> None:
    for name, body in craft_bodies(tmp_path).items():
        assert body.count("\n") <= craft.LINE_BUDGET, f"{name} 超出 {craft.LINE_BUDGET} 行"


def test_every_scaffolded_craft_carries_counter_examples(tmp_path: Path) -> None:
    """只说"该怎么做"的手艺没有判别力——员工分不清自己现在做的算不算数。"""
    for name, body in craft_bodies(tmp_path).items():
        assert "反例" in body and "❌" in body, f"{name} 没有具体反例"


def test_every_scaffolded_craft_has_a_self_check(tmp_path: Path) -> None:
    for name, body in craft_bodies(tmp_path).items():
        assert "自检" in body and "- [ ]" in body, f"{name} 没有自检清单"


def test_no_scaffolded_craft_restates_the_contract(tmp_path: Path) -> None:
    """输入输出定义在 procedure.yaml 里。两处各写一遍必然发散。"""
    for name, body in craft_bodies(tmp_path).items():
        assert "inputs:" not in body and "outputs:" not in body, f"{name} 复述了契约"
