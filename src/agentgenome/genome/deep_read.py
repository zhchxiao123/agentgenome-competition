"""③ 逐模块并行深读:每个模块一个作业。

## 为什么不能一个作业读完全库

一个作业有上下文上限、有轮次上限、有超时。让它通读几十万行代码,结果**不是读得慢,是读到
一半开始编**——它会为没读过的模块写出看起来合理的描述,而这些描述会被当成事实存进知识树,
然后误导之后的每一个任务。

知识污染最严重的一次,恰恰发生在系统对项目一无所知的时候:那时没有任何已有认知能跟它对质。

## 单个模块失败不中止

五十个模块跑了四十九个,不该因为一个失败而全部作废——那等于把并行的收益又还回去了,而且
重跑要从头再来一遍。失败模块单独列出来,人知道该重跑哪几个。

**但全部失败时整条流水线判失败**:部分成功才有意义,零成功没有——那种情况下"继续往下走"
只会把一棵空树提交上去。

## 顺序按热区

先读常改的地方。中途出问题时,已经建好的那部分是最有价值的。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agentgenome.core.store import task_dir

#: 进度落在任务目录里的位置。
PROGRESS_FILE = "deep-read.json"


@dataclass(frozen=True)
class ModuleOutcome:
    """一个模块深读的结果。**失败也是一种结果**,不是异常。"""

    module_id: str
    ok: bool
    detail: str = ""
    #: 这个模块读了多久,秒。**深读是整条流水线里唯一贵的一段**,而"哪个模块特别慢"是下一次
    #: 调预算与并发时唯一有用的那条线索。
    duration_s: float = 0.0


@dataclass
class DeepReadResult:
    done: list[str] = field(default_factory=list)
    failed: list[ModuleOutcome] = field(default_factory=list)
    #: 这一轮总共要读哪些模块。**没有它就答不了"还剩多少"**——只有已完成的清单时,一个跑了
    #: 二十分钟、读完三个模块的任务与一个只有三个模块的任务在界面上完全一样。
    planned: list[str] = field(default_factory=list)
    #: 每个读完的模块花了多久,秒。
    timing: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """**全部失败才算流水线失败。** 部分成功是有意义的产出。"""
        return bool(self.done)

    @property
    def pending(self) -> list[str]:
        settled = {*self.done, *(item.module_id for item in self.failed)}
        return [item for item in self.planned if item not in settled]

    def as_dict(self) -> dict[str, object]:
        return {
            "planned": list(self.planned),
            "done": [
                {"module_id": item, "duration_s": self.timing.get(item, 0.0)} for item in self.done
            ],
            "failed": [
                {
                    "module_id": item.module_id,
                    "detail": item.detail,
                    "duration_s": item.duration_s,
                }
                for item in self.failed
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DeepReadResult:
        done = payload.get("done") or []
        failed = payload.get("failed") or []
        return cls(
            # 老的进度文件里 `done` 是一串模块 id,新的是带耗时的对象。两种都收:
            # 一个跑到一半的任务不该因为版本升级而丢掉已经读完的那部分。
            done=[
                str(item.get("module_id", "")) if isinstance(item, dict) else str(item)
                for item in done
            ],
            timing={
                str(item.get("module_id", "")): float(item.get("duration_s") or 0.0)
                for item in done
                if isinstance(item, dict)
            },
            failed=[
                ModuleOutcome(
                    module_id=str(item.get("module_id", "")),
                    ok=False,
                    detail=str(item.get("detail", "")),
                    duration_s=float(item.get("duration_s") or 0.0),
                )
                for item in failed
                if isinstance(item, dict)
            ],
            planned=[str(item) for item in payload.get("planned") or []],
        )


def order_by_hot_paths(module_ids: Sequence[str], hot_paths: Sequence[str]) -> list[str]:
    """按热区排派发顺序。热区里没出现的排在后面,同档保持原序。

    **稳定排序不是洁癖**:顺序不稳的话,同一份仓库两次初始化会以不同顺序派发,而"中途出
    问题时已经建好的是哪几个"就不可复现了。
    """
    rank: dict[str, int] = {}
    for index, path in enumerate(hot_paths):
        head = path.split("/", 1)[0]
        rank.setdefault(head, index)
    return sorted(module_ids, key=lambda item: rank.get(item, len(hot_paths)))


async def deep_read_modules(
    module_ids: Sequence[str],
    run: Callable[[str], Awaitable[ModuleOutcome]],
    hot_paths: Sequence[str] = (),
    on_update: Callable[[DeepReadResult], None] | None = None,
) -> DeepReadResult:
    """把每个模块派出去读,并发度由 `run` 背后的池子管。

    **一个模块抛异常不拖垮别的。** 异常被收着记进清单——不这样的话,第四十九个模块的一次
    超时会把前面四十八个的产出一起丢掉。

    `on_update` **每读完一个就调一次**,不是最后调一次:"跑到哪了"这个问题要在跑的过程中
    答得上来,而一个只在结束时落盘的进度,恰恰在唯一有人盯着的那段时间里是空的。

    `done` 里是**完成顺序**,不是派发顺序——它服务于"刚才读完了哪个",而那正是看进度的人
    想知道的。要按计划顺序看的话,`planned` 在那儿。
    """
    ordered = order_by_hot_paths(module_ids, hot_paths)
    result = DeepReadResult(planned=list(ordered))
    if on_update:
        on_update(result)

    async def one(module_id: str) -> None:
        started = time.monotonic()
        try:
            outcome = await run(module_id)
        except asyncio.CancelledError:
            # **取消要往上传。** 收着的话,一次人为取消会被记成"每个模块都失败了"然后正常
            # 结束——而那正是取消这条路要停下来的东西。
            raise
        except Exception as error:  # noqa: BLE001 - 收着,不让一个模块拖垮别的
            outcome = ModuleOutcome(module_id, ok=False, detail=str(error))
        # 计时在这里而不是在 `run` 里:**每个调用方各计一遍的话,口径迟早不一样**,而
        # "哪个模块特别慢"这个比较只在同一口径下才成立。
        elapsed = round(time.monotonic() - started, 3)
        if outcome.ok:
            result.done.append(module_id)
            result.timing[module_id] = elapsed
        else:
            result.failed.append(replace(outcome, duration_s=elapsed))
        if on_update:
            on_update(result)

    await asyncio.gather(*(one(module_id) for module_id in ordered))
    return result


def write_progress(workspace_root: Path, task_id: str, result: DeepReadResult) -> Path:
    """把进度落在任务目录里。

    **落盘而不是留在内存里**:与「状态即事实」同一条——重启之后"读到哪了"要还在,而且看进度
    的人与跑深读的进程不在同一个地方。
    """
    target = task_dir(workspace_root, task_id) / PROGRESS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    # 每读完一个模块就写一次,而看进度的人随时可能正在读——半份 JSON 会让界面报解析错误。
    tmp.replace(target)
    return target


def read_progress(workspace_root: Path, task_id: str) -> DeepReadResult | None:
    """读回进度。**还没开始就是 `None`**,不是一个空结果——两者在界面上要说不同的话。"""
    target = task_dir(workspace_root, task_id) / PROGRESS_FILE
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # 解析得出但不是个对象,与"文件坏了"是同一件事。归一成空结果的话,界面会显示"跑过了、
    # 零个模块"——而真相是这份进度读不出来。
    return DeepReadResult.from_dict(payload) if isinstance(payload, dict) else None


__all__ = [
    "PROGRESS_FILE",
    "DeepReadResult",
    "ModuleOutcome",
    "deep_read_modules",
    "order_by_hot_paths",
    "read_progress",
    "write_progress",
]
