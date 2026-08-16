"""质量线:测试分工与对抗 QA 拧到哪一档,以及由此产生的任务级禁令。

## 为什么是一个模块而不是散在编排器里

这里算出来的东西有两个消费者:**选哪张图**(派发前)与**允许写哪儿**(派发时、结对结束时、
提交前复查时)。两者必须由同一份判断长出来——各算一遍的话,会出现"图按专职派了、写集却
没分离"这种半生效状态,而它在事件面上看起来一切正常。

## 写集分离的两侧不对称,而这是对的

- **出题人只能写测试**,是**角色级**事实:测试员工在任何任务里都只写测试。所以它写在员工
  定义的 `write_paths` 里——边界可读、可评审,而不是藏在代码算出来的一份禁令里。
- **实现人不许写测试**,是**任务级**事实:只有专职档才成立,`dev` 档下开发员工照旧自己写。
  写不进员工定义,所以由授权层在派发时叠加。

**"哪些路径算测试"只有一个答案:测试员工自己声明的那些。** 早先这里另有一个
`quality_line.test_globs` 配置项,那是第二个答案——改了配置而没改员工定义时,开发员工被
禁掉的路径与出题人能写的路径不重合,而不重合的那一段是**谁都写不了**的死区。要改哪些路径
算测试,就改 `tester-employee.yaml`:它是资产,走 git 评审,而且改一处两侧一起动。

## 测试路径靠约定,不靠模块地图的字段

模块地图里只有 `test_cmd`,没有测试目录。加一个字段是一次 schema 版本变更,会牵动知识
初始化的提示词与全部存量地图文件——为"哪些文件算测试"这件事付这个代价不值。
"""

from __future__ import annotations

from collections.abc import Sequence

from agentgenome.config import AdversaryMode, Config, TesterMode
from agentgenome.employees import TASK_MODULES_PLACEHOLDER


def tester_is_dedicated(config: Config, protected_hit: bool) -> bool:
    """这一次要不要让测试员工专职出题。

    `risk-based` 用的判据与精化环同一条:**受保护路径是项目自己声明"这里出事代价大"的
    地方**,而"这里值得多花一个 Job"是同一个判断。另立一张风险清单的话,两张清单迟早分叉,
    而分叉之后没人说得清哪张才是这个项目真正的风险面。
    """
    mode = config.quality_line.tester
    if mode is TesterMode.DEDICATED:
        return True
    return mode is TesterMode.RISK_BASED and protected_hit


def adversary_is_on(config: Config, protected_hit: bool) -> bool:
    """这一次要不要让对抗 QA 上场。"""
    mode = config.quality_line.adversary
    if mode is AdversaryMode.ALWAYS:
        return True
    return mode is AdversaryMode.PROTECTED_HIT and protected_hit


def test_globs(tester_write_paths: Sequence[str]) -> tuple[str, ...]:
    """测试员工声明的写集里,**落在业务模块下**的那些——也就是"哪些路径算测试"。

    只取带 `{task_modules}` 的:出题人还能写自己的任务目录,而那不是测试路径。不筛的话,
    开发员工会被顺带禁掉它自己的任务目录,而症状是"开发员工突然连自评产物都写不出来"。
    """
    return tuple(glob for glob in tester_write_paths if TASK_MODULES_PLACEHOLDER in glob)


def extra_forbid(
    employee_id: str, developer_id: str, tester_write_paths: Sequence[str], dedicated: bool
) -> tuple[str, ...]:
    """写集分离里**任务级**的那一半:专职档下,开发员工不许写测试。

    另一半(出题人只能写测试)是角色级的,住在测试员工的定义里——两边都在这里算的话,
    读 `tester.yaml` 的人会以为它能写整个模块,而那份定义正是给人读的。

    禁令**从出题人自己声明的写集推出来**,不另有一份清单:两份清单不重合的那一段,是一块
    谁都写不了的死区,而且没有任何症状能提示它存在。

    不是专职档时返回空:授权范围与这个机制不存在时逐字节相同。
    """
    if not dedicated or employee_id != developer_id:
        return ()
    return test_globs(tester_write_paths)


__all__ = [
    "adversary_is_on",
    "extra_forbid",
    "test_globs",
    "tester_is_dedicated",
]
