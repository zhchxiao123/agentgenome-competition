"""供应动作在事件面上的样子。**一处**,命令行与界面共用。

## 为什么不各写一遍

两个入口做的是同一件事,而事件面会被人和报告反复读。各写一份的话,同一次对齐从界面
做与从命令行做会留下两种形状的记录——按 `action` 统计的报表于是要先学会两种方言,而
第三个入口出现时它会再学一种。

## 令牌不进载荷

留痕的价值在于能被反复读;凭证一旦进去就再也收不回来。这里只接受动作与结果,
构造载荷的权力不外放。
"""

from __future__ import annotations

from pathlib import Path

from agentgenome.agents.agentteams.provision import WorkerRef
from agentgenome.core.events import SYSTEM_SUBJECT, EventLog, LogKind


def record_provision(
    root: Path,
    *,
    actor: str,
    employee_id: str,
    ref: WorkerRef,
    action: str,
    entrance: str = "",
) -> None:
    """记一次对齐。

    **`unchanged` 不记**:什么都没做不该留下记录——一份满是"无变化"的事件流,会让
    "这个 Worker 上次真的动过是什么时候"变成一个要翻页找的问题。
    """
    if action == "unchanged":
        return
    EventLog(root).append(
        SYSTEM_SUBJECT,
        actor=actor,
        kind=LogKind.WORKER_PROVISIONED,
        payload={
            "employee_id": employee_id,
            "worker": ref.name,
            "room": ref.room_id,
            "action": action,
            "entrance": str(entrance),
        },
    )


def record_lifecycle(
    root: Path,
    *,
    actor: str,
    employee_id: str,
    action: str,
    entrance: str = "",
) -> None:
    """记一次休眠或删除。

    与对齐同一个事件类型:审计问的是"这个员工的容器被谁动过",而"建"与"删"是同一个
    问题的两个答案。**删除尤其要记**——它不可逆,房间会重建、id 会变。
    """
    EventLog(root).append(
        SYSTEM_SUBJECT,
        actor=actor,
        kind=LogKind.WORKER_PROVISIONED,
        payload={"employee_id": employee_id, "action": action, "entrance": str(entrance)},
    )


__all__ = ["record_lifecycle", "record_provision"]
