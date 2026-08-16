"""人工闸门:草案落盘、答复写入。

这是全异步那条承诺的落地点。人敲完 `agctl init` 之后不该被迫守着终端——尤其这个闸门前面
还有一段几十分钟的扫描,而它要问的问题(模块边界划得对不对)只有熟悉项目的人答得了。

## 草案与答复分开存

合成一份的话,人一改就把"系统原本建议了什么"覆盖掉了——而"哪里是人改的"恰恰是这份产物最有
价值的部分:后来的人要靠它判断这套划分有多少是机器猜的、有多少是有人拍过板的。

## 这一层不认识阶段

它只负责三件事:有一份草案、有一份答复、答复合法就能往下走。**"合法"由调用方注入一个校验器
决定**——把"答复里要有 modules、每个要有 id"写死在这里,等于让每一种闸门都说初始化的方言,
而按模块重建、卡片补写要问的根本不是同一个问题。

## 不合契约的答复不落盘

落盘再拒的话,磁盘上会留下一份"看起来像答复但不算数"的文件,而下一次恢复读到它就会以为人
已经回答过了。

## 写入是原子的

`write_text` 在崩溃时会留下半截 JSON,而之后**每一次**恢复都会在读它的时候炸——一次写到
一半,永久卡住。先写临时文件再改名。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentgenome.core.store import task_dir

#: 闸门产物在任务目录里的位置。
GATE_DIR = Path("gate")
DRAFT_FILE = "draft.json"
ANSWER_FILE = "answer.json"


class NoDraft(FileNotFoundError):
    """这个任务还没有可回答的草案。"""


class AnswerInvalid(ValueError):
    """答复不合契约。"""


def gate_dir(workspace_root: Path, task_id: str) -> Path:
    return task_dir(workspace_root, task_id) / GATE_DIR


def _write_atomically(path: Path, payload: dict[str, Any]) -> Path:
    """先写临时文件再改名。

    直接写的话,崩在写到一半会留下半截 JSON,而之后每一次恢复都会在读它时炸——一次写到
    一半,任务永久卡住。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(path.suffix + ".partial")
    scratch.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    scratch.replace(path)
    return path


def _read_mapping(path: Path) -> dict[str, Any] | None:
    """读一份映射。**读不出来就是没有**——半截 JSON 与文件不存在,对调用方是同一件事:
    这里没有可用的答案。抛出去的话,一次写坏会让任务再也恢复不了。"""
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def write_draft(workspace_root: Path, task_id: str, draft: dict[str, Any]) -> Path:
    """把边界草案落盘。**任务随之可以进入待确认**——产物先在,状态才动。"""
    return _write_atomically(gate_dir(workspace_root, task_id) / DRAFT_FILE, draft)


def has_draft(workspace_root: Path, task_id: str) -> bool:
    """草案在不在。进入待确认之前要问的那一句——**没有草案的待确认是个死局**:
    两条入口都会拒绝回答,而任务永远等不到答复。"""
    return _read_mapping(gate_dir(workspace_root, task_id) / DRAFT_FILE) is not None


def read_draft(workspace_root: Path, task_id: str) -> dict[str, Any]:
    loaded = _read_mapping(gate_dir(workspace_root, task_id) / DRAFT_FILE)
    if loaded is None:
        raise NoDraft(f"{task_id} 还没有可用的边界草案")
    return loaded


#: 一个答复校验器:合法就原样返回,不合法就抛 `AnswerInvalid`。
Validator = Callable[[object], dict[str, Any]]


def any_mapping(answer: object) -> dict[str, Any]:
    """这一层自己只要求答复是个映射。**具体问什么由发起闸门的那条流水线说了算。**"""
    if not isinstance(answer, dict):
        raise AnswerInvalid("答复必须是一个映射")
    return answer


def module_boundary_answer(answer: object) -> dict[str, Any]:
    """知识初始化那个闸门的校验器:最终模块列表,每个带 id。

    「合并、拆分、改名、剔除」在数据上就是"给出最终列表"——不为每种动作定义单独的指令,
    否则每加一种人工操作都要动一次协议。
    """
    checked = any_mapping(answer)
    modules = checked.get("modules")
    if not isinstance(modules, list) or not modules:
        # 空答复与"还没回答"长得一模一样,而前者是错、后者是正常流程。
        raise AnswerInvalid("答复必须给出非空的 modules——最终的模块列表")
    for index, module in enumerate(modules):
        if not isinstance(module, dict) or not str(module.get("id") or "").strip():
            raise AnswerInvalid(f"modules.{index} 缺少 id")
        # 草案写的是单数 `path`,答复用复数 `paths`——两种都收。只认一种的话,一份照着
        # 草案改出来的答复会在这里被判成"没给目录"。
        paths = module.get("paths")
        if paths is None and module.get("path"):
            paths = [module["path"]]
        if not isinstance(paths, list):
            # 类型不对是**调用方写错了**。不查的话 `len()` 抛 `TypeError`,而它逃出
            # `AnswerInvalid` 之后是一个 500——那说的是服务端坏了,排查会从服务端日志开始。
            raise AnswerInvalid(f"modules.{index}({module['id']})的 paths 要是一个目录列表")
        if len(paths) != 1:
            # **一个模块对应一个目录。** 项目地图上的 `path` 是单值,而它同时是子模块的挂载
            # 点——两个目录合成一个模块在这个模型里表达不出来。
            #
            # 在这里拒,而不是在写地图时悄悄取第一条:取第一条的话,人合并的第二个目录会
            # 静默失去覆盖,而他在界面上看到的是"已确认"。要合并就给它们一个共同的父目录。
            raise AnswerInvalid(
                f"modules.{index}({module['id']})给了 {len(paths)} 个目录——"
                "一个模块对应一个目录;要把两个目录合成一个模块,请给它们的共同父目录"
            )
    return checked


def write_answer(
    workspace_root: Path,
    task_id: str,
    answer: object,
    validator: Validator = any_mapping,
) -> Path:
    """写下人的答复。

    **要求草案已经在**:没有草案就回答,答的是什么无从谈起。
    **先校验再落盘**:落盘再拒的话,磁盘上会留下一份看起来像答复但不算数的文件,而下一次
    恢复读到它就会以为人已经回答过了。
    """
    read_draft(workspace_root, task_id)
    checked = validator(answer)
    return _write_atomically(gate_dir(workspace_root, task_id) / ANSWER_FILE, checked)


def read_answer(workspace_root: Path, task_id: str) -> dict[str, Any] | None:
    """人回答过了吗。**没有不是错误**,是"还在等"。"""
    return _read_mapping(gate_dir(workspace_root, task_id) / ANSWER_FILE)


__all__ = [
    "ANSWER_FILE",
    "DRAFT_FILE",
    "GATE_DIR",
    "AnswerInvalid",
    "NoDraft",
    "Validator",
    "any_mapping",
    "gate_dir",
    "has_draft",
    "module_boundary_answer",
    "read_answer",
    "read_draft",
    "write_answer",
    "write_draft",
]
