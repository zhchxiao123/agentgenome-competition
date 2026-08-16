"""语义图谱:把生产对象与基因组对象连成一张图。

## 为什么是一个窄口

对 UModel 的访问收在一个协议后面,与 `Forge` 同一个思路。三个理由:

1. **没有 UModel 的部署照样能跑。** 图谱是增值,不是流程的一部分;
2. **测试不必依赖一个外部服务**——本地假实现走同一套断言;
3. 换一个图数据库时换的是实现,不是每个调用点。

## 同步是确定性的

项目地图变更或任务合并之后,脚本把实体与关系写进去。**不用 Agent**——这是一次数据搬运,
让大模型去"理解并同步"是纯粹的浪费和风险:它可能把 `order-service` 写成 `order_service`,
而那条边就此断了,没有任何报错。

## 同步失败不影响任务

与蒸馏管道同一条原则:任务已经完成了,一次图谱写入失败不该让它看起来失败。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class EntityKind(StrEnum):
    """图上的实体类型。**词汇只定在这一处。**

    两边各定一套的话,生产侧写进来的 `Service` 与我们写的 `service` 会是两个不同的节点,
    而"服务由哪个模块部署而来"这条边永远连不上。
    """

    MODULE = "module"
    INTERFACE = "interface"
    TASK = "task"
    #: 以下由运维已有的链路写入,我们只读。
    SERVICE = "service"
    ALERT = "alert"


class RelationKind(StrEnum):
    """边的类型。方向是有意义的,不要反过来读。"""

    #: 模块 → 契约:这个模块提供这个接口。
    PROVIDES = "provides"
    #: 模块 → 契约:这个模块消费这个接口。
    CONSUMES = "consumes"
    #: 模块 → 模块:依赖。
    DEPENDS_ON = "depends_on"
    #: 任务 → 模块:这个任务改过这个模块。
    CHANGED = "changed"
    #: 服务 → 模块:这个服务由这个模块部署而来。**由运维侧写入。**
    DEPLOYED_FROM = "deployed_from"
    #: 告警 → 服务:这条告警挂在这个服务上。**由运维侧写入。**
    FIRES_ON = "fires_on"


@dataclass(frozen=True)
class Entity:
    kind: EntityKind
    id: str
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.id}"


@dataclass(frozen=True)
class Relation:
    kind: RelationKind
    source: str
    target: str

    @property
    def key(self) -> str:
        return f"{self.source}-[{self.kind.value}]->{self.target}"


@runtime_checkable
class GraphStore(Protocol):
    """图谱的窄口。**只有四个方法**——窄口的价值就在于窄。"""

    def upsert(self, entities: Iterable[Entity], relations: Iterable[Relation]) -> int: ...

    def neighbors(self, key: str, kind: RelationKind | None = None) -> list[str]: ...

    def entity(self, key: str) -> Entity | None: ...

    def all_keys(self) -> list[str]: ...


class InMemoryGraph:
    """本地实现。测试用它,没配 UModel 的部署也用它。

    **不是一个"假"实现。** 它的语义与真实图谱完全一致,只是不持久化——所以拿它写的断言
    换成真实现之后仍然成立。
    """

    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        self._out: dict[str, list[Relation]] = {}

    def upsert(self, entities: Iterable[Entity], relations: Iterable[Relation]) -> int:
        """写入或覆盖。**幂等**——同步会被反复触发,重复写不该产生重复节点。"""
        changed = 0
        for entity in entities:
            if self._entities.get(entity.key) != entity:
                self._entities[entity.key] = entity
                changed += 1
        for relation in relations:
            edges = self._out.setdefault(relation.source, [])
            if all(item.key != relation.key for item in edges):
                edges.append(relation)
                changed += 1
        return changed

    def neighbors(self, key: str, kind: RelationKind | None = None) -> list[str]:
        return [item.target for item in self._out.get(key, []) if kind is None or item.kind is kind]

    def inbound(self, key: str, kind: RelationKind | None = None) -> list[str]:
        """指向这个节点的都有谁。告警反查靠它——边的方向是 `告警 → 服务 → 模块`,
        而我们要从服务找模块时走的是正向,从模块找任务时走的是反向。"""
        found = []
        for source, edges in self._out.items():
            for item in edges:
                if item.target == key and (kind is None or item.kind is kind):
                    found.append(source)
        return found

    def entity(self, key: str) -> Entity | None:
        return self._entities.get(key)

    def all_keys(self) -> list[str]:
        return sorted(self._entities)


__all__ = [
    "Entity",
    "EntityKind",
    "GraphStore",
    "InMemoryGraph",
    "Relation",
    "RelationKind",
]
