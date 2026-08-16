"""任务编号与它归哪条并发泳道。

两类任务共享 id 空间、靠前缀区分(见 `core.genome_task`)。**前缀住在哪儿决定了谁要 import
谁**:此前它跟持久化模块住在一起,于是执行池为了一个两行的纯函数,被迫 import 整张任务表的
schema 与 sqlite 仓储——而那也预埋了一个环:`core.genome_task` 哪天想要 `agents` 里的东西
就成环了。

放在这里,两个前缀第一次待在同一个文件里,"它们不能撞"这件事也就有了一个能一眼看全的地方。
"""

from __future__ import annotations

from enum import StrEnum

#: 研发任务编号前缀。
DEV_PREFIX = "ag"
#: 基因组任务编号前缀。
GENOME_PREFIX = "gn"


class Lane(StrEnum):
    """一个 Job 归哪条并发泳道。

    **两条泳道分开计数。** 一次全量知识初始化会派发几十个并行深读作业,共用研发任务那个闸
    的话它会把整条研发流水线堵死——而堵住的那段时间里,系统在外面看起来就是"不干活了"。
    反过来同理:一批研发任务不该让知识初始化排不进队。
    """

    DEV = "dev"
    GENOME = "genome"


def lane_of(task_id: str) -> Lane:
    """这个任务归哪条泳道。

    **从 id 前缀推,不另收一个泳道参数。** 前缀本来就是有含义的,再传一个显式参数等于让同一
    件事有两个来源,而它们能悄悄说不一致。

    **认不出前缀的一律算研发泳道**,这是有意的:`agctl job run` 这类手工原语可以拿任意字符串
    当 id,它们是人当场发起的一次性活儿,归研发泳道是对的。代价是——一个**本该**是基因组任务
    却还没有正式编号的作业会落在研发泳道上,而这正是知识初始化在流水线化(PRD 22)之前的处境:
    它现在以 `knowledge-init` 为 id 跑,因此仍占研发泳道、仍走研发预算。那要等它真的变成一条
    基因组任务才解决,不是这里能补的。
    """
    if task_id.startswith(f"{GENOME_PREFIX}-"):
        return Lane.GENOME
    return Lane.DEV


def lane_budget(dev_tokens: int, genome_tokens: int, task_id: str) -> int:
    """这个任务该用哪条泳道的 token 额度。

    **规则只住这一处。** 抄两遍的话,改泳道就成了一次散弹式修改,而漏掉的那一处会安静地
    继续按旧规则发钱。
    """
    return genome_tokens if lane_of(task_id) is Lane.GENOME else dev_tokens


__all__ = ["DEV_PREFIX", "GENOME_PREFIX", "Lane", "lane_budget", "lane_of"]
