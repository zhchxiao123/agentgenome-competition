"""② 模块边界草案:从扫描结果提出一份待人确认的划分。

一个存量项目的模块划分往往**不等于目录划分**:有些目录是历史遗留该合并,有些单目录里其实塞了
两个域。这个判断只有熟悉项目的人做得了——而在此之前,系统划完就直接往下跑,错了要等到某个
任务的影响面判定出问题才被发现,那时已经建了一整棵歪的知识树。

## 草案是确定性的,精修才需要 Agent

这一版直接从扫描结果推:挂载了的仓各算一个候选,语言与依赖当作划分依据。它不聪明,但它
**便宜、可复现、而且足够让人拍板**——人要做的是"这两个合并、那个拆开",而不是从白纸开始。

让 Agent 来提这份草案是后续的质量改进(设计文档 §6.2.1 的 ② 是 agentic),不是这一层缺的
接线:闸门要的是"有一份可审阅的草案",而这份草案已经可审阅了。

## 划分依据要写出来

只给一个模块列表的话,人无从判断该不该改它——而"为什么这么分"恰恰是他唯一能复核的东西。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentgenome.core.scope import is_under
from agentgenome.genome.scan import Candidate, MountState, ScanResult, scan_workspace


def _rationale(candidate: Candidate, hot: Sequence[str]) -> str:
    """这个候选凭什么算一个模块。**据实构造,一句都不能凑。**

    人拿这段话当尺子复核草案,而他复核完就往下跑了。此前"有独立的构建文件"是无条件拼上去的,
    于是没有构建文件的仓渲染成 `有独立的构建文件()`——一句自相矛盾的话比没有这句更糟:
    人会以为自己看漏了,而不是以为系统写错了。
    """
    if candidate.state is MountState.UNREADY:
        # 环境没就绪,不是一个待判断的候选。人在这里要做的是去拉代码,不是拍板边界——
        # 措辞把这两件事分开,否则他会对着一个空目录纠结"这算不算一个模块"。
        return "声明了但还没 checkout 出来,先把它拉下来"
    if candidate.state is MountState.EMPTY:
        # 绿地新仓的**正常**状态。措辞不能像在报错。
        return "已挂载,还没有代码"

    reasons = []
    if candidate.build_files:
        reasons.append(f"有独立的构建文件({', '.join(candidate.build_files)})")
    if candidate.language:
        reasons.append(f"语言 {candidate.language}")
    if candidate.dependencies:
        reasons.append(f"{len(candidate.dependencies)} 个直接依赖")
    if any(is_under(path, candidate.path) for path in hot):
        # 常改的地方值得人多看一眼:它要么是核心,要么是没被理顺的那块。
        reasons.append("近期变更频繁")
    # 一条依据都凑不出时也得说点什么——空白会让人以为这一行渲染坏了。
    return "；".join(reasons) or "挂载在这个 Workspace 里"


def propose_boundaries(scan: ScanResult) -> dict[str, Any]:
    """把扫描结果变成一份边界草案。

    产出的形状与闸门答复一致(`modules` 列表),这样人可以直接在草案上改——**让人重新组织
    一遍数据结构,是最容易让人放弃复核的那种设计**。
    """
    hot = [item.path for item in scan.hot_paths]
    return {
        "modules": [
            {
                # 挂载点的**末段**。模块 id 是门禁配置的文件名索引,带路径分隔符会落到一层
                # 意料之外的嵌套目录里;取末段也让这里的默认提议与初始化写进根索引的 id
                # 一致——同一个仓在两处给出不同的 id 没有任何理由。
                "id": candidate.path.rstrip("/").rsplit("/", 1)[-1],
                "path": candidate.path if candidate.path.endswith("/") else f"{candidate.path}/",
                "summary": "",
                "rationale": _rationale(candidate, hot),
            }
            for candidate in scan.candidates
        ],
        "note": (
            "这是机器按「挂载了就算一个候选」推出来的草案——它只知道这个 Workspace 挂了哪些仓。"
            "历史遗留的目录该合并、塞了两个域的目录该拆开——那是只有你知道的事。"
        ),
    }


class NotReadyForBoundaries(RuntimeError):
    """现在还不能划边界,理由已经写成给人看的话。"""


def scan_for_boundaries(root: Path, since_days: int) -> ScanResult:
    """扫一遍,并在"现在划边界没有意义"时当场停。

    **两个入口共用这一个。** 划边界的路有两条:命令行直接规划,以及基因组任务从扫描态被推进
    (`plan` 在建任务之后、草案就绪之前挂掉的话,任务就停在那儿等人推)。两边各写一遍的下场
    已经出现过一次——其中一条加了未就绪拦截、另一条没加,而没加的那条恰恰是恢复路径。

    停的两种情形:

    - **一个业务仓都没挂。** 没有候选,划什么都无从谈起;
    - **有挂载点还没 checkout 出来。** 对着空目录规划模块边界不产生任何价值,而让它一路走到
      深读才炸掉,中间会烧掉真金白银的 token。这不是拦住"绕过界面的操作"——那种是合法的、
      只需可查;这是环境根本没就绪,该当场说出来。
    """
    scanned = scan_workspace(root, since_days=since_days)
    if not scanned.candidates:
        raise NotReadyForBoundaries(
            "这个 Workspace 的 .gitmodules 里一个业务仓都没有。"
            "它是协作仓、本身不含业务代码,所以得先有挂载的仓才谈得上划模块。"
        )
    unready = [item.path for item in scanned.candidates if item.state is MountState.UNREADY]
    if unready:
        raise NotReadyForBoundaries(
            "这些业务仓声明了但还没 checkout 出来,先把它们拉下来:\n"
            + "".join(f"  {path}\n" for path in unready)
            + "  git submodule update --init --recursive"
        )
    return scanned


__all__ = ["NotReadyForBoundaries", "propose_boundaries", "scan_for_boundaries"]
