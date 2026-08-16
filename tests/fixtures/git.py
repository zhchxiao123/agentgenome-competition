"""跑 git 的测试助手。

五个测试文件各写一份之后,"测试里怎么跑 git"就有了五个略有出入的版本——身份传法不同、
是否放行 file 协议不同。集中一处。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

#: 夹具仓的提交身份。用 `-c` 传:`submodule add` clone 出来的子仓没有本地身份配置。
IDENTITY = ("-c", "user.name=Fixture", "-c", "user.email=fixture@agentgenome.test")


def git(cwd: Path, *args: str) -> str:
    """在 `cwd` 上跑一条 git,返回 stdout。失败即抛。

    带上 `protocol.file.allow=always`:夹具的远端都是本地路径,而 git 2.38 起默认
    禁止子模块走 file 协议(CVE-2022-39253)。
    """
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_all(cwd: Path, message: str) -> str:
    """把工作树里的一切提交掉,返回新的 HEAD。"""
    git(cwd, "add", "-A")
    git(cwd, *IDENTITY, "commit", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


def fake_checkout(root: Path, *mounts: str) -> None:
    """把挂载点标成"已 checkout"。

    真子模块在挂载点下有一个写着 `gitdir:` 的 `.git` 文件——扫描就是靠它把"没 checkout"
    与"checkout 了但仓是空的"分开的。照着造一个即可,不必真挂一次子模块。
    """
    for mount in mounts:
        target = root / mount
        target.mkdir(parents=True, exist_ok=True)
        (target / ".git").write_text(f"gitdir: ../../.git/modules/{mount}\n", encoding="utf-8")


def write_plan(root: Path, task_id: str, *modules: str) -> None:
    """给这个任务落一份计划产物。

    开发员工的可写范围就是这份清单——真实的开发 Job 一定有它,所以夹具也必须有,
    否则测出来的是"没有计划时会怎样",而那是另一条路径。
    """
    target = root / "tasks" / task_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "plan.yaml").write_text(
        yaml.safe_dump({"modules": list(modules)}, allow_unicode=True), encoding="utf-8"
    )
