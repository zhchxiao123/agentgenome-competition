"""整条知识初始化流水线:扫描 → 闸门 → 逐模块深读 → 汇总写入。

**这一组盯的是"连起来了"。** 五个阶段各自都有测试而且都是绿的,但它们此前没有任何东西把
彼此连起来——那种"每一节都能跑、整条跑不起来"的状态最容易被当成做完了。所以这里断言的不是
某一节的逻辑,是**一次真的从头跑到尾**:人在闸门上改过的那份列表真的被用了、每个模块真的
各派了一个作业、契约真的被合并而不是互相覆盖、进度真的在跑的过程中落了盘。

走回放运行时 + mall 夹具:CI 里跑的是真实的 git、真实的基因组加载器、真实的校验,唯独 Agent
那一段是确定性的。

PRD 34 之后,员工的产出是产物目录 `staging/` 下的真实树文件 + 一张小票;录制因此携带
`outputs/staging/**`。**Job 的成败由 staging 校验裁决**——坏产出在深读作业那一步就被拦下,
不再等到汇总。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from agentgenome import paths
from agentgenome.cli import app
from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.genome_task import GenomeTaskState, GenomeTaskStore
from agentgenome.genome.deep_read import (
    ModuleOutcome,
    deep_read_modules,
    read_progress,
    write_progress,
)
from agentgenome.genome.features import no_card_declarations
from agentgenome.genome.loader import load_project_map, load_tree
from agentgenome.genome.pipeline import DEEP_READ_DIR, REBUILD_REPORT_FILE, REPORT_FILE
from agentgenome.space.gitcmd import git, git_out
from tests.fixtures.knowledge_staging import build_staging, card, receipt_json
from tests.fixtures.mall import materialize_mall

runner = CliRunner()


def _module(
    module_id: str, features: list[dict[str, Any]] | None = None, **extra: Any
) -> dict[str, Any]:
    """一个模块的深读产出(交给 `build_staging` 的形状)。**只报自己那一个模块**。"""
    spec: dict[str, Any] = {"id": module_id}
    if features is not None:
        spec["features"] = features
    spec.update(extra)
    return spec


#: 两侧都报了那条共享契约(消费者只有调用方知道),而各自还报了一条只有自己知道的。
#: **逐模块写入的话,后写的那一份会把前一份整段抹掉**——那时列表里只会剩下一侧的契约。
ORDER_SIDE = build_staging(
    [_module("order-service")],
    interfaces=[
        {
            "id": "reserve-api",
            "kind": "http",
            "provider": "inventory-service",
            "consumers": ["order-service"],
            "confidence": 0.85,
        }
    ],
    datastores=[
        {"id": "order-db", "kind": "postgres", "owner": "order-service", "confidence": 0.7}
    ],
)
INVENTORY_SIDE = build_staging(
    [_module("inventory-service")],
    interfaces=[
        {
            "id": "reserve-api",
            "kind": "http",
            "provider": "inventory-service",
            "consumers": [],
            "confidence": 0.9,
        },
        {
            "id": "stock-events",
            "kind": "event",
            "provider": "inventory-service",
            "consumers": [],
            "confidence": 0.8,
        },
    ],
    datastores=[
        {"id": "stock-db", "kind": "postgres", "owner": "inventory-service", "confidence": 0.6}
    ],
)


def _record(
    library: Path,
    subject: str,
    staging: dict[str, str] | None,
    receipt: str | None = None,
    tokens: int = 1_200,
) -> None:
    """一份深读作业的录制:`outputs/staging/**` + 小票。

    **整目录重建,不是就地补写。** 测试会用"少了一张卡"的新录制覆盖旧的——残留的旧文件
    会以孤儿卡片的形态漏进回放,而那不是这份录制想说的剧本。
    `staging=None` 表示员工什么都没写(plan mode 空跑);`receipt=None` 表示没写小票。
    """
    directory = library / f"arch-employee__knowledge-init__{subject}__r1"
    if directory.is_dir():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for relative, content in (staging or {}).items():
        target = directory / "outputs" / "staging" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if receipt is not None:
        (directory / "result.json").write_text(receipt, encoding="utf-8")
    # 录制里声明用量,回放才报得出成本——不声明的话"这次初始化烧了多少"永远是 0。
    (directory / "meta.yaml").write_text(f"tokens_used: {tokens}\n", encoding="utf-8")


def _record_side(library: Path, subject: str, staging: dict[str, str], **receipt: Any) -> None:
    _record(library, subject, staging, receipt_json(**receipt))


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_RECORDINGS", str(tmp_path / "lib"))
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
    _record_side(tmp_path / "lib", "order-service", ORDER_SIDE)
    _record_side(tmp_path / "lib", "inventory-service", INVENTORY_SIDE)
    return root


def _plan(workspace: Path) -> str:
    result = runner.invoke(app, ["knowledge", "plan", "--workspace", str(workspace), "--json"])
    assert result.exit_code == 0, result.output
    return str(json.loads(result.stdout)["task_id"])


def _run(workspace: Path, *extra: str):
    result = runner.invoke(
        app, ["knowledge", "run", "--workspace", str(workspace), "--runtime", "replay", *extra]
    )
    # 推进器自己崩掉与"推不动"是两件事。不断言的话前者会伪装成后者,而那正是这一组要抓的。
    assert result.exit_code == 0, result.output + repr(result.exception)
    return result


def _answer(workspace: Path, task_id: str, module_ids: list[str]) -> None:
    """走真实入口回答闸门。

    直接写答复文件的话,任务还停在待确认——而那正是这一组要证明"闸门真的推得动"的地方。
    """
    answer = workspace / "answer.json"
    answer.write_text(
        json.dumps({"modules": [{"id": item, "paths": [f"{item}/"]} for item in module_ids]}),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["genome", "confirm", task_id, "--answer", str(answer), "--workspace", str(workspace)],
    )
    assert result.exit_code == 0, result.output


def test_a_gate_that_nobody_answered_is_left_alone(workspace: Path) -> None:
    """推它等于替人回答——而那个闸门存在的全部理由就是让人看一眼。

    断言的是**它一个作业都没派**,不只是状态没变:只看状态的话,推进器把每个模块都读了一遍
    然后卡在别处,这条照样是绿的。
    """
    task_id = _plan(workspace)

    _run(workspace)

    assert GenomeTaskStore(workspace).get(task_id).state is GenomeTaskState.AWAITING_CONFIRMATION
    assert not (workspace / "tasks" / task_id / DEEP_READ_DIR).exists()
    assert read_progress(workspace, task_id) is None


def test_the_whole_pipeline_runs_from_the_gate_to_a_written_knowledge_tree(
    workspace: Path,
) -> None:
    """一次真的从头跑到尾。

    此前五个阶段各自就绪但没有任何东西把它们连起来,而每一节的测试都是绿的——这条断言的是
    整条,不是某一节。
    """
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])

    # 一轮把闸门后的任务推进深读,再一轮做汇总。
    assert _run(workspace, "--rounds", "2").exit_code == 0

    task = GenomeTaskStore(workspace).get(task_id)
    assert task.state is GenomeTaskState.SUBMITTED, task.failure_reason
    project_map = load_project_map(workspace)
    assert project_map.module("order-service").test_cmd == "pytest -q"
    assert project_map.module("inventory-service").summary


def test_each_module_gets_its_own_job(workspace: Path) -> None:
    """回放键带模块 id。

    不带的话几十个同 employee 同 procedure 的作业全撞在一个键上,回放会给每个模块返回同一份
    产出——而测试照样是绿的。所以这里比的是**两个模块收进来的树片段各是各的**:只看"产出了
    两个目录"的话,两份内容相同的产出照样过。
    """
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])

    _run(workspace, "--rounds", "2")

    directory = workspace / "tasks" / task_id / DEEP_READ_DIR
    produced = sorted(item.name for item in directory.iterdir() if item.is_dir())
    assert produced == ["inventory-service", "order-service"]
    for module_id in produced:
        fragment = directory / module_id / "staging" / "modules" / module_id / "map.yaml"
        assert fragment.is_file(), f"{module_id} 的片段里没有它自己的模块地图"
        assert yaml.safe_load(fragment.read_text(encoding="utf-8"))["id"] == module_id


def test_contracts_reported_by_both_sides_are_merged_not_overwritten(workspace: Path) -> None:
    """两个模块各报各的契约,汇总时要合成一份。

    **逐模块写入的话第二份会把第一份整段抹掉**,症状是"契约索引里少了一半"——没有报错,
    只是少了。而下游的集成测试判定正是按契约 id 找关联方的。
    """
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])

    _run(workspace, "--rounds", "2")

    project_map = load_project_map(workspace)
    assert sorted(item.id for item in project_map.interfaces) == ["reserve-api", "stock-events"]
    assert sorted(item.id for item in project_map.datastores) == ["order-db", "stock-db"]
    # 消费者只有调用方那一侧知道,合并要把它留住(provider 那一侧报的是空列表)。
    reserve = next(item for item in project_map.interfaces if item.id == "reserve-api")
    assert reserve.consumers == ["order-service"]


def test_progress_lands_on_disk_while_the_read_is_running(tmp_path: Path) -> None:
    """ "跑到哪了"要在跑的过程中答得上来。

    只在结束时落盘的进度,恰恰在唯一有人盯着的那段时间里是空的。**所以这条在跑的过程中看**
    ——跑完再看的话,把 `on_update` 改成只在最后调一次,它照样是绿的。
    """
    seen: list[list[str]] = []

    async def run(module_id: str) -> ModuleOutcome:
        seen.append(
            list(read_progress(tmp_path, "gn-1").done if read_progress(tmp_path, "gn-1") else [])
        )
        return ModuleOutcome(module_id, ok=True)

    asyncio.run(
        deep_read_modules(
            ["a", "b", "c"],
            run=run,
            on_update=lambda found: write_progress(tmp_path, "gn-1", found),
        )
    )

    # 第二、三个模块开跑时,前面那些已经在磁盘上了。
    assert seen[-1], "跑到最后一个模块时,前面读完的还没落盘"
    assert read_progress(tmp_path, "gn-1").planned == ["a", "b", "c"]  # type: ignore[union-attr]


def test_only_the_modules_the_human_confirmed_are_read(workspace: Path) -> None:
    """模块清单来自人在闸门上给的那份最终列表,不是草案。

    用草案的话,人做的那次合并、拆分、剔除全都白做了——而界面上他看到的是"已确认"。
    """
    task_id = _plan(workspace)
    # 人只确认了 order-service,于是地图上只剩它——契约引用不到别人,录制也不报契约。
    _record_side(
        workspace.parent / "lib", "order-service", build_staging([_module("order-service")])
    )
    _answer(workspace, task_id, ["order-service"])

    _run(workspace, "--rounds", "2")

    directory = workspace / "tasks" / task_id / DEEP_READ_DIR
    produced = sorted(item.name for item in directory.iterdir() if item.is_dir())
    assert produced == ["order-service"]


def test_the_report_for_humans_lands_on_disk(workspace: Path) -> None:
    """初始化跑几个小时,没人会守着终端等这段输出。"""
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])

    _run(workspace, "--rounds", "2")

    report = (workspace / "tasks" / task_id / REPORT_FILE).read_text(encoding="utf-8")
    assert "order-service" in report


def test_a_module_whose_job_fails_does_not_take_the_others_down(workspace: Path) -> None:
    """四十九个模块里有一个超时,前面四十八个的产出不该跟着丢。

    **部分成功是有意义的产出**,而失败的那几个在进度里点名,可以从详情页就地重建。
    """
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])
    # 抹掉一份录制:回放找不到它,那个模块的作业就失败。
    shutil.rmtree(
        workspace.parent / "lib" / "arch-employee__knowledge-init__inventory-service__r1"
    )

    _run(workspace, "--rounds", "2")

    progress = read_progress(workspace, task_id)
    assert progress is not None
    assert progress.done == ["order-service"]
    assert [item.module_id for item in progress.failed] == ["inventory-service"]
    # 汇总照跑:知识树里有成功那个模块的认知。
    assert load_project_map(workspace).module("order-service").test_cmd == "pytest -q"


def test_an_empty_run_fails_with_no_output(workspace: Path) -> None:
    """员工什么都没写(plan mode 空跑)→ staging 缺失 → 判失败,理由指向"无产物"。

    `contract.py` 里"plan mode 退出码 0 无产物"的教训同样被 staging 覆盖(PRD 34 D2):
    没有 staging 树就是没有产物——**自述写得再好也替代不了树文件**,何况这次连自述都没有。
    """
    task_id = _plan(workspace)
    _record(workspace.parent / "lib", "order-service", staging=None, receipt=None)
    _answer(workspace, task_id, ["order-service"])

    _run(workspace, "--rounds", "2")

    task = GenomeTaskStore(workspace).get(task_id)
    assert task.state is GenomeTaskState.FAILED
    progress = read_progress(workspace, task_id)
    assert progress is not None
    (failed,) = progress.failed
    assert "没有产物" in failed.detail


def test_a_missing_receipt_fails_even_when_the_tree_is_valid(workspace: Path) -> None:
    """小票缺失算格式失败,树写得再好也一样(PRD 34 D2)。

    小票不参与裁决,但它是校对清单(questions / low_confidence)的唯一入口——允许缺失的话,
    这个入口会安静地消失。契约重试(=1)的语义由 `contract_loop` 的既有测试钉住;回放
    运行时不走重试,这里断言的是"缺小票 = 契约失败"这半。
    """
    task_id = _plan(workspace)
    _record(workspace.parent / "lib", "order-service", ORDER_SIDE, receipt=None)
    _answer(workspace, task_id, ["order-service"])

    _run(workspace, "--rounds", "2")

    task = GenomeTaskStore(workspace).get(task_id)
    assert task.state is GenomeTaskState.FAILED
    progress = read_progress(workspace, task_id)
    assert progress is not None
    (failed,) = progress.failed
    assert "result.json" in failed.detail


def test_a_broken_staging_file_is_rejected_file_by_file_at_the_job(workspace: Path) -> None:
    """坏一个文件,拒绝原因逐文件点名仅此文件——回注 JSON path 只会让第二次错得一模一样。

    坏产出**在深读作业那一步就被拦下**(裁决即校验),不再等到汇总;而好模块照常走完。
    """
    task_id = _plan(workspace)
    broken = build_staging(
        [
            _module(
                "order-service",
                features=[
                    {
                        "id": "place-order",
                        "summary": "下单",
                        "scope": ["repos/order-service/src/**"],
                        "card_text": card("place-order"),
                    },
                    {
                        "id": "reserve-flow",
                        "summary": "预占",
                        "scope": ["repos/order-service/src/**"],
                        "card_text": "没有 front matter 的坏卡\n",
                    },
                ],
            )
        ]
    )
    _record_side(workspace.parent / "lib", "order-service", broken)
    _answer(workspace, task_id, ["order-service", "inventory-service"])

    _run(workspace, "--rounds", "2")

    progress = read_progress(workspace, task_id)
    assert progress is not None
    assert progress.done == ["inventory-service"]
    (failed,) = progress.failed
    assert "reserve-flow.md" in failed.detail
    assert "place-order.md" not in failed.detail, "拒绝原因只该点名坏的那个文件"


def test_rebuilding_one_module_skips_the_gate(workspace: Path) -> None:
    """按模块重建的边界早就拍过板了。

    再问一次人只会让他点一次"就这样"——而一个每次都被无脑确认的闸门,下一次真有问题时也会
    被无脑确认。
    """
    created = runner.invoke(
        app,
        ["genome", "reinit", "--module", "order-service", "--workspace", str(workspace), "--json"],
    )
    assert created.exit_code == 0, created.output
    task_id = json.loads(created.stdout)["tasks"][0]["task_id"]

    _run(workspace, "--rounds", "2")

    task = GenomeTaskStore(workspace).get(task_id)
    assert task.state is GenomeTaskState.SUBMITTED


def test_rebuilding_one_module_keeps_the_other_modules_contracts(workspace: Path) -> None:
    """按模块重建**不该抹掉别人报过的契约**。

    汇总那一步整段替换契约索引，而重建只读了一个模块——不把树上已有的算进合并的话，其余
    模块的契约会静默消失：没有报错，只是少了。而下游「这次改动会波及谁」正是按契约找关联方的。
    """
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])
    _run(workspace, "--rounds", "2")
    assert sorted(item.id for item in load_project_map(workspace).interfaces) == [
        "reserve-api",
        "stock-events",
    ]

    created = runner.invoke(
        app,
        ["genome", "reinit", "--module", "order-service", "--workspace", str(workspace), "--json"],
    )
    assert created.exit_code == 0, created.output
    _run(workspace, "--rounds", "2")

    assert sorted(item.id for item in load_project_map(workspace).interfaces) == [
        "reserve-api",
        "stock-events",
    ]
    assert sorted(item.id for item in load_project_map(workspace).datastores) == [
        "order-db",
        "stock-db",
    ]


def test_a_module_the_human_merged_at_the_gate_actually_gets_built(workspace: Path) -> None:
    """人在闸门上合并出来的模块要真的进项目地图。

    只拿答复决定「读哪几个」的那一版里，合并出来的新模块会在深读作业上撞上「未知模块」——
    一次跑了几十分钟的初始化，死在人已经答对了的那个环节上。
    """
    task_id = _plan(workspace)
    _record_side(
        workspace.parent / "lib",
        "mall-core",
        build_staging([_module("mall-core", path="mall-core/")]),
    )
    _answer(workspace, task_id, ["mall-core"])

    _run(workspace, "--rounds", "2")

    task = GenomeTaskStore(workspace).get(task_id)
    assert task.state is GenomeTaskState.SUBMITTED, task.failure_reason
    assert [item.id for item in load_project_map(workspace).modules] == ["mall-core"]


def test_a_knowledge_write_the_gate_refuses_stops_in_place(workspace: Path) -> None:
    """校验拦下来时**停在汇总中，不判死**。

    深读是整条流水线里唯一贵的一段，它的产出还在任务目录里；判成终态失败的话那批产出再也
    用不上了——人修完树想重跑④都没有入口。

    片段级问题在深读作业上就被拦(见上面逐文件那条);这里构造的是**只有合并后的整树才
    看得出来的问题**——契约的 schema 指针指向不存在的文件,引用完整性是树级校验。
    """
    task_id = _plan(workspace)
    rogue = build_staging(
        [_module("order-service")],
        interfaces=[
            # 只有 order 一侧知道的契约:合并时不会被另一侧的同 id 上报顶掉。
            {
                "id": "order-events",
                "kind": "event",
                "provider": "order-service",
                "consumers": [],
                "schema": "no/such/schema.yaml",
                "confidence": 0.9,
            }
        ],
    )
    _record_side(workspace.parent / "lib", "order-service", rogue)
    _answer(workspace, task_id, ["order-service", "inventory-service"])

    _run(workspace, "--rounds", "2")

    task = GenomeTaskStore(workspace).get(task_id)
    assert task.state is GenomeTaskState.SUMMARISING
    # 深读的产出还在，重跑④不必重读。
    assert (
        workspace / "tasks" / task_id / DEEP_READ_DIR / "order-service" / "staging"
    ).is_dir()


def test_a_module_already_read_is_not_dispatched_again(workspace: Path) -> None:
    """深读跑到一半崩了再起来，不该把最贵的那段重跑一遍。

    与研发那台编排器「先看产物在不在」是同一条。
    """
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])
    _run(workspace, "--rounds", "2")
    fragment = (
        workspace
        / "tasks"
        / task_id
        / DEEP_READ_DIR
        / "order-service"
        / "staging"
        / "modules"
        / "order-service"
        / "map.yaml"
    )
    stamp = fragment.stat().st_mtime

    # 把任务推回深读，再跑一轮：已有产出的那个模块不该被重派。
    store = GenomeTaskStore(workspace)
    store.save(store.get(task_id).evolve(state=GenomeTaskState.DEEP_READ))
    _run(workspace)

    assert fragment.stat().st_mtime == stamp


def test_the_tokens_a_genome_task_burned_survive_the_process(workspace: Path) -> None:
    """池是进程内的。不落库的话预算那条硬上限只在单次进程内成立，而界面上成本永远是 0。"""
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service"])

    _run(workspace, "--rounds", "2")

    assert GenomeTaskStore(workspace).get(task_id).tokens_used > 0


def test_the_tree_gets_feature_points_not_just_module_cards(workspace: Path) -> None:
    """③ 要产出**功能点**，不只是模块认知卡。

    不产出的话，19 的「完备」检查是空过的——零个功能点当然完备；而汇总报告里「无需卡片」
    与「低置信度」两节在真实数据上永远是空的，那两节恰恰是那份报告的全部意义。
    """
    task_id = _plan(workspace)
    _record_side(
        workspace.parent / "lib",
        "order-service",
        build_staging(
            [
                _module(
                    "order-service",
                    features=[
                        {
                            "id": "place-order",
                            "summary": "下单",
                            "scope": ["repos/order-service/src/**"],
                            "card_text": card("place-order", "下单的时序与坑点。"),
                        },
                        {
                            "id": "migrations",
                            "summary": "迁移脚本",
                            "scope": ["repos/order-service/migrations/**"],
                            "no_card": "只是一串迁移脚本，读代码比读卡片快",
                        },
                    ],
                )
            ]
        ),
    )
    _answer(workspace, task_id, ["order-service"])

    _run(workspace, "--rounds", "2")

    task = GenomeTaskStore(workspace).get(task_id)
    assert task.state is GenomeTaskState.SUBMITTED, task.failure_reason
    tree = load_tree(workspace)
    assert [item.id for item in tree.features("order-service")] == ["place-order", "migrations"]
    assert tree.card("order-service", "place-order") is not None
    # 「无需卡片」那条带着理由——空理由等于没写。
    (declared,) = no_card_declarations(tree)
    assert declared.feature.no_card


def test_the_report_lists_what_the_human_has_to_look_at(workspace: Path) -> None:
    """报告里「无需卡片」那一节要真的有东西。

    它是这份报告存在的两个理由之一：校验挡得住空理由，挡不住「不需要」三个字，而这是那条
    完备性不变式唯一的软肋，也是唯一有效的补强。
    """
    task_id = _plan(workspace)
    _record_side(
        workspace.parent / "lib",
        "order-service",
        build_staging(
            [
                _module(
                    "order-service",
                    features=[
                        {
                            "id": "migrations",
                            "summary": "迁移脚本",
                            "scope": ["repos/order-service/migrations/**"],
                            "no_card": "只是一串迁移脚本",
                        }
                    ],
                )
            ]
        ),
    )
    _answer(workspace, task_id, ["order-service"])

    _run(workspace, "--rounds", "2")

    report = (workspace / "tasks" / task_id / REPORT_FILE).read_text(encoding="utf-8")
    assert "migrations" in report
    assert "只是一串迁移脚本" in report


def test_the_questions_on_the_receipt_reach_the_report(workspace: Path) -> None:
    """小票上的 questions 要进校对清单(PRD 34 US4)。

    小票不参与裁决,但它是员工唯一能说"这里我拿不准、请人拍板"的地方——契约改造不该把
    人工复核的这个入口弄丢。
    """
    task_id = _plan(workspace)
    _record_side(
        workspace.parent / "lib",
        "order-service",
        ORDER_SIDE,
        questions=["订单超时补偿的语义我拿不准,请人确认"],
    )
    _answer(workspace, task_id, ["order-service", "inventory-service"])

    _run(workspace, "--rounds", "2")

    report = (workspace / "tasks" / task_id / REPORT_FILE).read_text(encoding="utf-8")
    assert "订单超时补偿的语义我拿不准" in report


def test_a_rebuild_diffs_the_new_cards_against_the_old_ones(workspace: Path) -> None:
    """⑤ 比初始化多的那一步：产物与现有卡片先做 diff。

    「我改过的东西下次扫描就没了」会让人彻底放弃维护知识——而让他看见哪些被动了、哪些没被
    动，是唯一能挡住这件事的东西。
    """
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])
    _run(workspace, "--rounds", "2")

    created = runner.invoke(
        app,
        ["genome", "reinit", "--module", "order-service", "--workspace", str(workspace), "--json"],
    )
    rebuild_id = json.loads(created.stdout)["tasks"][0]["task_id"]
    _run(workspace, "--rounds", "2")

    diff = (workspace / "tasks" / rebuild_id / REBUILD_REPORT_FILE).read_text(encoding="utf-8")
    assert "重建 order-service" in diff
    # 全量初始化那次不产出这一页：面对一棵空树，diff 就是「每一张都是新的」，没有信息量。
    assert not (workspace / "tasks" / task_id / REBUILD_REPORT_FILE).exists()


def test_a_human_edited_card_survives_a_rebuild_and_is_named_in_the_diff(
    workspace: Path,
) -> None:
    """带人工编辑标记的一律不动，而且要在那份 diff 上点名。"""
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])
    _run(workspace, "--rounds", "2")
    card_file = workspace / "genome" / "knowledge" / "modules" / "order-service" / "overview.md"
    card_file.write_text("<!-- human-edited -->\n我自己写的。\n", encoding="utf-8")

    created = runner.invoke(
        app,
        ["genome", "reinit", "--module", "order-service", "--workspace", str(workspace), "--json"],
    )
    rebuild_id = json.loads(created.stdout)["tasks"][0]["task_id"]
    _run(workspace, "--rounds", "2")

    assert "我自己写的。" in card_file.read_text(encoding="utf-8")
    diff = (workspace / "tasks" / rebuild_id / REBUILD_REPORT_FILE).read_text(encoding="utf-8")
    # 断言它在**「你手改过的」那一节**里。只查文件名的话，保护失效、它被列进「会被改写的」，
    # 这条照样绿——而那正是这条要抓的。
    protected = diff.split("你手改过的")[-1]
    assert "overview.md" in protected


def test_the_knowledge_change_lands_on_the_version_plane(workspace: Path) -> None:
    """不提交的话，知识变更不在版本面上——缺口检测看不见它，版本历史里也没有它。"""
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])

    _run(workspace, "--rounds", "2")

    subject = git_out(workspace, "log", "-1", "--format=%s", "--", "genome")
    assert subject.startswith("knowledge(")
    body = git_out(workspace, "log", "-1", "--format=%b", "--", "genome")
    # 提交信息里带任务号：「这条认知从哪次经验来的」靠它回答。
    assert task_id in body


def test_every_run_is_attributable_to_the_task_that_did_it(workspace: Path) -> None:
    """每一次汇总都在版本面上留一条能追回任务的提交。

    **一次内容没变的重建也会留下提交**——项目地图的版本号与时间戳每次都会动，那是既有语义
    （「认知被重新生成过一次」），这里不改它。所以这条断言的不是「没变就没有提交」，而是
    「每一条提交都指得回是哪次任务写的」：那才是「这条认知从哪次经验来的」能被回答的前提。
    """
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])
    _run(workspace, "--rounds", "2")

    created = runner.invoke(
        app,
        ["genome", "reinit", "--module", "order-service", "--workspace", str(workspace), "--json"],
    )
    assert created.exit_code == 0, created.output
    rebuild_id = json.loads(created.stdout)["tasks"][0]["task_id"]
    _run(workspace, "--rounds", "2")

    assert rebuild_id in git_out(workspace, "log", "-1", "--format=%b", "--", "genome")
    # 上一条是初始化那次，两条各归各的任务。
    assert task_id in git_out(workspace, "log", "-1", "--skip=1", "--format=%b", "--", "genome")


def test_the_gate_refuses_a_module_covering_two_directories(workspace: Path) -> None:
    """一个模块对应一个目录——项目地图上的 `path` 是单值，也是子模块的挂载点。

    **在提交那一刻拒，不是在写地图时悄悄取第一条。** 取第一条的话，人合并的第二个目录会
    静默失去覆盖，而他在界面上看到的是「已确认」——而这个闸门存在的全部理由就是让他看一眼。
    """
    task_id = _plan(workspace)
    answer = workspace / "answer.json"
    answer.write_text(
        json.dumps(
            {
                "modules": [
                    {"id": "mall", "paths": ["repos/order-service/", "repos/inventory-service/"]}
                ]
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["genome", "confirm", task_id, "--answer", str(answer), "--workspace", str(workspace)],
    )

    assert result.exit_code != 0
    assert "共同父目录" in result.output
    # 任务还在待确认等下一次:人答错了不是系统出错。
    assert GenomeTaskStore(workspace).get(task_id).state is GenomeTaskState.AWAITING_CONFIRMATION


def test_a_feature_with_no_knowledge_at_all_is_refused_at_the_job(workspace: Path) -> None:
    """既没有卡片、也没有一条说明为什么不需要的理由——这份产出在深读作业上就被拦下来。

    补一张空卡片的话，「这里没知识」与「这里不需要知识」就再也分不开了，而那正是完备性
    这条不变式存在的全部理由（ADR-0003）。
    """
    task_id = _plan(workspace)
    _record_side(
        workspace.parent / "lib",
        "order-service",
        build_staging(
            [
                _module(
                    "order-service",
                    features=[
                        {"id": "ghost", "summary": "空的", "scope": ["repos/order-service/src/**"]}
                    ],
                )
            ]
        ),
    )
    _answer(workspace, task_id, ["order-service", "inventory-service"])

    _run(workspace, "--rounds", "2")

    progress = read_progress(workspace, task_id)
    assert progress is not None
    assert [item.module_id for item in progress.failed] == ["order-service"]
    assert not (workspace / "genome/knowledge/modules/order-service/features/ghost.md").exists()


def test_a_feature_id_cannot_escape_the_genome_directory(workspace: Path) -> None:
    """id 会被拼成 `features/<id>.md`。

    不查的话，一个 `../../` 开头的 id 能把文件写到 Workspace 之外去——而「只写 genome/**」
    那条承诺当场作废。作业自己的越权检查管不到这里：写入发生在编排器里，不在沙箱作业里。
    """
    task_id = _plan(workspace)
    _record_side(
        workspace.parent / "lib",
        "order-service",
        build_staging(
            [
                _module(
                    "order-service",
                    features=[
                        {
                            "id": "../../../escaped",
                            "summary": "跑出去",
                            "scope": ["repos/order-service/src/**"],
                            "no_card": "借口",
                        }
                    ],
                )
            ]
        ),
    )
    _answer(workspace, task_id, ["order-service", "inventory-service"])

    _run(workspace, "--rounds", "2")

    progress = read_progress(workspace, task_id)
    assert progress is not None
    assert [item.module_id for item in progress.failed] == ["order-service"]
    assert not (workspace.parent / "escaped.md").exists()


def test_a_refused_write_leaves_the_tree_valid(workspace: Path) -> None:
    """被拒的产出不能把树留在半更新状态。

    坏产出如今在深读作业上就被拦下,根本走不到写入;而万一走到(合并级问题),
    `apply_staged` 的快照会逐字节还原。两条路的终点一致:树原样、`genome validate`
    照样绿、下一次写入不会在第一行就死。
    """
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])
    _run(workspace, "--rounds", "2")
    before = (workspace / "genome/knowledge/project-map.yaml").read_text(encoding="utf-8")

    created = runner.invoke(
        app,
        ["genome", "reinit", "--module", "order-service", "--workspace", str(workspace), "--json"],
    )
    rebuild_id = json.loads(created.stdout)["tasks"][0]["task_id"]
    _record_side(
        workspace.parent / "lib",
        "order-service",
        build_staging(
            [
                _module(
                    "order-service",
                    features=[{"id": "ghost", "scope": ["x/**"], "no_card": "借口"}],
                )
            ]
        ),
    )
    _run(workspace, "--rounds", "2")

    task = GenomeTaskStore(workspace).get(rebuild_id)
    assert task.state is GenomeTaskState.FAILED
    # 树原样不动，`genome validate` 照样绿，下一次写入不会在第一行就死。
    assert (workspace / "genome/knowledge/project-map.yaml").read_text(encoding="utf-8") == before
    assert load_tree(workspace) is not None


def _features(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return list(items)


def test_a_feature_dropped_by_a_rebuild_does_not_orphan_its_card(workspace: Path) -> None:
    """重新拆分模块是正常操作，它不该把知识树卡死。

    只换清单不动文件的话，上一轮那张卡片会变成没有任何功能点指向它的孤儿——而孤儿卡片会让
    整棵树校验不过，于是下一次写入在第一行就失败。
    """
    task_id = _plan(workspace)
    _record_side(
        workspace.parent / "lib",
        "order-service",
        build_staging(
            [
                _module(
                    "order-service",
                    features=_features(
                        {
                            "id": "a",
                            "scope": ["repos/order-service/src/**"],
                            "card_text": card("a"),
                        },
                        {
                            "id": "b",
                            "scope": ["repos/order-service/src/**"],
                            "card_text": card("b"),
                        },
                    ),
                )
            ]
        ),
    )
    _answer(workspace, task_id, ["order-service", "inventory-service"])
    _run(workspace, "--rounds", "2")
    assert (workspace / "genome/knowledge/modules/order-service/features/b.md").is_file()

    created = runner.invoke(
        app,
        ["genome", "reinit", "--module", "order-service", "--workspace", str(workspace), "--json"],
    )
    rebuild_id = json.loads(created.stdout)["tasks"][0]["task_id"]
    _record_side(
        workspace.parent / "lib",
        "order-service",
        build_staging(
            [
                _module(
                    "order-service",
                    features=_features(
                        {
                            "id": "a",
                            "scope": ["repos/order-service/src/**"],
                            "card_text": card("a"),
                        }
                    ),
                )
            ]
        ),
    )
    _run(workspace, "--rounds", "2")

    task = GenomeTaskStore(workspace).get(rebuild_id)
    assert task.state is GenomeTaskState.SUBMITTED, task.failure_reason
    assert not (workspace / "genome/knowledge/modules/order-service/features/b.md").exists()
    # 退役的那张要在重建报告里点名——不列的话「这次刷新删掉了哪张卡片」是静默的。
    diff = (workspace / "tasks" / rebuild_id / REBUILD_REPORT_FILE).read_text(encoding="utf-8")
    assert "会被删掉的" in diff
    assert "b.md" in diff


def test_a_human_edited_card_is_not_retired_even_when_it_stops_being_reported(
    workspace: Path,
) -> None:
    """人手改过的那张，连它的清单条目一起留着。

    删掉的话，「我改过的东西下次扫描就没了」这件事以最彻底的方式发生——文件被删，而人只会
    在需要它的时候才发现。
    """
    task_id = _plan(workspace)
    _record_side(
        workspace.parent / "lib",
        "order-service",
        build_staging(
            [
                _module(
                    "order-service",
                    features=_features(
                        {
                            "id": "a",
                            "scope": ["repos/order-service/src/**"],
                            "card_text": card("a"),
                        },
                        {
                            "id": "mine",
                            "scope": ["repos/order-service/src/**"],
                            "card_text": card("mine"),
                        },
                    ),
                )
            ]
        ),
    )
    _answer(workspace, task_id, ["order-service", "inventory-service"])
    _run(workspace, "--rounds", "2")
    card_file = workspace / "genome/knowledge/modules/order-service/features/mine.md"
    card_file.write_text(
        "---\nid: mine\n---\n<!-- human-edited -->\n我自己写的。\n", encoding="utf-8"
    )

    created = runner.invoke(
        app,
        ["genome", "reinit", "--module", "order-service", "--workspace", str(workspace), "--json"],
    )
    assert created.exit_code == 0, created.output
    _record_side(
        workspace.parent / "lib",
        "order-service",
        build_staging(
            [
                _module(
                    "order-service",
                    features=_features(
                        {
                            "id": "a",
                            "scope": ["repos/order-service/src/**"],
                            "card_text": card("a"),
                        }
                    ),
                )
            ]
        ),
    )
    _run(workspace, "--rounds", "2")

    assert "我自己写的。" in card_file.read_text(encoding="utf-8")
    assert [item.id for item in load_tree(workspace).features("order-service")] == ["a", "mine"]


def test_the_knowledge_commit_does_not_sweep_up_someone_elses_work(workspace: Path) -> None:
    """只提交这次写过的那几个文件。

    拿 `genome/**` 整个 add 的话，同一个检出里别人未提交的改动（人手改的规则、蒸馏刚写的
    经验卡片）会被一起打包进来——署名成员工、标题写着知识更新，而那不是它。
    """
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])
    mine = workspace / "genome" / "knowledge" / "lessons" / "mine.md"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("我正在写的经验卡片\n", encoding="utf-8")

    _run(workspace, "--rounds", "2")

    touched = git_out(workspace, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert touched, "知识变更没有落进版本面"
    assert "genome/knowledge/lessons/mine.md" not in touched
    # 别人的改动还在工作区里没被提交。
    assert git(workspace, "status", "--porcelain", "--", str(mine)).stdout.strip()


def test_the_knowledge_change_opens_a_pull_request_when_a_forge_is_configured(
    workspace: Path, tmp_path: Path
) -> None:
    """配了托管平台时，这次提交要推成一条分支并开一个知识 PR。

    没有这一条，「④ 走知识 PR」那半是没被验证过的——而它恰恰是详情页那张「产出的知识 PR」
    卡片唯一的真产出者。
    """
    config = yaml.safe_load((workspace / paths.ROOT_CONFIG).read_text(encoding="utf-8")) or {}
    config.setdefault("platform", {})["git_host"] = "local"
    (workspace / paths.ROOT_CONFIG).write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )
    remote = tmp_path / "ws.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    git(workspace, "add", "-A")
    git(workspace, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "chore: 本地 forge")
    git(workspace, "remote", "add", "origin", str(remote))
    git(workspace, "push", "-q", "-u", "origin", "main")

    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])
    _run(workspace, "--rounds", "2")

    events = [
        event for event in EventLog(workspace).events(task_id) if event.kind is LogKind.GENOME_PR
    ]
    assert events, "配了 forge 却没有知识 PR"
    # **只记指针**：改成了什么去那个 PR 里看，事件面不存内容。
    assert events[0].payload["asset"] == "knowledge"
    assert events[0].payload["pr"]["head"] == f"knowledge/{task_id}"


def test_a_forge_that_is_not_installed_does_not_fail_the_run(workspace: Path) -> None:
    """`gh` 没装、远端不通、分支已存在——这些都不该让一次已经写好并提交了的知识更新变成失败。

    **收着但要留痕**，否则「我的知识 PR 呢」查无可查。
    """
    git(workspace, "remote", "add", "origin", "/nowhere/at/all.git")
    task_id = _plan(workspace)
    _answer(workspace, task_id, ["order-service", "inventory-service"])

    _run(workspace, "--rounds", "2")

    task = GenomeTaskStore(workspace).get(task_id)
    assert task.state is GenomeTaskState.SUBMITTED, task.failure_reason
    notes = [
        event.payload["note"]
        for event in EventLog(workspace).events(task_id)
        if event.kind is LogKind.NOTE
    ]
    assert any("知识 PR 没开成" in note for note in notes)
