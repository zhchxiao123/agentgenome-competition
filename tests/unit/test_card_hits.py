"""功能卡片的命中计数:知识也要接受自然选择。

**计数反映有效性,不是曝光度。** 一张总被选中但任务总失败的卡片,它的高曝光恰恰是个坏信号
——按曝光计数的话,最没用的那批会排在最前面,然后在预算裁剪时优先活下来:自然选择会选出
最差的那批。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentgenome import paths
from agentgenome.genome.hits import (
    CREDITS_FILE,
    CardRef,
    LedgerUnreadable,
    apply_credits,
    pending_credits,
    record_credits,
    record_exposure,
    take_credits,
    take_exposures,
)
from agentgenome.genome.loader import load_tree
from agentgenome.genome.models import ProjectMap
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
    write_tree(tmp_path, ProjectMap.model_validate(BASE))
    target = tmp_path / paths.MODULES / "order-service" / MODULE_MAP
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["features"] = [
        {
            "id": "reserve-flow",
            "summary": "下单预占",
            "scope": ["repos/order-service/src/**"],
            "card": "features/reserve-flow.md",
        }
    ]
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    card = tmp_path / paths.MODULES / "order-service" / "features" / "reserve-flow.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(
        "---\nid: reserve-flow\nsummary: 下单预占\nhits: 4\n---\n细节。\n", encoding="utf-8"
    )
    return tmp_path


REF = CardRef("order-service", "reserve-flow")


# --- 曝光账本 ----------------------------------------------------------------


def test_an_exposure_survives_a_restart(workspace: Path) -> None:
    """任务可能跑几十分钟、跨进程重启,而「这一轮用了哪几张」要活到任务结束那一刻。"""
    record_exposure(workspace, "ag-1", [REF])

    assert take_exposures(workspace, "ag-1") == (REF,)


def test_taking_exposures_clears_them(workspace: Path) -> None:
    """留着的话同一个任务重跑会被重复计数,而那让计数从「有效性」退化成「被重跑过几次」。"""
    record_exposure(workspace, "ag-1", [REF])
    take_exposures(workspace, "ag-1")

    assert take_exposures(workspace, "ag-1") == ()


def test_the_same_card_twice_in_one_task_counts_once(workspace: Path) -> None:
    record_exposure(workspace, "ag-1", [REF])
    record_exposure(workspace, "ag-1", [REF])

    assert take_exposures(workspace, "ag-1") == (REF,)


def test_a_reference_carries_its_module(workspace: Path) -> None:
    """功能点 id 只在模块内唯一。不带模块的话跨模块同名会互相加分。"""
    assert CardRef.parse("order-service/reserve-flow") == REF


# --- 记账走知识写入路径 -------------------------------------------------------


def test_applying_credits_bumps_the_card(workspace: Path) -> None:
    apply_credits(workspace, load_tree(workspace), (REF,))

    assert load_tree(workspace).card("order-service", "reserve-flow").hits == 5


def test_the_count_survives_a_reload(workspace: Path) -> None:
    apply_credits(workspace, load_tree(workspace), (REF,))
    apply_credits(workspace, load_tree(workspace), (REF,))

    assert load_tree(workspace).card("order-service", "reserve-flow").hits == 6


def test_applying_credits_keeps_the_body(workspace: Path) -> None:
    """记一次账把正文弄丢的话,这条链路的收益远小于它的破坏。"""
    apply_credits(workspace, load_tree(workspace), (REF,))

    assert "细节。" in load_tree(workspace).card("order-service", "reserve-flow").body


def test_an_unknown_reference_is_ignored(workspace: Path) -> None:
    """卡片可能已经被合并或归档。为一条过时的引用抛异常会拖垮整批记账。"""
    apply_credits(workspace, load_tree(workspace), (CardRef("order-service", "gone"),))

    assert load_tree(workspace).card("order-service", "reserve-flow").hits == 4


# --- 编排器不写知识树 ---------------------------------------------------------


def test_the_orchestrator_only_emits_an_event(workspace: Path) -> None:
    """**这条约束比计数本身重要。** 开了「编排器顺手改一下卡片」这个口子，下一个需求会
    走同一个口子，几轮之后编排器就在随便改知识了——而「知识树只由架构员工写入」是整棵树
    的校验体系依赖的前提。"""
    import inspect

    from agentgenome.jobs import orchestrator

    source = inspect.getsource(orchestrator)

    assert "take_exposures" in source
    assert "apply_credits" not in source, "编排器直接改了卡片文件"
    # 它记账本,那是运行态目录里的一个文件,不是知识树。
    assert "record_credits" in source


# --- 两本账:曝光按任务清,待记账等知识写入路径来消费 ---------------------------


def test_settled_hits_wait_in_a_ledger_for_the_knowledge_path(workspace: Path) -> None:
    """结算之后不是直接写卡片,而是进待记账。

    编排器写的是账本(住在 `tasks/`,是运行态),不是知识树。这条是那条约束在数据上的落点。
    """
    record_credits(workspace, (REF,))

    assert pending_credits(workspace) == (REF,)
    # 还没有人记账,卡片上的数字不该动。
    assert load_tree(workspace).card("order-service", "reserve-flow").hits == 4


def test_the_same_card_hit_by_two_tasks_counts_twice(workspace: Path) -> None:
    """两个任务各命中一次就该加两次——去重的话,计数会退化成"被多少个任务用过至少一次"。"""
    record_credits(workspace, (REF,))
    record_credits(workspace, (REF,))

    assert pending_credits(workspace) == (REF, REF)


def test_a_dry_run_does_not_eat_the_ledger(workspace: Path) -> None:
    """**一个把数据吃掉的预演比没有预演更糟。**

    只报告那条路要是调了 `take_credits`,一次 `--dry-run` 会把这批命中清空,而它们再也
    回不来——而人跑预演的目的恰恰是"先看看再决定"。
    """
    record_credits(workspace, (REF,))

    assert pending_credits(workspace) == (REF,)
    assert pending_credits(workspace) == (REF,)


def test_taking_credits_clears_the_ledger(workspace: Path) -> None:
    """**先清账再写卡片。** 反过来的话,写到一半崩溃会让这一批命中被记两次——计数一旦虚高,
    "哪些知识真正被用上了"就再也答不准了。宁可少记一次:少记的下一轮还会再来。"""
    record_credits(workspace, (REF,))

    taken = take_credits(workspace)

    assert taken == (REF,)
    assert pending_credits(workspace) == ()


def test_a_broken_ledger_is_an_error_not_an_empty_one(workspace: Path) -> None:
    """当成空的话，一次损坏的读会让下一次写把整批待记账覆盖掉。

    而里面每一条都对应一次真实发生过的命中——丢了再也补不回来。账本坏了是要人看一眼的事。
    """
    (workspace / CREDITS_FILE).parent.mkdir(parents=True, exist_ok=True)
    (workspace / CREDITS_FILE).write_text("{ 半份", encoding="utf-8")

    with pytest.raises(LedgerUnreadable):
        pending_credits(workspace)


def test_the_command_writes_the_ledger_into_the_cards(workspace: Path) -> None:
    """命令是这个账本唯一的消费者。没有它，命中计数永远停在事件面，而界面上那一列永远是 0。"""
    from typer.testing import CliRunner

    from agentgenome.cli import app

    record_credits(workspace, (REF, REF))

    result = CliRunner().invoke(app, ["knowledge", "credits", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert load_tree(workspace).card("order-service", "reserve-flow").hits == 6
    assert pending_credits(workspace) == ()


def test_a_dry_run_of_the_command_leaves_the_ledger_alone(workspace: Path) -> None:
    from typer.testing import CliRunner

    from agentgenome.cli import app

    record_credits(workspace, (REF,))

    result = CliRunner().invoke(
        app, ["knowledge", "credits", "--dry-run", "--workspace", str(workspace)]
    )

    assert result.exit_code == 0, result.output
    assert pending_credits(workspace) == (REF,)
    # 卡片上的数字没动。
    assert load_tree(workspace).card("order-service", "reserve-flow").hits == 4
