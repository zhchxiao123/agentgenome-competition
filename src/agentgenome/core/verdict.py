"""一次质量判定的失败性质。

## 为什么这个词汇必须只有一份

状态机按它决定去哪:语义失败进修复循环,环境失败与配置篡改直接升级人工。门禁与集成测试
是两套独立的判定,但它们喂给状态机的是**同一个字段**——编排层读的是 `payload["kind"]` 的
字符串值,它不关心这份产物是谁产的。

各自定义一套枚举的话,两套的成员靠文档保持一致,而文档不会在漏掉一个成员时报错。症状是
集成测试的环境失败被当成语义失败塞进修复循环,白烧三轮 token 去改一段没问题的代码。

## 放在 core 而不是 gates

编排层不该为了读一个字段去依赖门禁包,集成测试包也不该。词汇是领域概念,住在领域层。
"""

from __future__ import annotations

from enum import StrEnum


class VerdictKind(StrEnum):
    """这次没过是什么性质。"""

    NONE = "none"
    #: 测试挂了、检查没过。改代码能解决,走修复循环。
    SEMANTIC = "semantic"
    #: 缺工具、环境没起来、编排文件缺失。改代码解决不了,直接升级人工。
    ENVIRONMENT = "environment"
    #: 判定配置被本轮改动过。这是安全事件,直接升级人工。
    TAMPERED = "tampered"


#: 改代码解决不了的性质。修复循环对它们没有意义。
UNRECOVERABLE = frozenset({VerdictKind.ENVIRONMENT, VerdictKind.TAMPERED})


def is_recoverable(kind: str | None) -> bool:
    """这种失败改代码能不能解决。

    收字符串而不是枚举:调用方拿到的是刚从 JSON 里读出来的产物,而**未知的值按"能解决"
    处理**——一个我们不认识的 kind 更可能是新加的语义分类,把它当成环境失败会让任务直接
    升级人工,那个方向的错更贵。
    """
    return kind not in {item.value for item in UNRECOVERABLE}


__all__ = ["UNRECOVERABLE", "VerdictKind", "is_recoverable"]
