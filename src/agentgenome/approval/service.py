"""审批:高风险任务停下来等一个明确的答复。

## 身份在服务端校验,不信任调用方声明

`--as <who>` 只是**声明**。名单来自根配置 `approval.approvers`,由这一层核对。不核对的话
这道关卡就只是个仪式——任何能调 CLI 的人都能自己批准自己,而高风险评级也就白评了。

真正的身份认证(SSO、细粒度角色)在 PRD 18。本期是配置文件里的一个列表,但**校验这个动作
本身必须在服务端**:先把位置留对,后面换认证源只是换数据来源。

## 驳回必须带意见

没有意见的驳回对下一轮没有任何价值——员工只知道"人不满意",不知道哪里不满意,于是下一轮
大概率换个形式再犯一遍。所以空意见直接拒绝,而不是接受一个空字符串。

意见作为结构化条目注入下一轮开发上下文,与门禁失败报告同优先级、置顶。**这是人的判断唯一
能直接影响下一轮的通道**,回注得不好等于让人白读了一遍 diff。

## 超时不自动放行

没有任何一条代码路径会因为等太久而放行。自动放行会让高风险评级完全失去意义。代价是没人管
的任务会永远停着;配套的可见性(审批队列积压进指标)在 PRD 09。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentgenome.core.events import EventLog, LogKind
from agentgenome.core.states import TaskEvent, TaskState
from agentgenome.core.task import Task
from agentgenome.core.transitions import Facts
from agentgenome.jobs.orchestrator import Orchestrator
from agentgenome.jobs.reports import write_failure_report


class NotAnApprover(PermissionError):
    """这个身份不在审批人名单里。"""


class ApprovalRefused(RuntimeError):
    """这个任务现在不该被审批。"""


@dataclass(frozen=True)
class LineComment:
    """挂在 diff 具体一行上的批注。"""

    file: str
    line: int
    side: str = "new"
    content: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "side": self.side, "content": self.content}


@dataclass(frozen=True)
class ApprovalRecord:
    """一次审批动作。审批人与时间要能被追溯——责任归属靠它。"""

    task_id: str
    actor: str
    approved: bool
    comment: str = ""
    line_comments: tuple[LineComment, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "actor": self.actor,
            "approved": self.approved,
            "comment": self.comment,
            "line_comments": [item.as_dict() for item in self.line_comments],
        }


def render_rejection(comment: str, line_comments: tuple[LineComment, ...]) -> str:
    """驳回意见回注给下一轮的完整文本。

    **界面预览显示的必须是这同一份文本**,不是一份近似——否则"提交前预览"这个功能只是
    让审批人误以为自己确认过了实际发送的内容。行级批注排在前面:它比总意见精确一个量级,
    该是员工先读到的那部分。
    """
    lines = []
    for item in line_comments:
        lines.append(f"- {item.file}:{item.line}({item.side})——{item.content}")
    if comment.strip():
        lines.append(f"- (整体意见)——{comment.strip()}")
    return "\n".join(lines)


def approve(root: Path, task_id: str, actor: str, comment: str = "") -> Task:
    """批准一个任务,让它进入合并。"""
    return _decide(root, task_id, actor, approved=True, comment=comment)


def reject(
    root: Path,
    task_id: str,
    actor: str,
    comment: str,
    line_comments: tuple[LineComment, ...] = (),
) -> Task:
    """驳回一个任务,并把意见带给下一轮。"""
    if not comment.strip() and not line_comments:
        raise ApprovalRefused(
            "驳回必须附意见(整体或行级至少一种)。没有意见的驳回对下一轮没有任何价值——"
            "员工只知道人不满意,不知道哪里不满意。"
        )
    return _decide(
        root, task_id, actor, approved=False, comment=comment, line_comments=line_comments
    )


def approvers(orchestrator: Orchestrator) -> list[str]:
    return list(orchestrator.config.approval.approvers)


def _decide(
    root: Path,
    task_id: str,
    actor: str,
    approved: bool,
    comment: str,
    line_comments: tuple[LineComment, ...] = (),
) -> Task:
    orchestrator = Orchestrator(root)
    task = orchestrator.store.get(task_id)

    # **身份先于状态。** 反过来的话,一个不在名单里的人会先被告知"这个任务不在等审批"——
    # 那是它无权知道的事,而且"你不是审批人"本来就是更根本的那条拒绝。
    allowed = approvers(orchestrator)
    if actor not in allowed:
        # 先记再抛:一次未遂的越权审批本身就是要留痕的事。
        EventLog(root).append(
            task_id,
            actor=actor,
            kind=LogKind.NOTE,
            payload={"note": "不在名单里的身份尝试审批,已拒绝", "approved": approved},
        )
        raise NotAnApprover(
            f"{actor} 不在审批人名单里(名单: {', '.join(allowed) or '(空)'})。"
            "名单配在根配置的 approval.approvers。"
        )

    if task.state is not TaskState.REVIEWING:
        raise ApprovalRefused(f"{task_id} 当前在 {task.state.value},不在等审批")

    record = ApprovalRecord(
        task_id=task_id,
        actor=actor,
        approved=approved,
        comment=comment,
        line_comments=line_comments,
    )
    EventLog(root).append(task_id, actor=actor, kind=LogKind.APPROVAL, payload=record.as_dict())
    if not approved:
        _land_comment(orchestrator, task, record)
    return orchestrator.deliver(
        task_id,
        TaskEvent.APPROVE if approved else TaskEvent.REJECT,
        facts=Facts(
            max_fix_rounds=orchestrator.max_fix_rounds,
            approver_ok=True,
            enforce_budget=orchestrator.config.budgets.enforce,
        ),
    )


def _land_comment(orchestrator: Orchestrator, task: Task, record: ApprovalRecord) -> None:
    """把驳回意见写成下一轮读得到的失败报告。

    **编号用递增之后的轮次**,与门禁失败报告同一套约定:下一轮开发的 `round_` 是它加一,
    读的时候按 `before_round` 过滤,两边对得上。
    """
    rejection = render_rejection(record.comment, record.line_comments)
    write_failure_report(
        orchestrator.store.task_dir(task.id),
        round_=task.fix_rounds + 1,
        title=f"第 {task.fix_rounds + 1} 轮被 {record.actor} 驳回",
        payload={
            "passed": False,
            "failures": [
                {
                    "message": f"人工评审驳回:\n{rejection}",
                    "evidence": {"approver": record.actor},
                }
            ],
        },
    )


__all__ = [
    "ApprovalRecord",
    "ApprovalRefused",
    "LineComment",
    "NotAnApprover",
    "approve",
    "approvers",
    "reject",
    "render_rejection",
]
