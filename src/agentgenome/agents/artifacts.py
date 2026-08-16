"""Job 产物的文件名约定。

集中在一处:回放与真实运行必须产出同名的东西,否则下游按名字找产物的代码会在
回放下悄悄失效——而那正是全部端到端测试所依赖的路径。散落成字面量的话,
改一个名字要改两个文件,漏一个就是一次无声的漂移。
"""

from __future__ import annotations

#: 结果契约文件。运行时校验它,回放写它,录制拷贝它。
RESULT_FILENAME = "result.json"


def context_filename(attempt: int) -> str:
    """这一次尝试实际收到的上下文包。

    每次尝试单独落盘:"当时它到底看到了什么"必须是能被回答的问题,重试那次尤其
    ——它的输入与首次不同。
    """
    return f"context-attempt-{attempt}.md"


def context_bundle_filename(round_: int) -> str:
    """`agctl context build` 单独组装出来的那一份上下文包。

    **轮次不是尝试。** 轮次是状态机的修复循环(每一轮是全新的 Job),尝试是一个 Job
    内部因契约失败而重跑的那次。两者共用一套文件名的话,`--round 2` 组装出来的包会
    被某次 Job 的第 2 次尝试覆盖掉,而两份内容完全不同。
    """
    return f"context-round-{round_}.md"


def log_filename(attempt: int) -> str:
    """这一次尝试的归一化事件流。"""
    return f"job-attempt-{attempt}.jsonl"


__all__ = [
    "RESULT_FILENAME",
    "context_bundle_filename",
    "context_filename",
    "log_filename",
]
