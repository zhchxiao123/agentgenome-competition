"""员工定义与权限塌缩:文件进,模型或可读错误出。"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from agentgenome.employees import (
    EmployeeNotFound,
    ProcedureNotAllowed,
    ensure_procedure_allowed,
    load_employees,
)
from agentgenome.genome.errors import GenomeValidationError

VALID = """\
id: dev-employee
runtime: claude-code
model: default
prompt: prompts/dev.md
procedures:
  - branch-worktree
  - code-develop
  - unit-gate
tools:
  allow: [Bash, Read, Write, Edit, Grep, Glob]
  deny: [WebFetch, WebSearch]
permissions:
  write_paths: ["repos/**", "tasks/{task_id}/**"]
  forbid_paths: ["genome/rules/**", ".github/**"]
limits:
  job_timeout_s: 1800
  max_tokens_per_job: 200000
"""


def _employees(tmp_path: Path, **definitions: str) -> Path:
    """摆一个 employees/ 目录。缺省写一份合法的 dev。"""
    root = tmp_path / "ws" / "employees"
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "dev.md").write_text("你是开发数字员工。\n", encoding="utf-8")
    for name, body in (definitions or {"dev-employee": VALID}).items():
        (root / f"{name}.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    return root


def _expect_failure(root: Path) -> GenomeValidationError:
    with pytest.raises(GenomeValidationError) as excinfo:
        load_employees(root)
    return excinfo.value


# --- 加载 -------------------------------------------------------------------


def test_loads_a_valid_employee(tmp_path: Path) -> None:
    employees = load_employees(_employees(tmp_path))

    dev = employees.get("dev-employee")
    assert dev.runtime == "claude-code"
    assert dev.procedures == ["branch-worktree", "code-develop", "unit-gate"]
    assert dev.tools.allow[:2] == ["Bash", "Read"]
    assert dev.tools.deny == ["WebFetch", "WebSearch"]
    assert dev.limits.job_timeout_s == 1800
    assert dev.limits.max_tokens_per_job == 200_000


def test_the_prompt_is_read_from_disk(tmp_path: Path) -> None:
    employees = load_employees(_employees(tmp_path))

    assert "开发数字员工" in employees.get("dev-employee").prompt_text


def test_optional_sections_have_usable_defaults(tmp_path: Path) -> None:
    minimal = """\
    id: tiny
    runtime: claude-code
    prompt: prompts/dev.md
    """
    employees = load_employees(_employees(tmp_path, tiny=minimal))

    tiny = employees.get("tiny")
    assert tiny.procedures == []
    assert tiny.tools.allow == []
    assert tiny.permissions.write_paths == []


def test_missing_directory_yields_an_empty_set(tmp_path: Path) -> None:
    """还没定义员工是正常状态,不是错误。"""
    assert load_employees(tmp_path / "nope").all() == ()


def test_employees_are_listed_in_a_stable_order(tmp_path: Path) -> None:
    employees = load_employees(
        _employees(
            tmp_path,
            zebra="id: zebra\nruntime: claude-code\nprompt: prompts/dev.md\n",
            alpha="id: alpha\nruntime: claude-code\nprompt: prompts/dev.md\n",
        )
    )

    assert [e.id for e in employees.all()] == ["alpha", "zebra"]


# --- 校验 -------------------------------------------------------------------


def test_id_must_match_the_file_name(tmp_path: Path) -> None:
    error = _expect_failure(
        _employees(tmp_path, **{"dev-employee": VALID.replace("id: dev-employee", "id: other")})
    )

    assert "other" in error.render()
    assert "dev-employee" in error.render()


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    error = _expect_failure(_employees(tmp_path, **{"dev-employee": VALID + "surprise: true\n"}))

    assert "surprise" in error.render()


def test_a_missing_prompt_file_is_rejected(tmp_path: Path) -> None:
    """提示词是员工的全部人设。指错文件的话它会带着空人格上岗。"""
    error = _expect_failure(
        _employees(tmp_path, **{"dev-employee": VALID.replace("prompts/dev.md", "prompts/gone.md")})
    )

    assert "prompts/gone.md" in error.render()


def test_all_problems_are_reported_at_once(tmp_path: Path) -> None:
    broken = VALID.replace("id: dev-employee", "id: other").replace(
        "prompts/dev.md", "prompts/gone.md"
    )
    error = _expect_failure(_employees(tmp_path, **{"dev-employee": broken}))

    assert len(error.issues) >= 2


def test_a_broken_definition_does_not_stop_the_others(tmp_path: Path) -> None:
    """一个手滑写坏的员工不该让整个编排器起不来。"""
    root = _employees(tmp_path)
    (root / "broken.yaml").write_text("id: mismatch\nruntime: claude-code\n")

    employees = load_employees(root, strict=False)

    assert [e.id for e in employees.all()] == ["dev-employee"]
    assert "broken" in employees.rejected


# --- 权限 -------------------------------------------------------------------


def test_task_id_placeholder_is_expanded(tmp_path: Path) -> None:
    """员工只能写业务码与**本任务**目录,不是所有任务目录。"""
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    resolved = dev.write_globs(task_id="ag-20260901-001")

    assert "tasks/ag-20260901-001/**" in resolved
    assert "tasks/{task_id}/**" not in resolved


def test_forbid_wins_over_write(tmp_path: Path) -> None:
    """反过来的话,一条宽泛的 write_paths 会悄悄把禁止规则吃掉。"""
    definition = VALID.replace(
        'write_paths: ["repos/**", "tasks/{task_id}/**"]', 'write_paths: ["**"]'
    )
    dev = load_employees(_employees(tmp_path, **{"dev-employee": definition})).get("dev-employee")

    assert dev.may_write("repos/order-service/app.py") is True
    assert dev.may_write("genome/rules/architecture.md") is False


def test_a_path_outside_write_paths_is_denied(tmp_path: Path) -> None:
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    assert dev.may_write("repos/order-service/src/app.py") is True
    assert dev.may_write("genome/knowledge/project-map.yaml") is False


def test_an_employee_without_write_paths_may_write_nothing(tmp_path: Path) -> None:
    """空白名单表示"什么都不能写",不是"什么都能写"——默认值必须是安全的那一侧。"""
    minimal = "id: tiny\nruntime: claude-code\nprompt: prompts/dev.md\n"
    tiny = load_employees(_employees(tmp_path, tiny=minimal)).get("tiny")

    assert tiny.may_write("anything.py") is False


def test_may_write_honours_the_expanded_task_id(tmp_path: Path) -> None:
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    assert dev.may_write("tasks/ag-1/notes.md", task_id="ag-1") is True
    assert dev.may_write("tasks/ag-2/notes.md", task_id="ag-1") is False


# --- Procedure 白名单 -----------------------------------------------------------


def test_a_procedure_on_the_whitelist_is_allowed(tmp_path: Path) -> None:
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    ensure_procedure_allowed(dev, "code-develop")


def test_a_procedure_off_the_whitelist_is_refused(tmp_path: Path) -> None:
    """这是**员工能力边界**,与 Procedure 自己的 trigger.states 是不同层次的检查。"""
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    with pytest.raises(ProcedureNotAllowed) as excinfo:
        ensure_procedure_allowed(dev, "knowledge-update")

    message = str(excinfo.value)
    assert "dev-employee" in message
    assert "knowledge-update" in message
    assert "code-develop" in message, "拒绝信息要说明白名单里有什么"


def test_an_empty_whitelist_allows_nothing(tmp_path: Path) -> None:
    """同上:默认值站在安全的那一侧。"""
    minimal = "id: tiny\nruntime: claude-code\nprompt: prompts/dev.md\n"
    tiny = load_employees(_employees(tmp_path, tiny=minimal)).get("tiny")

    with pytest.raises(ProcedureNotAllowed):
        ensure_procedure_allowed(tiny, "code-develop")


# --- 查询 -------------------------------------------------------------------


def test_getting_an_unknown_employee_lists_what_exists(tmp_path: Path) -> None:
    employees = load_employees(_employees(tmp_path))

    with pytest.raises(EmployeeNotFound) as excinfo:
        employees.get("ghost")

    assert "dev-employee" in str(excinfo.value)


def test_employee_definitions_are_immutable(tmp_path: Path) -> None:
    """跑到一半有人改了权限,事件流里记的责任归属就对不上实际发生的事。"""
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    with pytest.raises(Exception):  # noqa: B017 — pydantic 的冻结错误类型不该被测试固化
        dev.runtime = "qwen-code"  # type: ignore[misc]


def test_effective_limits_fall_back_to_the_root_config(tmp_path: Path) -> None:
    """员工没声明时用部署参数兜底,声明了就以员工为准。"""
    from agentgenome.config import Config

    minimal = "id: tiny\nruntime: claude-code\nprompt: prompts/dev.md\n"
    employees = load_employees(_employees(tmp_path, tiny=minimal, **{"dev-employee": VALID}))
    config = Config.model_validate(
        {"limits": {"job_timeout_s": 60}, "budgets": {"per_job_tokens": 999}}
    )

    assert employees.get("tiny").effective_timeout(config) == 60
    assert employees.get("tiny").effective_max_tokens(config) == 999
    assert employees.get("dev-employee").effective_timeout(config) == 1800
    assert employees.get("dev-employee").effective_max_tokens(config) == 200_000


def test_declared_credentials_are_carried(tmp_path: Path) -> None:
    """员工进程只拿到它这次需要的凭证——名单在定义里,注入在派发时。"""
    definition = VALID + "credentials:\n  - ANTHROPIC_API_KEY\n"
    dev = load_employees(_employees(tmp_path, **{"dev-employee": definition})).get("dev-employee")

    assert dev.credentials == ["ANTHROPIC_API_KEY"]


def test_as_dict_exposes_the_effective_configuration(tmp_path: Path) -> None:
    """ "这个员工的有效配置到底是什么"要能被一条命令回答。"""
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    payload: dict[str, Any] = dev.as_dict(task_id="ag-1")

    assert payload["id"] == "dev-employee"
    assert payload["runtime"] == "claude-code"
    assert "tasks/ag-1/**" in payload["permissions"]["write_paths"]


# --- 授权范围 ---------------------------------------------------------------
#
# glob 语义本身在 `test_scope_policy.py` 里逐条钉死,这里只验员工这一侧的接线。


def test_scope_expands_the_placeholder_and_stacks_protected_paths(tmp_path: Path) -> None:
    """派发前的"能不能写"与 Job 结束后的"实际碰了什么"必须是同一个对象。"""
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    policy = dev.scope(task_id="ag-1", protected=["docs/**"])

    assert "tasks/ag-1/**" in policy.write_paths
    assert "docs/**" in policy.forbid_paths
    assert "genome/rules/**" in policy.forbid_paths, "员工自己的禁令不该被叠加冲掉"


def test_scope_stacks_the_extra_forbid_paths_the_caller_hands_in(tmp_path: Path) -> None:
    """调用方叠的禁写与员工自己的禁写同权:追加,不替换。

    质量线的写集分离靠它——"这个任务里开发员工不许写测试"是任务级事实,不是角色级事实,
    没法写进员工定义。
    """
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    policy = dev.scope(task_id="ag-1", protected=["docs/**"], extra_forbid=["repos/**/tests/**"])

    assert "repos/**/tests/**" in policy.forbid_paths
    assert "genome/rules/**" in policy.forbid_paths, "员工自己的禁令不该被叠加冲掉"
    assert "docs/**" in policy.forbid_paths, "受保护路径也不该被叠加冲掉"
    assert policy.allows("repos/order-service/tests/test_x.py") is False


def test_extra_forbid_is_reported_as_a_forbidden_rule_not_as_out_of_range(tmp_path: Path) -> None:
    """两类越权的处置不同,分类不能混。

    命中禁写通常意味着提示注入或目标漂移;写到授权目录之外多半只是理解偏差。叠进来的这条
    要是被判成后者,写集分离的越权会被当成一次"走错了地方"轻轻放过。
    """
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    policy = dev.scope(task_id="ag-1", extra_forbid=["repos/**/tests/**"])

    assert policy.matched_forbid("repos/order-service/tests/test_x.py") == "repos/**/tests/**"


def test_extra_forbid_expands_placeholders_like_the_employees_own_rules(tmp_path: Path) -> None:
    """同一套展开,不是第二套。

    调用方给的是模板(`{task_modules}/**/tests/**`),按生效模块展开成每个模块一条——
    自己在调用方拼一遍的话,两处的拼法迟早分叉。
    """
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    policy = dev.scope(
        task_id="ag-1",
        module_paths=["repos/order-service/", "repos/inventory-service/"],
        extra_forbid=["{task_modules}/**/tests/**"],
    )

    assert "repos/order-service/**/tests/**" in policy.forbid_paths
    assert "repos/inventory-service/**/tests/**" in policy.forbid_paths


def test_an_empty_extra_forbid_leaves_the_scope_byte_identical(tmp_path: Path) -> None:
    """缺省不传时,授权范围与开这个口子之前逐字节相同。

    这条是这次改动的全部承诺:口子开好了,但没有任何现有路径的行为变了。
    """
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    before = dev.scope(task_id="ag-1", protected=["docs/**"], module_paths=["repos/order/"])
    after = dev.scope(
        task_id="ag-1", protected=["docs/**"], module_paths=["repos/order/"], extra_forbid=()
    )

    assert before == after


def test_extra_forbid_does_not_duplicate_a_rule_the_employee_already_has(tmp_path: Path) -> None:
    """重复的禁令只留一条。两条一模一样的规则不会更安全,只会让越权报告读起来像有两个问题。"""
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    policy = dev.scope(task_id="ag-1", extra_forbid=["genome/rules/**"])

    assert policy.forbid_paths.count("genome/rules/**") == 1


def test_a_deny_glob_on_a_directory_also_covers_the_directory_itself(tmp_path: Path) -> None:
    """把受保护目录整个删掉,同样是碰了它。"""
    dev = load_employees(_employees(tmp_path)).get("dev-employee")

    assert dev.may_write("genome/rules") is False


# --- 按任务收窄 ---------------------------------------------------------------
#
# `{task_modules}` 与 `{task_id}` 同一个机制、同一处展开,但它展开成 **N 条** glob——
# 每个模块一条。选占位符而不是在匹配层做隐式交集,是为了让"这个角色按任务收敛"这条性质
# 从员工定义里就读得出来:藏进代码的话,读 dev.yaml 的人会以为它能写所有业务仓。

TASK_SCOPED = """\
id: dev-employee
runtime: claude-code
prompt: prompts/dev.md
permissions:
  write_paths: ["{task_modules}/**", "tasks/{task_id}/**"]
"""


def _dev(tmp_path: Path) -> Any:
    root = _employees(tmp_path, **{"dev-employee": TASK_SCOPED})
    return load_employees(root).get("dev-employee")


def test_the_placeholder_expands_to_one_glob_per_module(tmp_path: Path) -> None:
    dev = _dev(tmp_path)

    globs = dev.write_globs("ag-1", ["repos/order-service", "repos/inventory-service"])

    assert "repos/order-service/**" in globs
    assert "repos/inventory-service/**" in globs


def test_both_placeholders_expand_together(tmp_path: Path) -> None:
    """两个占位符在同一份定义里共存,各展开各的。"""
    dev = _dev(tmp_path)

    globs = dev.write_globs("ag-1", ["repos/order-service"])

    assert "tasks/ag-1/**" in globs
    assert "tasks/{task_id}/**" not in globs


def test_no_modules_means_nothing_writable_not_everything(tmp_path: Path) -> None:
    """**这条是整片改动的安全支点。**

    展开成零条 → 可写集合为空 → 什么都不能写。反过来(空 = 不限制)看起来更方便,但它意味着
    "计划没写清楚"会静默地变成"权限全开"——而那正是最需要它别全开的场合。
    """
    dev = _dev(tmp_path)

    assert dev.write_globs("ag-1", []) == ["tasks/ag-1/**"]
    assert dev.may_write("repos/order-service/src/app.py", "ag-1", []) is False


def test_a_module_outside_the_plan_is_not_writable(tmp_path: Path) -> None:
    dev = _dev(tmp_path)

    assert dev.may_write("repos/order-service/src/app.py", "ag-1", ["repos/order-service"]) is True
    assert (
        dev.may_write("repos/inventory-service/src/x.py", "ag-1", ["repos/order-service"]) is False
    )


def test_a_definition_without_the_placeholder_is_untouched(tmp_path: Path) -> None:
    """零影响回归:不用这个占位符的员工,展开结果与改动前逐字一致。"""
    root = _employees(tmp_path, **{"dev-employee": VALID})
    dev = load_employees(root).get("dev-employee")

    assert dev.write_globs("ag-1", []) == ["repos/**", "tasks/ag-1/**"]
    assert dev.write_globs("ag-1", ["repos/order-service"]) == ["repos/**", "tasks/ag-1/**"]


def test_the_mount_point_trailing_slash_does_not_double_up(tmp_path: Path) -> None:
    """根索引里的挂载点带尾斜杠(`repos/order-service/`),而 glob 自己也带一个。

    不去掉的话拼出来是 `repos/order-service//**`,匹配不上任何东西——一条看起来写了的
    授权实际是空的。
    """
    dev = _dev(tmp_path)

    assert dev.write_globs("ag-1", ["repos/order-service/"]) == [
        "repos/order-service/**",
        "tasks/ag-1/**",
    ]


def test_without_a_task_the_placeholder_survives_verbatim(tmp_path: Path) -> None:
    """不针对某个任务问的时候,规则要原样呈现。

    展开成零条的话,`employee show` 不带 `--task` 会把"可写业务代码"这条规则整条吞掉——
    人看到的是一份**少了一条规则**的定义,比看到一个未展开的占位符误导得多。
    """
    dev = _dev(tmp_path)

    assert dev.write_globs() == ["{task_modules}/**", "tasks/{task_id}/**"]


# --- 归属排他 ---------------------------------------------------------------
#
# plan 类工序决定"这一个任务怎么打"。两个员工都能干同一道的话,事件面上"该质询谁"就没有
# 答案了——而复盘时那正是唯一想问的问题。

TWO_CLAIMANTS = """\
id: {id}
runtime: claude-code
prompt: prompts/dev.md
procedures: [requirement-analysis]
"""


def test_two_employees_claiming_the_same_plan_procedure_is_refused(tmp_path: Path) -> None:
    root = _employees(
        tmp_path,
        **{
            "arch-employee": TWO_CLAIMANTS.format(id="arch-employee"),
            "decision-employee": TWO_CLAIMANTS.format(id="decision-employee"),
        },
    )

    with pytest.raises(GenomeValidationError) as excinfo:
        load_employees(root, exclusive=["requirement-analysis"])

    rendered = excinfo.value.render()
    assert "requirement-analysis" in rendered
    assert "arch-employee" in rendered and "decision-employee" in rendered


def test_the_refusal_says_how_to_fix_it(tmp_path: Path) -> None:
    """一条不告诉人怎么修的报错就是一堵墙。存量工作区撞上它时,下一步必须是明确的。"""
    root = _employees(
        tmp_path,
        **{
            "arch-employee": TWO_CLAIMANTS.format(id="arch-employee"),
            "decision-employee": TWO_CLAIMANTS.format(id="decision-employee"),
        },
    )

    with pytest.raises(GenomeValidationError) as excinfo:
        load_employees(root, exclusive=["requirement-analysis"])

    assert "agctl roster migrate" in excinfo.value.render()


def test_a_shared_procedure_may_be_claimed_by_everyone(tmp_path: Path) -> None:
    """排他只管 plan 类。门禁这类"谁都可能干"的工序被多个角色声明是正常的。"""
    root = _employees(
        tmp_path,
        **{
            "arch-employee": TWO_CLAIMANTS.format(id="arch-employee"),
            "decision-employee": TWO_CLAIMANTS.format(id="decision-employee"),
        },
    )

    registry = load_employees(root, exclusive=["itest-decide"])

    assert len(registry) == 2


def test_one_claimant_is_exactly_what_exclusive_means(tmp_path: Path) -> None:
    root = _employees(
        tmp_path, **{"decision-employee": TWO_CLAIMANTS.format(id="decision-employee")}
    )

    registry = load_employees(root, exclusive=["requirement-analysis"])

    assert registry.conflicts == ()


def test_a_non_strict_load_reports_the_conflict_instead_of_raising(tmp_path: Path) -> None:
    """非严格加载是为了"一份坏定义不该让整个编排器起不来"。

    归属冲突同理:它不该让员工名单看不了,但它必须被说出来——静默的话,"这个决定归谁"
    会在半年后的一次归因争议里才被发现。
    """
    root = _employees(
        tmp_path,
        **{
            "arch-employee": TWO_CLAIMANTS.format(id="arch-employee"),
            "decision-employee": TWO_CLAIMANTS.format(id="decision-employee"),
        },
    )

    registry = load_employees(root, strict=False, exclusive=["requirement-analysis"])

    assert len(registry) == 2, "冲突不该让员工消失——它们是真实存在的定义"
    assert any("requirement-analysis" in item for item in registry.conflicts)
