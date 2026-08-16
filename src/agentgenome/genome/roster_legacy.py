"""`requirement-analysis` 1.0.0 那一版的三份资产,**逐字来自 git 历史**。

迁移(`roster_migrate`)靠它们判断"这个工作区的工序动没动过":与这里逐字相同 = 没动过,
可以安全刷新到当前版;不同 = 使用者改过,迁移只报告、不覆盖。

**不要从当前 `roster.py` 反推这三份**——反推出来的"旧版"永远与推导逻辑一致,比对就成了
套套逻辑,而它要挡的恰恰是"新旧其实对不上"这种事。
"""

from __future__ import annotations

from typing import Any

REQUIREMENT_ANALYSIS_V1_MANIFEST = "id: requirement-analysis\nversion: 1.0.0\nsummary: 读需求与项目地图,产出这次要动哪些模块、验收标准与预估风险\nkind: agentic\n\n# plan 类:决定\"这一个任务怎么打\"。**归属排他**——加载器会拒收两个员工同时声明它的花名册。\nownership: plan\n\ntrigger:\n  states: [CREATED]\n\ninputs:\n  schema:\n    type: object\n    required: [requirement]\n    properties:\n      requirement: {type: string}\n      task_id: {type: string}\n\noutputs:\n  schema_ref: schemas/out.json\n\ncompat:\n  runtimes: [claude-code]\n"  # noqa: E501 —— 逐字来自 git 历史,不折行

REQUIREMENT_ANALYSIS_V1_PROMPT = "# requirement-analysis\n\n读需求原文与项目地图,回答四个问题:这次要动哪些模块、是不是跨模块、验收标准是什么、\n风险在哪。\n\n## 这份产物首先是给人看的\n\n需求方会在系统开始写代码**之前**读它。\"涉及 order-service 与 inventory-service\"这一行\n如果写错了,他现在纠正的成本比等代码写完之后再纠正低两个数量级。所以宁可多写一句\"我是\n这么理解的\",也不要写得像个已经确定的事实。\n\n## 它同时决定了下一轮能看见什么\n\n`modules` 会被用来切基因组:只有这几个模块(以及它们依赖的模块)的认知卡片会进开发员工的\n上下文。写少了它看不到该看的,写多了真正相关的几行会被淹掉。\n\n## 产物要写什么\n\n- `modules[]`:涉及的模块 id。**必须是项目地图里真实存在的 id**,编造的 id 会让这份计划\n  整个作废。\n- `cross_module`:是否跨模块。跨模块的改动之后要跑集成测试。\n- `acceptance[]`:验收标准。每条是一句可判定的话,不是\"功能正常\"这种没法验的说法。\n- `risks[]`:你看到的风险。没有就写空数组,不要编。\n- `passed`:你有没有把需求读懂。读不懂就填 `false` 并在 `failures[]` 里说清楚缺什么信息\n  ——猜一个计划出来比承认读不懂糟得多。\n- `nodes[]`(可选):**这次的活能不能拆成同时干的几件**。拆不开就别写——一条线也是合法的\n  计划,不为并行而并行。\n\n## 怎么拆(写了 `nodes[]` 才需要读这一节)\n\n每个节点要说清三件事:\n\n- `needs[]` / `produces[]`:它**消费什么产物、产出什么产物**。边由它们推出来——\n  「先 A 后 B」不构成依赖,**只有真实的产物流动构成依赖**。\n- `write_scope[]`:它会写哪些路径(glob)。**两个能同时跑的节点写集必须不相交**,\n  否则合并回任务分支时会撞车。\n\n**fake-edge 提问法**:每画一条 A→B 之前问一句\"B 到底从 A 那里拿什么?\"——说不出具体的\n产物名,这条边就不该有,而它会白白让 B 等着 A。\n\n**diamond 是缺省形状**:一个拆分点扇出几条互不相干的支线,最后汇合到一个收口节点。\n但形状由任务本身决定:一个只动一个模块的需求就该是一条线。\n"  # noqa: E501 —— 逐字来自 git 历史,不折行

REQUIREMENT_ANALYSIS_V1_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "task_id",
        "producer",
        "created_at",
        "passed",
        "modules",
        "cross_module",
        "acceptance"
    ],
    "properties": {
        "task_id": {
            "type": "string"
        },
        "producer": {
            "type": "string"
        },
        "created_at": {
            "type": "string"
        },
        "passed": {
            "type": "boolean"
        },
        "failures": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "message"
                ],
                "properties": {
                    "message": {
                        "type": "string"
                    },
                    "evidence": {}
                }
            }
        },
        "modules": {
            "type": "array",
            "items": {
                "type": "string"
            },
            "minItems": 1
        },
        "cross_module": {
            "type": "boolean"
        },
        "acceptance": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },
        "risks": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "write_scope"
                ],
                "properties": {
                    "id": {
                        "type": "string"
                    },
                    "needs": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        }
                    },
                    "produces": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        }
                    },
                    "write_scope": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        }
                    }
                }
            }
        }
    }
}

__all__ = [
    "REQUIREMENT_ANALYSIS_V1_MANIFEST",
    "REQUIREMENT_ANALYSIS_V1_PROMPT",
    "REQUIREMENT_ANALYSIS_V1_SCHEMA",
]
