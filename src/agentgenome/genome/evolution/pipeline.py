"""蒸馏管道:任务结束之后,把发生过的事变成下一次的输入。

## 整条管道包在异常隔离里

**管道失败绝不影响已经完成的任务。** 一个蒸馏 bug 会让所有任务看起来失败——那是这条管道能
造成的最大伤害,比不蒸馏糟得多。所以这里没有任何一条向上抛的路径,失败只记事件。

## 独立预算

蒸馏烧的钱不该算在业务任务头上,也不该无上限。超限就跳过并记一条事件——"这次没蒸馏"是可以
接受的结果,"这个月账单翻倍"不是。

## lint 门禁里的证据可达性

指向不存在产物的"证据"就是编造。这是防污染的第二道:第一道(`cards.parse_cards`)挡掉压根
没写证据的,这一道挡掉写了但指不到东西的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentgenome import paths
from agentgenome.core.events import ORCHESTRATOR, EventLog, LogKind
from agentgenome.core.states import TaskState
from agentgenome.core.store import task_dir
from agentgenome.core.task import Task
from agentgenome.genome.evolution.cards import (
    Intake,
    LessonCard,
    Level,
    load_cards,
    merge,
    next_number,
    parse_cards,
)
from agentgenome.genome.evolution.collect import Material, collect

#: L2/L3/L4 的候选存档区。本期只识别并归类,消费在 PRD 13/17。
CANDIDATES_DIR = paths.LESSONS / "candidates"

#: 蒸馏的产物 stage 与执行者。只有架构员工有 `genome/**` 的写权限。
STAGE_DISTILL = "distill"
DISTILL_PROCEDURE = "experience-distill"
DISTILL_EMPLOYEE = "arch-employee"


@dataclass
class DistillResult:
    """一次蒸馏跑完的结果。**失败也是一种结果**,不是异常。"""

    task_id: str
    ok: bool = True
    skipped: str = ""
    intake: Intake = field(default_factory=Intake)
    written: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "ok": self.ok,
            "skipped": self.skipped,
            "written": list(self.written),
            "deferred": list(self.deferred),
            "error": self.error,
            **self.intake.as_dict(),
        }


def lint(card: LessonCard, workspace_root: Path) -> list[str]:
    """入库前的格式与证据可达性检查。返回问题清单,空表示过。

    **证据必须指得到真东西。** 指向不存在产物的"证据"就是编造——它看起来完全合规,而且比
    没有证据更难被发现。
    """
    problems = []
    if not card.conclusion.strip():
        problems.append("结论是空的")
    for item in card.evidence:
        target = task_dir(workspace_root, item.task_id) / item.path
        if not target.exists():
            problems.append(f"证据指向不存在的产物: {item.task_id}/{item.path}")
    return problems


def land_cards(workspace_root: Path, intake: Intake) -> tuple[list[str], list[str], Intake]:
    """把过了 lint 的 L1 卡片写进 `lessons/`,其余归类存档。

    返回 `(写进去的, 归类存档的, 补充了拒收原因之后的 intake)`。
    """
    lessons = Path(workspace_root) / paths.LESSONS
    written: list[str] = []
    deferred: list[str] = []
    rejected = list(intake.rejected)

    for card in intake.accepted:
        problems = lint(card, workspace_root)
        if problems:
            rejected.append((card.title, "；".join(problems)))
            continue
        if card.level is not Level.L1:
            # 本期只有 L1 走完整闭环。其余存档待 PRD 13/17 消费——丢掉的话,那几级的
            # 素材就要在将来重新蒸馏一遍,而那时任务现场早就没了。
            target = Path(workspace_root) / CANDIDATES_DIR / f"{card.id}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(card.render(), encoding="utf-8")
            deferred.append(card.id)
            continue
        lessons.mkdir(parents=True, exist_ok=True)
        (lessons / f"{card.id}.md").write_text(card.render(), encoding="utf-8")
        written.append(card.id)

    return written, deferred, Intake(intake.accepted, tuple(rejected), intake.reinforced)


def distill(
    workspace_root: Path,
    task: Task,
    produce: Any,
    budget_tokens: int | None = None,
    spent_tokens: int = 0,
) -> DistillResult:
    """跑一次蒸馏。**永远返回结果,永远不抛。**

    `produce` 是"给我素材、还我候选卡片"的那一步——真实实现是派发 `experience-distill`,
    测试里可以是一个函数。把它做成参数而不是在这里直接派发,是为了让管道的编排逻辑能被
    单独测:异常隔离、预算、过滤、归类,一条都不该依赖跑一次真 Agent 才能验。
    """
    result = DistillResult(task_id=task.id)
    log = EventLog(workspace_root)
    try:
        if budget_tokens is not None and spent_tokens >= budget_tokens:
            result.skipped = f"蒸馏预算已用尽({spent_tokens} / {budget_tokens})"
            _note(log, task, result)
            return result

        material = collect(Path(workspace_root), task)
        if material.is_empty:
            # 直通且没有审批意见的任务没什么可蒸馏的。为它烧一次 Agent 是纯浪费。
            result.skipped = "这个任务没有留下可蒸馏的素材"
            _note(log, task, result)
            return result

        existing = load_cards(Path(workspace_root) / paths.LESSONS)
        raw = produce(material)
        intake = merge(existing, parse_cards(raw, task.id, next_number(existing)))
        written, deferred, intake = land_cards(Path(workspace_root), intake)
        result.intake = intake
        result.written = written
        result.deferred = deferred
    except Exception as error:  # noqa: BLE001
        # **一条向上抛的路径都不留。** 蒸馏是增值,任务已经完成了;让一个蒸馏 bug 把所有
        # 任务标成失败,是这条管道能造成的最大伤害。
        result.ok = False
        result.error = f"{type(error).__name__}: {error}"
    _note(log, task, result)
    return result


async def distill_with_agent(
    orchestrator: Any,
    task: Task,
    budget_tokens: int | None = None,
    spent_tokens: int = 0,
) -> DistillResult:
    """真实的蒸馏:把 `produce` 接到架构员工的 `experience-distill` 上。

    **拆成 `produce` 参数是有意的**(见 `distill`):编排逻辑——异常隔离、预算、两道过滤、
    分级归类——都能脱离真 Agent 单独测。这个函数只负责"把素材换成候选卡片"这一步。

    派发失败照样不抛:`distill` 自己包了异常隔离,而这里的 `produce` 在它里面跑。
    """

    # 同步的 `distill` 里没法 await,所以**先**把素材换成卡片,再把现成的结果喂进去。
    # 反过来(在 `produce` 里起一个事件循环)会在已经跑着的循环里再起一个,那是最典型的
    # asyncio 死锁写法。
    material = collect(Path(orchestrator.root), task)
    cards = await _dispatch(orchestrator, task, material) if not material.is_empty else []
    return distill(
        orchestrator.root,
        task,
        lambda _material: cards,
        budget_tokens=budget_tokens,
        spent_tokens=spent_tokens,
    )


async def _dispatch(orchestrator: Any, task: Task, material: Material) -> Any:
    """派一次 `experience-distill`,把候选卡片读回来。

    读不出来时返回空列表而不是抛:蒸馏挂了不该影响已经完成的任务,而空列表会让管道走到
    "这次没提炼出东西"那条正常路径上。

    卡片是产物目录 `staging/lessons/` 下的真实文件(PRD 34),Job 成败已经由 staging
    校验裁决过——这里装载的是已验证的产物,不是解包 JSON。
    """
    from agentgenome.genome.staging import STAGING_DIR, load_lesson_candidates

    slot = orchestrator.bus(task).allocate(STAGE_DISTILL)
    try:
        result = await orchestrator.dispatch(
            task,
            DISTILL_PROCEDURE,
            DISTILL_EMPLOYEE,
            slot,
            TaskState.CREATED,
            inputs={"task_id": task.id, "material": material.as_dict()},
        )
        return load_lesson_candidates(slot.path / STAGING_DIR) if result.ok else []
    except Exception:  # noqa: BLE001
        return []


def material_of(workspace_root: Path, task: Task) -> Material:
    """给 Procedure 派发用的素材。独立出来是为了 CLI 能单独看一眼。"""
    return collect(Path(workspace_root), task)


def _note(log: EventLog, task: Task, result: DistillResult) -> None:
    log.append(
        task.id,
        actor=ORCHESTRATOR,
        kind=LogKind.DISTILLED,
        payload=result.as_dict(),
    )


__all__ = [
    "CANDIDATES_DIR",
    "DISTILL_EMPLOYEE",
    "DISTILL_PROCEDURE",
    "STAGE_DISTILL",
    "DistillResult",
    "distill",
    "distill_with_agent",
    "land_cards",
    "lint",
    "material_of",
]
