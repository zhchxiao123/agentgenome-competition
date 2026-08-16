"""staging 通道:校验器、逐文件报错与原子应用(PRD 34)。

这一组钉的是「验证产物,不验证自述」的确定性那一半:

- 校验器对树片段跑的是**树自己的规则**(FeatureEntry / FeatureCard / 预算),不为
  staging 放松半分,也不另发明一套 schema;
- 报错**逐文件定位**——它是回注给下一次尝试的上下文,含糊的"格式不对"只会让第二次
  错得一模一样;
- 应用是原子的:校验未全绿,`genome/knowledge/` 一个字节不变。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from agentgenome import paths
from agentgenome.config import KnowledgeConfig
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.loader import load_tree
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.staging import (
    STAGING_DIR,
    apply_staged,
    knowledge_output_check,
    load_knowledge_staging,
    read_receipt,
    render_issues,
    validate_knowledge_staging,
)
from agentgenome.genome.tree import MODULE_MAP, write_tree

BASE = {
    "version": 1,
    "project": {"name": "mall"},
    "modules": [
        {"id": "order-service", "path": "repos/order-service/"},
        {"id": "inventory-service", "path": "repos/inventory-service/"},
    ],
}

CARD = """\
---
id: {id}
confidence: high
---

{id} 的时序与坑点。
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / paths.KNOWLEDGE).mkdir(parents=True)
    for repo in ("order-service", "inventory-service"):
        (tmp_path / "repos" / repo / "src").mkdir(parents=True)
    write_tree(tmp_path, ProjectMap.model_validate(BASE))
    return tmp_path


def _staging(workspace: Path) -> Path:
    target = workspace / "tasks" / "gn-1" / "artifacts" / STAGING_DIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write(staging: Path, relative: str, text: str) -> Path:
    target = staging / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _map_yaml(module_id: str, *features: dict[str, object], **extra: object) -> str:
    payload: dict[str, object] = {
        "id": module_id,
        "lang": "python",
        "test_cmd": "pytest -q",
        "confidence": 0.9,
        "features": list(features),
        **extra,
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _feature(feature_id: str, **extra: object) -> dict[str, object]:
    return {
        "id": feature_id,
        "summary": feature_id,
        "scope": ["repos/order-service/src/**"],
        **extra,
    }


def _stage_minimal(workspace: Path) -> Path:
    """一份全绿的最小片段:一个模块、一张卡、一条 no_card、认知卡与根补丁。"""
    staging = _staging(workspace)
    _write(
        staging,
        "modules/order-service/map.yaml",
        _map_yaml(
            "order-service",
            _feature("place-order", card="features/place-order.md"),
            _feature("migrations", no_card="一串迁移脚本,读代码比读卡片快"),
            doc="genome/knowledge/modules/order-service/overview.md",
        ),
    )
    _write(staging, "modules/order-service/overview.md", "# order-service\n\n下单。\n")
    _write(
        staging, "modules/order-service/features/place-order.md", CARD.format(id="place-order")
    )
    _write(
        staging,
        "project-map.yaml",
        yaml.safe_dump(
            {
                "modules": [
                    {
                        "id": "order-service",
                        "path": "repos/order-service/",
                        "summary": "订单服务",
                        "map": "modules/order-service/map.yaml",
                    }
                ]
            }
        ),
    )
    return staging


# --- 校验器 --------------------------------------------------------------------


def test_a_missing_staging_tree_is_reported_as_no_output(workspace: Path) -> None:
    """没有 staging 树就是没有产物,理由指向"无产物"。

    与 `contract.py` 里 "plan mode 退出码 0 无产物" 是同一课:自述写得再好,
    产物不存在这个 Job 就没成。
    """
    issues = validate_knowledge_staging(workspace, _staging(workspace).parent / "nowhere")

    assert len(issues) == 1
    assert "没有产物" in issues[0].message


def test_a_minimal_valid_fragment_passes_and_loads(workspace: Path) -> None:
    staging = _stage_minimal(workspace)

    assert validate_knowledge_staging(workspace, staging) == []
    staged = load_knowledge_staging(workspace, staging)
    (module,) = staged.modules
    assert module.id == "order-service"
    assert module.summary == "订单服务"
    assert module.fields["test_cmd"] == "pytest -q"
    assert module.doc_text is not None and "下单" in module.doc_text
    assert module.features is not None
    by_id = {item.id: item for item in module.features}
    assert by_id["place-order"].card_text is not None
    assert by_id["migrations"].no_card


def test_a_broken_card_is_named_file_by_file_and_nothing_else_is(workspace: Path) -> None:
    """坏一张卡,报错只点名那一个文件。

    这是 PRD 34 的核心经济性:回注"哪个文件坏了",第二次尝试只修它——回注 JSON path
    的话,重试只能全量重新生成。(夹具用"缺 front matter"构造坏卡;PRD 32 的 kind 等
    分类学规则落地后会走同一条通道。)
    """
    staging = _stage_minimal(workspace)
    _write(
        staging,
        "modules/order-service/map.yaml",
        _map_yaml(
            "order-service",
            _feature("place-order", card="features/place-order.md"),
            _feature("reserve-flow", card="features/reserve-flow.md"),
            doc="genome/knowledge/modules/order-service/overview.md",
        ),
    )
    _write(staging, "modules/order-service/features/reserve-flow.md", "没有 front matter 的卡\n")

    issues = validate_knowledge_staging(workspace, staging)

    assert len(issues) == 1
    assert issues[0].file == f"{STAGING_DIR}/modules/order-service/features/reserve-flow.md"
    rendered = render_issues(issues)
    assert "reserve-flow.md" in rendered
    assert "place-order" not in rendered
    assert "已通过的文件不要重写" in rendered


def test_an_invented_module_is_refused(workspace: Path) -> None:
    """模块由 init 确定,员工不该发明——凭空的模块会让下游影响判定失去依据。"""
    staging = _staging(workspace)
    _write(staging, "modules/nobody-knows-me/map.yaml", _map_yaml("nobody-knows-me"))

    issues = validate_knowledge_staging(workspace, staging)

    assert any("未知模块" in item.message for item in issues)


def test_focus_refuses_other_modules_and_requires_its_own(workspace: Path) -> None:
    """这一趟只读 focus:多写的拒绝,没写的点名——静默覆盖与静默缺席都不允许。"""
    staging = _staging(workspace)
    _write(staging, "modules/order-service/map.yaml", _map_yaml("order-service"))

    issues = validate_knowledge_staging(workspace, staging, focus="inventory-service")

    messages = "\n".join(item.message for item in issues)
    assert "只读 inventory-service" in messages
    assert "没有它的树片段" in messages


def test_a_feature_with_no_knowledge_at_all_is_refused(workspace: Path) -> None:
    """卡片与「无需卡片」都不给,这个模块的知识树不完备(ADR-0003)。"""
    staging = _staging(workspace)
    _write(
        staging,
        "modules/order-service/map.yaml",
        _map_yaml("order-service", _feature("ghost")),
    )

    issues = validate_knowledge_staging(workspace, staging)

    assert any("缺口" in item.message or "no_card" in item.message for item in issues)


def test_a_scope_that_points_nowhere_is_refused_with_a_hint(workspace: Path) -> None:
    staging = _staging(workspace)
    _write(
        staging,
        "modules/order-service/map.yaml",
        _map_yaml(
            "order-service",
            _feature("ghost", scope=["no/such/dir/**"], no_card="不需要"),
        ),
    )

    issues = validate_knowledge_staging(workspace, staging)

    assert any("覆盖的代码路径不存在" in item.message for item in issues)


def test_a_scope_missing_the_module_prefix_is_normalized_not_refused(workspace: Path) -> None:
    """员工在深读单个模块,自然会写模块相对路径——能算出来的不该让它重跑。"""
    staging = _staging(workspace)
    _write(
        staging,
        "modules/order-service/map.yaml",
        _map_yaml("order-service", _feature("src-stuff", scope=["src/**"], no_card="不需要")),
    )

    assert validate_knowledge_staging(workspace, staging) == []
    staged = load_knowledge_staging(workspace, staging)
    (module,) = staged.modules
    assert module.features is not None
    assert module.features[0].scope == ("repos/order-service/src/**",)


def test_a_card_citing_a_missing_path_is_refused_file_by_file(workspace: Path) -> None:
    """证据锚点必真(PRD 41):卡片正文引用的仓内路径不存在,逐文件点名卡片与缺失路径。

    行号允许漂移,存在性不允许——所以断言的是"路径被点名",不是行号。
    """
    staging = _stage_minimal(workspace)
    _write(
        staging,
        "modules/order-service/features/place-order.md",
        "---\nid: place-order\nconfidence: high\n---\n\n"
        "- 不变式:`src/no-such-file.py:12` 的余额不为负。\n",
    )

    issues = validate_knowledge_staging(workspace, staging)

    assert len(issues) == 1
    assert issues[0].file == f"{STAGING_DIR}/modules/order-service/features/place-order.md"
    assert "src/no-such-file.py" in issues[0].message
    assert "引用的仓内路径不存在" in issues[0].message


def test_true_anchors_pass_under_both_bases_and_both_anchor_forms(workspace: Path) -> None:
    """锚点基准与 scope 同一套:Workspace 相对或模块相对;`路径:行号` 与 `路径::符号` 都合法。"""
    (workspace / "repos" / "order-service" / "src" / "app.py").write_text(
        "def place():\n    pass\n", encoding="utf-8"
    )
    staging = _stage_minimal(workspace)
    _write(
        staging,
        "modules/order-service/features/place-order.md",
        "---\nid: place-order\nconfidence: high\n---\n\n"
        "- 不变式:`src/app.py:1` 只在 `repos/order-service/src/app.py::place` 里改余额。\n",
    )

    assert validate_knowledge_staging(workspace, staging) == []


def test_anchor_check_skips_fences_globs_and_bare_identifiers(workspace: Path) -> None:
    """只查像文件引用的行内代码:围栏里的示例、glob、不带锚点的裸文件名都不是引用。"""
    staging = _stage_minimal(workspace)
    _write(
        staging,
        "modules/order-service/features/place-order.md",
        "---\nid: place-order\nconfidence: high\n---\n\n"
        "- `TaskStore` 是事实源;scope 是 `repos/order-service/src/**`;跑 `pytest -q`。\n"
        "- `task.json` 只是快照——提到运行态文件名的散文不算引用。\n\n"
        "```yaml\nentrypoints: [src/imaginary/example.py]\n```\n",
    )

    assert validate_knowledge_staging(workspace, staging) == []


def test_a_bare_filename_with_an_anchor_is_a_citation_and_must_exist(workspace: Path) -> None:
    """纪律教的"模块目录相对"允许指到模块根——`runner.py:12` 是引用,查存在性;
    教什么就查什么(ADR-0011),否则合法写法恰好逃过门禁。"""
    staging = _stage_minimal(workspace)
    _write(
        staging,
        "modules/order-service/features/place-order.md",
        "---\nid: place-order\nconfidence: high\n---\n\n"
        "- 不变式:`ghost.py:12` 恒真。\n",
    )

    issues = validate_knowledge_staging(workspace, staging)

    assert len(issues) == 1
    assert "ghost.py" in issues[0].message

    # 同样的形状,文件真的在模块根时是合法引用。
    (workspace / "repos/order-service/ghost.py").write_text("x = 1\n", encoding="utf-8")
    assert validate_knowledge_staging(workspace, staging) == []


def test_a_broken_anchor_repairs_incrementally(workspace: Path) -> None:
    """锚点失败走同一条增量修复路:只有坏卡被点名,修好它之后其余文件逐字节没动过。"""
    good_card = "modules/order-service/overview.md"
    staging = _stage_minimal(workspace)
    _write(
        staging,
        "modules/order-service/features/place-order.md",
        "---\nid: place-order\nconfidence: high\n---\n\n- `src/gone.py:3` 恒真。\n",
    )
    before = hashlib.sha256((staging / good_card).read_bytes()).hexdigest()

    issues = validate_knowledge_staging(workspace, staging)
    assert [item.file for item in issues] == [
        f"{STAGING_DIR}/modules/order-service/features/place-order.md"
    ]

    # 增量修复:只重写被点名的那一个文件。
    _write(
        staging,
        "modules/order-service/features/place-order.md",
        CARD.format(id="place-order"),
    )

    assert validate_knowledge_staging(workspace, staging) == []
    assert hashlib.sha256((staging / good_card).read_bytes()).hexdigest() == before


def test_an_orphan_staged_card_is_refused(workspace: Path) -> None:
    """staging 里没人指向的卡片要报——静默丢掉的话,员工以为交付了的知识没进树。"""
    staging = _staging(workspace)
    _write(staging, "modules/order-service/map.yaml", _map_yaml("order-service"))
    _write(staging, "modules/order-service/features/stray.md", CARD.format(id="stray"))

    issues = validate_knowledge_staging(workspace, staging)

    assert any("孤儿卡片" in item.message for item in issues)


def test_an_employee_cannot_lock_its_own_output(workspace: Path) -> None:
    """`human_edited` 是人工定稿的标记。员工能自己置位的话,这道锁就不存在了。"""
    staging = _staging(workspace)
    _write(
        staging,
        "modules/order-service/map.yaml",
        _map_yaml(
            "order-service", _feature("mine", no_card="不需要", human_edited=True)
        ),
    )

    issues = validate_knowledge_staging(workspace, staging)

    assert any("human_edited" in item.message for item in issues)


def test_an_unrecognized_file_is_refused_not_skipped(workspace: Path) -> None:
    staging = _stage_minimal(workspace)
    _write(staging, "notes.md", "随手写的\n")

    issues = validate_knowledge_staging(workspace, staging)

    assert any("不认识的文件" in item.message for item in issues)


def test_a_card_over_the_line_budget_is_refused_in_staging(workspace: Path) -> None:
    """预算在 staging 就查。等合进树再查的话,回滚发生在整棵树写完之后——白烧一轮。"""
    staging = _stage_minimal(workspace)
    _write(
        staging,
        "modules/order-service/features/place-order.md",
        CARD.format(id="place-order") + "填充。\n" * 300,
    )

    issues = validate_knowledge_staging(
        workspace, staging, limits=KnowledgeConfig(card_lines=100)
    )

    assert any("超出行数预算" in item.message for item in issues)


def test_the_root_patch_cannot_move_a_module(workspace: Path) -> None:
    """id 与 path 不可变:它们是 init 算出来的事实,不是员工的判断。"""
    staging = _stage_minimal(workspace)
    _write(
        staging,
        "project-map.yaml",
        yaml.safe_dump(
            {
                "modules": [
                    {
                        "id": "order-service",
                        "path": "somewhere/else/",
                        "summary": "订单服务",
                        "map": "modules/order-service/map.yaml",
                    }
                ]
            }
        ),
    )

    issues = validate_knowledge_staging(workspace, staging)

    assert any("path 不可变" in item.message for item in issues)


def test_contracts_must_carry_confidence(workspace: Path) -> None:
    staging = _stage_minimal(workspace)
    _write(
        staging,
        "interfaces.yaml",
        yaml.safe_dump(
            {
                "interfaces": [
                    {"id": "reserve-api", "kind": "http", "provider": "inventory-service"}
                ]
            }
        ),
    )

    issues = validate_knowledge_staging(workspace, staging)

    assert any("缺 confidence" in item.message for item in issues)


def test_a_locked_feature_report_is_ignored_not_validated(workspace: Path) -> None:
    """人工定稿(`human_edited: true`)的 id,员工的报告整条忽略——不必合规,反正不落盘。"""
    target = workspace / paths.MODULES / "order-service" / MODULE_MAP
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["features"] = [
        {
            "id": "mine",
            "scope": ["repos/order-service/src/**"],
            "no_card": "人定的",
            "human_edited": True,
        }
    ]
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    staging = _staging(workspace)
    _write(
        staging,
        "modules/order-service/map.yaml",
        # 员工对锁定 id 的报告缺 scope、缺卡片——两条都不该被报出来。
        _map_yaml("order-service", {"id": "mine"}),
    )

    assert validate_knowledge_staging(workspace, staging) == []
    staged = load_knowledge_staging(workspace, staging)
    (module,) = staged.modules
    assert module.features == ()


def test_the_output_check_closure_renders_per_file_errors(workspace: Path) -> None:
    """给 `JobSpec.output_check` 的闭包:通过给 `None`,失败给逐文件清单。"""
    check = knowledge_output_check(workspace)
    output_dir = workspace / "tasks" / "gn-1" / "artifacts"

    detail = check(output_dir)
    assert detail is not None and "没有产物" in detail

    _stage_minimal(workspace)
    assert check(output_dir) is None


# --- 原子应用 ------------------------------------------------------------------


def _tree_bytes(workspace: Path) -> dict[str, str]:
    base = workspace / paths.KNOWLEDGE
    return {
        str(item.relative_to(base)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(base.rglob("*"))
        if item.is_file()
    }


def test_apply_writes_the_fragment_into_the_tree(workspace: Path) -> None:
    staging = _stage_minimal(workspace)
    staged = load_knowledge_staging(workspace, staging)

    update = apply_staged(workspace, staged)

    tree = load_tree(workspace)
    assert update.version == 2
    module = tree.project_map.module("order-service")
    assert module.summary == "订单服务"
    assert module.test_cmd == "pytest -q"
    assert [item.id for item in tree.features("order-service")] == ["place-order", "migrations"]
    assert tree.card("order-service", "place-order") is not None
    card = workspace / paths.MODULES / "order-service" / "features" / "place-order.md"
    assert str(card.relative_to(workspace)) in update.cards_written


def test_a_failed_apply_restores_the_tree_byte_for_byte(workspace: Path) -> None:
    """staging 校验绿、整树校验红(合并后超预算)→ 一个字节都不能变。

    这是「半更新树不可能」那条验收硬条件:留在半更新状态的话,下一次写入在第一行的
    `load_tree` 就失败,一次被拒的产出把整条知识路径永久卡死。
    """
    staging = _stage_minimal(workspace)
    staged = load_knowledge_staging(workspace, staging)
    before = _tree_bytes(workspace)

    with pytest.raises(GenomeValidationError):
        # 校验时用宽松预算、应用时用苛刻预算,模拟"片段自身合法、合并后整树不合法"。
        apply_staged(workspace, staged, KnowledgeConfig(module_map_lines=1))

    assert _tree_bytes(workspace) == before


def test_apply_does_not_overwrite_a_human_edited_card(workspace: Path) -> None:
    staging = _stage_minimal(workspace)
    staged = load_knowledge_staging(workspace, staging)
    apply_staged(workspace, staged)
    card = workspace / paths.MODULES / "order-service" / "features" / "place-order.md"
    card.write_text(
        "---\nid: place-order\n---\n<!-- human-edited -->\n我自己写的。\n", encoding="utf-8"
    )

    update = apply_staged(workspace, staged)

    assert "我自己写的。" in card.read_text(encoding="utf-8")
    assert str(card.relative_to(workspace)) in update.cards_preserved


def test_apply_keeps_a_locked_entry_even_when_it_is_not_reported(workspace: Path) -> None:
    """锁定 entry 连 `scope` 都不换,员工这一轮没报它也不丢——锁不住的定稿不是定稿。"""
    target = workspace / paths.MODULES / "order-service" / MODULE_MAP
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    locked = {
        "id": "mine",
        "summary": "人定的",
        "scope": ["repos/order-service/src/**"],
        "no_card": "人定的",
        "human_edited": True,
    }
    payload["features"] = [locked]
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    staging = _stage_minimal(workspace)
    staged = load_knowledge_staging(workspace, staging)

    apply_staged(workspace, staged)

    features = {item.id: item for item in load_tree(workspace).features("order-service")}
    assert "mine" in features
    assert features["mine"].human_edited
    assert features["mine"].scope == ["repos/order-service/src/**"]


def test_read_receipt_swallows_garbage(workspace: Path, tmp_path: Path) -> None:
    """小票只补充校对清单,不参与裁决——坏了不该炸汇总。"""
    target = tmp_path / "receipt.json"
    target.write_text("{broken", encoding="utf-8")

    assert read_receipt(target) == {}
    assert read_receipt(tmp_path / "missing.json") == {}


# --- 增量修复(契约重试 × staging,PRD 34 D3) ----------------------------------


async def test_a_broken_card_is_repaired_incrementally_on_retry(workspace: Path) -> None:
    """坏 1 张卡 → 首次校验失败,拒绝原因逐文件点名仅此文件 → 重试只修它 → 通过。

    这是本次契约改造最大的经济性收益:原子失败变局部失败,重试成本从"全量重生成"
    降为"修几个文件"。断言分三层:真的重试了、回注只点名坏文件、其余文件内容没变。
    """
    import json as json_mod
    import sys

    from agentgenome.agents.runtime import JobSpec
    from agentgenome.agents.subprocess_runtime import SubprocessRuntime
    from agentgenome.genome.staging import RECEIPT_SCHEMA
    from tests.fixtures import fake_agent
    from tests.fixtures.fake_agent import SCRIPT_ENV

    output_dir = workspace / "tasks" / "gn-1" / "artifacts"
    good_card = CARD.format(id="place-order")
    staged = {
        "staging/modules/order-service/map.yaml": _map_yaml(
            "order-service",
            _feature("place-order", card="features/place-order.md"),
            _feature("reserve-flow", card="features/reserve-flow.md"),
            doc="genome/knowledge/modules/order-service/overview.md",
        ),
        "staging/modules/order-service/overview.md": "# order-service\n\n下单。\n",
        "staging/modules/order-service/features/place-order.md": good_card,
        # 坏的那一张:没有 front matter。
        "staging/modules/order-service/features/reserve-flow.md": "裸正文,没有 front matter\n",
        "result.json": json_mod.dumps({"task_id": "gn-1", "producer": "arch-employee"}),
    }
    script = {
        "workdir": str(workspace),
        "output_dir": str(output_dir),
        "output_files": staged,
        "retry_output_files": {
            "staging/modules/order-service/features/reserve-flow.md": CARD.format(
                id="reserve-flow"
            ),
        },
    }
    context = workspace / "context.md"
    context.write_text("# knowledge-init\n", encoding="utf-8")
    spec = JobSpec(
        task_id="gn-1",
        employee_id="arch-employee",
        procedure_id="knowledge-init",
        procedure_version="2.0.0",
        round=1,
        subject="order-service",
        workdir=workspace,
        context_file=context,
        output_dir=output_dir,
        output_schema=RECEIPT_SCHEMA,
        output_check=knowledge_output_check(workspace, focus="order-service"),
        contract_retries=1,
        timeout_s=20,
        max_tokens=1_000_000,
        credentials={SCRIPT_ENV: json_mod.dumps(script, ensure_ascii=False)},
    )

    result = await SubprocessRuntime(argv=[sys.executable, fake_agent.__file__]).run_job(spec)

    assert result.ok is True, result.failure_detail
    assert result.attempts == 2
    # 回注的拒绝原因只点名坏的那一个文件。
    retry_context = (output_dir / "context-attempt-2.md").read_text(encoding="utf-8")
    assert "reserve-flow.md" in retry_context
    assert "place-order.md" not in retry_context
    # 其余 staging 文件内容原封不动(哈希相等的等价断言)。
    survived = output_dir / "staging/modules/order-service/features/place-order.md"
    assert survived.read_text(encoding="utf-8") == good_card


# --- 蒸馏的 staging(lessons,PRD 34 D6) ----------------------------------------


LESSON = """\
---
title: 迁移目录改动要走人工审批
level: L2
applies_to:
  modules: [order-service]
evidence:
  - task_id: ag-1
    path: artifacts/x.json
confidence: 0.6
---

迁移目录的改动应该强制走人工审批。
"""


def test_staged_lessons_load_into_candidates(tmp_path: Path) -> None:
    """staged 卡片装载成 `parse_cards` 吃的候选形状——搬运源是已验证文件,不是解包 JSON。"""
    from agentgenome.genome.staging import load_lesson_candidates, validate_lessons_staging

    staging = tmp_path / STAGING_DIR
    (staging / "lessons").mkdir(parents=True)
    (staging / "lessons" / "migration-review.md").write_text(LESSON, encoding="utf-8")

    assert validate_lessons_staging(staging) == []
    (candidate,) = load_lesson_candidates(staging)
    assert candidate["title"] == "迁移目录改动要走人工审批"
    assert candidate["level"] == "L2"
    assert candidate["conclusion"].startswith("迁移目录的改动")
    assert candidate["evidence"][0]["task_id"] == "ag-1"


def test_zero_lessons_is_a_valid_distill_output(tmp_path: Path) -> None:
    """"一次任务提炼出零条经验"是正常结果(宁可少写)——与知识树的"无产物即失败"刻意不同。"""
    from agentgenome.genome.staging import load_lesson_candidates, validate_lessons_staging

    assert validate_lessons_staging(tmp_path / STAGING_DIR) == []
    assert load_lesson_candidates(tmp_path / STAGING_DIR) == []


def test_a_broken_lesson_is_named_file_by_file(tmp_path: Path) -> None:
    from agentgenome.genome.staging import validate_lessons_staging

    staging = tmp_path / STAGING_DIR
    (staging / "lessons").mkdir(parents=True)
    (staging / "lessons" / "good.md").write_text(LESSON, encoding="utf-8")
    (staging / "lessons" / "bad.md").write_text("没有 front matter\n", encoding="utf-8")

    issues = validate_lessons_staging(staging)

    assert len(issues) == 1
    assert issues[0].file == f"{STAGING_DIR}/lessons/bad.md"


def test_an_unknown_level_is_refused_with_the_allowed_values(tmp_path: Path) -> None:
    from agentgenome.genome.staging import validate_lessons_staging

    staging = tmp_path / STAGING_DIR
    (staging / "lessons").mkdir(parents=True)
    (staging / "lessons" / "x.md").write_text(
        LESSON.replace("level: L2", "level: L9"), encoding="utf-8"
    )

    issues = validate_lessons_staging(staging)

    assert any("level 不认识" in item.message and "L3a" in item.message for item in issues)


def test_the_distill_procedure_declares_lessons_staging(tmp_path: Path) -> None:
    """`outputs.staging: lessons` 是裁决的开关——派发方按它给 Job 挂确定性校验。"""
    from agentgenome.genome.procedures import load_procedure
    from agentgenome.genome.roster import scaffold_roster
    from agentgenome.genome.staging import RECEIPT_SCHEMA, output_check_for

    scaffold_roster(tmp_path)
    spec = load_procedure(tmp_path / paths.PROCEDURES / "experience-distill")

    assert spec.outputs.staging == "lessons"
    # 小票与知识类工序共用同一份 schema——不另造第二种"小而平"。
    assert spec.output_schema == RECEIPT_SCHEMA
    check = output_check_for(spec.outputs.staging, tmp_path)
    assert check is not None
    assert check(tmp_path / "empty-out") is None
