"""ReplayRuntime:全仓主测试缝。

后续每一份 PRD 的端到端测试都建在它上面,所以三条不能妥协的性质各有一条测试:
真实写文件、未命中抛错、工作在归一化层与具体运行时无关。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentgenome.agents.recording import RecordingLibrary, RecordingNotFound
from agentgenome.agents.replay import ReplayRuntime
from agentgenome.agents.runtime import FailureKind, JobSpec

GOOD_RESULT = {
    "task_id": "ag-1",
    "producer": "dev",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
}
SCHEMA = {"type": "object", "required": ["task_id", "passed"]}


def _spec(tmp_path: Path, **overrides: Any) -> JobSpec:
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ctx.md").write_text("# 上下文\n")
    spec = JobSpec(
        task_id="ag-1",
        employee_id="dev-employee",
        procedure_id="code-develop",
        procedure_version="1.0.0",
        round=1,
        workdir=tmp_path / "work",
        context_file=tmp_path / "ctx.md",
        output_dir=tmp_path / "out",
        output_schema=SCHEMA,
    )
    return replace(spec, **overrides) if overrides else spec


def _record(
    library_root: Path,
    employee: str = "dev-employee",
    procedure: str = "code-develop",
    round_: int = 1,
    *,
    result: dict[str, Any] | None = None,
    files: dict[str, str] | None = None,
    stream: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
) -> Path:
    """手写一份最小录制。大多数录制都该是这样的剧本,而不是真实快照。"""
    directory = library_root / f"{employee}__{procedure}__r{round_}"
    directory.mkdir(parents=True, exist_ok=True)
    if result is not None:
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    for relative, content in (files or {}).items():
        target = directory / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if stream is not None:
        (directory / "stream.jsonl").write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in stream)
        )
    if meta is not None:
        import yaml

        (directory / "meta.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True))
    return directory


# --- 性质一:必须真实写文件 -------------------------------------------------


async def test_replay_really_writes_files_into_the_workdir(tmp_path: Path) -> None:
    """这是这个缝能成立的前提。

    只返回一个假 JobResult 的话,下游全部测试都在测桩,毫无价值。
    """
    _record(tmp_path / "lib", result=GOOD_RESULT, files={"src/app.py": "print('hi')\n"})
    runtime = ReplayRuntime(RecordingLibrary(tmp_path / "lib"))
    spec = _spec(tmp_path)

    result = await runtime.run_job(spec)

    assert result.ok is True
    assert (spec.workdir / "src" / "app.py").read_text() == "print('hi')\n"


async def test_files_written_by_replay_are_visible_to_real_git(tmp_path: Path) -> None:
    """贯通验证:回放写完之后,真 git 看得见它。

    这条是整套测试策略成立与否的验证——如果回放的产物对下游工具不可见,
    后续每一份 PRD 的端到端测试都建在沙上。
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q", str(workdir)], check=True)
    _record(tmp_path / "lib", result=GOOD_RESULT, files={"new.txt": "回放写的\n"})

    await ReplayRuntime(RecordingLibrary(tmp_path / "lib")).run_job(_spec(tmp_path))

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=workdir, capture_output=True, text=True, check=True
    ).stdout
    assert "new.txt" in status


async def test_files_written_by_replay_are_runnable_by_real_pytest(tmp_path: Path) -> None:
    """同上,另一半:真 pytest 跑得过它。"""
    _record(
        tmp_path / "lib",
        result=GOOD_RESULT,
        files={"test_written.py": "def test_ok():\n    assert 1 + 1 == 2\n"},
    )

    await ReplayRuntime(RecordingLibrary(tmp_path / "lib")).run_job(_spec(tmp_path))

    proc = subprocess.run(
        ["python", "-m", "pytest", "-q", "test_written.py"],
        cwd=tmp_path / "work",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- 性质二:未命中必须抛错 -------------------------------------------------


async def test_a_miss_raises_instead_of_returning_an_empty_result(tmp_path: Path) -> None:
    """静默降级会制造假绿测试,而假绿比没有测试更危险。"""
    (tmp_path / "lib").mkdir()
    runtime = ReplayRuntime(RecordingLibrary(tmp_path / "lib"))

    with pytest.raises(RecordingNotFound) as excinfo:
        await runtime.run_job(_spec(tmp_path))

    message = str(excinfo.value)
    assert "dev-employee" in message
    assert "code-develop" in message
    assert "r1" in message


async def test_the_miss_error_lists_what_the_library_does_have(tmp_path: Path) -> None:
    """ "缺哪个键"要能一眼看出来,否则补录制全靠猜。"""
    _record(tmp_path / "lib", round_=2, result=GOOD_RESULT)
    runtime = ReplayRuntime(RecordingLibrary(tmp_path / "lib"))

    with pytest.raises(RecordingNotFound) as excinfo:
        await runtime.run_job(_spec(tmp_path, round=1))

    assert "r2" in str(excinfo.value)


# --- 性质三:工作在归一化层,与具体运行时无关 -------------------------------


async def test_the_same_recording_replays_regardless_of_runtime_name(tmp_path: Path) -> None:
    """录制一旦与某个 CLI 的原生格式耦合,接第二个运行时时整套测试策略要重写。"""
    _record(tmp_path / "lib", result=GOOD_RESULT, stream=[{"kind": "text", "text": "干活"}])
    library = RecordingLibrary(tmp_path / "lib")

    for name in ("claude-code", "qwen-code", "whatever-comes-next"):
        target = tmp_path / name
        (target / "work").mkdir(parents=True, exist_ok=True)
        (target / "ctx.md").write_text("# 上下文\n")
        result = await ReplayRuntime(library, name=name).run_job(
            _spec(tmp_path, workdir=target / "work", output_dir=target / "out")
        )
        assert result.ok is True


async def test_replayed_log_is_normalized_jsonl(tmp_path: Path) -> None:
    _record(
        tmp_path / "lib",
        result=GOOD_RESULT,
        stream=[{"kind": "text", "text": "干活"}, {"kind": "usage", "usage": {"output_tokens": 7}}],
    )

    result = await ReplayRuntime(RecordingLibrary(tmp_path / "lib")).run_job(_spec(tmp_path))

    assert result.log_path is not None
    kinds = [json.loads(line)["kind"] for line in result.log_path.read_text().splitlines() if line]
    assert kinds == ["text", "usage"]
    assert result.tokens_used == 7


async def test_replayed_usage_is_deduped_by_message_id(tmp_path: Path) -> None:
    """回放走与真实运行同一套去重规则。

    自己写一遍求和的话,"同一次响应的多行带相同快照、逐行累加会翻倍"这条只在真实
    运行那边生效——而回放正是大多数测试跑的那一条路,错在这里最难被发现。
    """
    _record(
        tmp_path / "lib",
        result=GOOD_RESULT,
        stream=[
            {"kind": "usage", "message_id": "msg_a", "usage": {"input_tokens": 100}},
            {"kind": "usage", "message_id": "msg_a", "usage": {"input_tokens": 100}},
            {"kind": "usage", "message_id": "msg_b", "usage": {"input_tokens": 50}},
        ],
    )

    result = await ReplayRuntime(RecordingLibrary(tmp_path / "lib")).run_job(_spec(tmp_path))

    assert result.tokens_used == 150


async def test_replayed_authoritative_usage_replaces_the_estimate(tmp_path: Path) -> None:
    """终态权威值是总量,不是又一笔花销。"""
    _record(
        tmp_path / "lib",
        result=GOOD_RESULT,
        stream=[
            {"kind": "usage", "message_id": "msg_a", "usage": {"input_tokens": 100}},
            {"kind": "usage", "usage": {"input_tokens": 140}, "detail": {"authoritative": True}},
        ],
    )

    result = await ReplayRuntime(RecordingLibrary(tmp_path / "lib")).run_job(_spec(tmp_path))

    assert result.tokens_used == 140


# --- 轮次:修复循环的前提 ---------------------------------------------------


async def test_different_rounds_return_different_results(tmp_path: Path) -> None:
    """同一个 (employee, procedure) 在多轮里必须能返回不同结果。"""
    _record(tmp_path / "lib", round_=1, result=GOOD_RESULT, files={"v.txt": "第一轮\n"})
    _record(tmp_path / "lib", round_=2, result=GOOD_RESULT, files={"v.txt": "第二轮\n"})
    runtime = ReplayRuntime(RecordingLibrary(tmp_path / "lib"))

    await runtime.run_job(_spec(tmp_path))
    assert (tmp_path / "work" / "v.txt").read_text() == "第一轮\n"

    await runtime.run_job(_spec(tmp_path, round=2))
    assert (tmp_path / "work" / "v.txt").read_text() == "第二轮\n"


# --- meta.yaml 声明的失败类型 -----------------------------------------------


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("timeout", FailureKind.TIMEOUT),
        ("budget", FailureKind.BUDGET),
        ("process", FailureKind.PROCESS),
        ("contract", FailureKind.CONTRACT),
    ],
)
async def test_declared_failure_kinds_are_returned_without_really_waiting(
    tmp_path: Path, declared: str, expected: FailureKind
) -> None:
    """异常路径不需要真的等待、也不需要真的烧 token 就能构造。"""
    _record(tmp_path / "lib", result=GOOD_RESULT, meta={"failure_kind": declared})

    result = await ReplayRuntime(RecordingLibrary(tmp_path / "lib")).run_job(_spec(tmp_path))

    assert result.ok is False
    assert result.failure_kind is expected


async def test_meta_can_declare_token_usage(tmp_path: Path) -> None:
    _record(tmp_path / "lib", result=GOOD_RESULT, meta={"tokens_used": 12345})

    result = await ReplayRuntime(RecordingLibrary(tmp_path / "lib")).run_job(_spec(tmp_path))

    assert result.tokens_used == 12345


async def test_a_recording_without_a_result_fails_the_contract(tmp_path: Path) -> None:
    """回放也走同一套契约校验,不给自己开后门。"""
    _record(tmp_path / "lib", files={"x.txt": "只写了文件,没写结果\n"})

    result = await ReplayRuntime(RecordingLibrary(tmp_path / "lib")).run_job(_spec(tmp_path))

    assert result.failure_kind is FailureKind.CONTRACT


async def test_dry_run_still_skips_everything(tmp_path: Path) -> None:
    _record(tmp_path / "lib", result=GOOD_RESULT, files={"nope.txt": "x"})
    spec = _spec(tmp_path)

    result = await ReplayRuntime(RecordingLibrary(tmp_path / "lib")).run_job(spec, dry_run=True)

    assert result.dry_run is True
    assert not (spec.workdir / "nope.txt").exists()


# --- 录制模式 ---------------------------------------------------------------


async def test_recording_a_real_run_produces_a_replayable_directory(tmp_path: Path) -> None:
    """录制—回放闭环:录下来的东西必须能被原样读回去。"""
    import sys

    from agentgenome.agents.recording import record_job
    from agentgenome.agents.subprocess_runtime import SubprocessRuntime
    from tests.fixtures import fake_agent
    from tests.fixtures.fake_agent import SCRIPT_ENV

    script = {
        "result": GOOD_RESULT,
        "files": {"made.txt": "真实跑出来的\n"},
        "workdir": str(tmp_path / "work"),
        "output_dir": str(tmp_path / "out"),
    }
    spec = _spec(tmp_path, credentials={SCRIPT_ENV: json.dumps(script)})
    runtime = SubprocessRuntime(argv=[sys.executable, fake_agent.__file__])

    result = await runtime.run_job(spec)
    record_job(tmp_path / "lib", spec, result)

    # 换一个干净工作区,用回放跑一遍,产物要一模一样。
    replayed = tmp_path / "replayed"
    (replayed / "work").mkdir(parents=True)
    (replayed / "ctx.md").write_text("# 上下文\n")
    await ReplayRuntime(RecordingLibrary(tmp_path / "lib")).run_job(
        _spec(tmp_path, workdir=replayed / "work", output_dir=replayed / "out")
    )

    assert (replayed / "work" / "made.txt").read_text() == "真实跑出来的\n"


def test_the_same_procedure_on_two_modules_replays_two_different_recordings(
    tmp_path: Path,
) -> None:
    """**并行派发逼出来的那一维。** 逐模块深读会同时派几十个同 employee 同 procedure 的作业；
    不带模块的话它们全撞在一个键上，回放给每个模块返回同一份产出——而测试照样是绿的。
    那正是「假绿比没有测试更危险」的教科书形态。
    """
    from agentgenome.agents.recording import recording_key

    assert recording_key("arch", "deep-read", 1, "order-service") != recording_key(
        "arch", "deep-read", 1, "inventory-service"
    )


def test_an_unsubjected_key_is_unchanged(tmp_path: Path) -> None:
    """既有录制不必重录。"""
    from agentgenome.agents.recording import recording_key

    assert recording_key("arch", "knowledge-init", 1) == "arch__knowledge-init__r1"
