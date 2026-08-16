"""⑤ 按模块重建:同一条流水线的裁剪版。

某个模块大改之后,不该为了刷新那一块认知而重跑全库——那既贵又慢,而且会把其余模块上人已经
校对过的东西重新搅一遍。

## 跳过前两步

重建不需要重新扫描全仓,也不需要再问一次模块边界:**边界已经拍过板了**。它直接进深读与汇总。

## 产物先 diff 再进 PR

重建产出的是"这个模块现在看起来是什么样",而现有卡片里可能有人写过的东西。直接覆盖等于让
一次刷新吞掉一次人工校对——**而「我改过的东西下次扫描就没了」会让人彻底放弃维护知识**,
那时知识树就只剩机器写的那一半,而机器写的那一半正是最需要人复核的。

所以:带人工编辑标记的内容不动,其余的差异列成 diff 让人在 PR 上逐条比对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agentgenome.genome.staging import HUMAN_EDITED_MARKER


@dataclass(frozen=True)
class CardDiff:
    """一张卡片的重建差异。"""

    relative: str
    #: 这张卡片带人工编辑标记,重建不动它。
    protected: bool
    before: str
    after: str

    @property
    def changed(self) -> bool:
        return not self.protected and self.before != self.after


@dataclass
class RebuildPlan:
    """一次重建打算改什么。**先算出来给人看,再落盘。**"""

    module_id: str
    diffs: list[CardDiff] = field(default_factory=list)

    @property
    def protected(self) -> list[str]:
        return [item.relative for item in self.diffs if item.protected]

    @property
    def changed(self) -> list[str]:
        return [item.relative for item in self.diffs if item.changed and item.after]

    @property
    def removed(self) -> list[str]:
        """这一轮没再产出、会被退役的卡片。

        **单列一节。** 混在"会被改写的"里的话,"这次刷新删掉了哪张卡片"这条最该被看见的
        信息,会被一堆内容更新淹掉。
        """
        return [item.relative for item in self.diffs if item.changed and not item.after]

    def as_dict(self) -> dict[str, object]:
        return {
            "module_id": self.module_id,
            "changed": self.changed,
            "removed": self.removed,
            "protected": self.protected,
        }


def plan_rebuild(root: Path, module_id: str, produced: dict[str, str]) -> RebuildPlan:
    """算出这次重建会改什么。

    `produced` 是这一轮深读产出的 `Workspace 相对路径 → 新内容`。

    **带人工编辑标记的一律不动。** 判断按现有文件的内容,不按产出——产出里当然不会有那个
    标记,拿产出去判等于永远判不出来。
    """
    target = Path(root)
    plan = RebuildPlan(module_id=module_id)
    for relative in sorted(produced):
        path = target / relative
        before = path.read_text(encoding="utf-8") if path.is_file() else ""
        plan.diffs.append(
            CardDiff(
                relative=relative,
                protected=HUMAN_EDITED_MARKER in before,
                before=before,
                after=produced[relative],
            )
        )
    return plan


def apply_rebuild(root: Path, plan: RebuildPlan) -> list[str]:
    """把重建落盘。返回真正写过的文件。

    **受保护的一个都不碰**,哪怕内容确实变了——那正是这条保护存在的意义。
    """
    target = Path(root)
    written: list[str] = []
    for diff in plan.diffs:
        if not diff.changed:
            continue
        path = target / diff.relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(diff.after, encoding="utf-8")
        written.append(diff.relative)
    return written


def render_rebuild_report(plan: RebuildPlan) -> str:
    """给人在 PR 上比对的那一页。"""
    lines = [f"# 重建 {plan.module_id}", ""]
    lines += ["## 会被改写的", ""]
    lines += [f"- {item}" for item in plan.changed] or ["(没有变化)"]
    if plan.removed:
        lines += ["", "## 会被删掉的（这一轮没再产出）", ""]
        lines += [f"- {item}" for item in plan.removed]
    if plan.protected:
        lines += ["", "## 你手改过的（这次一个都没动）", ""]
        lines += [f"- {item}" for item in plan.protected]
    return "\n".join(lines) + "\n"


__all__ = [
    "CardDiff",
    "RebuildPlan",
    "apply_rebuild",
    "plan_rebuild",
    "render_rebuild_report",
]
