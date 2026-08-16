"""能派发的执行拓扑,以及它们在人面前长什么样。

## 一份名单,三个消费者

派发时用它裁决"这张图构造得出来吗"(`jobs.orchestrator`),提交时用它裁决"这个拓扑名合法
吗"(REST 与 CLI 各一处入口)。**三处问的是同一个问题,所以答案只有这一份**——分三处登记
的话,加第六个模板时漏掉的那一处不会报错,只会让一个明明能跑的策略在某条入口上被拒。

## 注册了执行器不等于派得出来

`core.topology` 的执行器注册表比这份名单宽:`test-first` 与 `probe-after-gate` 有执行器,
但它们的图是**别的模板在运行时拼出来的**(前置链、判定链),不是人在提交时能点的东西。
校验拿执行器注册表当名单的话,一个点了 `test-first` 的任务会一路活到派发那一刻才炸。

## 说明文案住在这里,不住在前端

`summary` 与 `steps` 是"这个策略是什么、选了它会发生什么"。放后端的理由不是分层洁癖:
前端硬编码一份的话,加模板时前端要跟着改,而漏改的表现是**下拉里出现一个没有说明的选项**
或者更糟——右栏还在描述另一条流程。`steps` 描述的是整条任务流水线,差别落在开发那一步上,
因为拓扑管的正是"一个状态内部怎么协作"。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from agentgenome.config import Config
from agentgenome.core.task import Task
from agentgenome.core.topology import (
    ASSISTED,
    BEST_OF_N,
    CRITIQUE_LOOP,
    DAG,
    SINGLE,
    UnknownTopology,
)

#: 现在能真的派出去的模板。**注册了执行器不等于派得出来**——见模块说明。
BUILDABLE = frozenset({SINGLE, CRITIQUE_LOOP, ASSISTED, DAG, BEST_OF_N})


@dataclass(frozen=True)
class TemplateOption:
    """一个执行拓扑在提交页上的样子。"""

    id: str
    name: str
    summary: str
    #: 选了它之后整条流水线会发生什么。**逐条给人看**,不是一段散文。
    steps: tuple[str, ...] = ()
    #: 机制在、结论还没有。带这个标记的选项不该被读成推荐。
    experimental: bool = False
    #: 现在点不点得了。**不是"存不存在"**:点不了的也要列出来并说明原因,静默消失会让人
    #: 以为这个能力根本不存在,于是"要不要用"这个问题连提都提不出来。
    available: bool = True
    unavailable_reason: str = ""
    #: 比单路贵几倍。单路自己是 1。
    cost_multiplier: int = 1
    #: 按这个项目**真实花过的钱**折算出来的绝对值。**没有历史就是 None**,不是 0——
    #: 见 `estimate_tokens`。
    cost_estimate_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """摊平给出去。**REST 与 CLI 共用这一个**——各摊一遍的话,加一个字段时漏掉的
        那一处会安静地少给一样东西(比如少了倍数,而那正是这份数据存在的理由)。
        """
        return {**asdict(self), "steps": list(self.steps)}


#: 每个模板的固定文案。可用性与成本是算出来的,不在这里。
_COPY: dict[str, tuple[str, str, tuple[str, ...]]] = {
    SINGLE: (
        "单路",
        "一个员工干一道工序,今天的样子。不确定选什么时就是它。",
        (
            "架构员工读需求与项目地图,产出计划 —— 你在写代码之前先看到它",
            "开发员工在隔离工作区实现,自跑本模块测试",
            "质量门禁。挂了带着失败报告回开发,最多 3 轮",
            "影响规则判定要不要跑集成测试",
            "提交前检查:越权复查 + 密钥扫描 + 风险评级",
            "低风险自动合并;高风险停下来等你",
        ),
    ),
    CRITIQUE_LOOP: (
        "精化环",
        "开发之后加一轮「只批不改」的评审,再由开发按意见改一遍。多花评审的钱,换少走一轮返工。",
        (
            "架构员工读需求与项目地图,产出计划",
            "开发员工实现",
            "**评审员工只批不改**:指出问题,不动代码",
            "开发员工按意见改一遍。收敛或到轮次上限就出环",
            "质量门禁 → 集成测试判定 → 提交前检查",
            "低风险自动合并;高风险停下来等你",
        ),
    ),
    ASSISTED: (
        "人确认",
        "员工的产出先变成一条待办,人点头才往下走。信任还没建立时从这里起步。",
        (
            "架构员工读需求与项目地图,产出计划",
            "开发员工实现",
            "**产出变成一条待办等人确认**,人不点头就停在这儿",
            "质量门禁 → 集成测试判定 → 提交前检查",
            "低风险自动合并;高风险停下来等你",
        ),
    ),
    DAG: (
        "并行图",
        "计划把活拆成几个能同时干的节点,各自一个工作区,按拓扑序合并。适合改动面宽的需求。",
        (
            "架构员工产出计划,**并且把活拆成一张图**",
            "能并行的节点同时开工,各自一个隔离工作区",
            "按拓扑序合并回任务分支;有节点挂了只重跑那一个",
            "质量门禁 → 集成测试判定 → 提交前检查",
            "低风险自动合并;高风险停下来等你",
        ),
    ),
    BEST_OF_N: (
        "多路择优",
        "同一道工序 N 路并行,各带不同方案取向,**门禁即适应度**,再由裁决者从过闸的里面挑一个。",
        (
            "架构员工读需求与项目地图,产出计划",
            "**N 路并行实现**,各带一个方案取向(最小改动 / 性能优先 / 契约先行)",
            "**每一路各自过门禁**——跑不过测试的方案再优雅也出局",
            "裁决者只在过闸的里面挑;一路都没过就带着 N 份失败对比回开发",
            "胜者合回任务分支,落选方案进经验蒸馏",
            "集成测试判定 → 提交前检查 → 合并或等你",
        ),
    ),
}

#: 一路的"多路择优"不是择优,是单路加一次裁决的钱。
_BEST_OF_N_NEEDS_TWO = "至少要两路变体才谈得上择优(topology.best_of_n.attempts)"

#: 估算取多少条历史。**中位数而不是均值**:一个跑飞了的任务能把均值拉到没人认得出来,
#: 而看这个数字的人正要拿它决定花不花这笔钱。
_HISTORY_WINDOW = 20


def check_choice(template_id: str) -> None:
    """任务级执行拓扑的合法性。**REST 与 CLI 共用这一个函数**。

    空表示"跟随项目缺省",是合法的:提交时不把它展开成具体模板名,否则"没表态"与
    "明确选了 single"会变成同一条记录,而它们在追因时是两件不同的事。
    """
    if not template_id or template_id in BUILDABLE:
        return
    raise UnknownTopology(
        f"没有 {template_id} 这个执行拓扑(能选的: {', '.join(sorted(BUILDABLE))})"
    )


def single_path_spends(tasks: Iterable[Task]) -> tuple[int, ...]:
    """哪些任务算得上"单路花了多少钱"的样本。**REST 与 CLI 共用这一个判定。**

    **只算已经结束的任务**:跑到一半的那些只花了一部分,拿它们当样本估出来的数会系统性
    偏低——而这个数字正是给人拿来决定要不要花 N 倍钱的。

    **只算真的按单路跑过的任务**:精化环多一轮批判、并行图多几个节点、多路择优本身就是
    N 倍。把它们算进"单路要花多少"里,倍数就被算了两遍——而这个数存在的全部意义是让人
    比较"一路"与"N 路"。空的 `topology` 也算数:它表示"跟随项目缺省",而缺省就是单路。

    顺序原样传下去(任务库给的是新的在前),`estimate_tokens` 的窗口靠它截"最近"。
    """
    return tuple(
        task.tokens_used
        for task in tasks
        if task.is_terminal and task.topology in ("", SINGLE)
    )


def estimate_tokens(spends: Iterable[int]) -> int | None:
    """单路任务实际花了多少。**编不出来就返回 None,不返回 0。**

    显示一个假的绝对值比不显示更糟:人会拿它做决定,而 0 读起来像"不要钱"。

    **花了 0 的任务不算数**——在第一步就被取消的任务是一条真实记录,但它不是"一个任务
    要花多少钱"这个问题的答案;算进去的话,估算会一路趋近于 0 而没有任何人发现。

    `spends` **按时间倒序给,新的在前**:窗口要截的是"最近这些",不是"最贵的那些"。
    先排序再截的话,截出来的永远是花得最多的那批,而估算会一路偏高。
    """
    recent = [spend for spend in spends if spend > 0][:_HISTORY_WINDOW]
    real = sorted(recent)
    if not real:
        return None
    middle = len(real) // 2
    if len(real) % 2:
        return real[middle]
    return (real[middle - 1] + real[middle]) // 2


def options(config: Config, spends: Iterable[int] = ()) -> tuple[TemplateOption, ...]:
    """提交页上能看到的全部拓扑,按名单顺序。

    `spends` 是这个项目单路任务真实花过的 token。**由调用方给**:这一层不认识任务库,
    而"哪些任务算数"是一个会变的产品判断,不该长在文案表旁边。
    """
    single = estimate_tokens(spends)
    return tuple(_option(template_id, config, single) for template_id in sorted(BUILDABLE))


def _option(template_id: str, config: Config, single: int | None) -> TemplateOption:
    name, summary, steps = _COPY[template_id]
    experimental = template_id == BEST_OF_N
    available = True
    reason = ""
    multiplier = 1
    if template_id == BEST_OF_N:
        multiplier = len(config.topology.best_of_n.attempts)
        if multiplier < 2:
            available = False
            reason = _BEST_OF_N_NEEDS_TWO
    return TemplateOption(
        id=template_id,
        name=name,
        summary=summary,
        steps=steps,
        experimental=experimental,
        available=available,
        unavailable_reason=reason,
        cost_multiplier=multiplier,
        cost_estimate_tokens=None if single is None else single * multiplier,
    )


__all__ = [
    "BUILDABLE",
    "TemplateOption",
    "check_choice",
    "estimate_tokens",
    "single_path_spends",
    "options",
]
