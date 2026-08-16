"""可疑账:知识过期要被察觉,但保鲜靠信号,不靠门禁(PRD 41)。

卡片自带 `scope`,任务自带变更清单——此前两者只在路由里相遇("取知识"),这里补上
反方向("质疑知识"):任务终结时,变更命中了某卡的覆盖范围而知识没动,就记一条可疑。

三条底线,与 hits 的两本账同一套哲学:

- **检测是纯函数**:输入是树与变更清单,不做 I/O;
- **编排器只记账**:可疑账住 `tasks/`,`genome/` 一个字节不碰;
- **软信号**:账非空不阻塞任何迁移与提交——状态机与提交管线根本不认识这本账。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentgenome import paths
from agentgenome.genome.hits import LedgerUnreadable
from agentgenome.genome.loader import load_tree
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.suspects import (
    SUSPECTS_FILE,
    Suspect,
    SuspectKind,
    pending_suspects,
    record_suspects,
    stale_suspects,
)
from agentgenome.genome.tree import MODULE_MAP, write_tree

BASE = {
    "version": 1,
    "project": {"name": "mall"},
    "modules": [{"id": "order-service", "path": "repos/order-service/"}],
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / paths.KNOWLEDGE).mkdir(parents=True)
    (tmp_path / "repos/order-service" / "src").mkdir(parents=True)
    (tmp_path / "repos/order-service" / "migrations").mkdir(parents=True)
    write_tree(tmp_path, ProjectMap.model_validate(BASE))
    target = tmp_path / paths.MODULES / "order-service" / MODULE_MAP
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["features"] = [
        {
            "id": "reserve-flow",
            "summary": "下单预占",
            "scope": ["repos/order-service/src/**"],
            "card": "features/reserve-flow.md",
        },
        {
            "id": "migrations",
            "summary": "迁移脚本",
            "scope": ["repos/order-service/migrations/**"],
            "no_card": "读代码比读卡片快",
        },
    ]
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    card = tmp_path / paths.MODULES / "order-service" / "features" / "reserve-flow.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("---\nid: reserve-flow\n---\n细节。\n", encoding="utf-8")
    return tmp_path


# --- 检测是纯函数 --------------------------------------------------------------


def test_a_change_hitting_a_card_scope_without_a_knowledge_update_is_suspect(
    workspace: Path,
) -> None:
    found = stale_suspects(
        load_tree(workspace),
        changed=["repos/order-service/src/reserve.py"],
        task_id="ag-20260813-001",
        round_=2,
    )

    assert found == (
        Suspect(
            kind=SuspectKind.STALE,
            task_id="ag-20260813-001",
            card="order-service/reserve-flow",
            changed=("repos/order-service/src/reserve.py",),
            round=2,
        ),
    )


def test_a_no_card_feature_yields_no_suspect(workspace: Path) -> None:
    """没有知识就没有过期——`no_card` 是"这里不需要知识"的明确声明。"""
    found = stale_suspects(
        load_tree(workspace),
        changed=["repos/order-service/migrations/0002.sql"],
        task_id="ag-1",
        round_=1,
    )

    assert found == ()


def test_a_task_that_also_updated_the_card_is_not_suspect(workspace: Path) -> None:
    """变更清单里带着卡片文件本身,说明知识在同一个任务里跟上了。"""
    found = stale_suspects(
        load_tree(workspace),
        changed=[
            "repos/order-service/src/reserve.py",
            "genome/knowledge/modules/order-service/features/reserve-flow.md",
        ],
        task_id="ag-1",
        round_=1,
    )

    assert found == ()


def test_a_change_matching_nothing_yields_no_suspect(workspace: Path) -> None:
    found = stale_suspects(
        load_tree(workspace),
        changed=["repos/order-service/README.md"],
        task_id="ag-1",
        round_=1,
    )

    assert found == ()


# --- 账本 ---------------------------------------------------------------------


def _stale(task_id: str = "ag-1") -> Suspect:
    return Suspect(
        kind=SuspectKind.STALE,
        task_id=task_id,
        card="order-service/reserve-flow",
        changed=("repos/order-service/src/reserve.py",),
        round=1,
    )


def test_a_recorded_suspect_survives_a_restart(workspace: Path) -> None:
    record_suspects(workspace, (_stale(),))

    assert pending_suspects(workspace) == (_stale(),)


def test_recording_twice_for_the_same_task_and_card_counts_once(workspace: Path) -> None:
    """崩溃恢复会重放终态收尾——同一条可疑记两遍的话,账面余额就成了"被重放过几次"。"""
    record_suspects(workspace, (_stale(),))
    record_suspects(workspace, (_stale(),))

    assert pending_suspects(workspace) == (_stale(),)


def test_reading_the_ledger_does_not_eat_it(workspace: Path) -> None:
    record_suspects(workspace, (_stale(),))

    assert pending_suspects(workspace) == (_stale(),)
    assert pending_suspects(workspace) == (_stale(),)


def test_a_broken_ledger_is_an_error_not_an_empty_one(workspace: Path) -> None:
    (workspace / SUSPECTS_FILE).parent.mkdir(parents=True, exist_ok=True)
    (workspace / SUSPECTS_FILE).write_text("{ 半份", encoding="utf-8")

    with pytest.raises(LedgerUnreadable):
        pending_suspects(workspace)


# --- 软信号:状态机与提交管线根本不认识这本账 -----------------------------------


def test_the_state_machine_and_commit_path_never_read_the_ledger() -> None:
    """账非空不阻塞任何迁移与提交——最可靠的保证是那两层根本不 import 这本账。"""
    import agentgenome

    src = Path(agentgenome.__file__).parent
    offenders = [
        str(path.relative_to(src))
        for base in ("core", "commit", "space", "approval")
        for path in (src / base).rglob("*.py")
        if "suspects" in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"状态机/提交路径读了可疑账: {offenders}"


def test_the_orchestrator_records_the_ledger_but_never_writes_knowledge() -> None:
    """编排器记账(运行态),不写知识树——与 hits 同一条约束,同一种守法。"""
    import inspect

    from agentgenome.jobs import orchestrator

    source = inspect.getsource(orchestrator)

    assert "record_suspects" in source
    assert "stale_suspects" in source


# --- 消费:改卡与声明无变化,二选一都算响应(工单 06) --------------------------


def test_declaring_unchanged_clears_the_card_and_leaves_a_trace(workspace: Path) -> None:
    """ "核对过,无需更新"清账并留痕——不留痕的清账和静默丢弃在事后没有任何区别。"""
    from agentgenome.genome.suspects import ResolutionAction, resolutions, resolve_suspects

    record_suspects(workspace, (_stale("ag-1"), _stale("ag-2")))

    resolved = resolve_suspects(
        workspace,
        card="order-service/reserve-flow",
        action=ResolutionAction.UNCHANGED,
        note="核对过,scope 未变",
    )

    assert {item.task_id for item in resolved} == {"ag-1", "ag-2"}
    assert pending_suspects(workspace) == ()
    trace = resolutions(workspace)
    assert len(trace) == 2
    assert {item["task_id"] for item in trace} == {"ag-1", "ag-2"}
    assert all(item["action"] == "unchanged" for item in trace)
    assert all("核对过" in item["note"] for item in trace)


def test_resolving_one_card_leaves_the_others_pending(workspace: Path) -> None:
    from agentgenome.genome.suspects import resolve_suspects

    other = Suspect(
        kind=SuspectKind.STALE,
        task_id="ag-9",
        card="order-service/another",
        changed=("repos/order-service/src/y.py",),
    )
    record_suspects(workspace, (_stale(), other))

    resolve_suspects(workspace, card="order-service/reserve-flow")

    assert pending_suspects(workspace) == (other,)


def test_an_evaporation_signal_is_resolved_by_task(workspace: Path) -> None:
    """蒸发信号没有卡,按任务清——蒸馏补跑了或人写了复盘,都是"捡起来了"。"""
    from agentgenome.genome.suspects import resolve_suspects

    evaporated = Suspect(kind=SuspectKind.EVAPORATED, task_id="ag-7", round=2)
    record_suspects(workspace, (evaporated, _stale()))

    resolved = resolve_suspects(workspace, task_id="ag-7", note="人已写复盘")

    assert resolved == (evaporated,)
    assert pending_suspects(workspace) == (_stale(),)


def test_declaring_unchanged_never_touches_the_knowledge_tree(workspace: Path) -> None:
    """声明无变化 = 卡片零 diff。响应住账本层,knowledge 一个字节不动。"""
    from agentgenome.genome.suspects import resolve_suspects

    knowledge = workspace / paths.KNOWLEDGE
    before = {
        path: path.read_text(encoding="utf-8")
        for path in sorted(knowledge.rglob("*"))
        if path.is_file()
    }
    record_suspects(workspace, (_stale(),))

    resolve_suspects(workspace, card="order-service/reserve-flow")

    after = {
        path: path.read_text(encoding="utf-8")
        for path in sorted(knowledge.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_resolving_nothing_is_a_no_op(workspace: Path) -> None:
    from agentgenome.genome.suspects import resolve_suspects

    record_suspects(workspace, (_stale(),))

    assert resolve_suspects(workspace, card="order-service/ghost") == ()
    assert pending_suspects(workspace) == (_stale(),)


# --- 命令是"声明无变化"的入口 --------------------------------------------------


def test_the_command_lists_the_ledger_without_eating_it(workspace: Path) -> None:
    from typer.testing import CliRunner

    from agentgenome.cli import app

    record_suspects(workspace, (_stale(),))

    result = CliRunner().invoke(app, ["knowledge", "suspects", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "order-service/reserve-flow" in result.output
    assert pending_suspects(workspace) == (_stale(),)


def test_the_command_resolves_a_card_with_a_trace(workspace: Path) -> None:
    from typer.testing import CliRunner

    from agentgenome.cli import app
    from agentgenome.genome.suspects import resolutions

    record_suspects(workspace, (_stale(),))

    result = CliRunner().invoke(
        app,
        [
            "knowledge",
            "suspects",
            "--resolve",
            "order-service/reserve-flow",
            "--note",
            "对过当前代码,不变量未动",
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    assert pending_suspects(workspace) == ()
    trace = resolutions(workspace)
    assert trace and trace[0]["action"] == "unchanged"
    assert "不变量未动" in trace[0]["note"]


def test_an_empty_ledger_says_so_instead_of_staying_silent(workspace: Path) -> None:
    from typer.testing import CliRunner

    from agentgenome.cli import app

    result = CliRunner().invoke(app, ["knowledge", "suspects", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "可疑账为空" in result.output


# --- status:不靠自述的健康答案(工单 07) --------------------------------------


def test_status_shows_the_balance_and_the_queue(workspace: Path) -> None:
    from typer.testing import CliRunner

    from agentgenome.cli import app

    record_suspects(workspace, (_stale(),))

    result = CliRunner().invoke(app, ["knowledge", "status", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "可疑账 1 条" in result.output
    assert "order-service/reserve-flow" in result.output
    assert "深化队列" in result.output


def test_status_says_empty_instead_of_staying_silent(workspace: Path) -> None:
    from typer.testing import CliRunner

    from agentgenome.cli import app

    result = CliRunner().invoke(app, ["knowledge", "status", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "可疑账为空" in result.output


def test_status_is_read_only_byte_for_byte(workspace: Path) -> None:
    from typer.testing import CliRunner

    from agentgenome.cli import app

    record_suspects(workspace, (_stale(),))

    def snapshot() -> dict[str, bytes]:
        return {
            str(path): path.read_bytes()
            for base in (workspace / "genome", workspace / "tasks")
            if base.is_dir()
            for path in sorted(base.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    result = CliRunner().invoke(app, ["knowledge", "status", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert snapshot() == before
