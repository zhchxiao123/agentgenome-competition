"""knowledge-init:架构员工读代码、补全项目地图与认知卡片。

走回放运行时 + mall 夹具——CI 里跑的是真实的 git、真实的基因组加载器、真实的
校验,唯独 Agent 那一段是确定性的。

PRD 34 之后,产出是产物目录 `staging/` 下的真实树文件 + 一张小票;Job 成败由
staging 校验裁决(验证产物,不验证自述),坏产出的报错逐文件定位。
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.genome.loader import load_project_map
from agentgenome.space.scope_guard import SCOPE_REPORT
from tests.fixtures.knowledge_staging import build_staging, card, receipt_json
from tests.fixtures.mall import materialize_mall

runner = CliRunner()

#: 架构员工产出的完整认知(交给 `build_staging` 的形状)。
MODULES: list[dict[str, Any]] = [
    {
        "id": "order-service",
        "summary": "订单域,依赖 inventory 的预占接口",
        "map": {
            "entrypoints": ["src/order/app.py"],
            "build_cmd": "make build",
            "depends_on": ["inventory-service"],
        },
        "overview": "# order-service\n\n订单域。下单时向库存申请预占。\n",
    },
    {
        "id": "inventory-service",
        "summary": "库存域,对外提供预占能力",
        "map": {"entrypoints": ["src/inventory/app.py"], "confidence": 0.8},
        "overview": "# inventory-service\n\n库存域。契约见 api/reserve.yaml。\n",
    },
]
INTERFACES = [
    {
        "id": "reserve-api",
        "kind": "http",
        "provider": "inventory-service",
        "consumers": ["order-service"],
        "schema": "repos/inventory-service/api/reserve.yaml",
        "confidence": 0.85,
        "description": "库存预占接口。下单时由订单域调用。",
    }
]
DATASTORES = [
    {
        "id": "order-db",
        "kind": "postgres",
        "owner": "order-service",
        "migrations": "repos/order-service/migrations/",
        "confidence": 0.7,
        "description": "订单表与相关索引。",
    }
]


def _staging(modules: list[dict[str, Any]] | None = None) -> dict[str, str]:
    return build_staging(modules or MODULES, interfaces=INTERFACES, datastores=DATASTORES)


def _record(
    library: Path,
    staging: dict[str, str] | None,
    round_: int = 1,
    files: dict[str, str] | None = None,
    receipt: str | None = None,
) -> None:
    """一份录制:`outputs/staging/**` + 小票 + (可选)写进工作区的文件。整目录重建。"""
    directory = library / f"arch-employee__knowledge-init__r{round_}"
    if directory.is_dir():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for relative, content in (staging or {}).items():
        target = directory / "outputs" / "staging" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (directory / "result.json").write_text(receipt or receipt_json(), encoding="utf-8")
    for relative, content in (files or {}).items():
        target = directory / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


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
    _record(tmp_path / "lib", _staging())
    return root


def _run(workspace: Path, *extra: str):
    return runner.invoke(
        app, ["knowledge", "init", "--workspace", str(workspace), "--runtime", "replay", *extra]
    )


def test_fills_in_the_project_map_skeleton(workspace: Path) -> None:
    result = _run(workspace)

    assert result.exit_code == 0, result.output
    project_map = load_project_map(workspace)
    order = project_map.module("order-service")
    assert order.lang == "python"
    assert order.test_cmd == "pytest -q"
    assert order.depends_on == ["inventory-service"]
    assert order.summary


def test_keeps_the_skeleton_identity_fields(workspace: Path) -> None:
    """id 与 path 是 init 确定性算出来的,员工不该改动它们。"""
    _run(workspace)

    project_map = load_project_map(workspace)
    assert {m.id: m.path for m in project_map.modules} == {
        "order-service": "repos/order-service/",
        "inventory-service": "repos/inventory-service/",
    }


def test_produces_cross_module_interfaces(workspace: Path) -> None:
    """interfaces 是"这次改动会不会波及别人"的唯一结构化依据,质量要求最高。"""
    _run(workspace)

    interfaces = load_project_map(workspace).interfaces
    assert [i.id for i in interfaces] == ["reserve-api"]
    assert interfaces[0].provider == "inventory-service"
    assert interfaces[0].consumers == ["order-service"]
    assert interfaces[0].schema_path == "repos/inventory-service/api/reserve.yaml"


def test_produces_datastores_with_migrations(workspace: Path) -> None:
    _run(workspace)

    datastores = load_project_map(workspace).datastores
    assert datastores[0].owner == "order-service"
    assert datastores[0].migrations == "repos/order-service/migrations/"


def test_interfaces_and_datastores_accept_a_human_readable_description(
    workspace: Path,
) -> None:
    """真实架构员工的产出里,跨模块契约与数据存储几乎总带一句说明(比如"库存预占接口")。"""
    _run(workspace)

    project_map = load_project_map(workspace)
    assert project_map.interfaces[0].description == "库存预占接口。下单时由订单域调用。"
    assert project_map.datastores[0].description == "订单表与相关索引。"


def test_writes_a_module_card_per_module_and_links_it(workspace: Path) -> None:
    _run(workspace)

    project_map = load_project_map(workspace)
    for module in project_map.modules:
        assert module.doc, f"{module.id} 没有认知卡片"
        assert (workspace / module.doc).is_file()


def test_output_passes_genome_validation(workspace: Path) -> None:
    """产出必须立刻是合法状态,包括对认知卡片的引用完整性。"""
    _run(workspace)

    result = runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)])
    assert result.exit_code == 0, result.output


def test_bumps_the_project_map_version(workspace: Path) -> None:
    before = load_project_map(workspace).version

    _run(workspace)

    assert load_project_map(workspace).version == before + 1


def test_every_conclusion_carries_a_confidence(workspace: Path) -> None:
    """不确定的认知标注而非编造。"""
    _run(workspace)

    project_map = load_project_map(workspace)
    assert all(module.confidence is not None for module in project_map.modules)
    assert all(interface.confidence is not None for interface in project_map.interfaces)


def test_a_result_without_confidence_is_rejected(workspace: Path, tmp_path: Path) -> None:
    stripped = copy.deepcopy(MODULES)
    for module in stripped:
        module.setdefault("map", {})["confidence"] = None
    _record(tmp_path / "lib", _staging(stripped))

    result = _run(workspace)

    assert result.exit_code != 0
    assert "confidence" in result.output


# --- 重跑保护 ---------------------------------------------------------------


def test_rerun_preserves_human_edited_cards(workspace: Path, tmp_path: Path) -> None:
    """我对基因组的修改不该被下一次扫描覆盖掉。"""
    _run(workspace)
    card_file = workspace / "genome/knowledge/modules/order-service/overview.md"
    card_file.write_text("<!-- human-edited -->\n# order-service\n\n这段是我手写的,别动。\n")

    second = copy.deepcopy(MODULES)
    second[0]["overview"] = "# order-service\n\nAgent 想覆盖的内容\n"
    _record(tmp_path / "lib", _staging(second), round_=2)
    _run(workspace, "--round", "2")

    assert "这段是我手写的" in card_file.read_text()


def test_rerun_updates_cards_that_were_not_hand_edited(workspace: Path, tmp_path: Path) -> None:
    _run(workspace)

    second = copy.deepcopy(MODULES)
    second[1]["overview"] = "# inventory-service\n\n第二轮补充的认知\n"
    _record(tmp_path / "lib", _staging(second), round_=2)
    _run(workspace, "--round", "2")

    card_file = workspace / "genome/knowledge/modules/inventory-service/overview.md"
    assert "第二轮补充的认知" in card_file.read_text()


def test_rerun_injects_the_existing_map_into_the_context(workspace: Path) -> None:
    """重跑时已有地图作为输入注入,员工产出的是增量而非全量覆盖。"""
    _run(workspace)

    context = next((workspace / "tasks").rglob("context-attempt-1.md"))
    assert "order-service" in context.read_text()


def test_the_context_tells_the_employee_where_staging_lives(workspace: Path) -> None:
    """产物该写到哪不能只存在于编排器的脑子里——staging 的落点必须写进提示词。"""
    _run(workspace)

    context = next((workspace / "tasks").rglob("context-attempt-1.md"))
    body = context.read_text()
    assert "staging" in body
    assert "已通过的文件不要重写" in body


# --- 写入边界 ---------------------------------------------------------------


def test_business_repos_are_not_touched(workspace: Path) -> None:
    """写入权限只属于 genome/**。业务代码由开发员工改,不是架构员工。"""
    import subprocess

    _run(workspace)

    for module_path in ("repos/order-service", "repos/inventory-service"):
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace / module_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert status == "", f"{module_path} 被改动了"


def test_an_actual_write_outside_the_genome_fails_the_job(workspace: Path, tmp_path: Path) -> None:
    """员工拿着 Write 与 Bash,想写哪儿写哪儿——只校验它声明的路径是不够的。

    这条直接让录制往业务仓里落一个文件,断言 Job 因此失败。上一版只查声明路径,
    所以这个场景是完全放行的。
    """
    _record(
        tmp_path / "lib",
        _staging(),
        files={"repos/order-service/sneaky.py": "print('我不该在这')\n"},
    )

    result = _run(workspace)

    assert result.exit_code != 0
    assert "repos/order-service/sneaky.py" in result.output


def test_the_architect_goes_through_the_same_scope_check_as_everyone_else(
    workspace: Path, tmp_path: Path
) -> None:
    """这条路径此前靠一份自己的写入校验兜底——那份没有回滚,也没有结构化报告。

    断言的是那两样东西:越权的产出被清掉,且留下了一份可审计的报告。
    """
    _record(
        tmp_path / "lib",
        _staging(),
        files={"repos/order-service/sneaky.py": "print('我不该在这')\n"},
    )

    _run(workspace)

    assert not (workspace / "repos/order-service" / "sneaky.py").exists(), "越权产出要被回滚掉"
    # 任务目录名是那条基因组任务的编号——初始化不再以一个写死的字符串当 id,
    # 那正是它此前落在研发泳道、走研发预算的原因。
    reports = list((workspace / "tasks").rglob(SCOPE_REPORT))
    assert len(reports) == 1, f"越权报告没落盘或落了多份: {reports}"
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["violations"][0]["path"] == "repos/order-service/sneaky.py"


def test_a_doc_pointer_outside_the_convention_is_rejected(
    workspace: Path, tmp_path: Path
) -> None:
    """认知卡只能落在约定位置。放开的话,一个 `genome/../../evil.md` 式的指针能把
    "只写 genome/**"那条承诺整个作废——staging 校验在作业那一步就把它拦下。"""
    rogue = _staging()
    payload = yaml.safe_load(rogue["modules/order-service/map.yaml"])
    payload["doc"] = "repos/order-service/sneaky.md"
    rogue["modules/order-service/map.yaml"] = yaml.safe_dump(payload, allow_unicode=True)
    _record(tmp_path / "lib", rogue)

    result = _run(workspace)

    assert result.exit_code != 0
    assert "doc" in result.output
    assert not (workspace / "repos/order-service/sneaky.md").exists()


def test_unknown_module_in_the_result_is_rejected(workspace: Path, tmp_path: Path) -> None:
    """员工不能凭空发明模块——id 与 path 是 init 确定性算出来的。"""
    invented = _staging()
    invented["modules/ghost-service/map.yaml"] = yaml.safe_dump(
        {"id": "ghost-service", "confidence": 0.5}, allow_unicode=True
    )
    _record(tmp_path / "lib", invented)

    result = _run(workspace)

    assert result.exit_code != 0
    assert "ghost-service" in result.output


def test_a_missing_recording_surfaces_clearly(workspace: Path, tmp_path: Path) -> None:
    shutil.rmtree(tmp_path / "lib")

    result = _run(workspace)

    assert result.exit_code != 0
    assert "knowledge-init" in result.output


def test_an_empty_output_fails_and_says_no_product(workspace: Path, tmp_path: Path) -> None:
    """员工什么都没写(plan mode 空跑)→ staging 缺失 → 判失败,理由指向"无产物"。"""
    directory = tmp_path / "lib" / "arch-employee__knowledge-init__r1"
    shutil.rmtree(directory)
    directory.mkdir(parents=True)

    result = _run(workspace)

    assert result.exit_code != 0
    assert "没有产物" in result.output


def test_the_run_is_recorded_under_the_task_directory(workspace: Path) -> None:
    """产物与上下文包落盘,"当时它看到了什么"可以被回答。"""
    _run(workspace)

    artifacts = list((workspace / "tasks").rglob("result.json"))
    assert artifacts, "产物没有落盘"
    raw = yaml.safe_load((workspace / "genome/knowledge/project-map.yaml").read_text())
    assert raw["updated_at"], "知识更新要留下时间戳"


# --- 覆盖范围的路径基准 ---------------------------------------------------------
#
# 员工写了模块相对的路径时,校验按 Workspace 相对去找会全部报"路径不存在"——而真实跑一次
# 的教训是:整次产出因此作废,用户手上什么都不剩。补全是机器一查就知道答案的事。


def _with_features(scope_order: list[str], scope_inventory: list[str]) -> dict[str, str]:
    modules = copy.deepcopy(MODULES)
    modules[0]["features"] = [
        {
            "id": "order-placement",
            "summary": "下单",
            "scope": scope_order,
            "card_text": card("order-placement", "下单时序。"),
        }
    ]
    modules[1]["features"] = [
        {
            "id": "stock-reserve",
            "summary": "库存预占",
            "scope": scope_inventory,
            "card_text": card("stock-reserve", "预占时序。"),
        }
    ]
    return _staging(modules)


def _scope_of(workspace: Path, module_id: str, feature_id: str) -> list[str]:
    raw = yaml.safe_load(
        (workspace / "genome" / "knowledge" / "modules" / module_id / "map.yaml").read_text(
            encoding="utf-8"
        )
    )
    return next(item["scope"] for item in raw["features"] if item["id"] == feature_id)


def test_a_module_relative_scope_is_completed_instead_of_thrown_away(
    workspace: Path, tmp_path: Path
) -> None:
    """架构员工在深读单个模块,自然写 `src/order/app.py`;而 `scope` 是 Workspace 相对的。

    让它同时记住两套基准本来就不合理——同一份 YAML 里 `card` 还是模块相对的。
    "这条路径在模块目录底下存在"这种机器一查就知道的事,不该让它重跑。
    """
    _record(tmp_path / "lib", _with_features(["src/order/app.py"], ["src/inventory/app.py"]))

    result = _run(workspace)

    assert result.exit_code == 0, result.output
    assert _scope_of(workspace, "order-service", "order-placement") == [
        "repos/order-service/src/order/app.py"
    ]
    assert _scope_of(workspace, "inventory-service", "stock-reserve") == [
        "repos/inventory-service/src/inventory/app.py"
    ]


def test_an_already_correct_scope_is_left_alone(workspace: Path, tmp_path: Path) -> None:
    """已经是 Workspace 相对的不许动——改它反而会指错地方。"""
    _record(
        tmp_path / "lib",
        _with_features(
            ["repos/order-service/src/order/**"], ["repos/inventory-service/src/inventory/**"]
        ),
    )

    result = _run(workspace)

    assert result.exit_code == 0, result.output
    assert _scope_of(workspace, "order-service", "order-placement") == [
        "repos/order-service/src/order/**"
    ]


def test_a_path_that_exists_nowhere_still_fails_and_says_so(
    workspace: Path, tmp_path: Path
) -> None:
    """补前缀是纠正笔误,不是放行编造的路径。真找不到的仍然要拦。"""
    _record(tmp_path / "lib", _with_features(["src/nowhere/ghost.py"], ["src/inventory/app.py"]))

    result = _run(workspace)

    assert result.exit_code != 0
    assert "src/nowhere/ghost.py" in result.output


def test_an_unfixable_path_points_at_the_right_form_when_it_can(
    workspace: Path, tmp_path: Path
) -> None:
    """**「路径不存在」这句话指导不了任何人。**

    这里构造的是补全兜不住的情形,但报错至少要把没过的那条路径原样说出来,
    并且**逐文件定位到写它的那份地图**——回注给下一次尝试时它就是要修的对象。
    """
    staging = _with_features(["src/order/app.py", "srcx/order/app.py"], ["src/inventory/app.py"])
    _record(tmp_path / "lib", staging)

    result = _run(workspace)

    assert result.exit_code != 0
    assert "srcx/order/app.py" in result.output
    assert "modules/order-service/map.yaml" in result.output


# --- 人工定稿的功能点 ---------------------------------------------------------
#
# **`HUMAN_EDITED_MARKER` 只护得住卡片正文,护不住清单条目本身。** 人手把 `scope`
# 改对之后,只要架构员工重跑时还报告同一个 id,这条修复就会被原样覆盖——这正是
# "同一条路径不存在的报错一直没好"这类问题的根源:人的修复从没持久过。
# `human_edited: true` 锁的是整条 entry,不靠卡片文件里有没有标记。


def _lock_feature(workspace: Path, module_id: str, feature_id: str, **overrides: Any) -> None:
    map_file = workspace / "genome" / "knowledge" / "modules" / module_id / "map.yaml"
    payload = yaml.safe_load(map_file.read_text(encoding="utf-8"))
    for feature in payload["features"]:
        if feature["id"] == feature_id:
            feature["human_edited"] = True
            feature.update(overrides)
    map_file.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def test_a_locked_feature_ignores_a_differently_scoped_rerun(
    workspace: Path, tmp_path: Path
) -> None:
    """锁定之后,员工这一轮报了什么都不重要——scope 与 summary 原样留着。"""
    _record(tmp_path / "lib", _with_features(["src/order/app.py"], ["src/inventory/app.py"]))
    _run(workspace)
    _lock_feature(
        workspace,
        "order-service",
        "order-placement",
        scope=["repos/order-service/migrations/"],
        summary="人工定的稿",
    )

    second = _with_features(["src/order/app.py"], ["src/inventory/app.py"])
    payload = yaml.safe_load(second["modules/order-service/map.yaml"])
    payload["features"][0]["summary"] = "员工这次想改的摘要"
    second["modules/order-service/map.yaml"] = yaml.safe_dump(payload, allow_unicode=True)
    _record(tmp_path / "lib", second, round_=2)
    result = _run(workspace, "--round", "2")

    assert result.exit_code == 0, result.output
    assert _scope_of(workspace, "order-service", "order-placement") == [
        "repos/order-service/migrations/"
    ]
    raw = yaml.safe_load(
        (workspace / "genome/knowledge/modules/order-service/map.yaml").read_text()
    )
    locked = next(item for item in raw["features"] if item["id"] == "order-placement")
    assert locked["summary"] == "人工定的稿"


def test_a_locked_feature_is_not_retired_when_the_rerun_stops_reporting_it(
    workspace: Path, tmp_path: Path
) -> None:
    """锁定的功能点不再被员工提起时,也不该被当成孤儿清理掉。"""
    _record(tmp_path / "lib", _with_features(["src/order/app.py"], ["src/inventory/app.py"]))
    _run(workspace)
    _lock_feature(workspace, "order-service", "order-placement")

    second = _with_features(["src/order/app.py"], ["src/inventory/app.py"])
    payload = yaml.safe_load(second["modules/order-service/map.yaml"])
    payload["features"] = []
    second["modules/order-service/map.yaml"] = yaml.safe_dump(payload, allow_unicode=True)
    del second["modules/order-service/features/order-placement.md"]
    _record(tmp_path / "lib", second, round_=2)
    result = _run(workspace, "--round", "2")

    assert result.exit_code == 0, result.output
    raw = yaml.safe_load(
        (workspace / "genome/knowledge/modules/order-service/map.yaml").read_text()
    )
    assert [item["id"] for item in raw["features"]] == ["order-placement"]
