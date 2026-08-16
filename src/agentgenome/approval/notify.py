"""审批通知。**尽力而为的增强,不是流程的一部分。**

任务停在审批态本身已经是正确状态——通知只是让人早点知道。所以**发送失败绝不阻塞状态迁移**,
只记一条事件。反过来做的话,一个配错的 webhook 地址会让整条流水线卡死在一个与代码质量完全
无关的地方。

内容要够审批人判断紧急程度:任务编号、标题、风险评级、命中的规则。少了风险评级的通知只能
告诉人"有东西要审",而"有多急"才是他决定现在看还是下班前看的依据。

形态兼容钉钉/飞书/Slack:它们都接受 `POST` 一个 JSON,顶层有一段纯文本。这里发的是最小
公分母,不做各家的富文本卡片——那属于 PRD 12 的 IM 集成。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

#: 发送超时。通知不该让编排器等。
TIMEOUT_S = 10


@dataclass(frozen=True)
class Notification:
    task_id: str
    title: str
    risk: str = "low"
    matched_rules: tuple[str, ...] = ()
    approvers: tuple[str, ...] = ()

    def text(self) -> str:
        lines = [
            f"【AgentGenome】任务 {self.task_id} 等待审批",
            f"标题:{self.title}",
            f"风险:{self.risk}",
        ]
        if self.matched_rules:
            lines.append(f"命中规则:{', '.join(self.matched_rules)}")
        if self.approvers:
            lines.append(f"审批人:{', '.join(self.approvers)}")
        lines.append(f"批准:agctl task approve {self.task_id} --as <你>")
        lines.append(f"驳回:agctl task reject {self.task_id} --as <你> --comment <意见>")
        return "\n".join(lines)

    def payload(self) -> dict[str, Any]:
        return {"text": self.text(), "task_id": self.task_id, "risk": self.risk}


def send(webhook: str | None, notification: Notification) -> tuple[bool, str]:
    """发一条通知。返回 `(成功与否, 说明)`。**永远不抛异常。**

    没配 webhook 时既不发也不报错——通知渠道是可选的,没配就是"这个部署不需要"。
    """
    return send_payload(webhook, notification.payload())


def send_payload(webhook: str | None, payload: dict[str, Any]) -> tuple[bool, str]:
    """发一段任意 JSON。**审批通知与 PRD 12 的 IM 状态推送共用这一个传输层**——
    两者的差异只在文本内容怎么拼,POST 一段 JSON、超时、吞异常这些机制不该各写一份。
    """
    if not webhook:
        return False, "没有配置通知渠道"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        # 这里刻意吞掉异常:通知挂了不该把任务也搞挂。
        return False, f"发送失败: {error}"


__all__ = ["TIMEOUT_S", "Notification", "send", "send_payload"]
