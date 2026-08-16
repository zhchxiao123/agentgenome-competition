"""把基因组同步进图谱,以及从告警反查到代码。

## 同步:确定性搬运

模块、契约、任务实体与它们之间的边,全部从项目地图与任务记录里读出来直接写。零 token。

## 告警到代码:沿图游走

`告警 → 服务 → 模块 → 最近改过它的任务`。排障从"人读日志猜模块"变成"图谱定位"。

**定位不到模块时不建任务。** 瞎猜一个模块比不建更糟:它会让一个数字员工去改一段与故障
无关的代码,而人还以为系统在处理这次告警。

## 自动建任务必须有闸

一次告警风暴能创建几百个任务,把预算烧光。按 `(服务, 时间窗)` 去重,外加一个总闸。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agentgenome.genome.models import ProjectMap
from agentgenome.umodel.graph import Entity, EntityKind, InMemoryGraph, Relation, RelationKind

#: 同一个服务在这个时间窗内只建一个修复任务。
DEFAULT_DEDUP_WINDOW_S = 900


def sync_project_map(graph: InMemoryGraph, project_map: ProjectMap) -> int:
    """把模块与契约写进图谱。**幂等**——同步会被反复触发。"""
    entities = [
        Entity(EntityKind.MODULE, module.id, {"path": module.path, "lang": module.lang or ""})
        for module in project_map.modules
    ]
    entities += [
        Entity(EntityKind.INTERFACE, face.id, {"kind": face.kind, "schema": face.schema_path or ""})
        for face in project_map.interfaces
    ]

    relations = []
    for module in project_map.modules:
        source = f"{EntityKind.MODULE.value}:{module.id}"
        relations += [
            Relation(RelationKind.DEPENDS_ON, source, f"{EntityKind.MODULE.value}:{dep}")
            for dep in module.depends_on
        ]
    for face in project_map.interfaces:
        target = f"{EntityKind.INTERFACE.value}:{face.id}"
        relations.append(
            Relation(RelationKind.PROVIDES, f"{EntityKind.MODULE.value}:{face.provider}", target)
        )
        relations += [
            Relation(RelationKind.CONSUMES, f"{EntityKind.MODULE.value}:{consumer}", target)
            for consumer in face.consumers
        ]
    return graph.upsert(entities, relations)


def sync_task(graph: InMemoryGraph, task_id: str, modules: Sequence[str], merged_at: str) -> int:
    """任务合并之后,把"它改过哪些模块"写进图谱。

    这条边是告警反查的最后一跳:从模块找到最近改过它的任务,而那正是排障第一个要看的东西。
    """
    source = f"{EntityKind.TASK.value}:{task_id}"
    return graph.upsert(
        [Entity(EntityKind.TASK, task_id, {"merged_at": merged_at})],
        [
            Relation(RelationKind.CHANGED, source, f"{EntityKind.MODULE.value}:{module}")
            for module in modules
        ],
    )


@dataclass(frozen=True)
class Alert:
    """一条生产告警。"""

    id: str
    service: str
    summary: str
    fired_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class Localisation:
    """告警定位的结果。"""

    alert: Alert
    modules: tuple[str, ...] = ()
    recent_tasks: tuple[str, ...] = ()

    @property
    def located(self) -> bool:
        return bool(self.modules)

    def as_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert.id,
            "service": self.alert.service,
            "modules": list(self.modules),
            "recent_tasks": list(self.recent_tasks),
            "located": self.located,
        }

    def requirement(self) -> str:
        """自动建的任务用它当需求原文。

        **把告警上下文、嫌疑模块与最近变更一起写进去**——需求解析阶段就能看到,不必再去
        查一遍图谱。
        """
        lines = [
            f"生产告警 {self.alert.id}:{self.alert.summary}",
            "",
            f"告警服务:{self.alert.service}",
            f"嫌疑模块:{', '.join(self.modules) or '(定位不到)'}",
        ]
        if self.recent_tasks:
            lines.append(f"最近改过这些模块的任务:{', '.join(self.recent_tasks)}")
        lines += [
            "",
            "先确认这次告警与最近的哪次变更相关,再动手。定位不了就写进 questions,不要猜。",
        ]
        return "\n".join(lines)


def localise(graph: InMemoryGraph, alert: Alert, recent: int = 3) -> Localisation:
    """沿图游走:`告警 → 服务 → 模块 → 最近改过它的任务`。"""
    service_key = f"{EntityKind.SERVICE.value}:{alert.service}"
    modules = [
        key.split(":", 1)[1] for key in graph.neighbors(service_key, RelationKind.DEPLOYED_FROM)
    ]

    tasks: list[tuple[str, str]] = []
    for module in modules:
        for task_key in graph.inbound(f"{EntityKind.MODULE.value}:{module}", RelationKind.CHANGED):
            entity = graph.entity(task_key)
            merged = str((entity.props if entity else {}).get("merged_at", ""))
            item = (merged, task_key.split(":", 1)[1])
            if item not in tasks:
                tasks.append(item)
    # 最近的排前面:排障第一个要看的就是"上一次动它是什么时候"。
    tasks.sort(reverse=True)
    return Localisation(
        alert=alert,
        modules=tuple(sorted(modules)),
        recent_tasks=tuple(task_id for _, task_id in tasks[:recent]),
    )


class AlertGate:
    """自动建任务的闸。

    **一次告警风暴能创建几百个任务,把预算烧光。** 按 `(服务, 时间窗)` 去重:同一个服务在
    窗口内只建一个,后续的只记录不建。

    去重键用**服务**而不是告警 id:同一个故障会产生一串 id 各不相同的告警,按 id 去重等于
    没有去重。
    """

    def __init__(self, window_s: int = DEFAULT_DEDUP_WINDOW_S, enabled: bool = True) -> None:
        self.window = timedelta(seconds=window_s)
        self.enabled = enabled
        self._seen: dict[str, datetime] = {}

    def admit(self, alert: Alert) -> tuple[bool, str]:
        """这条告警该不该建任务。返回 `(准不准, 理由)`。"""
        if not self.enabled:
            return False, "自动建任务的总闸是关的,只记录不建"
        last = self._seen.get(alert.service)
        if last is not None and alert.fired_at - last < self.window:
            return False, f"{alert.service} 在 {self.window.seconds} 秒内已经建过任务,去重"
        self._seen[alert.service] = alert.fired_at
        return True, "准入"


__all__ = [
    "DEFAULT_DEDUP_WINDOW_S",
    "Alert",
    "AlertGate",
    "Localisation",
    "localise",
    "sync_project_map",
    "sync_task",
]
