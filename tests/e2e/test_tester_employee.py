"""测试员工:出题的人不是答题的人。

一个绕过了某条边界的实现,它给自己写的测试大概率绕过同一条边界——同一个头脑不会用测试
抓自己的盲区。所以这里验的是**机制**:出题真的先跑、用例真的落在同一棵工作树里被实现节点
读到、档位真的能关回今天的样子。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentgenome.core.events import LogKind
from agentgenome.core.states import TaskState
from agentgenome.core.topology import IMPLEMENT, TEST_FIRST, WRITE_TESTS
from agentgenome.employees import load_employees
from agentgenome.genome.roster import TESTER_EMPLOYEE
from agentgenome.jobs.orchestrator import Orchestrator
from tests.e2e.test_critique_loop import record_node  # noqa: PLC2701 —— 同一个回放缝
from tests.e2e.test_orchestrator import (  # noqa: PLC2701 —— 复用同一套夹具,不另造一份
    DEV_RESULT,
    ITEST_DECIDE_RESULT,
    PASSING_TEST,
    PLAN,
    _orchestrator,
    _record,
    _submit,
    library,
    workspace,
)
from tests.fixtures.git import commit_all

__all__ = ["library", "workspace"]

runner = CliRunner()

#: 出题人写下的用例:**红在断言不了的东西上**——实现还不存在,import 就失败。
#: 开发员工补上那个模块之后它变绿。两步共用一棵工作树,这条链才成立。
RED_TEST = "from order.reserve import reserve\n\n\ndef test_reserve():\n    assert reserve() == 1\n"

#: 让上面那条用例变绿的实现。**开发员工写的是它,不是用例本身**——专职档下用例碰不得。
GREEN_IMPL = "def reserve():\n    return 1\n"

#: 出题产物。`red` 是这一步唯一的质量信号:全绿的"新用例"是既有行为的复述。
TESTS_RESULT = {
    "task_id": "",
    "producer": TESTER_EMPLOYEE,
    "created_at": "2026-09-01T10:02:00Z",
    "passed": True,
    "test_files": ["repos/order-service/tests/test_reserve.py"],
    "acceptance_covered": [{"acceptance": "下单时调用预占接口", "tests": ["test_reserve"]}],
    "red": True,
}


def _quality_line(root: Path, **fields: object) -> None:
    config = root / "agentgenome.yaml"
    payload = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    payload["quality_line"] = {**payload.get("quality_line", {}), **fields}
    config.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    commit_all(root, "chore: 拧一下质量线")


def _protect(root: Path, path: str) -> None:
    target = root / "genome" / "rules" / "protected.yaml"
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    payload.setdefault("protected_paths", []).append({"path": path, "writable_by": []})
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    commit_all(root, "chore: 声明一条受保护路径")


def _arm_test_first(library: Path, task_id: str, *, dev_test: str = PASSING_TEST) -> None:
    """摆好一轮 test-first:决策出计划,测试员工出题(红),开发员工实现(绿)。"""
    _record(
        library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id}, {}
    )
    # **回放键带节点名。** test-first 是两个节点,不带的话两次派发撞在一个键上。
    record_node(
        library,
        TESTER_EMPLOYEE,
        "write-tests",
        "write-tests.1",
        TESTS_RESULT | {"task_id": task_id},
        {"repos/order-service/tests/test_reserve.py": RED_TEST},
    )
    record_node(
        library,
        "dev-employee",
        "code-develop",
        "implement.1",
        DEV_RESULT | {"task_id": task_id},
        # **实现,不是用例。** 专职档下开发员工碰用例会被判越权,而那正是分离的意思。
        {"repos/order-service/src/order/reserve.py": GREEN_IMPL},
    )
    # `single` 档下同一份开发产出住在不带节点名的键上。两份都摆:同一个 arm 要能同时
    # 服务"关掉"与"打开"两条链路,否则两条链路的差异里会混进夹具本身的差异。
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        {"repos/order-service/tests/test_reserve.py": dev_test},
    )
    _record(
        library,
        "decision-employee",
        "itest-decide",
        1,
        ITEST_DECIDE_RESULT | {"task_id": task_id},
        {},
    )


def _topology(orchestrator: Orchestrator, task_id: str, stage: str) -> dict:
    chosen = [
        event.payload
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.TOPOLOGY
        and event.payload.get("phase") == "chosen"
        and event.payload.get("stage") == stage
    ]
    assert chosen, f"{stage} 没有选图记录——「这次跑的是哪张图」就回答不了了"
    return dict(chosen[-1])


# --- 定义 -------------------------------------------------------------------


def test_the_workspace_gets_a_tester_and_its_procedure(workspace: Path) -> None:
    assert (workspace / "employees" / f"{TESTER_EMPLOYEE}.yaml").is_file()
    assert (workspace / "employees" / "prompts" / "tester.md").is_file()
    assert (workspace / "genome" / "procedures" / "write-tests" / "procedure.yaml").is_file()


def test_the_tester_may_write_tests_and_nothing_else(workspace: Path) -> None:
    """出题人碰不了实现——这是**角色级**事实,所以它写在定义里,不是算出来的。"""
    tester = load_employees(workspace / "employees").get(TESTER_EMPLOYEE)
    modules = ["repos/order-service/"]

    assert tester.may_write("repos/order-service/tests/test_x.py", "ag-1", modules) is True
    assert tester.may_write("repos/order-service/src/order/app.py", "ag-1", modules) is False


def test_the_tester_can_run_things(workspace: Path) -> None:
    """它要真的跑一遍确认自己出的题是红的。没有 Bash 的话那一条只能靠猜。"""
    tester = load_employees(workspace / "employees").get(TESTER_EMPLOYEE)

    assert "Bash" in tester.tools.allow


def test_the_write_tests_contract_demands_the_red_signal(workspace: Path) -> None:
    """全绿的「新用例」是既有行为的复述,而它会让下一步无事可做、门禁照样变绿。"""
    schema = json.loads(
        (workspace / "genome" / "procedures" / "write-tests" / "schemas" / "out.json").read_text(
            encoding="utf-8"
        )
    )

    assert "red" in schema["required"]
    assert "test_files" in schema["required"]


# --- 档位 -------------------------------------------------------------------


async def test_dev_mode_is_byte_for_byte_today(workspace: Path, library: Path) -> None:
    """缺省档下测试员工一次都不出场,选的图仍然是 single。"""
    task_id = _submit(workspace)
    _arm_test_first(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    develop = _topology(orchestrator, task_id, "develop")
    assert develop["template"]["id"] == "single"
    actors = {
        event.actor
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.JOB_STARTED
    }
    assert TESTER_EMPLOYEE not in actors


async def test_dedicated_mode_puts_the_tester_first(workspace: Path, library: Path) -> None:
    """红→绿分工:出题在前、实现在后,而且是**同一棵工作树**里的两步。"""
    _quality_line(workspace, tester="dedicated")
    task_id = _submit(workspace)
    _arm_test_first(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    task = await orchestrator.advance(task_id)

    develop = _topology(orchestrator, task_id, "develop")
    assert develop["template"]["id"] == TEST_FIRST
    assert [node["id"] for node in develop["template"]["nodes"]] == [WRITE_TESTS, IMPLEMENT]
    assert [node["employee"] for node in develop["template"]["nodes"]] == [
        TESTER_EMPLOYEE,
        "dev-employee",
    ]
    assert task.state is TaskState.UNIT_TESTING


async def test_the_implementer_reads_the_tests_the_tester_just_wrote(
    workspace: Path, library: Path
) -> None:
    """**这条是 test-first 成立与否的全部。**

    dag 的节点各有工作树、合并发生在全图跑完之后,于是实现节点根本看不到出题节点写的文件。
    顺序模板共用任务主工作树,所以这里断言的是文件层面的可见性,不是两条日志的先后。
    """
    _quality_line(workspace, tester="dedicated")
    task_id = _submit(workspace)
    _arm_test_first(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    workdir = orchestrator.workdir(orchestrator.store.get(task_id))
    written = workdir / "repos" / "order-service" / "tests" / "test_reserve.py"
    assert written.is_file(), "出题人写的用例不在实现人的工作树里"
    assert written.read_text(encoding="utf-8") == RED_TEST, "用例被实现人改过了"
    # 实现落在同一棵树上,而且它正是那条用例 import 的那个模块——红→绿在文件层面发生了。
    assert (workdir / "repos/order-service/src/order/reserve.py").is_file()


async def test_a_red_that_never_goes_green_fails_the_state(
    workspace: Path, library: Path
) -> None:
    """出题失败就不该派实现:它要读的题不存在,派出去只是把钱花在一次必然失败上。"""
    _quality_line(workspace, tester="dedicated")
    task_id = _submit(workspace)
    _record(
        library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id}, {}
    )
    record_node(
        library,
        TESTER_EMPLOYEE,
        "write-tests",
        "write-tests.1",
        TESTS_RESULT | {"task_id": task_id, "passed": False, "red": False},
        {},
    )
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.DEVELOPING, "出题没成,这一轮不该往门禁走"
    actors = [
        event.actor
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.JOB_STARTED
    ]
    assert "dev-employee" not in actors, "题都没出出来,不该派实现"


async def test_risk_based_only_fires_on_protected_paths(workspace: Path, library: Path) -> None:
    _quality_line(workspace, tester="risk-based")
    task_id = _submit(workspace)
    _arm_test_first(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    assert _topology(orchestrator, task_id, "develop")["template"]["id"] == "single"


async def test_risk_based_fires_when_the_plan_hits_a_protected_path(
    workspace: Path, library: Path
) -> None:
    _quality_line(workspace, tester="risk-based")
    _protect(workspace, "repos/order-service/**")
    task_id = _submit(workspace)
    _arm_test_first(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    chosen = _topology(orchestrator, task_id, "develop")
    assert chosen["template"]["id"] == TEST_FIRST
    assert chosen["why"] == "risk-based"


# --- 组合 -------------------------------------------------------------------


async def test_a_planned_graph_wins_and_says_so(workspace: Path, library: Path) -> None:
    """两者都要占开发态那一个位置。**降级必须可观测**——静默的话人会以为专职测试生效了。"""
    _quality_line(workspace, tester="dedicated")
    task_id = _submit(workspace)
    planned = PLAN | {
        "task_id": task_id,
        "nodes": [
            {"id": "a", "produces": ["api"], "write_scope": ["repos/order-service/src/a/**"]},
            {"id": "b", "needs": ["api"], "write_scope": ["repos/order-service/src/b/**"]},
        ],
    }
    _record(library, "decision-employee", "requirement-analysis", 1, planned, {})
    for node in ("a", "b"):
        record_node(
            library,
            "dev-employee",
            "code-develop",
            f"{node}.1",
            DEV_RESULT | {"task_id": task_id},
            {f"repos/order-service/src/{node}/impl.py": f"# {node}\n"},
        )
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    chosen = _topology(orchestrator, task_id, "develop")
    assert chosen["template"]["id"] == "dag"
    assert chosen["why"] == "plan-graph-wins", "降级没进事件面,等于静默降级"


@pytest.mark.parametrize("mode", ["dev", "dedicated", "risk-based"])
def test_every_mode_is_a_legal_configuration(workspace: Path, mode: str) -> None:
    from agentgenome.config import load_config

    _quality_line(workspace, tester=mode)

    assert load_config(workspace).quality_line.tester.value == mode


def test_a_typo_in_the_mode_is_refused(workspace: Path) -> None:
    """静默回退到缺省的话,「我明明开了专职测试」会变成一次漫长的排查。"""
    from agentgenome.config import load_config
    from agentgenome.genome.errors import GenomeValidationError

    _quality_line(workspace, tester="dedicate")

    with pytest.raises(GenomeValidationError):
        load_config(workspace)


# --- 写集分离 ---------------------------------------------------------------


async def test_the_implementer_may_not_write_tests_in_dedicated_mode(
    workspace: Path, library: Path
) -> None:
    """**分离是机制,不是约定。** 开发员工试写测试路径 → 判越权 → 回滚。"""
    _quality_line(workspace, tester="dedicated")
    task_id = _submit(workspace)
    _record(
        library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id}, {}
    )
    record_node(
        library,
        TESTER_EMPLOYEE,
        "write-tests",
        "write-tests.1",
        TESTS_RESULT | {"task_id": task_id},
        {"repos/order-service/tests/test_reserve.py": RED_TEST},
    )
    record_node(
        library,
        "dev-employee",
        "code-develop",
        "implement.1",
        DEV_RESULT | {"task_id": task_id},
        # 实现的同时顺手把题改了——这正是分离要拦的那件事。
        {"repos/order-service/tests/test_reserve.py": "def test_reserve():\n    assert True\n"},
    )
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.DEVELOPING, "越权那一轮不该往门禁走"
    slots = sorted((workspace / "tasks" / task_id / "artifacts").glob("*develop.implement"))
    assert slots, "实现节点的产物槽都没开出来"
    report = json.loads((slots[-1] / "scope-report.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    kinds = {item["kind"] for item in report["violations"]}
    assert kinds == {"forbidden"}, "写集分离的越权要被判成命中禁写,不是走错了地方"


async def test_dev_mode_leaves_the_developer_free_to_write_tests(
    workspace: Path, library: Path
) -> None:
    """缺省档下开发员工照旧自己写测试——这一条保证关掉策略就是今天的样子。"""
    task_id = _submit(workspace)
    _arm_test_first(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.UNIT_TESTING
    assert orchestrator.extra_forbid(orchestrator.store.get(task_id), "dev-employee") == ()


async def test_the_precheck_stacks_the_same_task_level_ban(
    workspace: Path, library: Path
) -> None:
    """派发时叠了、复查时不叠的话,越权能在 Job 那关被抓住却在最后一关被放行。"""
    _quality_line(workspace, tester="dedicated")
    task_id = _submit(workspace)
    _arm_test_first(library, task_id)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)

    task = orchestrator.store.get(task_id)
    by_id = {
        employee.id: policy
        for employee, policy in zip(
            orchestrator.participants(task), orchestrator.scope_policies(task), strict=True
        )
    }
    assert by_id["dev-employee"].allows("repos/order-service/tests/test_x.py") is False
    assert by_id[TESTER_EMPLOYEE].allows("repos/order-service/tests/test_x.py") is True
    assert by_id[TESTER_EMPLOYEE].allows("repos/order-service/src/order/app.py") is False


async def test_the_tester_may_not_write_implementation_either(
    workspace: Path, library: Path
) -> None:
    """**分离是双向的。** 单向的话,出题人会顺手把实现补上,分工当场作废。

    两侧被判的**类别不同**,而这是对的:开发员工写测试命中的是任务级禁令(命中禁写规则),
    出题人写实现是它压根没声明过那个范围(不在授权可写路径内)。处置不同——前者更像目标
    漂移,后者更像理解偏差——但两者都当场失败、都回滚。
    """
    _quality_line(workspace, tester="dedicated")
    task_id = _submit(workspace)
    _record(
        library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id}, {}
    )
    record_node(
        library,
        TESTER_EMPLOYEE,
        "write-tests",
        "write-tests.1",
        TESTS_RESULT | {"task_id": task_id},
        # 出题的同时顺手把实现写了。
        {"repos/order-service/src/order/reserve.py": GREEN_IMPL},
    )
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.DEVELOPING, "越权那一轮不该往门禁走"
    slots = sorted((workspace / "tasks" / task_id / "artifacts").glob("*develop.write-tests"))
    report = json.loads((slots[-1] / "scope-report.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["rolled_back_to"], "越权了却没回滚,实现就留在树上了"
    actors = [
        event.actor
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.JOB_STARTED
    ]
    assert "dev-employee" not in actors, "出题人越权那一轮,实现节点不该被派出去"


async def test_changing_what_counts_as_a_test_moves_both_sides_together(
    workspace: Path, library: Path
) -> None:
    """**"哪些路径算测试"只有一个答案:出题人自己声明的写集。**

    另存一份配置的话,改了一处没改另一处时,两侧不重合的那一段是一块谁都写不了的死区,
    而且没有任何症状提示它存在。
    """
    _quality_line(workspace, tester="dedicated")
    definition = workspace / "employees" / f"{TESTER_EMPLOYEE}.yaml"
    definition.write_text(
        definition.read_text(encoding="utf-8").replace(
            '"{task_modules}/**/tests/**"', '"{task_modules}/**/spec/**"'
        ),
        encoding="utf-8",
    )
    commit_all(workspace, "chore: 这个项目把测试放在 spec/ 下")
    task_id = _submit(workspace)
    orchestrator = _orchestrator(workspace, library)
    task = orchestrator.store.get(task_id)

    forbidden = orchestrator.extra_forbid(task, "dev-employee")

    assert "{task_modules}/**/spec/**" in forbidden, "出题人的新写集没有变成实现人的禁令"
    tester = load_employees(workspace / "employees").get(TESTER_EMPLOYEE)
    assert tester.may_write("repos/order-service/spec/test_x.py", "ag-1", ["repos/order-service/"])
