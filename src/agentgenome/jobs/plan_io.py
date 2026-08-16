"""计划产物的读取。

**独立成模块,不挂在编排器上。** 只想看一眼计划的调用方(集成测试入口、授权收窄、命令行)
不该被迫构造一整个编排器——那会连带加载配置、开数据库、读规则,而它们一样都不需要。
此前这两个函数就是自由函数,只是住在编排器文件里;搬出来是因为"这个任务当前授权什么"
那一层要用它,而那一层被编排器导入,反过来导入会成环。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentgenome.core.store import task_dir

#: 计划产物在任务目录里的文件名。
PLAN_FILE = "plan.yaml"


def read_plan(workspace_root: Path, task_id: str) -> dict[str, Any] | None:
    """读回一个任务落好的计划。读不出来就是 `None`,不是空字典——两者在调用方要说不同的话。"""
    path = task_dir(workspace_root, task_id) / PLAN_FILE
    if not path.is_file():
        return None
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def load_plan_modules(payload: dict[str, Any] | None) -> list[str]:
    """从计划产物里取涉及模块。给上下文切片与授权收窄用。"""
    if not payload:
        return []
    modules = payload.get("modules")
    return [str(item) for item in modules] if isinstance(modules, list) else []


__all__ = ["PLAN_FILE", "load_plan_modules", "read_plan"]
