"""战果转正:抓到真问题的攻击用例进业务仓的回归集。

## 一次攻击的价值不该只有"这次抓到"

红队打穿一条不变量之后,那个输入是**这条防线上唯一被证明有效的探针**。留在任务产物里的话,
它的寿命等于这个任务;进了回归集,它变成"永远防住"。所以转正不是一个可选的收尾动作,
它是这条线的产出本身。

## 提案,不是落地

这里只产出提案。**落地走正常提交路径**——由测试员工或开发员工在一次普通任务里把用例加进
业务仓,过门禁、过越权检查、过评审。一条能绕过门禁进仓的路径迟早会被用来绕过门禁,
而回归集恰恰是最不该有特权通道的地方。

## 只有抓到过东西的才转正

判据是"这条用例对应的发现导致过一次返工"。没打穿的尝试不进回归集:它只是一次没打中的
攻击,进了回归集就是永久的运行成本换零收益。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agentgenome import paths

#: 提案落在哪。与经验卡片同一层——它们都是"这次任务留给以后的东西"。
PROMOTIONS_DIR = paths.LESSONS / "promotions"


@dataclass(frozen=True)
class Promotion:
    """一条转正提案。**能反查到战史**:审计员要能从回归集里的一条测试问出它当年打穿了什么。"""

    task_id: str
    #: 来源产物槽。事件面与产物面按它对账。
    slot: str
    title: str
    #: 被打破的那条不变量。空表示红队没说清楚——那样的提案价值低得多,但不拦。
    invariant: str
    #: 当时那条能独立执行的复现命令。
    repro_cmd: str
    #: 攻击用例在任务目录里的位置。
    case_file: str
    #: **建议**落到业务仓的哪儿。接手转正的人可以改——真正决定落点的是他,不是这里。
    target: str

    def render(self) -> str:
        return "\n".join(
            [
                f"## 转正提案:{self.title}",
                "",
                f"- 来源任务:`{self.task_id}`",
                f"- 来源产物槽:`{self.slot}`",
                f"- 打破的不变量:{self.invariant or '(红队没说清楚)'}",
                f"- 当时的复现命令:`{self.repro_cmd}`",
                f"- 攻击用例:`{self.case_file}`",
                f"- 建议落点:`{self.target}`",
                "",
                "### 怎么落地",
                "",
                "**走正常提交路径。** 由测试员工或开发员工在一次普通任务里把这条用例加进业务仓,"
                "过门禁、过越权检查、过评审。这里不提供任何直接写进业务仓的通道——"
                "一条能绕过门禁进仓的路径,迟早会被用来绕过门禁。",
                "",
                "### 为什么值得永久留着",
                "",
                "它打穿过一次真实的实现,而当时的测试是全绿的。留在任务产物里,它的寿命等于"
                "那个任务;进了回归集,同一个错误再也不会以同样的方式发生第二次。",
                "",
            ]
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "slot": self.slot,
            "title": self.title,
            "invariant": self.invariant,
            "repro_cmd": self.repro_cmd,
            "case_file": self.case_file,
            "target": self.target,
        }


def promotions_for(
    task_id: str, payload: Mapping[str, Any], slot: str, target_dir: str
) -> list[Promotion]:
    """一次对抗的产物能提出几条转正提案。

    **`passed` 为真时一条都没有。** 那次攻击没打穿——它的价值是"这个角度试过了",不是
    一条值得永久跑下去的用例。

    没有 `case_file` 的发现同样不提案:提案要落地的是一份文件,而"某某地方有问题"落不了地。
    复现命令回答的是"怎么看到它",用例文件回答的是"以后靠什么挡住它",两者缺一不可。
    """
    if payload.get("passed", False):
        return []
    found: list[Promotion] = []
    for item in payload.get("findings") or []:
        if not isinstance(item, dict):
            continue
        case_file = str(item.get("case_file") or "")
        if not case_file:
            continue
        found.append(
            Promotion(
                task_id=task_id,
                slot=slot,
                title=str(item.get("title") or "(没有标题)"),
                invariant=str(item.get("invariant") or ""),
                repro_cmd=str(item.get("repro_cmd") or ""),
                case_file=case_file,
                target=str(PurePosixPath(target_dir) / PurePosixPath(case_file).name),
            )
        )
    return found


def write_promotions(workspace_root: Path, promotions: Sequence[Promotion]) -> list[Path]:
    """把提案落盘。**只写基因组里的提案目录**——业务仓一个字节都不碰。

    幂等:同一个 `(任务, 用例)` 重跑写同一个文件。崩溃恢复会重放这一步,重复写必须是安全的。
    """
    written: list[Path] = []
    target_dir = Path(workspace_root) / PROMOTIONS_DIR
    for promotion in promotions:
        target_dir.mkdir(parents=True, exist_ok=True)
        name = f"{promotion.task_id}-{PurePosixPath(promotion.case_file).stem}.md"
        target = target_dir / name
        target.write_text(promotion.render(), encoding="utf-8")
        written.append(target)
    return written


def load_promotions(workspace_root: Path) -> list[Path]:
    """已有的提案。按文件名排序,好 diff。"""
    target_dir = Path(workspace_root) / PROMOTIONS_DIR
    return sorted(target_dir.glob("*.md")) if target_dir.is_dir() else []


__all__ = [
    "PROMOTIONS_DIR",
    "Promotion",
    "load_promotions",
    "promotions_for",
    "write_promotions",
]
