"""③ 逐模块并行深读。

让一个作业通读几十万行代码，结果**不是读得慢，是读到一半开始编**——它会为没读过的模块写出
看起来合理的描述，而这些描述会被当成事实存进知识树，然后误导之后的每一个任务。
"""

from __future__ import annotations

import asyncio

from agentgenome.genome.deep_read import (
    ModuleOutcome,
    deep_read_modules,
    order_by_hot_paths,
)


def _ok(module_id: str) -> ModuleOutcome:
    return ModuleOutcome(module_id, ok=True)


# --- 派发顺序 ----------------------------------------------------------------


def test_hot_modules_go_first() -> None:
    """中途出问题时，已经建好的那部分是最有价值的。"""
    order = order_by_hot_paths(["a", "b", "c"], ["c/src/x.py", "c/src/y.py", "b/main.go"])

    assert order == ["c", "b", "a"]


def test_modules_outside_the_hot_list_keep_their_order() -> None:
    assert order_by_hot_paths(["a", "b"], []) == ["a", "b"]


def test_the_order_is_stable() -> None:
    """顺序不稳的话，「中途出问题时已经建好的是哪几个」就不可复现了。"""
    args = (["a", "b", "c"], ["b/x.py"])

    assert order_by_hot_paths(*args) == order_by_hot_paths(*args)


# --- 单模块失败不中止 ---------------------------------------------------------


async def test_all_modules_are_read() -> None:
    result = await deep_read_modules(["a", "b", "c"], lambda m: _async(_ok(m)))

    assert sorted(result.done) == ["a", "b", "c"]
    assert result.ok


async def test_one_failure_does_not_take_the_others_down() -> None:
    """五十个模块跑了四十九个，不该因为一个失败而全部作废。"""

    async def run(module_id: str) -> ModuleOutcome:
        if module_id == "b":
            return ModuleOutcome(module_id, ok=False, detail="超时")
        return _ok(module_id)

    result = await deep_read_modules(["a", "b", "c"], run)

    assert sorted(result.done) == ["a", "c"]
    assert [item.module_id for item in result.failed] == ["b"]
    assert result.ok


async def test_an_exception_is_a_failure_not_a_crash() -> None:
    """第四十九个模块的一次超时，不该把前面四十八个的产出一起丢掉。"""

    async def run(module_id: str) -> ModuleOutcome:
        if module_id == "b":
            raise RuntimeError("炸了")
        return _ok(module_id)

    result = await deep_read_modules(["a", "b", "c"], run)

    assert sorted(result.done) == ["a", "c"]
    assert "炸了" in result.failed[0].detail


async def test_everything_failing_fails_the_pipeline() -> None:
    """部分成功才有意义，零成功没有——那种情况下继续往下走只会提交一棵空树。"""
    result = await deep_read_modules(
        ["a", "b"], lambda m: _async(ModuleOutcome(m, ok=False, detail="x"))
    )

    assert not result.ok
    assert result.done == []


async def test_the_failed_list_is_actionable() -> None:
    """人要知道该重跑哪几个，而不是「有几个失败了」。"""
    result = await deep_read_modules(
        ["a", "b"],
        lambda m: _async(_ok(m) if m == "a" else ModuleOutcome(m, ok=False, detail="没有构建文件")),
    )

    (failed,) = result.as_dict()["failed"]  # type: ignore[misc]
    assert failed["module_id"] == "b"
    assert failed["detail"] == "没有构建文件"
    # 耗时也带上：「哪个模块特别慢」是下一次调预算与并发时唯一有用的那条线索。
    assert "duration_s" in failed


async def _async(value: ModuleOutcome) -> ModuleOutcome:
    await asyncio.sleep(0)
    return value
