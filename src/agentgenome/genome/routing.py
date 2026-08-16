"""把一次改动路由到功能点:上下文该带哪几张卡片。

## 为什么模块粒度不够

现在的基因组切片按**模块**过滤。员工要改订单服务的退款流程,拿到的是这个模块全部功能点的
全部知识——真正相关的那几十行被淹没在四十九条无关描述里,而预算已经花掉了大半。

## 三个输入来源,合并去重、不分优先级

需求解析给的模块与关键词、上一轮的变更文件清单、**上一轮门禁与集测报告里的失败文件与嫌疑
文件**。它们指向的是同一个问题的不同侧面。

第三条是这一层最大的实际收益:门禁告诉你哪个文件炸了,路由就把覆盖那个文件的知识捞到员工
面前——而此前那份信息完全没被用起来。

## 纯函数

输入是路由输入与知识树,输出是命中与未命中。不做 I/O、不看任务状态,所以它能被直接测,
不需要拉起一个任务。

## 零命中有两种,必须分开

「输入集合本身是空的」(正常的第一轮)与「输入非空但一条都没匹配上」(路由或覆盖范围写错了)
在结果上都是零命中。不分开的话,一次静默失败长得和正常情况一模一样——而这正是路径形态出错
时的表现:失败报告给了绝对路径,于是每一条都匹配不上,而系统看起来只是"这次没有相关知识"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentgenome.core.scope import compile_glob
from agentgenome.genome.features import FeatureEntry, KnowledgeTree


@dataclass(frozen=True)
class RouteInputs:
    """这次任务碰到了哪些地方。"""

    #: 上一轮开发结果里的变更文件清单(Workspace 相对路径)。
    paths: tuple[str, ...] = ()
    #: 门禁失败与集测嫌疑文件。**与 `paths` 分开收**,只为让"失败报告参与路由"这件事
    #: 在调用点上是可见的;进了路由就合成一份,不分优先级。
    failure_paths: tuple[str, ...] = ()
    #: 需求解析给的关键词。第一轮还没有任何文件路径时的兜底。
    keywords: tuple[str, ...] = ()

    @property
    def all_paths(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for item in (*self.paths, *self.failure_paths):
            seen.setdefault(_normalise(item), None)
        return tuple(seen)

    @property
    def is_empty(self) -> bool:
        return not self.all_paths and not self.keywords


@dataclass(frozen=True)
class Hit:
    """一次命中,连同它是怎么命中的。"""

    module_id: str
    feature: FeatureEntry
    #: 哪个输入匹配了哪条覆盖范围。**「为什么这个任务拿到了这张卡片」的答案。**
    reason: str


@dataclass(frozen=True)
class RouteRecord:
    """一次路由的完整结果。落盘之后,「为什么这个任务没拿到那张卡片」才答得上来。"""

    hits: tuple[Hit, ...] = ()
    #: 有卡片但这次没命中的功能点。声明为「无需卡片」的不在这里——它已经说过这里不需要
    #: 知识,再给一行目录是噪声。
    missed: tuple[FeatureEntry, ...] = ()
    #: 路由输入本身就是空的。**与"匹配不上"是两回事。**
    had_no_input: bool = False
    #: 看起来不是 Workspace 相对路径的输入。它们会静默零命中,所以要单独点出来。
    suspicious_paths: tuple[str, ...] = ()
    inputs: RouteInputs = field(default_factory=RouteInputs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "inputs": {
                "paths": list(self.inputs.paths),
                "failure_paths": list(self.inputs.failure_paths),
                "keywords": list(self.inputs.keywords),
            },
            "hits": [
                {
                    "module_id": hit.module_id,
                    "feature_id": hit.feature.id,
                    "reason": hit.reason,
                }
                for hit in self.hits
            ],
            "missed": [item.id for item in self.missed],
            "had_no_input": self.had_no_input,
            "suspicious_paths": list(self.suspicious_paths),
        }


def _normalise(path: str) -> str:
    return str(path).replace("\\", "/")


def _looks_relative(path: str) -> bool:
    """看起来是 Workspace 相对路径吗。

    绝对路径与 `../` 会**静默零命中**,而零命中和"确实没有相关知识"在结果上一模一样。
    这里不拦,只点名——路径形态是上游的事,这一层能做的是让它别无声无息。
    """
    return not path.startswith("/") and not path.startswith("../") and ":" not in path[:3]


def _match(feature: FeatureEntry, inputs: RouteInputs) -> str | None:
    """这个功能点被什么命中了。返回命中原因,没命中返回 `None`。"""
    for path in inputs.all_paths:
        for scope in feature.scope:
            if compile_glob(scope).match(path):
                return f"{path} 匹配 {scope}"
    haystack = f"{feature.id} {feature.summary}"
    for keyword in inputs.keywords:
        if keyword and keyword in haystack:
            return f"关键词 {keyword} 命中 {feature.id}"
    return None


def route(tree: KnowledgeTree, inputs: RouteInputs) -> RouteRecord:
    """这次任务该带哪几张功能卡片。

    命中多个时全部返回:挑一张会漏掉关键信息,而"哪几张放得下"是预算的事,不是这一层的事。
    """
    hits: list[Hit] = []
    missed: list[FeatureEntry] = []
    for module_id, features in tree.feature_index.items():
        for feature in features:
            if feature.card is None:
                # 声明过「这里不需要知识」的功能点,既不出正文也不出目录行。
                continue
            reason = _match(feature, inputs)
            if reason is None:
                missed.append(feature)
            else:
                hits.append(Hit(module_id, feature, reason))
    return RouteRecord(
        hits=tuple(hits),
        missed=tuple(missed),
        had_no_input=inputs.is_empty,
        suspicious_paths=tuple(path for path in inputs.all_paths if not _looks_relative(path)),
        inputs=inputs,
    )


__all__ = ["Hit", "RouteInputs", "RouteRecord", "route"]
