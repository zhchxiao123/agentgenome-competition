"""指标:这套系统到底有没有在变强。

## 全部从数据库与事件流派生

**不维护任何进程内计数器。** 内存计数器重启即丢,而且违反"状态即事实"——一个和数据库对不上
的计数器比没有计数器更糟,因为人会相信它,然后基于错的数字做决定。

代价是每次 `/metrics` 都要扫一遍库。在 M1 的数据量下这是免费的;真到了扫不动的那天,答案是
物化视图或者时序库,不是内存计数器。

## 为什么现在就要有 gate_first_pass_ratio 与 fix_rounds

它们是"系统是否在进化"的直接证据。M1 阶段数据量小、噪声大,看不出趋势——**这不是不做它的
理由**:数据必须从第一天开始积累,否则等到想看趋势时就没有历史了。

## approval_queue_depth

PRD 08 那条"审批超时不自动放行"的配套。没有它,"永远停着"会变成"悄悄地永远停着"。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.states import TaskState
from agentgenome.core.task import Task, TaskStore
from agentgenome.core.topology import Executor

#: 修复轮次的直方图分桶。0 单独一个桶——"一次过"与"修了一轮"是完全不同的两件事。
FIX_ROUND_BUCKETS = (0, 1, 2, 3, 5)

#: 任务耗时的分桶,秒。
DURATION_BUCKETS = (60, 300, 900, 1800, 3600, 7200)


@dataclass
class Snapshot:
    """一次采样。纯数据,方便单独断言。"""

    tasks_by_state: dict[str, int] = field(default_factory=dict)
    fix_rounds: list[int] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    tokens: dict[tuple[str, str], int] = field(default_factory=dict)
    #: 每个员工在每个任务上被派了几次活。**与 token 同源**(都数 `JOB_FINISHED`)——
    #: 各数各的话,"出场 3 次却花了 0 token"这种账没人对得上,而对账正是这份数据的用途。
    jobs: dict[tuple[str, str], int] = field(default_factory=dict)
    approval_queue_depth: int = 0
    #: 各执行三态干了多少次活:`auto` / `assisted` / `manual`。
    #:
    #: **它必须与门禁一次通过率一起看。** "自动化率从 30% 爬到 70%"是客户能拿去讲的数字,
    #: 但**关掉确认节点就能刷**——单独上报它等于奖励一个把信任爬坡走成信任跳崖的动作。
    execution_modes: dict[str, int] = field(default_factory=dict)
    escalated_total: int = 0
    gate_runs: int = 0
    gate_first_pass: int = 0
    #: 窗口内终态任务数。知识命中率与升级人工率的分母——用终态而非全部任务,
    #: 未走完的任务还没有"命中没命中""升没升级"这个结论。
    terminal_tasks: int = 0
    #: 窗口内被记为"知识命中"的次数,见 `Orchestrator._trigger_evolution`。
    knowledge_hits: int = 0

    @property
    def gate_first_pass_ratio(self) -> float:
        """第一轮门禁就过的任务比例。没有样本时是 0——不是 1。

        没跑过门禁就报"一次通过率 100%"是最误导人的默认值:它会让一个刚上线、什么都没验过
        的系统看起来完美。
        """
        return self.gate_first_pass / self.gate_runs if self.gate_runs else 0.0

    @property
    def escalate_rate(self) -> float:
        return self.escalated_total / self.terminal_tasks if self.terminal_tasks else 0.0

    @property
    def knowledge_hit_rate(self) -> float:
        return self.knowledge_hits / self.terminal_tasks if self.terminal_tasks else 0.0

    @property
    def avg_fix_rounds(self) -> float:
        return sum(self.fix_rounds) / len(self.fix_rounds) if self.fix_rounds else 0.0

    @property
    def avg_duration_minutes(self) -> float:
        return sum(self.durations) / len(self.durations) / 60 if self.durations else 0.0


def collect(
    workspace_root: Path, since: datetime | None = None, until: datetime | None = None
) -> Snapshot:
    """扫一遍库与事件流,算出指标。

    `since`/`until` 限定一个时间窗(按 `created_at`)——观测中心的两期对比就是拿同一个函数
    分别扫两个窗口,不是另开一条聚合逻辑。缺省不限定,即当前的全量快照。
    """
    store = TaskStore(workspace_root)
    log = EventLog(workspace_root)
    snapshot = Snapshot()

    # `all_tasks` 而不是 `open_tasks`:指标要看的恰恰包含已完成与已升级的——只统计在跑的
    # 任务,"一次通过率"永远算不出来。
    tasks = [
        task
        for task in store.all_tasks()
        if (since is None or task.created_at >= since)
        and (until is None or task.created_at < until)
    ]
    counts = Counter(task.state.value for task in tasks)
    snapshot.tasks_by_state = {state.value: counts.get(state.value, 0) for state in TaskState}
    snapshot.approval_queue_depth = counts.get(TaskState.REVIEWING.value, 0)
    snapshot.escalated_total = counts.get(TaskState.ESCALATED.value, 0)
    snapshot.terminal_tasks = sum(1 for task in tasks if task.is_terminal)

    for task in tasks:
        snapshot.fix_rounds.append(task.fix_rounds)
        snapshot.durations.append(max(0.0, (task.updated_at - task.created_at).total_seconds()))
        first_pass = _first_gate_verdict(log, task)
        if first_pass is not None:
            snapshot.gate_runs += 1
            snapshot.gate_first_pass += int(first_pass)
        for actor, used in _tokens_by_actor(log, task).items():
            snapshot.tokens[task.id, actor] = used
        for actor, count in _jobs_by_actor(log, task).items():
            snapshot.jobs[task.id, actor] = count
        for mode, count in _execution_modes(log, task).items():
            snapshot.execution_modes[mode] = snapshot.execution_modes.get(mode, 0) + count
        snapshot.knowledge_hits += sum(
            1
            for event in log.events(task.id)
            if event.kind is LogKind.NOTE and event.payload.get("note") == "knowledge_hit"
        )
    return snapshot


def render(snapshot: Snapshot) -> str:
    """Prometheus 文本格式。**不自造看板**——接现成的监控系统。"""
    lines: list[str] = []
    lines += _gauge(
        "agentgenome_execution_mode_total",
        "各执行三态干了多少次活。与 gate_first_pass_ratio 成对看:关掉确认就能刷高自动化率",
        {
            (("mode", mode),): float(count)
            for mode, count in sorted(snapshot.execution_modes.items())
        },
    )
    lines += _gauge(
        "agentgenome_tasks_by_state",
        "各状态下的任务数",
        {(("state", state),): value for state, value in snapshot.tasks_by_state.items()},
    )
    lines += _gauge(
        "agentgenome_approval_queue_depth",
        "停在人工审批上的任务数",
        {(): snapshot.approval_queue_depth},
    )
    lines += _counter(
        "agentgenome_escalated_total", "升级人工的任务数", {(): snapshot.escalated_total}
    )
    lines += _gauge(
        "agentgenome_gate_first_pass_ratio",
        "第一轮门禁就通过的任务比例",
        {(): snapshot.gate_first_pass_ratio},
    )
    lines += _histogram(
        "agentgenome_fix_rounds", "修复轮次分布", snapshot.fix_rounds, FIX_ROUND_BUCKETS
    )
    lines += _histogram(
        "agentgenome_task_duration_seconds", "任务耗时分布", snapshot.durations, DURATION_BUCKETS
    )
    lines += _counter(
        "agentgenome_tokens_used_total",
        "token 消耗",
        {
            (("task", task_id), ("employee", actor)): value
            for (task_id, actor), value in snapshot.tokens.items()
        },
    )
    return "\n".join(lines) + "\n"


#: 三态的名字。**auto 不是"没配过",是"这次真的没有人参与"**——两者在信任爬坡曲线上是同一个
#: 点,但在"这套系统现在能不能放手"这个问题上不是。
#:
#: `MODE_MANUAL` 直接取自执行者枚举:自造一份同名字符串的话,枚举改一个取值这个指标会**静默
#: 归零**,而它是要拿给客户看的那个数。
MODE_AUTO = "auto"
MODE_ASSISTED = "assisted"
MODE_MANUAL = Executor.MANUAL.value


def _execution_modes(log: EventLog, task: Task) -> dict[str, int]:
    """这个任务的各步分别是谁干的。

    数据源是拓扑事件与待办事件,不另埋一份计数:埋两份的话,"自动化率"会有两个答案,
    而它是要拿给客户看的那个数。
    """
    counts = {MODE_AUTO: 0, MODE_ASSISTED: 0, MODE_MANUAL: 0}
    for event in log.events(task.id):
        if event.kind is LogKind.TOPOLOGY and event.payload.get("phase") == "chosen":
            template = event.payload.get("template") or {}
            nodes = template.get("nodes") or []
            manual = [node for node in nodes if node.get("executor") == Executor.MANUAL.value]
            if not manual:
                counts[MODE_AUTO] += 1
            elif len(manual) == len(nodes):
                counts[MODE_MANUAL] += 1
            else:
                # 一部分节点是人干的:那正是 assisted——机器干、人确认。
                counts[MODE_ASSISTED] += 1
    return counts


def _first_gate_verdict(log: EventLog, task: Task) -> bool | None:
    """这个任务第一次跑门禁过没过。没跑过返回 `None`,不参与比例计算。"""
    for event in log.events(task.id):
        if event.kind is LogKind.GATE_RESULT:
            return bool(event.payload.get("passed"))
    return None


def _tokens_by_actor(log: EventLog, task: Task) -> dict[str, int]:
    found: dict[str, int] = {}
    for event in log.events(task.id):
        if event.kind is not LogKind.JOB_FINISHED:
            continue
        if not event.payload.get("tokens_available"):
            # 用量不可得时不加。加 0 与加"未知"在账面上一样,但把未知当成 0 会让人以为
            # 这个员工没花钱。
            continue
        found[event.actor] = found.get(event.actor, 0) + int(event.payload.get("tokens_used") or 0)
    return found


def _jobs_by_actor(log: EventLog, task: Task) -> dict[str, int]:
    """这个任务里每个员工出场几次。

    **数完成而不是数开始**:一次派发要么完成要么以失败完成,两者都算出场;而只数开始的话,
    一次崩在半路的派发会被算成出场,却没有任何成本与它对应——那正是对账对不上的样子。
    """
    found: dict[str, int] = {}
    for event in log.events(task.id):
        if event.kind is LogKind.JOB_FINISHED:
            found[event.actor] = found.get(event.actor, 0) + 1
    return found


def _labels(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    body = ",".join(f'{name}="{_escape(value)}"' for name, value in pairs)
    return "{" + body + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _gauge(name: str, help_: str, values: dict[tuple[tuple[str, str], ...], float]) -> list[str]:
    return _series(name, help_, "gauge", values)


def _counter(name: str, help_: str, values: dict[tuple[tuple[str, str], ...], float]) -> list[str]:
    return _series(name, help_, "counter", values)


def _series(
    name: str, help_: str, kind: str, values: dict[tuple[tuple[str, str], ...], float]
) -> list[str]:
    lines = [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
    lines += [f"{name}{_labels(labels)} {_number(value)}" for labels, value in values.items()]
    return lines


def _histogram(
    name: str, help_: str, samples: Sequence[float], buckets: tuple[int, ...]
) -> list[str]:
    lines = [f"# HELP {name} {help_}", f"# TYPE {name} histogram"]
    for edge in buckets:
        count = sum(1 for value in samples if value <= edge)
        lines.append(f'{name}_bucket{{le="{edge}"}} {count}')
    lines.append(f'{name}_bucket{{le="+Inf"}} {len(samples)}')
    lines.append(f"{name}_sum {_number(sum(samples))}")
    lines.append(f"{name}_count {len(samples)}")
    return lines


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.4f}"


__all__ = ["DURATION_BUCKETS", "FIX_ROUND_BUCKETS", "Snapshot", "collect", "render"]
