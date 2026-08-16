"""角色与权限。

## 前端权限只做展示裁剪,不是安全边界

**一个只在前端藏起来的按钮,用 curl 一样能调。** 这句话写在这里是因为它极容易被违反:
界面上看不见就"感觉"安全了,于是后端那道校验被省掉。

## 权限矩阵是数据,不是散在各处的 if

散着写的话,加一个角色要改十几处,而漏掉的那一处就是洞——而且是没有症状的洞:那个角色能做
一件它不该做的事,直到有人真的去做。

## 审批的校验位置不变

PRD 08 已经把审批做成**服务端校验**。这里换的是身份来源(配置文件里的名单 → 角色查询),
不是校验位置。位置对了,换数据源只是换一行。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    """四个角色。"""

    #: 提需求的。
    REQUESTER = "requester"
    #: 开发的。看任务、看日志、取消自己的任务。
    DEVELOPER = "developer"
    #: 审批的。
    APPROVER = "approver"
    #: 管理的。
    ADMIN = "admin"


class Action(StrEnum):
    """受控的操作。**只列写操作与敏感读操作**——把每一次列表查询都塞进矩阵没有收益。"""

    SUBMIT_TASK = "submit_task"
    CANCEL_TASK = "cancel_task"
    RUN_TASK = "run_task"
    APPROVE_TASK = "approve_task"
    OVERRIDE_ITEST = "override_itest"
    RERUN_ITEST = "rerun_itest"
    RESOLVE_INTERVENTION = "resolve_intervention"
    EXPORT_AUDIT = "export_audit"
    EDIT_GENOME = "edit_genome"
    EDIT_SETTINGS = "edit_settings"


#: 权限矩阵。**一份数据,不是散在各处的 if。**
MATRIX: dict[Role, frozenset[Action]] = {
    Role.REQUESTER: frozenset(
        {
            Action.SUBMIT_TASK,
            Action.CANCEL_TASK,
            Action.RUN_TASK,
            Action.RESOLVE_INTERVENTION,
        }
    ),
    Role.DEVELOPER: frozenset(
        {
            Action.SUBMIT_TASK,
            Action.CANCEL_TASK,
            Action.RUN_TASK,
            Action.OVERRIDE_ITEST,
            Action.RERUN_ITEST,
            Action.RESOLVE_INTERVENTION,
            Action.EXPORT_AUDIT,
        }
    ),
    # 审批人**不能**提需求也不能改基因组:审自己写的东西不叫审批。
    Role.APPROVER: frozenset({Action.APPROVE_TASK, Action.EXPORT_AUDIT}),
    Role.ADMIN: frozenset(Action),
}


class Forbidden(PermissionError):
    """这个身份不能做这件事。"""


@dataclass(frozen=True)
class Principal:
    """一个已经认证过的身份。**认证在这一层之外**——这里只管授权。"""

    subject: str
    roles: frozenset[Role] = frozenset()

    def allows(self, action: Action) -> bool:
        return any(action in MATRIX.get(role, frozenset()) for role in self.roles)

    def ensure(self, action: Action) -> None:
        """不许就抛。**服务端校验的唯一入口**——每个写接口都过它。"""
        if not self.allows(action):
            names = ", ".join(sorted(role.value for role in self.roles)) or "(无角色)"
            raise Forbidden(
                f"{self.subject}({names})不能执行 {action.value}。"
                "前端藏起按钮不是安全边界——这道校验在服务端。"
            )


#: 没有认证信息时的身份。**什么都不能做**——默认拒绝。
ANONYMOUS = Principal(subject="(未认证)", roles=frozenset())

#: 一台机器上自己跑、没有配任何账号时的身份。
#:
#: **它必须真的什么都做得了**,而不只是在入口处被放行:入口放行、写入层用匿名身份再判一次
#: 的话,界面上旋钮是亮的而按下保存 403——两处对"没配账号"给出了不同答案,而不一样的那两处
#: 谁都不会自己发现(单机开发正是默认形态,却也是最少有人去写权限测试的形态)。
#:
#: 名字进事件面,所以它是"(单机)"而不是"(未认证)":审计里那条记录说的是**这次是谁改的**,
#: 而"没配账号的本机管理员"是一个真实且有用的答案,"未认证"不是。
SINGLE_MACHINE = Principal(subject="(单机)", roles=frozenset({Role.ADMIN}))


def approvers_of(principals: list[Principal]) -> list[str]:
    """当前有审批权的人。PRD 08 的名单换成它之后,校验位置一行不用改。"""
    return sorted(item.subject for item in principals if item.allows(Action.APPROVE_TASK))


__all__ = [
    "ANONYMOUS",
    "MATRIX",
    "SINGLE_MACHINE",
    "Action",
    "Forbidden",
    "Principal",
    "Role",
    "approvers_of",
]
