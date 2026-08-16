"""回放运行时——全仓的主测试缝。

给这套编排系统写自动化测试有个死结:每跑一次 CI 就调一遍真实大模型——慢、贵、
结果每次不一样、断言没法写。解法是换上这个运行时,它按 `(employee, procedure, round)`
去查录制库,把当初录下来的输出原样重放。

于是 CI 里跑的是**真实的状态机、真实的 git、真实的 pytest**,唯独 Agent 那一段是
确定性的。

## 三条不能妥协的性质

1. **真实写文件。** 这是这个缝能成立的前提。只返回一个假 JobResult 的话,下游
   全部测试都在测桩。
2. **未命中直接抛错。** 静默降级会制造假绿测试,而假绿比没有测试更危险。
3. **工作在归一化事件层,与具体运行时无关。** 录制一旦与某个 CLI 的原生格式耦合,
   接第二个运行时时整套测试策略要重写。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agentgenome.agents.artifacts import RESULT_FILENAME, context_filename, log_filename
from agentgenome.agents.contract import check_result_contract
from agentgenome.agents.events import EventKind, NormalizedEvent, UsageAccumulator
from agentgenome.agents.recording import Recording, RecordingLibrary
from agentgenome.agents.runtime import (
    FailureKind,
    JobResult,
    JobSpec,
    SessionHandle,
    SessionSpec,
    SessionStream,
    SessionTurn,
)


class ReplayRuntime:
    """从录制库回放一次 Job。"""

    def __init__(self, library: RecordingLibrary, name: str = "replay") -> None:
        self.library = library
        self.name = name

    def preflight(self) -> None:
        if not self.library.root.is_dir():
            raise RuntimeError(f"录制库不存在: {self.library.root}")

    # --- 会话 ----------------------------------------------------------------

    async def start_session(self, spec: SessionSpec) -> SessionHandle:
        """开一个会话。回放不需要真的建立什么,记住标识即可。"""
        return SessionHandle(
            session_id=spec.session_id,
            native_session_id=spec.native_session_id or f"replay-{spec.session_id}",
            workdir=spec.workdir,
            employee_id=spec.employee_id,
        )

    def send_message(self, handle: SessionHandle, message: str) -> SessionStream:
        """回放会话的某一轮,**逐条**给出去。

        轮次由 `handle.message_index` 给,而不是让回放自己数调用次数:录制目录名因此
        自解释,也不与调用顺序隐式耦合——这与 Job 用显式 `round` 是同一条理由。
        """
        turn = SessionTurn(native_session_id=handle.native_session_id)

        async def stream() -> AsyncIterator[NormalizedEvent]:
            usage = UsageAccumulator()
            try:
                recording = self.library.find_session(
                    handle.employee_id, handle.session_id, handle.message_index
                )
                # 录制的文件真实写入工作区——结对会话靠它让门禁看到真实产物。
                if handle.workdir is not None:
                    recording.write_files_into(handle.workdir)
                for raw in recording.events:
                    event = _normalized(raw)
                    if event.usage is not None:
                        usage.observe_event(event)
                    yield event
            finally:
                # **账写在 finally 里。** 调用方中途退出(客户端断线、用户按停止)时,
                # 生成器会被 `aclose()`,而这一段照样跑——不然这一轮烧掉的 token 就不记账了。
                turn.tokens_used = usage.total()
                turn.tokens_available = usage.seen_any

        return SessionStream(stream(), turn)

    async def run_job(self, spec: JobSpec, dry_run: bool = False) -> JobResult:
        started = time.monotonic()
        spec.output_dir.mkdir(parents=True, exist_ok=True)

        if dry_run:
            self._write_context(spec)
            return JobResult(ok=True, dry_run=True, duration_s=time.monotonic() - started)

        # 未命中直接抛,不静默降级。
        recording = self.library.find(spec.employee_id, spec.procedure_id, spec.round, spec.subject)

        self._write_context(spec)
        recording.write_files_into(spec.workdir)
        # 产物目录的文件(staging 树等)也要真实落盘——树产出类工序的裁决对着它跑,
        # 不写的话回放下这类 Job 永远是"无产物"。
        recording.write_outputs_into(spec.output_dir)
        self._write_result(recording, spec)
        log_path = self._write_log(recording, spec)

        meta = recording.meta
        result = JobResult(
            ok=True,
            log_path=log_path,
            duration_s=time.monotonic() - started,
            exit_code=0,
        )
        self._apply_usage(result, recording, meta)

        declared = meta.get("failure_kind")
        if declared and declared != FailureKind.NONE.value:
            # 异常路径不需要真的等待、也不需要真的烧 token 就能构造。
            result.ok = False
            result.failure_kind = FailureKind(declared)
            result.failure_detail = meta.get("failure_detail") or f"录制声明的失败: {declared}"
            return result

        # 回放也走同一套契约校验,不给自己开后门。
        check = check_result_contract(spec.output_dir, spec.output_schema, spec.output_check)
        if not check.ok:
            result.ok = False
            result.failure_kind = FailureKind.CONTRACT
            result.failure_detail = check.detail
            return result
        result.result_path = check.path
        return result

    # --- 内部 ----------------------------------------------------------------

    @staticmethod
    def _write_context(spec: JobSpec) -> Path:
        """上下文包照样落盘——"当时它到底看到了什么"在回放下也该能被回答。"""
        target = spec.output_dir / context_filename(1)
        target.write_text(spec.context_file.read_text(encoding="utf-8"), encoding="utf-8")
        return target

    @staticmethod
    def _write_result(recording: Recording, spec: JobSpec) -> None:
        text = recording.result_text
        if text is not None:
            (spec.output_dir / RESULT_FILENAME).write_text(text, encoding="utf-8")

    @staticmethod
    def _write_log(recording: Recording, spec: JobSpec) -> Path | None:
        events = recording.events
        if not events:
            return None
        target = spec.output_dir / log_filename(1)
        target.write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
            encoding="utf-8",
        )
        return target

    @staticmethod
    def _apply_usage(result: JobResult, recording: Recording, meta: dict[str, Any]) -> None:
        """用量优先取 meta 声明的,否则从录制的归一化事件里算。

        走与真实运行同一个累加器。自己写一遍求和的话,"同一次响应的多行带相同快照、
        逐行累加会翻倍"这条规则就只在真实运行那边生效——而回放正是大多数测试跑的
        那一条路。
        """
        declared = meta.get("tokens_used")
        if isinstance(declared, int):
            result.tokens_used = declared
            result.tokens_available = True
            return
        accumulator = UsageAccumulator()
        for raw in recording.events:
            usage = raw.get("usage")
            if not isinstance(usage, dict):
                continue
            accumulator.observe_event(
                NormalizedEvent(
                    kind=EventKind.USAGE,
                    message_id=raw.get("message_id"),
                    usage=usage,
                    detail=raw.get("detail") or {},
                )
            )
        result.tokens_used = accumulator.total()
        result.tokens_available = accumulator.seen_any


def _normalized(raw: dict[str, Any]) -> NormalizedEvent:
    """录制里的一行 → 归一化事件。

    **回放工作在归一化事件层**,与具体运行时无关:录制一旦与某个 CLI 的原生格式耦合,
    接第二个运行时时整套测试策略要重写。
    """
    return NormalizedEvent(
        kind=EventKind(raw.get("kind", EventKind.TEXT.value)),
        text=raw.get("text", ""),
        message_id=raw.get("message_id"),
        usage=raw.get("usage"),
        detail=raw.get("detail") or {},
    )


__all__ = ["ReplayRuntime"]
