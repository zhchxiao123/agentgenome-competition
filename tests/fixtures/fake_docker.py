"""一个假的 `docker` 可执行文件。

开发环境里没有 docker,而 `itest.env` 的编排逻辑——拉起、等健康、跑、`finally` 销毁、
超时强杀——恰恰是最会出 bug 的地方。

**这不是把 docker mock 掉。** 被测代码照常 `subprocess` 拉一个真进程、真的等它、真的按
超时杀它;换掉的只是那个进程是谁。所以顺序、异常路径、超时这些都是真验过的。

它**不能**验的是 docker 自己的行为:`--wait` 到底等不等健康、并发项目会不会撞端口、
`down -v` 清不清干净。那三条是 docker 的事实而不是我们的逻辑,由 issue 01 的人工 spike
覆盖。看到这个文件时不要以为那三条已经有守护了。
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

#: 被识别的 compose 子命令。假可执行文件按它决定这一次该怎么表现。
SUBCOMMANDS = ("build", "up", "down", "run", "exec", "logs", "ps", "config")

_SOURCE = '''\
#!/usr/bin/env python3
"""测试用的假 docker。行为由 FAKE_DOCKER_PLAN 里的 JSON 决定。"""
import json
import os
import sys
import time

argv = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv, ensure_ascii=False) + "\\n")

plan = json.loads(os.environ.get("FAKE_DOCKER_PLAN") or "{}")
known = set(json.loads(os.environ["FAKE_DOCKER_SUBCOMMANDS"]))
command = next((item for item in argv if item in known), "")

if plan.get("hang") == command:
    time.sleep(600)
sys.stdout.write(plan.get("stdout", {}).get(command, ""))
sys.stderr.write(plan.get("stderr", {}).get(command, ""))
sys.exit(plan.get("exit", {}).get(command, 0))
'''


@dataclass
class FakeDocker:
    """一个已经写到盘上的假 docker,以及它收到过的全部 argv。"""

    path: Path
    log: Path
    #: 每个子命令的退出码。没写的按 0。
    exit_codes: dict[str, int] = field(default_factory=dict)
    stdout: dict[str, str] = field(default_factory=dict)
    stderr: dict[str, str] = field(default_factory=dict)
    #: 哪个子命令要卡住不返回。用来验超时。
    hang: str | None = None

    @property
    def command(self) -> str:
        return str(self.path)

    def env(self) -> dict[str, str]:
        """跑之前要塞进进程环境的东西。"""
        plan = {
            "exit": self.exit_codes,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "hang": self.hang,
        }
        return {
            "FAKE_DOCKER_LOG": str(self.log),
            "FAKE_DOCKER_SUBCOMMANDS": json.dumps(SUBCOMMANDS),
            "FAKE_DOCKER_PLAN": json.dumps(plan, ensure_ascii=False),
        }

    def install(self) -> None:
        """把环境变量装进当前进程。被测代码跑的子进程会继承它们。"""
        os.environ.update(self.env())

    def calls(self) -> list[list[str]]:
        if not self.log.is_file():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def subcommands(self) -> list[str]:
        """只保留 compose 子命令,按发生顺序。断言"顺序对不对"用它。"""
        found = []
        for call in self.calls():
            match = next((item for item in call if item in SUBCOMMANDS), None)
            if match:
                found.append(match)
        return found


def install_fake_docker(root: Path, **kwargs: object) -> FakeDocker:
    """在 `root` 下写出假 docker 并装好环境变量。"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "docker"
    path.write_text(_SOURCE, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    fake = FakeDocker(path=path, log=root / "calls.jsonl", **kwargs)  # type: ignore[arg-type]
    fake.install()
    return fake


def install_fake_docker_on_path(root: Path, monkeypatch: object, **kwargs: object) -> FakeDocker:
    """装一个假 docker,并把它所在的目录插到 PATH 最前面。

    端到端链路里被测代码跑的是子进程的子进程(编排器 → Procedure 脚本 → docker),没法逐层
    传一个可执行文件路径进去。PATH 是这条链路上唯一天然会被继承的东西。
    """
    fake = install_fake_docker(root, **kwargs)
    monkeypatch.setenv("PATH", f"{root}{os.pathsep}{os.environ['PATH']}")  # type: ignore[attr-defined]
    for key, value in fake.env().items():
        monkeypatch.setenv(key, value)  # type: ignore[attr-defined]
    return fake
