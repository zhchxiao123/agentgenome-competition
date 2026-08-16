"""待办的完成动作:人交回来的东西怎么接回流水线。

## 交完 = 产物已存在 = 崩溃恢复那条路

这是整套挂起机制不需要新恢复通路的原因:提交把产物写进**这次派发当初分配的那个产物槽**,
然后照常推一步——处理器的幂等约定会认出产物、走既有的重放路径抛出事件。

"继续"因此不依赖任何内存态,而挂起三天、进程重启十次都不影响它。

## 没有跳过校验的路径

人交的产物过与硅基员工**完全相同**的 schema 校验。有一个"跳过"按钮的话,"人也是一种运行时"
就只剩一半:流水线仍然会因为执行者是人而降低标准,而那正是这套设计要消灭的东西。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from agentgenome.agents.artifacts import RESULT_FILENAME
from agentgenome.agents.contract import check_result_contract, validate_result_payload
from agentgenome.config import load_config
from agentgenome.core.events import ORCHESTRATOR, ActorKind, EventLog, LogKind
from agentgenome.genome.procedures import load_workspace_registry
from agentgenome.jobs.split import VERDICT_SCHEMA, split_issues, write_verdict
from agentgenome.todo.store import DONE, PENDING, SPLIT, WORKTREE, Todo, TodoStore


class TodoRefused(RuntimeError):
    """这次提交不算数。

    **与"没有这张待办"分开**:调用方要能把"你要的东西不存在"与"存在但你不能这么干"分开
    ——前者是链接过期,后者是权限或状态问题,人看到的下一步完全不同。
    """


@dataclass(frozen=True)
class Submission:
    """一次提交的结果。"""

    todo: Todo
    ok: bool
    #: 校验没过时的原因,逐条可操作——它会被人直接照着改,与硅基员工拿到的是同一份。
    detail: str = ""


def submit(
    root: Path,
    todo_id: str,
    payload: dict[str, Any] | None = None,
    actor: str = "",
    now: datetime | None = None,
    roles: frozenset[str] = frozenset(),
) -> Submission:
    """人交活。

    `payload` 是产物类待办交上来的 `result.json`;工作树类待办不带它——那一类的产出是
    工作树里的真实改动,由完成动作那一侧的越权检查与门禁裁决(见 issue 03)。
    """
    store = TodoStore(root)
    todo = store.get(todo_id)
    log = EventLog(root)
    if todo.state != PENDING:
        raise TodoRefused(f"这张待办已经是 {todo.state} 了,不能再交: {todo_id}")
    if actor and not _may_submit(todo, actor, roles):
        # **服务端判,前端裁剪不是边界。** 一个能构造 HTTP 请求的人不受前端约束,
        # 而这条通路的产物会直接进流水线。
        #
        # **拒绝也要留痕**:"谁试过替别人交活"正是审计要问的问题,而一次被拒的提交在事件面
        # 上不存在的话,那个问题只能靠"没人报告过"来回答。
        log.append(
            todo.task_id,
            actor=actor,
            actor_kind=ActorKind.HUMAN,
            kind=LogKind.TODO,
            payload={"todo": todo.id, "action": "refused", "assignee": todo.assignee},
        )
        raise TodoRefused(f"{actor} 不是这张待办的指派人({todo.assignee}),不能替他交")

    if todo.kind == SPLIT:
        # **裁决不是产物。** 提案(result.json)已经在槽里了,人交的是"通过没有 + 意见"
        # ——写成旁边的 split-verdict.json,两个文件两个作者,谁都不覆盖谁。
        return _decide_the_split(root, todo, payload, actor, log)

    if todo.kind == WORKTREE:
        refusal = _check_the_worktree(root, todo, actor, log)
        if refusal is not None:
            return refusal
        payload = _with_the_real_diff(root, todo, payload)

    output_dir = root / todo.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (output_dir / RESULT_FILENAME).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    check = check_result_contract(output_dir, output_schema_of(root, todo.procedure_id))
    if not check.ok:
        # **失败不改待办状态。** 打回的产物留在原地供人对照修改——与硅基员工的契约重试
        # 拿到的是同一份现场。
        log.append(
            todo.task_id,
            actor=actor or todo.assignee,
            actor_kind=ActorKind.HUMAN,
            kind=LogKind.TODO,
            payload={"todo": todo.id, "action": "rejected", "detail": check.detail},
        )
        return Submission(todo=todo, ok=False, detail=check.detail or "产物不合契约")

    landed = store.save(replace(todo, state=DONE), now=now)
    log.append(
        todo.task_id,
        actor=actor or todo.assignee,
        actor_kind=ActorKind.HUMAN,
        kind=LogKind.TODO,
        payload={"todo": todo.id, "action": "submitted", "stage": todo.stage},
    )
    return Submission(todo=landed, ok=True)


def _decide_the_split(
    root: Path, todo: Todo, payload: dict[str, Any] | None, actor: str, log: EventLog
) -> Submission:
    """人对一份拆分提案的裁决落盘(PRD 48 D3)。

    **失败不改待办状态**——与产物类打回同一条纪律:裁决不合形状时留在原地让人改。
    """
    detail = validate_result_payload(payload, VERDICT_SCHEMA)
    if not detail and payload and payload.get("approved") and payload.get("children"):
        # 就地调整过的批次在**提交那一刻**验(PRD 48 issue 04):环、批外引用、配置上限。
        # 拖到落树再验的话,拒绝无处安放——待办已经 DONE,人没有"留在原地继续改"的路。
        edited = {"children": payload["children"]}
        issues = split_issues(edited, load_config(root).limits.split_max_children)
        if issues:
            detail = "; ".join(issues)
    if detail:
        log.append(
            todo.task_id,
            actor=actor or todo.assignee,
            actor_kind=ActorKind.HUMAN,
            kind=LogKind.TODO,
            payload={"todo": todo.id, "action": "rejected", "detail": detail},
        )
        return Submission(todo=todo, ok=False, detail=f"裁决不符合形状: {detail}")
    assert payload is not None  # required: [approved] 保证了非空
    write_verdict(root, todo.output_dir, payload)
    landed = TodoStore(root).save(replace(todo, state=DONE))
    log.append(
        todo.task_id,
        actor=actor or todo.assignee,
        actor_kind=ActorKind.HUMAN,
        kind=LogKind.TODO,
        payload={
            "todo": todo.id,
            "action": "submitted",
            "stage": todo.stage,
            # 落树不在这里发生:裁决只是落盘,树在编排器随后那一步长出来——
            # 与"交完 = 产物已存在 = 崩溃恢复那条路"同一条纪律。
            "split_approved": bool(payload.get("approved", False)),
        },
    )
    return Submission(todo=landed, ok=True)


def _may_submit(todo: Todo, actor: str, roles: frozenset[str]) -> bool:
    """这个人能不能交这张待办。

    **指派人可以是一个角色**(比如 `approver`):一份活派给"审批组"而不是某个具体的人,是
    真实团队的常态。所以判据是"你是这个人,或者你属于这个角色"——只认名字相等的话,派给角色
    的待办永远没人交得掉。
    """
    return actor == todo.assignee or todo.assignee in roles


def _check_the_worktree(root: Path, todo: Todo, actor: str, log: EventLog) -> Submission | None:
    """人在工作树里改完之后,照跑那条越权检查。

    **人在场不放宽边界。** 工具集这一层对人根本不存在(他手里是一台电脑),所以最小权限
    在这条路上**只剩这一道检查**——放过它,人就成了系统里唯一不受授权约束的执行者。

    与硅基员工的处置有一处**有意的不同:不回滚工作树**。回滚对机器是免费的(它重跑一遍
    就是了),对人不是——那是他刚花掉的半天。而这道检查真正要拦住的是"越权的改动进了合并",
    拒收这次提交已经拦住了;把人的劳动删掉不会让这条边界更牢,只会让人绕开这套系统干活。
    """
    from agentgenome.jobs.orchestrator import Orchestrator

    orchestrator = Orchestrator(root)
    task = orchestrator.store.get(todo.task_id)
    violated = orchestrator.enforce_human_scope(task, todo.employee_id)
    if violated is None:
        return None
    log.append(
        todo.task_id,
        actor=actor or todo.assignee,
        actor_kind=ActorKind.HUMAN,
        kind=LogKind.TODO,
        payload={"todo": todo.id, "action": "scope-violation", "detail": violated},
    )
    return Submission(
        todo=todo,
        ok=False,
        detail=f"改动超出这个任务的授权范围,没有收下这次提交:{violated}",
    )


def _with_the_real_diff(
    root: Path, todo: Todo, payload: dict[str, Any] | None
) -> dict[str, Any] | None:
    """把小票里的"我改了哪些文件"换成 git 里真实的那份。

    **验证产物,不验证自述**:自评会漏、会瞒,而 git 不会。这条对人和对员工是同一句话
    ——下游(集测判定、提交流水线)读的正是这个清单,它错一次,后面全错。
    """
    if payload is None:
        return None
    from agentgenome.jobs.orchestrator import Orchestrator

    orchestrator = Orchestrator(root)
    task = orchestrator.store.get(todo.task_id)
    return {**payload, "changed_files": orchestrator.human_changed_paths(task)}


def output_schema_of(root: Path, procedure_id: str) -> dict[str, Any]:
    """这个工序的产物 schema。

    **与硅基员工用的是同一份**——各读一份的话,人这条路迟早会因为某次 schema 变更而与
    机器那条路分叉,而分叉的方向永远是"人这边松一点"。
    """
    registry = load_workspace_registry(root)
    return dict(registry.get(procedure_id).output_schema)


def record_delivery(root: Path, todo: Todo, actor: str = ORCHESTRATOR) -> None:
    """投递也要进事件面。

    三平面对 human Job 没有例外:"这个 Job 是谁干的、何时投递何时完成"要能一条时间线讲清楚,
    人机混合的任务尤其如此。
    """
    EventLog(root).append(
        todo.task_id,
        actor=actor,
        kind=LogKind.TODO,
        payload={
            "todo": todo.id,
            "action": "delivered",
            "assignee": todo.assignee,
            "stage": todo.stage,
            "kind": todo.kind,
        },
    )


__all__ = ["Submission", "TodoRefused", "output_schema_of", "record_delivery", "submit"]
