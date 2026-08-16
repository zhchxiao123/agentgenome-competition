"""任务级失败报告:回退时注入下一轮的那份东西。

**格式归编排,不归门禁。** 它是状态机在回退时注入下一轮的材料——门禁只是众多来源之一
(单元门禁、集成测试、提交前检查、人工驳回都会往里写)。让门禁定义它的话,另外三个来源
就得各自再定一份,而上下文注入那一侧要认四种格式。

**位置是任务级而不是 Procedure 级。** 第三轮的员工要看到前两轮的**全部**失败,不管它们出自
哪个门禁——按 Procedure 分目录的话,注入方得先知道去哪几个目录找。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentgenome.core.events import LogKind

if TYPE_CHECKING:
    from agentgenome.core.events import Event
    from agentgenome.core.task import Task

#: 失败报告在任务目录里的位置。
FAILURES_DIR = "failures"

#: 终态汇总。
TASK_REPORT = "report.md"


def report_path(task_dir: Path, round_: int) -> Path:
    return Path(task_dir) / FAILURES_DIR / f"round-{round_}.md"


def paths_path(task_dir: Path, round_: int) -> Path:
    """这一轮失败牵扯到哪些文件,**存成数据而不是只渲进正文**。

    报告正文是给员工读的散文,里面的文件路径混在 Markdown 里。下一轮的知识路由要拿它们
    去匹配功能点的覆盖范围,而从散文里再抠一遍路径是件必然会写错的事——门禁改一次措辞,
    路由就静默零命中,而零命中和"确实没有相关知识"在结果上一模一样。
    """
    return Path(task_dir) / FAILURES_DIR / f"round-{round_}.paths.json"


def failure_paths(task_dir: Path, before_round: int | None = None) -> tuple[str, ...]:
    """历史各轮失败牵扯到的全部文件,去重后按首次出现排序。"""
    directory = Path(task_dir) / FAILURES_DIR
    if not directory.is_dir():
        return ()
    seen: dict[str, None] = {}
    for path in sorted(directory.glob("round-*.paths.json")):
        round_ = int(path.name.split("-", 1)[1].split(".", 1)[0])
        if before_round is not None and round_ >= before_round:
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in loaded if isinstance(loaded, list) else []:
            if isinstance(item, str) and item:
                seen.setdefault(item, None)
    return tuple(seen)


def _paths_in(payload: dict[str, Any]) -> list[str]:
    """产物里指名道姓的那些文件:失败用例所在的文件,以及集测报告里的嫌疑文件。"""
    found: list[str] = []
    for gate in payload.get("gates") or [payload]:
        if not isinstance(gate, dict):
            continue
        for failure in gate.get("failures") or []:
            if not isinstance(failure, dict):
                continue
            if isinstance(where := failure.get("file"), str) and where:
                found.append(where)
            for suspect in failure.get("suspect_files") or []:
                if isinstance(suspect, str) and suspect:
                    found.append(suspect)
    return found


@dataclass(frozen=True)
class FailureRecord:
    """一轮失败。"""

    round: int
    title: str
    body: str


def write_failure_report(task_dir: Path, round_: int, title: str, payload: dict[str, Any]) -> Path:
    """把一次失败写成下一轮能读的报告。

    **写的是产物里的 `failures[]`,不是整个产物。** 整份 JSON 塞进上下文会把真正有用的
    那几行淹掉,而 `failures[]` 的每一条都带 `message` 与 `evidence`——那正是下一轮要的。
    """
    target = report_path(task_dir, round_)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render(title, payload), encoding="utf-8")
    paths_path(task_dir, round_).write_text(
        json.dumps(_paths_in(payload), ensure_ascii=False), encoding="utf-8"
    )
    return target


def read_failure_reports(task_dir: Path, before_round: int | None = None) -> list[FailureRecord]:
    """读回历史失败报告,按轮次升序。

    **全量,不只是最近一轮。** 只给最近一轮会导致"改 A 坏 B、改 B 坏 A"的震荡——员工
    看不到自己两轮前已经试过这个方向。
    """
    directory = Path(task_dir) / FAILURES_DIR
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("round-*.md")):
        round_ = int(path.stem.split("-", 1)[1])
        if before_round is not None and round_ >= before_round:
            continue
        body = path.read_text(encoding="utf-8")
        records.append(FailureRecord(round=round_, title=_title_of(body), body=body))
    return sorted(records, key=lambda record: record.round)


def _render(title: str, payload: dict[str, Any]) -> str:
    regressions = payload.get("regressions") or []
    if regressions:
        # 回归排在最前,标题就说清楚。员工要先看到"你修坏了东西"——它比"又挂了三个测试"
        # 有用得多:前者指向上一轮的改动,后者只能从头猜。
        title = f"{title}(**这一轮引入了 {len(regressions)} 个新失败**)"
    lines = [f"# {title}", ""]
    if regressions:
        lines += ["## 这一轮新出现的失败(上一轮还是好的)", ""]
        lines += [f"- `{item.get('test', '?')}`:{item.get('message', '')}" for item in regressions]
        lines.append("")
    fixed = payload.get("fixed") or []
    if fixed:
        lines += ["## 这一轮修好的", ""]
        lines += [f"- `{item.get('test', '?')}`" for item in fixed]
        lines.append("")
    failures = payload.get("failures")
    if isinstance(failures, list) and failures:
        for index, failure in enumerate(failures, start=1):
            # 用例名排在最前:它是这条失败的身份,也是员工唯一能拿去定位的东西。
            # 只写消息的话,"AssertionError: 期望 3 实际 0" 这种在多个用例里长得一模一样。
            head = failure.get("test") or failure.get("gate") or failure.get("case") or ""
            title = (
                f"{head} — {failure.get('message', '')}"
                if head
                else failure.get("message", "(没写清楚是什么失败)")
            )
            lines.append(f"## {index}. {title}")
            where = _where(failure)
            if where:
                lines.append(f"位置:{where}")
            lines += _diagnosis(failure)
            evidence = failure.get("evidence")
            if evidence:
                lines += ["", "```", json.dumps(evidence, ensure_ascii=False, indent=2), "```"]
            lines.append("")
    else:
        # 产物说自己失败了却没写 failures[],这件事本身要被下一轮看到——
        # 悄悄写一份空报告的话,员工会以为上一轮什么都没发生。
        lines += [
            "产物标记为失败,但没有给出 `failures[]`。",
            "这一轮先弄清楚上一轮到底哪里不对,再动手。",
            "",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2)[:2000],
            "```",
        ]
    lines += _log_section(payload)
    return "\n".join(lines) + "\n"


def _diagnosis(failure: dict[str, Any]) -> list[str]:
    """集成测试的三件套:建议、嫌疑文件、复现命令,再加这条失败自己的日志尾部。

    **建议排在最前。** 它是这三样里唯一直接可行动的;日志排最后是因为它体量最大而信息
    密度最低——反过来的话,员工读到建议之前先啃完四十行容器日志。

    门禁的失败没有这几个字段,于是这一段自然什么都不输出:两种产物共用一个渲染器,不必
    在调用处分叉。
    """
    lines = []
    if suggestion := failure.get("suggestion"):
        lines += ["", f"**建议**:{suggestion}"]
    if suspects := failure.get("suspect_files"):
        lines += ["", "嫌疑文件(按可疑度排序):"]
        lines += [f"- `{item}`" for item in suspects]
    if repro := failure.get("repro_cmd"):
        lines += ["", "复现:", "", "```bash", repro, "```"]
    if tail := failure.get("log_tail"):
        lines += ["", "日志尾部:", "", "```", tail, "```"]
    return lines


def _log_section(payload: dict[str, Any]) -> list[str]:
    """原始日志附在最后。

    **日志是补充材料,不是主菜。** 但没有它的话,解析器没读懂的那部分失败就彻底不可见了
    ——而"解析器没读懂"恰恰在最奇怪的失败上最常发生。
    """
    tails = [
        (gate.get("id", "?"), gate.get("log_tail", ""))
        for gate in payload.get("gates") or []
        if not gate.get("passed", True) and gate.get("log_tail")
    ]
    if not tails:
        return []
    lines = ["## 原始日志(尾部)", ""]
    for gate_id, tail in tails:
        lines += [f"### {gate_id}", "", "```", tail, "```", ""]
    return lines


def _where(failure: dict[str, Any]) -> str:
    """出问题的位置。拿不到就不写——编一个出来会让员工跑去改一个无关的文件。"""
    file = failure.get("file")
    if not file:
        return ""
    line = failure.get("line")
    return f"{file}:{line}" if line else str(file)


def _title_of(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "失败报告"


def render_task_report(
    task: Task,
    events: Sequence[Event],
    plan: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
    itest: dict[str, Any] | None = None,
    merge: dict[str, Any] | None = None,
) -> str:
    """把一个任务渲染成给人看的那一页。**纯函数。**

    需求方不该为了知道"发生了什么"去读三百条事件。这份报告要能直接贴进 PR 描述或者发给
    需求方。

    **不用 Agent 写摘要。** 用它看起来更好读,但会引入不确定性与成本,而报告的价值在于准确
    而非文采——一份措辞漂亮但把轮次说错的报告,比一份干巴巴但准确的糟得多。

    材料缺失时写明"(没有)",不编造:一份把"没跑集成测试"写成"集成测试通过"的报告,是这份
    报告能造成的最大伤害。
    """
    transitions = [event for event in events if event.kind is LogKind.TRANSITION]
    jobs = [event for event in events if event.kind is LogKind.JOB_FINISHED]
    lines = [
        f"# {task.id} · {task.state.value}",
        "",
        f"**{task.title}**",
        "",
        "## 需求原文",
        "",
        task.requirement.strip() or "(没有)",
        "",
        "## 计划",
        "",
    ]
    lines += _plan_section(plan)
    lines += ["", "## 经过", ""]
    lines += [
        f"{index}. {event.payload['from']} → {event.payload['to']}({event.payload['event']})"
        for index, event in enumerate(transitions, start=1)
    ] or ["(没有状态迁移)"]
    lines += ["", f"修复轮次:**{task.fix_rounds}**", "", "## 质量", ""]
    lines += _verdict_line("单元门禁", gate)
    lines += _verdict_line("集成测试", itest)
    lines += ["", "## 变更", ""]
    lines += _changed_files(events) or ["(没有记录到变更文件)"]
    lines += ["", "## 证据", ""]
    lines += _evidence(task, merge)
    lines += ["", "## 成本", ""]
    budget = f" / {task.budget_tokens}" if task.budget_tokens else ""
    lines.append(f"- token:{task.tokens_used}{budget}")
    lines.append(f"- Job 数:{len(jobs)}")
    lines.append(f"- 耗时:{_duration(task)}")
    if task.escalate_reason:
        lines += ["", f"> 结束原因:{task.escalate_reason}"]
    return "\n".join(lines) + "\n"


def write_task_report(
    task_dir: Path,
    task: Task,
    events: Sequence[Event],
    plan: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
    itest: dict[str, Any] | None = None,
    merge: dict[str, Any] | None = None,
) -> Path:
    """渲染并落盘。"""
    target = Path(task_dir) / TASK_REPORT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_task_report(task, events, plan, gate, itest, merge), encoding="utf-8")
    return target


def _plan_section(plan: dict[str, Any] | None) -> list[str]:
    if not plan:
        return ["(没有计划产物)"]
    lines = [f"涉及模块:{', '.join(str(item) for item in plan.get('modules') or []) or '(未声明)'}"]
    acceptance = plan.get("acceptance") or []
    if acceptance:
        lines += ["", "验收标准:"]
        lines += [f"- {item}" for item in acceptance]
    return lines


def _verdict_line(name: str, payload: dict[str, Any] | None) -> list[str]:
    if not payload:
        return [f"- {name}:(没跑)"]
    mark = "通过" if payload.get("passed") else f"未通过({payload.get('kind', '?')})"
    failures = payload.get("failures") or []
    detail = f",{len(failures)} 条失败" if failures else ""
    return [f"- {name}:{mark}{detail}"]


def _changed_files(events: Sequence[Event]) -> list[str]:
    """从开发员工的产物记录里取变更文件。

    从事件流取而不是现去 git 里算:报告可能在工作区已经被清理之后才被读,那时 git 里什么
    都没有了。
    """
    found: list[str] = []
    for event in events:
        for item in event.payload.get("changed_files") or []:
            if item not in found:
                found.append(str(item))
    return [f"- `{item}`" for item in found]


def _evidence(task: Task, merge: dict[str, Any] | None) -> list[str]:
    lines = [f"- 产物目录:`tasks/{task.id}/artifacts/`", f"- 事件流:`tasks/{task.id}/logs/`"]
    for step in (merge or {}).get("submodules") or []:
        pr = step.get("pr") or {}
        if pr:
            lines.append(f"- PR:{pr.get('repo')} #{pr.get('number')}")
    top = (merge or {}).get("workspace_pr") or {}
    if top:
        lines.append(f"- 顶层 PR:{top.get('repo')} #{top.get('number')}")
    return lines


def _duration(task: Task) -> str:
    delta = task.updated_at - task.created_at
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "(算不出来)"
    return f"{seconds // 60} 分 {seconds % 60} 秒"


__all__ = [
    "render_task_report",
    "FAILURES_DIR",
    "TASK_REPORT",
    "FailureRecord",
    "read_failure_reports",
    "report_path",
    "write_failure_report",
    "write_task_report",
]
