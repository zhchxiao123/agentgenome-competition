"""知识初始化的入口:扫描、建任务、草案落盘、停在闸门。

CLI(`agctl knowledge plan`)与 REST(`POST /genome/tasks/init`)共用这一层——
两个薄壳各写一遍的话,查重、预算、草案三样迟早有一样只在其中一条路上生效,
而那种分叉没有任何测试会红。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agentgenome import paths
from agentgenome.config import GenomeTaskConfig
from agentgenome.core.events import ORCHESTRATOR, EventLog
from agentgenome.core.genome_driver import GenomeDriver
from agentgenome.core.genome_gate import write_draft
from agentgenome.core.genome_task import GenomeTask, GenomeTaskKind, GenomeTaskStore, Origin
from agentgenome.core.genome_transitions import GenomeEvent
from agentgenome.genome.boundary import propose_boundaries, scan_for_boundaries


class InitAlreadyOpen(RuntimeError):
    """已有一个未终结的知识初始化在跑。

    两个都停在闸门的初始化没有任何一个是对的——人答了哪个都只推进一半。报错带着在跑的
    那个 id,因为下一步该做的是去答它(或取消它),不是重试这次请求。
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"已有一个未了结的知识初始化在跑: {task_id}。先去回答它的闸门,或取消它。")


@dataclass(frozen=True)
class PlannedInit:
    task: GenomeTask
    #: 扫描提出的候选边界路径,给发起入口回显。
    candidates: tuple[str, ...]


def plan_init(root: Path, config: GenomeTaskConfig, actor: str = ORCHESTRATOR) -> PlannedInit:
    """扫描仓库、建 INIT 任务、落边界草案,停在闸门等人确认。

    **查重在扫描之前**:扫描要读整个仓,先做便宜的检查。`NotReadyForBoundaries`
    原样上抛,由两个薄壳各自翻译成退出码或状态码——判断只有这一份。
    """
    store = GenomeTaskStore(root)
    open_inits = [task for task in store.open_tasks() if task.kind is GenomeTaskKind.INIT]
    if open_inits:
        raise InitAlreadyOpen(open_inits[0].id)

    scanned = scan_for_boundaries(root, config.hot_path_since_days)
    record = store.create(
        title="知识初始化",
        kind=GenomeTaskKind.INIT,
        origin=Origin.HUMAN,
        budget_tokens=config.per_task_tokens,
    )
    (root / paths.TASKS / record.id).mkdir(parents=True, exist_ok=True)
    (root / paths.TASKS / record.id / "scan.json").write_text(
        json.dumps(scanned.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_draft(root, record.id, propose_boundaries(scanned))
    applied = GenomeDriver(store, EventLog(root)).deliver(
        record.id, GenomeEvent.DRAFT_READY, actor=actor
    )
    return PlannedInit(
        task=applied.task, candidates=tuple(item.path for item in scanned.candidates)
    )


__all__ = ["InitAlreadyOpen", "PlannedInit", "plan_init"]
