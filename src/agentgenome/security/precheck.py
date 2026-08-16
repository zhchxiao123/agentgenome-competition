"""提交前的确定性检查:越权复查与密钥扫描。

## 为什么这一关不经过 Agent

"检查有没有越权"和"检查有没有密钥"是脚本的活。让被检查的人来检查自己是荒谬的——而一个
被提示注入影响过的员工,恰恰最有动机说"我没越权"。所以这整层是确定性的:输入是真实的 git
工作树,输出是一份任何人都能复算的报告。

## 为什么越权要查两遍

PRD 04 已经在每个 Job 结束后查过一遍。这里再查一遍防的不是同一类问题:

- 逐 Job 检查限制**爆炸半径**并给员工即时反馈;
- 提交前检查是最后一道,覆盖**组合效应**(多个 Job 各自合规但合起来越界),以及"逐 Job
  检查本身有 bug"。

两次共用同一个检测器(`core.scope`),成本只是一次 git 调用。

## 密钥报告绝不回显匹配内容

只记文件、行号、规则 id。回显等于把密钥写进日志、写进上下文包、写进事件流——一次泄漏变成
三次。排查因此变难是**可接受的代价**:拿着文件与行号去看那一行,人自己能看到。

这条在实现时极容易被违反,因为不回显会让排查变难,第一版通常会"顺手"把匹配行加进去。
测试里有一条把报告全文搜一遍确认不含密钥字符串,违反它的实现会直接红。

## 工具没装是环境问题

`gitleaks` 不在 PATH 上时判环境类失败并升级人工,不进修复循环——让开发员工去装一个它装不了
的东西,只会以完全相同的方式失败三轮。词汇与门禁共用 `core.verdict`。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentgenome.core.scope import ScopePolicy
from agentgenome.core.verdict import VerdictKind
from agentgenome.employees import EmployeeConfig
from agentgenome.genome.rules import ProtectedRules

#: 报告在产物目录里的名字。
REPORT_FILE = "precheck-report.json"

#: 密钥扫描命令。`--redact` 让 gitleaks 自己也不打印明文——纵深防御:即使我们读错了字段,
#: 它的 stdout 里也没有可泄漏的东西。
SECRETS_CMD = ("gitleaks", "detect", "--no-banner", "--no-git", "--redact")

SECRETS_TIMEOUT_S = 300


@dataclass(frozen=True)
class Finding:
    """一条问题。**不含任何匹配到的内容。**"""

    check: str
    path: str
    rule: str = ""
    line: int | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "path": self.path,
            "rule": self.rule,
            "line": self.line,
            "detail": self.detail,
        }

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where} — {self.detail or self.rule}"


@dataclass
class PrecheckReport:
    """提交前检查的结论。"""

    task_id: str
    passed: bool = True
    kind: VerdictKind = VerdictKind.NONE
    findings: list[Finding] = field(default_factory=list)
    #: 这次检查看的是哪些路径。为空说明没有改动可查——那本身值得记下来。
    checked_paths: list[str] = field(default_factory=list)
    producer: str = "precheck"
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        from datetime import UTC, datetime

        return {
            "task_id": self.task_id,
            "producer": self.producer,
            "created_at": self.created_at or datetime.now(UTC).isoformat(),
            "passed": self.passed,
            "kind": self.kind.value,
            "checked_paths": list(self.checked_paths),
            "failures": [
                {"message": item.render(), "evidence": item.as_dict()} for item in self.findings
            ],
        }

    def write(self, output_dir: Path) -> Path:
        target = Path(output_dir) / REPORT_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return target


def participant_policies(
    employees: Sequence[EmployeeConfig],
    task_id: str,
    protected: ProtectedRules,
    module_paths: Sequence[str] = (),
    extra_forbid: Mapping[str, Sequence[str]] | None = None,
) -> list[ScopePolicy]:
    """参与过这个任务的每个员工**各自**的授权范围。

    **不能把它们并成一个 `ScopePolicy`。** 授权是"可写路径减去禁写路径",而两个员工的禁写
    集合并起来会互相污染:架构员工禁写 `repos/**`,开发员工正是靠它干活——并集之后开发
    员工写的每一行业务代码都成了越权。第一版就是这么写的,症状是每个任务都在提交前被打回。

    正确的判定是**或**:一条路径只要有任意一个参与者本可以合法写它,就不算越权。这条检查
    因此仍然有意义——它抓的是"落在所有人授权范围之外"的路径,也就是组合效应真正危险的那种。

    **收窄会自然穿过这条"或"。** 开发员工按任务收敛之后,计划外模块的路径不再有任何参与者
    能合法写它,于是复查抓得住——不必在这里为收窄补一条特判。

    `extra_forbid` 按员工 id 给:任务级禁令**每个员工各不相同**(写集分离里开发员工禁写测试、
    测试员工禁写实现),给一份共用的话两边会互相禁死。派发时叠了、复查时不叠的话,一条越权
    改动能在 Job 那一关被抓住却在提交前被放行——而提交前这一关正是最后一道。
    """
    by_employee = extra_forbid or {}
    return [
        employee.scope(
            task_id,
            protected.paths_for(employee.id),
            module_paths=module_paths,
            extra_forbid=by_employee.get(employee.id, ()),
        )
        for employee in employees
    ]


def check_scope(changed: Sequence[str], policies: Sequence[ScopePolicy]) -> list[Finding]:
    """越权复查。**纯函数**——路径进,问题出。

    没有任何参与者时一律判越权:一个没有员工干过活却产生了改动的任务,那些改动是哪来的?
    这个问题必须被问出来,而不是因为"名单是空的所以没有约束"被放过去。
    """
    found = []
    for path in sorted(set(changed)):
        if any(policy.allows(path) for policy in policies):
            continue
        reasons = [policy.matched_forbid(path) for policy in policies]
        rule = next((item for item in reasons if item), "")
        found.append(
            Finding(
                check="scope",
                path=path,
                rule=rule,
                detail=(
                    f"{path}:命中禁写规则 {rule}"
                    if rule
                    else f"{path}:不在任何参与者的授权可写路径内"
                ),
            )
        )
    return found


def scan_secrets(workdir: Path, command: Sequence[str] = SECRETS_CMD) -> tuple[list[Finding], bool]:
    """扫描工作树里的密钥。返回 `(问题清单, 工具是否可用)`。

    走 JSON 报告文件而不是解析 stdout:stdout 的格式会随版本变,而**解析错了的方向是漏报**。
    """
    if shutil.which(command[0]) is None:
        return [], False

    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "gitleaks.json"
        argv = [
            *command,
            "--source",
            str(workdir),
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ]
        try:
            subprocess.run(argv, capture_output=True, text=True, timeout=SECRETS_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            return [], False
        if not report.is_file():
            return [], True
        try:
            payload = json.loads(report.read_text(encoding="utf-8") or "[]")
        except json.JSONDecodeError:
            return [], True

    findings = []
    for item in payload if isinstance(payload, list) else []:
        # **只取这三个字段。** `Secret`、`Match`、`Line` 都含明文,碰都不要碰。
        findings.append(
            Finding(
                check="secrets",
                path=str(item.get("File", "(未知文件)")),
                rule=str(item.get("RuleID", "(未知规则)")),
                line=item.get("StartLine"),
                detail="疑似密钥。内容不回显——按文件与行号去看那一行。",
            )
        )
    return findings, True


def run_prechecks(
    task_id: str,
    workdir: Path,
    changed: Sequence[str],
    policies: Sequence[ScopePolicy],
    secrets_command: Sequence[str] | None = None,
) -> PrecheckReport:
    """顺序跑两道检查,产出一份报告。**不抛异常。**

    两道都跑完再判,不是第一道挂了就返回:一次把问题报全,免得员工修完越权再来一轮才发现
    还有密钥。
    """
    # 在调用时取而不是写成默认参数:默认参数在 def 那一刻就绑定了,配置或测试改掉模块
    # 常量之后它仍然指向旧值——而那种失效完全没有症状。
    secrets_command = secrets_command or SECRETS_CMD
    report = PrecheckReport(task_id=task_id, checked_paths=sorted(changed))
    report.findings += check_scope(changed, policies)

    secrets, available = scan_secrets(Path(workdir), secrets_command)
    if not available:
        report.passed = False
        report.kind = VerdictKind.ENVIRONMENT
        report.findings.append(
            Finding(
                check="secrets",
                path="(工具)",
                rule=secrets_command[0],
                detail=f"{secrets_command[0]} 不可用,无法确认这次改动里有没有密钥。",
            )
        )
        return report

    report.findings += secrets
    if report.findings:
        report.passed = False
        report.kind = VerdictKind.SEMANTIC
    return report


__all__ = [
    "REPORT_FILE",
    "SECRETS_CMD",
    "Finding",
    "PrecheckReport",
    "check_scope",
    "run_prechecks",
    "scan_secrets",
    "participant_policies",
]
