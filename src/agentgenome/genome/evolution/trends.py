"""效果度量:这套系统到底有没有在变强。

## 目标趋势是明确的

随任务量上升:平均修复轮次 ↓、门禁一次通过率 ↑、任务时长 ↓、升级人工率 ↓、知识命中率 ↑。
**方向写死在指标定义里**,不靠人记——一个不知道自己该往哪走的指标,看多久都得不出结论。

## 数据不足时说数据不足

**不硬画曲线。** 三个任务画出来的"下降趋势"是噪声,而一旦它被贴进汇报,后面所有的判断都
建立在它上面。这条比任何单个指标都重要:一个诚实说"还看不出来"的报告是有用的,一个画着好看
曲线的假报告是有害的。

## 周报是确定性渲染

不用 Agent 写。一份措辞漂亮但把趋势说反的周报,比一份干巴巴的糟得多。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: 少于这么多样本就不给趋势。
MIN_SAMPLES = 10


class Direction(StrEnum):
    """这个指标该往哪走。写死在定义里,不靠人记。"""

    DOWN = "down"
    UP = "up"

    @property
    def label(self) -> str:
        return "越低越好" if self is Direction.DOWN else "越高越好"


@dataclass(frozen=True)
class Metric:
    """一个指标的两期对比。"""

    name: str
    direction: Direction
    previous: float
    current: float
    samples: int

    @property
    def enough(self) -> bool:
        return self.samples >= MIN_SAMPLES

    @property
    def delta(self) -> float:
        return self.current - self.previous

    @property
    def improving(self) -> bool | None:
        """在变好吗。**样本不够时返回 `None`,不是 `False`。**

        返回 `False` 的话,"没数据"会被读成"在变差"——那是两件完全不同的事,而后者会触发
        本不该有的行动。
        """
        if not self.enough:
            return None
        if self.delta == 0:
            return None
        return self.delta < 0 if self.direction is Direction.DOWN else self.delta > 0

    def render(self) -> str:
        if not self.enough:
            return f"- **{self.name}**:数据不足({self.samples} / {MIN_SAMPLES} 个样本),不给趋势"
        arrow = {True: "✅ 在变好", False: "⚠️ 在变差", None: "— 持平"}[self.improving]
        return (
            f"- **{self.name}**:{self.previous:.2f} → {self.current:.2f}"
            f"({self.direction.label}){arrow}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction.value,
            "previous": self.previous,
            "current": self.current,
            "samples": self.samples,
            "enough": self.enough,
            "improving": self.improving,
        }


@dataclass(frozen=True)
class Weekly:
    """一份周报。"""

    period: str
    metrics: tuple[Metric, ...]

    @property
    def has_enough(self) -> bool:
        return any(metric.enough for metric in self.metrics)

    def render(self) -> str:
        lines = [f"# 进化周报 · {self.period}", ""]
        if not self.has_enough:
            # 一个诚实说"还看不出来"的报告是有用的,一个画着好看曲线的假报告是有害的。
            lines += [
                "**这一期数据不足,不给任何趋势结论。**",
                "",
                f"每个指标都需要至少 {MIN_SAMPLES} 个样本。这不是谨慎——三个任务画出来的",
                "「下降趋势」是噪声,而一旦它被贴进汇报,后面所有判断都建立在它上面。",
                "",
                "## 当前样本量",
                "",
            ]
            lines += [f"- {metric.name}:{metric.samples}" for metric in self.metrics]
            return "\n".join(lines) + "\n"

        lines += ["## 指标", ""]
        lines += [metric.render() for metric in self.metrics]

        better = [m.name for m in self.metrics if m.improving is True]
        worse = [m.name for m in self.metrics if m.improving is False]
        lines += ["", "## 结论", ""]
        lines.append(f"在变好:{', '.join(better) or '(无)'}")
        lines.append(f"在变差:{', '.join(worse) or '(无)'}")
        if worse:
            lines += ["", f"**{worse[0]}** 值得先看——趋势反了通常意味着某类任务在系统性地卡住。"]
        return "\n".join(lines) + "\n"


#: 五个指标与它们的目标方向。
DEFINITIONS: tuple[tuple[str, Direction], ...] = (
    ("平均修复轮次", Direction.DOWN),
    ("门禁一次通过率", Direction.UP),
    ("平均任务时长", Direction.DOWN),
    ("升级人工率", Direction.DOWN),
    ("知识命中率", Direction.UP),
)


def weekly(
    period: str, previous: Sequence[float], current: Sequence[float], samples: Sequence[int]
) -> Weekly:
    """把两期数值拼成一份周报。**纯函数。**"""
    metrics = tuple(
        Metric(
            name=name,
            direction=direction,
            previous=previous[index] if index < len(previous) else 0.0,
            current=current[index] if index < len(current) else 0.0,
            samples=samples[index] if index < len(samples) else 0,
        )
        for index, (name, direction) in enumerate(DEFINITIONS)
    )
    return Weekly(period=period, metrics=metrics)


__all__ = ["DEFINITIONS", "MIN_SAMPLES", "Direction", "Metric", "Weekly", "weekly"]
