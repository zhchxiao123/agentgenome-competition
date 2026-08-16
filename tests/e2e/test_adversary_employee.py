"""对抗 QA:门禁拦得住错的,拦不住脆的。

这里验的是那条闭环真的合上了:靶子的测试全绿 → 红队依卡片上的不变量抓到它 → 复现命令
**真的能独立跑通** → 任务带着发现回到开发态。少任何一环,"高风险变更被攻击过"就只是
一句流程宣称。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agentgenome.agents.contract import check_result_contract
from agentgenome.core.events import LogKind
from agentgenome.core.states import TaskState
from agentgenome.core.topology import PROBE, PROBE_AFTER_GATE, UNIT_GATE
from agentgenome.employees import load_employees
from agentgenome.genome.evolution.promotion import load_promotions
from agentgenome.genome.roster import ADVERSARY_EMPLOYEE
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
from tests.fixtures.mall import install_invariant_cards

__all__ = ["library", "workspace"]

#: 红队的战果。**每条 finding 必须带 repro_cmd**,否则产物契约当场拒收。
FINDINGS = {
    "task_id": "",
    "producer": ADVERSARY_EMPLOYEE,
    "created_at": "2026-09-01T10:07:00Z",
    "passed": False,
    "findings": [
        {
            "title": "批量预占不是原子的:长度不齐时越界读,而库存已经扣了一半",
            "invariant": "批量预占是原子的:任何一项失败,整批都不生效",
            "repro_cmd": "python tasks/ag-20260813-001/attack_batch.py",
            "evidence": "IndexError,且 stock['sku-1'] 从 10 变成 7",
            "severity": "major",
        }
    ],
    "attempted": ["边界值", "属性", "异常注入", "重复与并发"],
}

CLEAN = {**FINDINGS, "passed": True, "findings": []}

#: 打靶脚本。**红队自己写下的复现命令要真的能跑**——这条测试会亲自执行它。
ATTACK_SCRIPT = """\
import sys

sys.path.insert(0, "repos/inventory-service/src")
from inventory.app import InventoryService

service = InventoryService(stock={"sku-1": 10, "sku-2": 5})
try:
    service.reserve_batch(["sku-1", "sku-2"], [3], order_id="ord-1")
except IndexError:
    if service.stock["sku-1"] != 10:
        print("批量预占不是原子的:", service.stock)
        raise SystemExit(1)
raise SystemExit("没打穿")
"""


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


def _arm(library: Path, task_id: str, probe: dict, files: dict[str, str] | None = None) -> None:
    _record(
        library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id}, {}
    )
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT
        | {
            "task_id": task_id,
            "changed_files": ["repos/inventory-service/src/inventory/app.py"],
        },
        {"repos/order-service/tests/test_reserve.py": PASSING_TEST},
    )
    record_node(
        library,
        "dev-employee",
        "unit-gate",
        f"{UNIT_GATE}.1",
        {},  # 门禁是确定性脚本,回放不消费这份 result
    )
    record_node(
        library,
        ADVERSARY_EMPLOYEE,
        "adversarial-probe",
        f"{PROBE}.1",
        probe | {"task_id": task_id},
        files or {},
    )
    _record(
        library,
        "decision-employee",
        "itest-decide",
        1,
        ITEST_DECIDE_RESULT | {"task_id": task_id},
        {},
    )


def _record_round(
    library: Path, employee: str, procedure: str, subject: str, round_: int, result: dict
) -> None:
    """带轮次的那一份录制。修复循环里同一个节点会在第 N 轮被再叫一次。"""
    directory = library / f"{employee}__{procedure}__{subject}__r{round_}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def _topology(orchestrator: Orchestrator, task_id: str, stage: str) -> dict:
    chosen = [
        event.payload
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.TOPOLOGY
        and event.payload.get("phase") == "chosen"
        and event.payload.get("stage") == stage
    ]
    assert chosen, f"{stage} 没有选图记录"
    return dict(chosen[-1])


# --- 定义 -------------------------------------------------------------------


def test_the_workspace_gets_an_adversary(workspace: Path) -> None:
    assert (workspace / "employees" / f"{ADVERSARY_EMPLOYEE}.yaml").is_file()
    assert (workspace / "employees" / "prompts" / "adversary.md").is_file()
    assert (workspace / "genome" / "procedures" / "adversarial-probe" / "procedure.yaml").is_file()


def test_the_adversary_can_run_things_but_cannot_touch_the_code(workspace: Path) -> None:
    """没有 Bash 就产不出可验证的复现命令;能写业务仓的话,一次"攻击"会顺手改了被测代码。"""
    adversary = load_employees(workspace / "employees").get(ADVERSARY_EMPLOYEE)

    assert "Bash" in adversary.tools.allow
    assert adversary.may_write("tasks/ag-1/notes.md", task_id="ag-1") is True
    assert adversary.may_write("repos/order-service/src/order/app.py") is False
    assert adversary.may_write("genome/knowledge/project-map.yaml") is False


def _contract(workspace: Path, tmp_path: Path, payload: dict):
    """把一份产物摆进目录再过契约——**走的是真的那条校验路**,不是自己拿 schema 比一遍。"""
    schema = json.loads(
        (
            workspace / "genome" / "procedures" / "adversarial-probe" / "schemas" / "out.json"
        ).read_text(encoding="utf-8")
    )
    output = tmp_path / "out"
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return check_result_contract(output, schema)


def test_a_finding_without_a_repro_command_is_refused(workspace: Path, tmp_path: Path) -> None:
    """**红队不喊空炮。** 不能复现的发现没法被修、没法被转正,也没法被证伪。"""
    naked = {**FINDINGS, "task_id": "ag-1", "findings": [{"title": "感觉这里并发有风险"}]}

    check = _contract(workspace, tmp_path, naked)

    assert check.ok is False
    assert "repro_cmd" in (check.detail or "")


def test_a_finding_with_an_empty_repro_command_is_refused_too(
    workspace: Path, tmp_path: Path
) -> None:
    """空字符串不是命令。允许它的话,契约会退化成一个所有人都填空串的字段。"""
    naked = {**FINDINGS, "task_id": "ag-1", "findings": [{"title": "x", "repro_cmd": ""}]}

    check = _contract(workspace, tmp_path, naked)

    assert check.ok is False


def test_a_finding_with_a_real_repro_command_passes(workspace: Path, tmp_path: Path) -> None:
    """拒收要有对照:不然一个把什么都拒掉的 schema 也能让上面两条变绿。"""
    check = _contract(workspace, tmp_path, FINDINGS | {"task_id": "ag-1"})

    assert check.ok is True, check.detail


# --- 编入与否决 -------------------------------------------------------------


async def test_off_is_byte_for_byte_today(workspace: Path, library: Path) -> None:
    task_id = _submit(workspace)
    _arm(library, task_id, CLEAN)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    assert _topology(orchestrator, task_id, "unit-gate")["template"]["id"] == "single"
    actors = {
        event.actor
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.JOB_STARTED
    }
    assert ADVERSARY_EMPLOYEE not in actors


async def test_always_puts_the_probe_after_the_gate(workspace: Path, library: Path) -> None:
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    _arm(library, task_id, CLEAN)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    chosen = _topology(orchestrator, task_id, "unit-gate")
    assert chosen["template"]["id"] == PROBE_AFTER_GATE
    assert [node["id"] for node in chosen["template"]["nodes"]] == [UNIT_GATE, PROBE]
    assert [node["kind"] for node in chosen["template"]["nodes"]] == ["work", "work"], (
        "对抗节点标成 checker 的话,它抓到真 bug 状态照样抛门禁通过"
    )


async def test_a_clean_probe_lets_the_task_move_on(workspace: Path, library: Path) -> None:
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    _arm(library, task_id, CLEAN)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.READY_TO_COMMIT


async def test_a_hit_sends_the_task_back_to_developing(workspace: Path, library: Path) -> None:
    """**这一条是整条线成立与否的全部。** 抓到了就得回开发态,而不是照样往下走。"""
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    _arm(library, task_id, FINDINGS)
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.DEVELOPING
    assert task.fix_rounds == 1, "对抗发现导致的返工计入修复轮次——这是一个被看见的取舍"


async def test_the_finding_comes_back_with_the_task(workspace: Path, library: Path) -> None:
    """三件套照常回传:下一轮的开发员工要看得到红队发现了什么。"""
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    _arm(library, task_id, FINDINGS)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    reports = list((workspace / "tasks" / task_id).glob("failures/*.json"))
    assert reports, "失败报告没落盘,下一轮看不到红队的发现"


async def test_the_probe_is_handed_the_invariants_it_should_attack(
    workspace: Path, library: Path
) -> None:
    """**攻击清单来自卡片,不来自灵感。** 不给清单的话,红队的产出无法复核也无法沉淀。"""
    install_invariant_cards(workspace)
    commit_all(workspace, "chore: 补上不变量卡片")
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    _arm(library, task_id, CLEAN)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    handed = orchestrator.probe_inputs(orchestrator.store.get(task_id))
    assert any("原子" in item for item in handed["invariants"]), handed["invariants"]
    assert handed["changed_files"] == ["repos/inventory-service/src/inventory/app.py"]


async def test_protected_hit_only_fires_on_protected_paths(
    workspace: Path, library: Path
) -> None:
    _quality_line(workspace, adversary="protected-hit")
    task_id = _submit(workspace)
    _arm(library, task_id, CLEAN)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    assert _topology(orchestrator, task_id, "unit-gate")["template"]["id"] == "single"


async def test_protected_hit_fires_when_the_plan_touches_one(
    workspace: Path, library: Path
) -> None:
    _quality_line(workspace, adversary="protected-hit")
    _protect(workspace, "repos/order-service/**")
    task_id = _submit(workspace)
    _arm(library, task_id, CLEAN)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    chosen = _topology(orchestrator, task_id, "unit-gate")
    assert chosen["template"]["id"] == PROBE_AFTER_GATE
    assert chosen["why"] == "protected-hit"


async def test_a_red_gate_never_reaches_the_probe(workspace: Path, library: Path) -> None:
    """门禁没过就不该攻击:红队要攻的是一份"测试全绿"的实现,而任务该回开发态修。"""
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    _record(
        library, "decision-employee", "requirement-analysis", 1, PLAN | {"task_id": task_id}, {}
    )
    _record(
        library,
        "dev-employee",
        "code-develop",
        1,
        DEV_RESULT | {"task_id": task_id},
        {"repos/order-service/tests/test_reserve.py": "def test_broken():\n    assert False\n"},
    )
    orchestrator = _orchestrator(workspace, library)

    await orchestrator.advance(task_id)
    await orchestrator.advance(task_id)
    task = await orchestrator.advance(task_id)

    assert task.state is TaskState.DEVELOPING
    actors = [
        event.actor
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.JOB_STARTED
    ]
    assert ADVERSARY_EMPLOYEE not in actors


# --- 复现命令 ---------------------------------------------------------------


async def test_the_repro_command_actually_reproduces(workspace: Path, library: Path) -> None:
    """**复现命令要能被人独立执行。** 不然"可复现"只是产物里的一个字符串。"""
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    # 攻击脚本写在**红队自己的任务目录**里——它的写集只有那儿,写别处会被判越权回滚。
    _arm(library, task_id, FINDINGS, {f"tasks/{task_id}/attack_batch.py": ATTACK_SCRIPT})
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    workdir = orchestrator.workdir(orchestrator.store.get(task_id))
    proc = subprocess.run(
        [sys.executable, f"tasks/{task_id}/attack_batch.py"],
        cwd=workdir,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "批量预占不是原子的" in proc.stdout


@pytest.mark.parametrize("mode", ["off", "protected-hit", "always"])
def test_every_mode_is_a_legal_configuration(workspace: Path, mode: str) -> None:
    from agentgenome.config import load_config

    _quality_line(workspace, adversary=mode)

    assert load_config(workspace).quality_line.adversary.value == mode


# --- 战果转正 ---------------------------------------------------------------


WITH_CASE = {
    **FINDINGS,
    "findings": [{**FINDINGS["findings"][0], "case_file": "tasks/ag/attack_batch.py"}],
}


async def test_a_hit_produces_a_promotion_proposal(workspace: Path, library: Path) -> None:
    """一次攻击的价值从"这次抓到"延伸为"永远防住"——而那要从一份提案开始。"""
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    _arm(library, task_id, WITH_CASE, {f"tasks/{task_id}/attack_batch.py": ATTACK_SCRIPT})
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    written = load_promotions(workspace)
    assert written, "抓到了真问题却没有提案,红队的胜利只活到这个任务结束"
    body = written[0].read_text(encoding="utf-8")
    # 可审计:从回归集里的一条测试要能反查到它当年打穿了什么。
    assert task_id in body
    assert "批量预占不是原子的" in body
    assert FINDINGS["findings"][0]["repro_cmd"] in body
    assert "repos/order-service/tests" in body, "没说清楚建议落到哪儿"


async def test_the_proposal_is_on_the_event_plane(workspace: Path, library: Path) -> None:
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    _arm(library, task_id, WITH_CASE, {f"tasks/{task_id}/attack_batch.py": ATTACK_SCRIPT})
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    events = [
        event
        for event in orchestrator.log.events(task_id)
        if event.kind is LogKind.PROMOTION
    ]
    assert len(events) == 1
    assert events[0].payload["proposals"][0]["slot"], "提案没指回来源产物槽,反查不了战史"


async def test_a_clean_probe_proposes_nothing(workspace: Path, library: Path) -> None:
    """没打穿的尝试不进回归集:它是永久的运行成本换零收益。"""
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    _arm(library, task_id, CLEAN)
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    assert load_promotions(workspace) == []


async def test_promotion_never_writes_into_the_business_repo(
    workspace: Path, library: Path
) -> None:
    """**没有旁路。** 一条能绕过门禁进仓的路径,迟早会被用来绕过门禁。"""
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    _arm(library, task_id, WITH_CASE, {f"tasks/{task_id}/attack_batch.py": ATTACK_SCRIPT})
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    workdir = orchestrator.workdir(orchestrator.store.get(task_id))
    assert not (workdir / "repos/order-service/tests/attack_batch.py").exists()
    for written in load_promotions(workspace):
        assert "lessons/promotions" in written.as_posix()


def test_the_promoted_case_is_a_real_regression_test(tmp_path: Path) -> None:
    """转正的用例要真的是一道防线:靶子在时它红,靶子修好之后它绿。

    只断言"提案生成了"的话,进回归集的可能是一条永远绿的用例——那比没有更糟,
    它会让"这条线被守住了"看起来是真的。
    """
    from tests.fixtures.mall import materialize_mall

    repo = materialize_mall(tmp_path / "src")["inventory-service"].worktree
    case = repo / "tests" / "test_attack_batch.py"
    case.write_text(
        "import pytest\n"
        "from inventory.app import InventoryService\n\n\n"
        "def test_batch_reserve_is_atomic():\n"
        "    service = InventoryService(stock={'sku-1': 10})\n"
        "    with pytest.raises(Exception):\n"
        "        service.reserve_batch(['sku-1', 'sku-2'], [3], order_id='ord-1')\n"
        "    assert service.stock['sku-1'] == 10\n",
        encoding="utf-8",
    )

    red = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_attack_batch.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert red.returncode != 0, "转正的用例在未修复的靶子上是绿的——它守不住任何东西"

    app = repo / "src" / "inventory" / "app.py"
    app.write_text(
        app.read_text(encoding="utf-8").replace(
            "        reservations = []\n",
            "        if len(skus) != len(quantities):\n"
            "            raise ValueError('sku 与数量的长度必须一致')\n"
            "        reservations = []\n",
        ),
        encoding="utf-8",
    )
    green = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_attack_batch.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert green.returncode == 0, green.stdout + green.stderr


async def test_the_attack_artifacts_are_on_the_lineage_manifest(
    workspace: Path, library: Path
) -> None:
    """走既有产物编址,于是它们进血缘清单。另开一个目录等于把这批产物排除在审计之外。"""
    _quality_line(workspace, adversary="always")
    task_id = _submit(workspace)
    _arm(library, task_id, FINDINGS, {f"tasks/{task_id}/attack_batch.py": ATTACK_SCRIPT})
    orchestrator = _orchestrator(workspace, library)

    for _ in range(3):
        await orchestrator.advance(task_id)

    slots = sorted((workspace / "tasks" / task_id / "artifacts").glob(f"*unit-gate.{PROBE}"))
    assert slots, "对抗产物没落在既有编址下"
    manifest = json.loads((slots[-1] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["producer"] == ADVERSARY_EMPLOYEE
    assert manifest["node"] == PROBE
    assert manifest["outputs"] == ["result.json"]


async def test_repeated_hits_escalate_instead_of_looping_forever(
    workspace: Path, library: Path
) -> None:
    """对抗返工计入修复轮次,所以撞上限时按既有语义升级人工——**这是一个被看见的取舍**。"""
    _quality_line(workspace, adversary="always")
    architecture = workspace / "genome" / "rules" / "architecture.md"
    architecture.write_text(
        architecture.read_text(encoding="utf-8").replace(
            "layering: []", "layering: []\nmax_fix_rounds: 3"
        ),
        encoding="utf-8",
    )
    commit_all(workspace, "test: 显式收紧修复轮次")
    task_id = _submit(workspace)
    _arm(library, task_id, FINDINGS)
    for round_ in range(2, 6):
        _record(
            library,
            "dev-employee",
            "code-develop",
            round_,
            DEV_RESULT | {"task_id": task_id},
            {"repos/order-service/tests/test_reserve.py": PASSING_TEST},
        )
        # 回放键的第四维是 `(节点, 这个节点的第几个槽)`:第 N 轮里门禁与对抗各自是第 N 个槽。
        _record_round(library, "dev-employee", "unit-gate", f"{UNIT_GATE}.{round_}", round_, {})
        _record_round(
            library,
            ADVERSARY_EMPLOYEE,
            "adversarial-probe",
            f"{PROBE}.{round_}",
            round_,
            FINDINGS | {"task_id": task_id},
        )
    orchestrator = _orchestrator(workspace, library)

    task = orchestrator.store.get(task_id)
    for _ in range(12):
        task = await orchestrator.advance(task_id)
        if task.state is TaskState.ESCALATED:
            break

    assert task.state is TaskState.ESCALATED
    assert "修复轮次" in task.escalate_reason
