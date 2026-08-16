"""④ 汇总:把逐模块的产出合成一棵树,并把该让人看的东西列出来。

## 契约要去重

同一条跨模块契约会被 provider 与 consumer **两侧各上报一次**。不去重的话,契约索引里会出现
两条 id 相同、消费者列表却各缺一半的记录——而下游的集成测试判定正是按契约 id 找关联方的。

## 报告是给人看的那一页

初始化跑完之后,人真正要做的只有两件事:复核系统没把握的地方,以及扫一眼那些被判定为"不需要
知识"的功能点。所以报告里这两节是**单独列出来的**,而不是散在各模块的产出里等人自己去翻。

**低置信度不阻塞。** 设计文档里「②是唯一必须人参与的节点」这个说法只对**阻塞**成立:低置信
条目的正确处理方式是合入之后按需修正,拦住整条初始化只会让人为了让它跑完而随手把置信度调高。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agentgenome.genome.deep_read import DeepReadResult
from agentgenome.genome.features import Confidence, KnowledgeTree
from agentgenome.genome.models import Datastore, Interface

#: 低于它就进人工校对清单。high/medium/low 里只有 low 需要人复核——把 medium 也拉进来
#: 会让清单长到没人看,而一份没人看的清单等于没有。
_NEEDS_REVIEW = 0.6


@dataclass(frozen=True)
class ReviewItem:
    """一条要人复核的认知。"""

    where: str
    what: str
    confidence: float | None


@dataclass
class InitReport:
    """初始化跑完之后给人看的那一页。"""

    modules_done: list[str] = field(default_factory=list)
    modules_failed: list[dict[str, str]] = field(default_factory=list)
    low_confidence: list[ReviewItem] = field(default_factory=list)
    no_card_declarations: list[dict[str, str]] = field(default_factory=list)
    #: 员工在小票上留的问题与指针。**只进校对清单,不参与裁决**(PRD 34):小票是员工唯一
    #: 能说"这里我拿不准、请人拍板"的地方,契约改造不该把这个入口弄丢。
    questions: list[dict[str, str]] = field(default_factory=list)
    human_adjustments: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "modules_done": list(self.modules_done),
            "modules_failed": list(self.modules_failed),
            "low_confidence": [
                {"where": item.where, "what": item.what, "confidence": item.confidence}
                for item in self.low_confidence
            ],
            "no_card_declarations": list(self.no_card_declarations),
            "questions": list(self.questions),
            "human_adjustments": dict(self.human_adjustments),
        }


def merge_contracts(
    reported: list[tuple[list[Interface], list[Datastore]]],
) -> tuple[list[Interface], list[Datastore]]:
    """把各模块上报的契约合成一份。

    **按 id 合并,消费者取并集。** 同一条契约被两侧各报一次,后写覆盖前写的话会丢掉一半
    消费者——而集成测试判定正是按契约找关联方的,丢了就判不出该跑集测。
    """
    interfaces: dict[str, Interface] = {}
    for reported_interfaces, _ in reported:
        for item in reported_interfaces:
            existing = interfaces.get(item.id)
            if existing is None:
                interfaces[item.id] = item
                continue
            consumers = list(dict.fromkeys([*existing.consumers, *item.consumers]))
            interfaces[item.id] = existing.model_copy(update={"consumers": consumers})

    datastores: dict[str, Datastore] = {}
    for _, reported_datastores in reported:
        for store in reported_datastores:
            # 数据存储只有一个 owner,不存在"两侧各报一次"的问题——先报的算数。
            datastores.setdefault(store.id, store)

    return (
        [interfaces[key] for key in sorted(interfaces)],
        [datastores[key] for key in sorted(datastores)],
    )


def build_report(
    tree: KnowledgeTree,
    deep_read: DeepReadResult,
    human_adjustments: dict[str, Any] | None = None,
    receipts: dict[str, dict[str, Any]] | None = None,
) -> InitReport:
    """把这次初始化的结果整理成人要看的那一页。

    `receipts` 是逐模块的小票(module id → 小票内容)。里面的 `questions` 与
    `low_confidence` 指针进校对清单——**它们是补充,不是判据**,树的合法性在这一步
    之前就已经裁决完了。
    """
    report = InitReport(
        modules_done=list(deep_read.done),
        modules_failed=[
            {"module_id": item.module_id, "detail": item.detail} for item in deep_read.failed
        ],
        human_adjustments=dict(human_adjustments or {}),
    )
    for module_id, receipt in sorted((receipts or {}).items()):
        for question in receipt.get("questions") or []:
            report.questions.append({"module_id": module_id, "question": str(question)})
        for pointer in receipt.get("low_confidence") or []:
            report.low_confidence.append(
                ReviewItem(where=f"{module_id}(小票)", what=str(pointer), confidence=None)
            )

    for module in tree.project_map.modules:
        if module.confidence is not None and module.confidence < _NEEDS_REVIEW:
            report.low_confidence.append(
                ReviewItem(where=module.id, what="模块认知", confidence=module.confidence)
            )

    for (module_id, feature_id), card in sorted(tree.cards.items()):
        # **卡片这一层的置信度也要进清单。** 只看模块级的话,一张自己标了 low 的功能卡片
        # 永远不会被人复核——而分层之后,细节恰恰都搬到了这一层。
        if card.confidence is Confidence.LOW:
            report.low_confidence.append(
                ReviewItem(where=f"{module_id}/{feature_id}", what=card.summary, confidence=None)
            )

    for module_id, features in sorted(tree.feature_index.items()):
        for feature in features:
            if feature.no_card is not None:
                # **单独列出来让人扫一眼。** 校验挡得住空理由,挡不住「不需要」三个字——
                # 这是完备性那条不变式唯一的软肋,也是唯一有效的补强。
                report.no_card_declarations.append(
                    {
                        "module_id": module_id,
                        "feature_id": feature.id,
                        "reason": feature.no_card,
                    }
                )
    return report


def render_report(report: InitReport) -> str:
    """报告的人类可读版。**低置信与无需卡片排在最前**——它们是人唯一需要行动的两节。"""
    lines = ["# 知识初始化报告", ""]
    if report.low_confidence:
        lines += ["## 需要复核(系统对这些没把握)", ""]
        lines += [
            f"- {item.where}:{item.what}(置信度 {item.confidence})"
            for item in report.low_confidence
        ]
        lines.append("")
    if report.questions:
        lines += ["## 员工留下的问题(小票,请人拍板)", ""]
        lines += [f"- {item['module_id']}:{item['question']}" for item in report.questions]
        lines.append("")
    if report.no_card_declarations:
        lines += ["## 被判定为「无需卡片」(扫一眼有没有偷懒)", ""]
        lines += [
            f"- {item['module_id']}/{item['feature_id']}:{item['reason']}"
            for item in report.no_card_declarations
        ]
        lines.append("")
    lines += ["## 模块", "", f"- 已完成:{', '.join(report.modules_done) or '(无)'}"]
    if report.modules_failed:
        lines.append("- 失败(重跑这几个即可,不必重跑全库):")
        lines += [f"  - {item['module_id']}:{item['detail']}" for item in report.modules_failed]
    if report.human_adjustments:
        lines += ["", "## 你对模块边界做过的调整", ""]
        lines += ["```json", json.dumps(report.human_adjustments, ensure_ascii=False), "```"]
    return "\n".join(lines) + "\n"


__all__ = ["InitReport", "ReviewItem", "build_report", "merge_contracts", "render_report"]
