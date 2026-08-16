"""任务中途扩到的模块:授予、留痕、读回。

## 为什么不写回计划产物

计划是架构员工的产物。让编排器回写它,"这份计划是谁写的"就变成一个答不出的问题——而计划
恰恰是事后复盘"当初怎么理解这个需求"的唯一依据。扩权是**运行态**的事实,住在任务目录里。

## 为什么留全理由

扩权自动放行,对价是这个任务必经人工审批。审批人面对的是一份可能横跨两个域的 diff,
不告诉他"原本只授权了订单域、中途申请加了库存域、理由是 X",他就得自己从 diff 里把这件事
重新推一遍——而那恰恰是最容易漏看的部分。
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentgenome.core.store import task_dir

#: 扩权记录在任务目录里的位置。
GRANTS_FILE = "scope-grants.json"


@dataclass(frozen=True)
class ScopeGrant:
    """一次被批准的扩权。"""

    module: str
    reason: str
    round_: int

    def as_dict(self) -> dict[str, Any]:
        return {"module": self.module, "reason": self.reason, "round": self.round_}


def read_grants(workspace_root: Path, task_id: str) -> list[ScopeGrant]:
    """这个任务到目前为止扩到了哪些模块。**没扩过就是空列表**,不是错误。"""
    path = task_dir(workspace_root, task_id) / GRANTS_FILE
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 读不出来时当作没扩过。方向是**更窄**——而更窄是可恢复的:员工撞上越权、拿到
        # 失败报告、下一轮再申请一次。反过来把读不出来当成"扩过了"才是危险的。
        return []
    if not isinstance(payload, list):
        return []
    return [
        ScopeGrant(
            module=str(item.get("module", "")),
            reason=str(item.get("reason", "")),
            round_=int(item.get("round", 0)),
        )
        for item in payload
        if isinstance(item, dict) and item.get("module")
    ]


def append_grants(workspace_root: Path, task_id: str, grants: list[ScopeGrant]) -> list[ScopeGrant]:
    """把新批准的扩权追加进去,返回追加之后的全集。"""
    everything = [*read_grants(workspace_root, task_id), *grants]
    path = task_dir(workspace_root, task_id) / GRANTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([item.as_dict() for item in everything], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return everything


def granted_modules(workspace_root: Path, task_id: str) -> list[str]:
    """扩到的模块 id,去重、保序。"""
    return list(dict.fromkeys(item.module for item in read_grants(workspace_root, task_id)))


def effective_modules(workspace_root: Path, task_id: str) -> list[str]:
    """这个任务**当前**授权的模块:计划命中的,加上中途批准扩到的。

    **派发、结对复查、提交前复查、手工派发都必须用它,而不是只用计划。** 只用计划的话,
    扩权批下来却进不了 Job 的授权——整条申请通道是死的,而且死得很安静:批准留了痕、
    事件也记了,只有员工下一轮照样撞墙。这个 bug 真的发生过一次。
    """
    from agentgenome.jobs.plan_io import load_plan_modules, read_plan

    planned = load_plan_modules(read_plan(workspace_root, task_id))
    return list(dict.fromkeys([*planned, *granted_modules(workspace_root, task_id)]))


def effective_mount_paths(workspace_root: Path, task_id: str) -> list[str]:
    """生效模块的**挂载点**。权限层说的是路径,而身份到位置的翻译只做一次。

    读不出根索引就给空,方向是**更窄**——更窄是可恢复的(员工撞上越权、拿到失败报告、
    下一轮再来),反过来把读不出来当成"不限制"才是危险的。
    """
    from agentgenome.genome.loader import load_project_map

    modules = effective_modules(workspace_root, task_id)
    if not modules:
        return []
    with contextlib.suppress(Exception):
        return load_project_map(workspace_root).mount_paths(modules)
    return []


__all__ = [
    "GRANTS_FILE",
    "effective_modules",
    "effective_mount_paths",
    "ScopeGrant",
    "append_grants",
    "granted_modules",
    "read_grants",
]
