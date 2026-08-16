"""Procedure 规格与校验:目录进,模型或可读错误出。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.procedures import ProcedureKind, load_procedure

VALID = """\
id: unit-gate
version: 1.2.0
summary: 运行单元测试/静态检查/构建并输出结构化结果
kind: deterministic
trigger:
  states: [UNIT_TESTING]
inputs:
  schema:
    type: object
    properties:
      module_id: {type: string}
outputs:
  artifacts: [gate-report.json]
  schema_ref: schemas/gate-report.schema.json
tools:
  required_cmds: [pytest, ruff]
  mcp: []
failure:
  retry: 1
  on_fail: gate_fail
  escalate_after: 3
compat:
  runtimes: [none]
"""


def _procedure(
    tmp_path: Path,
    manifest: str = VALID,
    name: str = "unit-gate",
    *,
    prompt: str | None = None,
    entry: str | None = "run.py",
    schema: bool = True,
) -> Path:
    directory = tmp_path / "procedures" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "procedure.yaml").write_text(textwrap.dedent(manifest), encoding="utf-8")
    if prompt is not None:
        (directory / "prompt.md").write_text(prompt, encoding="utf-8")
    if entry is not None:
        scripts = directory / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / entry).write_text("# 入口\n", encoding="utf-8")
    if schema:
        schemas = directory / "schemas"
        schemas.mkdir(exist_ok=True)
        (schemas / "gate-report.schema.json").write_text('{"type": "object"}', encoding="utf-8")
    return directory


# 注:这个文件里的构造器刻意保留独立——它要把 procedure.yaml 的**原始文本**拿来做定点
# 替换以构造各类非法形态,而共享夹具是按字段拼装的,表达不了"把某一行改成乱码"。


def _expect_failure(directory: Path) -> GenomeValidationError:
    with pytest.raises(GenomeValidationError) as excinfo:
        load_procedure(directory)
    return excinfo.value


# --- 合法路径 ---------------------------------------------------------------


def test_loads_a_valid_procedure(tmp_path: Path) -> None:
    spec = load_procedure(_procedure(tmp_path))

    assert spec.id == "unit-gate"
    assert spec.version == "1.2.0"
    assert spec.kind is ProcedureKind.DETERMINISTIC
    assert spec.trigger.states == ["UNIT_TESTING"]
    assert spec.outputs.artifacts == ["gate-report.json"]
    assert spec.tools.required_cmds == ["pytest", "ruff"]
    assert spec.failure.retry == 1
    assert spec.compat.runtimes == ["none"]


def test_procedure_ref_carries_the_version(tmp_path: Path) -> None:
    """报告要能精确说出"当时是哪一版能力干的活"。"""
    assert load_procedure(_procedure(tmp_path)).ref == "unit-gate@1.2.0"


def test_optional_sections_have_usable_defaults(tmp_path: Path) -> None:
    minimal = """\
    id: tiny
    version: 0.1.0
    summary: 最小 Procedure
    kind: deterministic
    """
    spec = load_procedure(_procedure(tmp_path, minimal, name="tiny", schema=False))

    assert spec.trigger.states == []
    assert spec.outputs.artifacts == []
    assert spec.tools.required_cmds == []
    assert spec.failure.retry == 0
    assert spec.compat.runtimes == []


# --- 标识与版本 -------------------------------------------------------------


def test_id_must_match_the_directory_name(tmp_path: Path) -> None:
    """目录名是它在注册表里的身份,对不上会让"我改的是哪个"变成一个问题。"""
    error = _expect_failure(
        _procedure(tmp_path, VALID.replace("id: unit-gate", "id: something-else"))
    )

    assert "something-else" in error.render()
    assert "unit-gate" in error.render()


@pytest.mark.parametrize("version", ["1.2", "v1.2.0", "latest", "1.2.0.0"])
def test_version_must_be_semver(tmp_path: Path, version: str) -> None:
    error = _expect_failure(
        _procedure(tmp_path, VALID.replace("version: 1.2.0", f"version: {version}"))
    )

    assert version in error.render()


# --- kind 与它蕴含的要求 ----------------------------------------------------


def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    error = _expect_failure(
        _procedure(tmp_path, VALID.replace("kind: deterministic", "kind: magic"))
    )

    assert "magic" in error.render()


def test_agentic_without_a_prompt_is_rejected(tmp_path: Path) -> None:
    """kind 不是分类标签,是成本与可靠性的开关。agentic 却没有指令说明它写错了。"""
    error = _expect_failure(
        _procedure(tmp_path, VALID.replace("kind: deterministic", "kind: agentic"), prompt=None)
    )

    assert "prompt.md" in error.render()


def test_hybrid_without_a_prompt_is_rejected(tmp_path: Path) -> None:
    error = _expect_failure(
        _procedure(tmp_path, VALID.replace("kind: deterministic", "kind: hybrid"), prompt=None)
    )

    assert "prompt.md" in error.render()


def test_hybrid_without_a_script_is_rejected(tmp_path: Path) -> None:
    error = _expect_failure(
        _procedure(
            tmp_path,
            VALID.replace("kind: deterministic", "kind: hybrid"),
            prompt="做点什么\n",
            entry=None,
        )
    )

    assert "scripts" in error.render()


def test_deterministic_without_a_script_is_rejected(tmp_path: Path) -> None:
    """确定性 Procedure 的全部逻辑都在脚本里。没有脚本它什么也不会做。"""
    error = _expect_failure(_procedure(tmp_path, entry=None))

    assert "scripts" in error.render()


def test_agentic_does_not_need_scripts(tmp_path: Path) -> None:
    spec = load_procedure(
        _procedure(
            tmp_path,
            VALID.replace("kind: deterministic", "kind: agentic"),
            prompt="干活\n",
            entry=None,
        )
    )

    assert spec.kind is ProcedureKind.AGENTIC
    assert spec.prompt == "干活\n"


# --- 契约引用 ---------------------------------------------------------------


def test_schema_ref_must_point_at_an_existing_file(tmp_path: Path) -> None:
    error = _expect_failure(_procedure(tmp_path, schema=False))

    assert "schemas/gate-report.schema.json" in error.render()


def test_inputs_schema_must_be_a_valid_json_schema(tmp_path: Path) -> None:
    """非法的入参 schema 会让派发前校验变成一个随机行为。"""
    broken = VALID.replace("    type: object", "    type: not-a-type")
    error = _expect_failure(_procedure(tmp_path, broken))

    assert "inputs" in error.render()


def test_output_schema_is_loaded_and_usable(tmp_path: Path) -> None:
    spec = load_procedure(_procedure(tmp_path))

    assert spec.output_schema == {"type": "object"}


# --- 失败策略与事件词汇 -----------------------------------------------------


def test_on_fail_only_accepts_state_machine_events(tmp_path: Path) -> None:
    """`on_fail` 的取值就是状态机的事件名。两边各定一套再做映射,迟早对不上。"""
    error = _expect_failure(
        _procedure(tmp_path, VALID.replace("on_fail: gate_fail", "on_fail: boom"))
    )

    assert "boom" in error.render()


def test_the_event_vocabulary_lives_in_one_place() -> None:
    from agentgenome.core.states import TaskEvent

    assert "gate_fail" in {event.value for event in TaskEvent}


def test_negative_retry_is_rejected(tmp_path: Path) -> None:
    error = _expect_failure(_procedure(tmp_path, VALID.replace("retry: 1", "retry: -1")))

    assert "retry" in error.render()


# --- 其余约束 ---------------------------------------------------------------


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    error = _expect_failure(_procedure(tmp_path, VALID + "unexpected: true\n"))

    assert "unexpected" in error.render()


def test_missing_manifest_is_a_readable_error(tmp_path: Path) -> None:
    directory = tmp_path / "procedures" / "ghost"
    directory.mkdir(parents=True)

    error = _expect_failure(directory)

    assert "procedure.yaml" in error.render()


def test_all_problems_are_reported_at_once(tmp_path: Path) -> None:
    """修一条跑一次的反馈回路太长。"""
    broken = VALID.replace("version: 1.2.0", "version: nope").replace(
        "on_fail: gate_fail", "on_fail: boom"
    )
    error = _expect_failure(_procedure(tmp_path, broken, schema=False))

    assert len(error.issues) >= 3


def test_every_issue_names_the_offending_procedure(tmp_path: Path) -> None:
    error = _expect_failure(_procedure(tmp_path, VALID.replace("version: 1.2.0", "version: nope")))

    assert all("unit-gate" in issue.file for issue in error.issues)
