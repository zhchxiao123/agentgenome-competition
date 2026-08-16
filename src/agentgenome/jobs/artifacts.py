"""ArtifactBus:任务产物目录的分配、血缘与检索。

**员工之间不直接通信,只通过产物目录交换信息**(黑板模式)。每次交接都天然留痕——
"上一阶段到底给了下一阶段什么"永远是一个能被回答的问题;直接传对象的话,这个问题在
进程结束的那一刻就没了答案。

## 序号不复用

修复循环会多次进同一个 stage。覆盖上一轮的产物等于把失败现场擦掉,而失败现场正是下一轮
要读的东西。所以每次分配都是一个新目录,序号只增不减。

## 编址有四维,不是一维

一个状态内部可以跑一张图,所以"这份产物是谁的"要靠 `(stage, node, variant, attempt)` 才
答得全:哪个阶段、图里的哪个节点、该节点的哪一路变体、那个节点自己的第几轮。前三维进目录
名,第四维**从磁盘派生**——记在内存里的话,重启之后轮次会从头再来。

节点与变体缺省为空时,目录名与只有 stage 时**逐字节相同**:`single` 那条路必须一个字节都
不变。

## 分配必须是并发安全的

并行节点会同时来要槽。"扫盘取 max+1"加 `mkdir(exist_ok=True)` 在那一刻是一次**静默的数据
损坏**:两个节点拿到同一个号、写进同一个目录、后写的赢,而且没有任何报错或事件能指向这里。
所以取号用 `exist_ok=False` 加撞号重排——让"两个节点抢同一个槽"从一种损坏变成一次必然成功
的重试。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 每个产物目录里的血缘清单。
MANIFEST = "manifest.json"

#: 产物目录名:`NN-<stage>[.<node>[.<variant>]]`。
_SLOT = re.compile(r"^(\d+)-([^.]+)(?:\.([^.]+))?(?:\.([^.]+))?$")

#: stage / node / variant 都进路径,所以只允许这些字符——放任 `../` 进去等于让产物写到
#: 任务目录之外。
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: 撞号重试的上限。撞到这个次数说明的不是并发,是别的东西坏了。
_MAX_RETRIES = 64

#: 取号标记目录。**它住在产物目录外面**:产物目录是证据面,而取号标记是机械装置——
#: 混进去的话,任何"这个任务产出了哪几份东西"的枚举都要先学会跳过它。
_RESERVATIONS = ".artifact-slots"


@dataclass(frozen=True)
class Slot:
    """一个已分配的产物目录。"""

    path: Path
    stage: str
    number: int
    #: 图里的哪个节点产出的。空表示这个状态没跑图(`single`)。
    node: str = ""
    #: 该节点的哪一路变体。空表示单路。
    variant: str = ""
    #: 这是该 `(stage, node, variant)` 的第几轮,从 1 起。**从磁盘派生,不是记下来的。**
    attempt: int = 1

    def write_manifest(
        self,
        producer: str,
        inputs: list[str] | None = None,
        outputs: list[str] | None = None,
        summary: str = "",
        task_attempt: int = 0,
        result_ok: bool | None = None,
        failure_kind: str = "",
        failure_detail: str = "",
        suspended: bool = False,
    ) -> Path:
        payload = {
            "stage": self.stage,
            "node": self.node,
            "variant": self.variant,
            "attempt": self.attempt,
            # 这个槽属于**任务的**第几轮(修复轮次)。节点级幂等靠它把"这一轮已经产出过的"
            # 与"上一轮留下的"分开——不分的话,门禁挂了之后回到开发态的新一轮会把上一轮的
            # 产物当成自己的,于是那一轮压根不跑。
            "task_attempt": task_attempt,
            "producer": producer,
            "inputs": list(inputs or []),
            "outputs": list(outputs or []),
            "summary": summary,
        }
        if result_ok is not None:
            payload["result_ok"] = result_ok
            payload["failure_kind"] = failure_kind
            payload["failure_detail"] = failure_detail
            payload["suspended"] = suspended
        target = self.path / MANIFEST
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def manifest(self) -> dict[str, Any] | None:
        """读回血缘清单。没写过就是 `None`——"忘了写"与"没产物"要分得开。"""
        target = self.path / MANIFEST
        if not target.is_file():
            return None
        payload: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
        return payload


class ArtifactBus:
    """一个任务的产物目录集合。"""

    def __init__(self, task_dir: Path) -> None:
        self.root = Path(task_dir) / "artifacts"

    def allocate(self, stage: str, node: str = "", variant: str = "") -> Slot:
        """给这个 `(stage, node, variant)` 开一个新目录。反复进入会拿到递增的新号。

        **取号与建目录是同一个原子动作。** 分两步的话,并发下两个调用者会取到同一个号。
        """
        self._check_name(stage, "stage")
        if node:
            self._check_name(node, "node")
        if variant:
            self._check_name(variant, "variant")
            if not node:
                # 变体属于某个节点。没有节点的变体在编址上没有位置,语义上也说不通。
                raise ValueError("variant 必须与 node 一起给:变体属于某个节点")

        suffix = "".join(f".{part}" for part in (node, variant) if part)
        for _ in range(_MAX_RETRIES):
            number = self._reserve()
            if number is None:
                continue
            path = self.root / f"{number:02d}-{stage}{suffix}"
            # **`exist_ok=False`**:号已经被占住了,目录还能存在只说明别的地方坏了
            # (有人手工建过、清理没清干净)。静默复用一个已有目录 = 两次执行的产物混在
            # 一起,而那正是这次改动要消灭的失败形态,不该在这里悄悄放它进来。
            path.mkdir(parents=True, exist_ok=False)
            return Slot(
                path=path,
                stage=stage,
                number=number,
                node=node,
                variant=variant,
                attempt=len(self.by_node(stage, node, variant)),
            )
        raise RuntimeError(f"分配产物目录连续 {_MAX_RETRIES} 次撞号: {self.root}")

    def _reserve(self) -> int | None:
        """占住下一个序号。占不住(别人抢先)返回 `None`,由调用方重来。

        **占号靠一个 `O_EXCL` 的标记文件,不靠"扫一眼目录再建"。** 后者在并发下不成立:
        两个调用者会同时扫到同一个最大号,而目录名各带各的节点后缀,于是两次 `mkdir` 都
        成功——序号撞了却没有任何症状,直到有人按目录名去对事件流才发现对不上。

        标记目录是取号的**唯一权威**;它不存在的老任务目录靠已有产物目录的最大号兜底,
        于是这次改动对既有工作区是透明的。
        """
        marks = self.root.parent / _RESERVATIONS
        marks.mkdir(parents=True, exist_ok=True)
        taken = {int(item.name) for item in marks.iterdir() if item.name.isdigit()}
        taken.update(slot.number for slot in self.all())
        number = max(taken, default=0) + 1
        try:
            (marks / str(number)).touch(exist_ok=False)
        except FileExistsError:
            return None
        return number

    @staticmethod
    def _check_name(value: str, what: str) -> None:
        if not _NAME.match(value):
            raise ValueError(f"{what} 名只能是小写字母、数字、`-` 与 `_`: {value!r}")

    def all(self) -> tuple[Slot, ...]:
        """全部已分配的目录,按序号升序,`attempt` 已按 `(stage, node, variant)` 数好。

        从磁盘扫而不是记在内存里:崩溃恢复时新进程要能看见旧进程分配过的目录,
        不然序号会从头再来、把上一轮的产物盖掉。
        """
        if not self.root.is_dir():
            return ()
        raw = []
        for entry in self.root.iterdir():
            match = _SLOT.match(entry.name)
            if entry.is_dir() and match:
                number, stage, node, variant = match.groups()
                raw.append((int(number), stage, node or "", variant or "", entry))

        counters: dict[tuple[str, str, str], int] = {}
        slots = []
        for number, stage, node, variant, entry in sorted(raw, key=lambda item: item[0]):
            key = (stage, node, variant)
            counters[key] = counters.get(key, 0) + 1
            slots.append(
                Slot(
                    path=entry,
                    stage=stage,
                    number=number,
                    node=node,
                    variant=variant,
                    attempt=counters[key],
                )
            )
        return tuple(slots)

    def by_stage(self, stage: str) -> tuple[Slot, ...]:
        """这个 stage 的全部槽,**含它底下所有节点的**。

        节点维度上的检索用 `by_node`:混用两者会让一个节点的重试被算到另一个头上。
        """
        return tuple(slot for slot in self.all() if slot.stage == stage)

    def by_node(self, stage: str, node: str = "", variant: str = "") -> tuple[Slot, ...]:
        """这个 `(stage, node, variant)` 自己的槽,按分配顺序升序。

        它是崩溃恢复定位轮次的输入:某个节点的第 k 个槽就是它的第 k 轮。
        """
        return tuple(
            slot
            for slot in self.all()
            if slot.stage == stage and slot.node == node and slot.variant == variant
        )

    def latest(self, stage: str) -> Slot | None:
        found = self.by_stage(stage)
        return found[-1] if found else None


__all__ = ["MANIFEST", "ArtifactBus", "Slot"]
