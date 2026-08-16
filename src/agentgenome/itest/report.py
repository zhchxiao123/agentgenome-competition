"""集成测试报告:三件套里确定性的那两件。

## 为什么是 log_tail 而不是整份日志

几个容器的日志混在一起动辄上万行。原样注入上下文会把包挤爆,而真正相关的那几行反而被
淹掉——那正是 `ContextAssembler` 在解决的问题,不要在这里把它破坏掉。完整日志另行归档,
报告里给路径:需要深挖时材料还在。

**截断这件事本身要在报告里可见。** 静默截断读起来像"日志就这么多",而那正好是排查时最
误导人的一句话。

## repro_cmd 是拒绝 `--keep-env` 的前提

环境跑完就销毁。"保留现场排查"的需求由这一行命令满足,所以它必须**真的能跑**,不能是个
示意——参数带空格要加引号,项目名与编排文件要写全,而且要先重建镜像,因为粘贴它的那台
机器上什么都没有。

## submodule_pointers 不是可有可无的元数据

它是这份报告里唯一能回答"这个结果对应哪一组版本"的字段。跨模块任务里同一份代码在不同
指针组合下结果可能不同;没有它,历史报告是不可解释的。

## suspect_files 与 suggestion 在这里是空的

诊断那一半是 Agent 的活。这一层只定形状并保证**报告永远产出**:诊断挂了不该把测试结果
一起丢掉——测试结果是事实,诊断是增值。
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentgenome.core.verdict import VerdictKind
from agentgenome.itest.env import EnvPlan

#: 报告在产物目录里的名字。
REPORT_FILE = "itest-report.json"

#: 完整容器日志的归档目录,相对产物目录。
LOGS_DIR = "logs"

#: 每条失败带多少行日志尾部。
LOG_TAIL_LINES = 60

#: 报告的产出者。集成测试员工与脚本一起产出它,记员工。
PRODUCER = "itest-employee"


#: 这次失败的性质。状态机按它决定回开发态还是直接升级人工。
#:
#: **与门禁共用同一个枚举**,因为编排层的"改代码解决不了"判定读的是同一个 `kind` 字段。
#: 两套词汇的话,集成测试的环境失败会被当成语义失败塞进修复循环,白烧三轮。
ItestKind = VerdictKind


@dataclass(frozen=True)
class ItestFailure:
    """一条失败,以及排查它需要的全部东西。"""

    case: str
    message: str
    log_tail: str = ""
    repro_cmd: str = ""
    log_truncated: bool = False
    #: 以下两个是 Agent 诊断的产出。诊断没跑或跑挂了就是空的。
    suspect_files: tuple[str, ...] = ()
    suggestion: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "message": self.message,
            "log_tail": self.log_tail,
            "log_truncated": self.log_truncated,
            "repro_cmd": self.repro_cmd,
            "suspect_files": list(self.suspect_files),
            "suggestion": self.suggestion,
        }

    def with_diagnosis(self, suspect_files: list[str], suggestion: str) -> ItestFailure:
        return ItestFailure(
            case=self.case,
            message=self.message,
            log_tail=self.log_tail,
            repro_cmd=self.repro_cmd,
            log_truncated=self.log_truncated,
            suspect_files=tuple(suspect_files),
            suggestion=suggestion,
        )


@dataclass(frozen=True)
class EnvSnapshot:
    """这次测的是哪一套环境、哪一组版本。"""

    project: str
    compose_file: str
    built_modules: tuple[str, ...] = ()
    submodule_pointers: dict[str, str] = field(default_factory=dict)
    #: 这一轮闭包**覆盖到**的契约 id。覆盖不等于验证过——见 `task_itest` 的说明。
    interfaces: tuple[str, ...] = ()

    def as_dict(self, logs: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "project": self.project,
            "compose_file": self.compose_file,
            "built_modules": list(self.built_modules),
            "interfaces": list(self.interfaces),
            "submodule_pointers": dict(self.submodule_pointers),
            "logs": dict(logs or {}),
        }


@dataclass
class ItestReport:
    """一次集成测试的完整结论。"""

    task_id: str
    env: EnvSnapshot
    failures: list[ItestFailure] = field(default_factory=list)
    kind: ItestKind = ItestKind.NONE
    created_at: str = ""
    producer: str = PRODUCER
    #: 服务名 -> 归档日志的相对路径。由 `archive_logs` 填。
    log_paths: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "producer": self.producer,
            "created_at": self.created_at or datetime.now(UTC).isoformat(),
            "passed": self.passed,
            "kind": self.kind.value,
            "failures": [failure.as_dict() for failure in self.failures],
            "env": self.env.as_dict(self.log_paths),
        }

    def write(self, output_dir: Path) -> Path:
        target = Path(output_dir) / REPORT_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return target


def tail_of(text: str, lines: int = LOG_TAIL_LINES) -> tuple[str, bool]:
    """取日志尾部。返回 `(尾部, 是否发生了截断)`。

    取**尾部**而不是头部:要的是"最后发生了什么"。
    """
    split = text.splitlines(keepends=True)
    if len(split) <= lines:
        return text, False
    return "".join(split[-lines:]), True


def repro_command(
    plan: EnvPlan,
    service: str,
    argv: list[str],
    with_build: bool = False,
) -> str:
    """拼出一行可以直接粘贴执行的复现命令。**纯函数。**

    用 `shlex.quote` 而不是空格拼接:`-k "a or b"` 这种参数不加引号的话,粘过去就是
    另一条命令——而一条跑不通的复现命令比没有更糟,它会让人以为问题不复现。
    """
    base = ["docker", "compose", "-p", plan.project, "-f", str(plan.compose_file)]
    run = shlex.join([*base, "run", "--rm", service, *argv])
    if not with_build:
        return run
    return f"{shlex.join([*base, 'build', *plan.modules])} && {run}"


def archive_logs(output_dir: Path, logs: dict[str, str]) -> dict[str, str]:
    """把完整容器日志逐个服务落盘,返回服务名到相对路径的映射。

    服务名来自编排文件。那是项目自己的资产,不是可信输入——一个叫 `../../etc/passwd`
    的服务会把日志写到产物目录之外。
    """
    written = {}
    directory = Path(output_dir) / LOGS_DIR
    for service, content in logs.items():
        if "/" in service or "\\" in service or service in {"", ".", ".."}:
            raise ValueError(f"服务名不能当文件名用: {service!r}")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{service}.log").write_text(content, encoding="utf-8")
        written[service] = f"{LOGS_DIR}/{service}.log"
    return written


__all__ = [
    "LOGS_DIR",
    "LOG_TAIL_LINES",
    "PRODUCER",
    "REPORT_FILE",
    "EnvSnapshot",
    "ItestKind",
    "ItestFailure",
    "ItestReport",
    "archive_logs",
    "repro_command",
    "tail_of",
]
