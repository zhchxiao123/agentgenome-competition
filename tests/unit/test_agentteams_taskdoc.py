"""MatrixMinio 传输的纯逻辑层:任务文档渲染/解析、状态映射、工作区差分。

一个不碰网络与磁盘的模块——这里的每个断言都不需要任何替身。
"""

from __future__ import annotations

import json
from typing import Any

from agentgenome.agents.agentteams.taskdoc import (
    build_outcome,
    diff_workspace,
    normalize_artifact_paths,
    parse_meta_status,
    render_meta,
    render_spec,
    task_ref,
)
from agentgenome.agents.agentteams.transport import TransportJob


def _job(**overrides: Any) -> TransportJob:
    fields: dict[str, Any] = {
        "task_id": "ag-1",
        "employee_id": "dev-employee",
        "procedure_ref": "code-develop@1.0.0",
        "round": 1,
        "attempt": 1,
        "subject": "",
        "context_text": "# 上下文包\n请完成任务。\n",
        "craft": "## 交作业合同\n结果写成 result.json。\n",
        "workspace": {"src/app.py": "print(1)\n"},
    }
    fields.update(overrides)
    return TransportJob(**fields)


# --- 任务引用 ---------------------------------------------------------------


def test_retries_and_subjects_get_distinct_task_refs() -> None:
    """远端目录按引用命名——撞名等于第二次尝试覆盖第一次的现场。"""
    base = task_ref(_job())
    refs = {
        base,
        task_ref(_job(attempt=2)),
        task_ref(_job(round=2)),
        task_ref(_job(subject="order-service")),
    }

    assert len(refs) == 4


def test_a_subject_with_a_slash_still_yields_a_single_path_segment() -> None:
    ref = task_ref(_job(subject="repos/order-service/src"))

    assert "/" not in ref


def test_procedures_of_the_same_round_get_distinct_refs() -> None:
    """真机发现:计划与开发同任务同轮,引用不含工序时共用远端目录——
    断点续接会把上一道工序的产物当成这一道的。"""
    plan = task_ref(_job(procedure_ref="requirement-analysis@1.0.0"))
    dev = task_ref(_job(procedure_ref="code-develop@1.0.0"))

    assert plan != dev


# --- spec.md 与 meta.json ---------------------------------------------------


def test_the_spec_carries_context_craft_and_the_delivery_convention() -> None:
    spec = render_spec(_job())

    assert "请完成任务" in spec
    assert "交作业合同" in spec
    assert "workspace/" in spec, "Worker 要知道在哪改文件"
    assert "artifacts/result.json" in spec, "Worker 要知道结果写到哪"
    assert "artifacts/staging/x" in spec, "树产物必须明确相对远端产物根交付"
    assert "绝不能写成 `artifacts/<模块或槽位>/staging/x`" in spec


def test_the_spec_defines_one_remote_output_directory() -> None:
    """本地 output_dir 的任意相对路径在远端都必须落进 artifacts/。"""
    spec = render_spec(_job())

    assert "产物目录是 `artifacts/`" in spec
    assert "`staging/lessons/x.md`" in spec
    assert "`artifacts/staging/lessons/x.md`" in spec


def test_tool_boundaries_are_stated_as_advice(  # 平台没有硬约束,如实措辞
) -> None:
    spec = render_spec(_job(tools_deny=("run_command",)))

    assert "run_command" in spec


def test_meta_renders_and_parses_round_trip() -> None:
    meta = render_meta(_job())

    status, terminal = parse_meta_status(json.dumps(meta))

    assert status == "PENDING"
    assert terminal is False


def test_running_states_are_not_mistaken_for_terminal() -> None:
    for status in ("PENDING", "RUNNING", "ACKNOWLEDGED"):
        parsed, terminal = parse_meta_status(json.dumps({"status": status}))
        assert terminal is False, f"{parsed} 被误判为终态"


def test_all_upstream_terminal_states_are_recognized() -> None:
    for status in ("SUCCESS", "SUCCESS_WITH_NOTES", "REVISION_NEEDED", "BLOCKED", "INTERRUPTED"):
        _, terminal = parse_meta_status(json.dumps({"status": status}))
        assert terminal is True, f"{status} 该是终态"


def test_garbage_meta_is_not_terminal() -> None:
    """meta.json 写了一半被读到是常态(mc 同步不是原子的)——按进行中处理,下一轮再看。"""
    status, terminal = parse_meta_status("{ 这不是 JSON")

    assert terminal is False


# --- 工作区差分 -------------------------------------------------------------


def test_diff_covers_adds_edits_and_deletes() -> None:
    pushed = {"a.py": "1\n", "b.py": "2\n", "c.py": "3\n"}
    pulled = {"a.py": "1\n", "b.py": "改了\n", "d.py": "新增\n"}

    changed = diff_workspace(pushed, pulled)

    assert changed == {"b.py": "改了\n", "d.py": "新增\n", "c.py": None}


def test_an_untouched_workspace_diffs_to_nothing() -> None:
    files = {"a.py": "1\n"}

    assert diff_workspace(files, dict(files)) == {}


def test_only_the_current_subjects_misplaced_staging_tree_is_recovered() -> None:
    artifacts = {
        "result.json": "{}",
        "sql-db/staging/project-map.yaml": "旧位置\n",
        "staging/project-map.yaml": "标准位置\n",
        "other/staging/map.yaml": "别的普通产物\n",
    }

    normalized = normalize_artifact_paths(artifacts, subject="sql-db")

    assert normalized == {
        "result.json": "{}",
        "staging/project-map.yaml": "标准位置\n",
        "other/staging/map.yaml": "别的普通产物\n",
    }


def test_task_plane_files_echoed_into_the_workspace_are_stripped() -> None:
    """真机发现:Worker 习惯把 result.md 等交付文件也复制进 workspace/。
    它们是任务面约定,不是代码改动——不滤掉的话,一个只读工序会因为
    Worker 的这个习惯被判越权。只滤顶层,嵌套的同名代码文件不受影响。"""
    from agentgenome.agents.agentteams.taskdoc import strip_task_plane

    pulled = {
        "result.md": "小结\n",
        "plan.md": "计划\n",
        "meta.json": "{}",
        "spec.md": "说明\n",
        "progress/2026-08-11.md": "进度\n",
        "artifacts/result.json": "{}",
        "src/app.py": "print(1)\n",
        "docs/result.md": "这是代码库自己的文档,要留\n",
    }

    kept = strip_task_plane(pulled)

    assert kept == {
        "src/app.py": "print(1)\n",
        "docs/result.md": "这是代码库自己的文档,要留\n",
    }


# --- outcome 装配 -----------------------------------------------------------


def test_a_success_builds_an_ok_outcome_with_changes_and_artifacts() -> None:
    outcome = build_outcome(
        status="SUCCESS_WITH_NOTES",
        result_md="干完了,注意事项见下。\n",
        artifacts={"result.json": '{"passed": true}'},
        pushed={"a.py": "1\n"},
        pulled={"a.py": "2\n"},
    )

    assert outcome.ok is True
    assert outcome.changed_files == {"a.py": "2\n"}
    assert outcome.artifacts == {"result.json": '{"passed": true}'}
    assert any("干完了" in event.get("text", "") for event in outcome.events), "result.md 进日志面"


def test_an_upstream_failure_keeps_its_reason() -> None:
    outcome = build_outcome(
        status="BLOCKED",
        result_md="缺数据库凭证,干不下去。\n",
        artifacts={},
        pushed={},
        pulled={},
    )

    assert outcome.ok is False
    assert "BLOCKED" in (outcome.detail or "")
    assert "缺数据库凭证" in (outcome.detail or "")


def test_usage_is_always_unavailable_by_source_level_fact() -> None:
    """平台无逐任务计量(见 source-analysis.md)——恒为不可得,不填 0。"""
    outcome = build_outcome(status="SUCCESS", result_md="", artifacts={}, pushed={}, pulled={})

    assert outcome.tokens_used is None


def test_two_employees_running_the_same_procedure_get_distinct_refs() -> None:
    """按员工分容器之后,撞名意味着两个不同容器往同一个远端目录里写,
    而它们互相看不见对方。"""
    arch = task_ref(_job(employee_id="arch-employee"))
    dev = task_ref(_job(employee_id="dev-employee"))

    assert arch != dev
