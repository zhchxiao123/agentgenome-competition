"""会话的两个出口:咨询转任务、质询回注意见。

## 共同的规矩:确认之前不落库

`escalate` 产出的是**草稿**,不是任务。自动建任务会让"随口问一句"变成"莫名多了个任务",
而咨询会话的全部价值就在于它足够轻——轻到人愿意随便问。一旦问一句就可能产生一个待办,
人就会开始犹豫要不要问,这个平面也就废了。

`inject` 写的是**意见**,不是审批结果。审批人身份仍由服务端二次校验,会话不构成身份旁路
——能问不等于能批,合起来会让"我问过了"变成一种事实上的审批权。

## 来源会话 id 双向有用

建出来的任务记得住它是从哪次对话来的;回头看一个任务时也能找到当初把它讲清楚的那次对话。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentgenome.sessions import log
from agentgenome.sessions.model import Session
from agentgenome.space.git_ws import SESSION_BRANCH_PREFIX

#: 需求草稿的标题长度上限。太长的标题在看板上会被截断,不如在这里就切好。
TITLE_LIMIT = 40

INJECTIONS_FILE = "injections.jsonl"


def draft_from(session: Session, history: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """把一次会话蒸馏成需求草稿。

    **这一版是确定性的**:取对话里用户说过的话拼成需求正文,标题取第一句。让架构员工做一次
    judgment call 会更好,但那要多烧一次 token,而且在草稿本来就要人过目的前提下收益有限
    ——**人反正要改**。要换成 agentic 版本时,替换这个函数即可,出口契约不变。

    ## 可写会话要带上它那条分支

    可写会话已经改了东西,那些改动躺在 `session/<id>` 上。草稿不带这个信息的话,接手的人
    只拿到一段需求描述,而**已经写好的代码没有任何线索能找回来**——它既不在主线上,也不在
    任何任务分支上,除非有人恰好记得去列 `session/` 开头的分支。

    "转成任务"是可写会话唯一的出口(改动要进主线只能由任务接管后走门禁与审批),这条线索
    因此是这个出口能不能走通的前提,不是一个锦上添花的字段。
    """
    questions = [
        str(item.get("text", "")).strip()
        for item in history
        if item.get("kind") == "text" and (item.get("detail") or {}).get("role") == "user"
    ]
    questions = [item for item in questions if item]
    body = "\n".join(f"- {item}" for item in questions)
    title = session.title or (questions[0][:TITLE_LIMIT] if questions else "来自会话的需求")
    return {
        "title": title,
        "requirement": body or "(这次会话里没有提出具体需求,请补充)",
        "modules": [],
        "source_session_id": session.id,
        # 只读会话没有分支可交——给一个空串而不是省略这个键,调用方少一次"有没有这个字段"
        # 的分支判断。
        "source_branch": f"{SESSION_BRANCH_PREFIX}/{session.id}" if session.writable else "",
    }


def record_injection(
    root: Path, session: Session, decision: str, comment: str, now: datetime | None = None
) -> Path:
    """把质询结论落成一条待回注的意见。

    **不直接改任务状态。** 它只是把结论记下来并带上来源会话;真正的审批仍要走
    `POST /tasks/{id}/approval`,那里会二次校验审批人身份。
    """
    target = log.session_dir(root, session.id) / INJECTIONS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session.id,
        "task_id": session.task_id,
        "decision": decision,
        "comment": comment,
        "created_at": (now or datetime.now(UTC)).isoformat(),
    }
    with target.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return target


def injections(root: Path, session_id: str) -> list[dict[str, Any]]:
    target = log.session_dir(root, session_id) / INJECTIONS_FILE
    if not target.is_file():
        return []
    return [
        json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


__all__ = ["INJECTIONS_FILE", "TITLE_LIMIT", "draft_from", "injections", "record_injection"]
