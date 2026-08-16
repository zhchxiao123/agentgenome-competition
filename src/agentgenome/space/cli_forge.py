"""`Forge` 的生产实现:封装 gh / glab。

**本模块刻意不写自动化测试**——它是对外部 CLI 的薄封装,测它等于测 `gh`。
以一次手工冒烟验证代替,步骤见 `docs/forge-smoke-test.md`。

因此这里的规矩是:不放任何逻辑。凡是需要判断的东西(幂等、冲突处理、顺序编排)
都归上层,这一层只做参数拼装与输出解析。一旦发现自己在这里写 if,说明那段逻辑
放错地方了。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from agentgenome.config import GitHost
from agentgenome.space.forge import (
    ForgeError,
    PRNotFound,
    PRRef,
    PRState,
    PRStatus,
    Rev,
)

_STATE_MAP = {
    "OPEN": PRState.OPEN,
    "MERGED": PRState.MERGED,
    "CLOSED": PRState.CLOSED,
    "opened": PRState.OPEN,
    "merged": PRState.MERGED,
    "closed": PRState.CLOSED,
}


class CliForge:
    """通过 gh / glab 与代码托管平台交互。"""

    def __init__(self, host: GitHost = "github") -> None:
        self.host = host
        self.command = "glab" if host == "gitlab" else "gh"

    def preflight(self) -> None:
        """启动期自检:CLI 装没装、登没登录。

        配置缺失应该在启动时报错,而不是等任务跑到提交阶段才炸。
        """
        if shutil.which(self.command) is None:
            raise ForgeError(f"未找到 {self.command},请先安装代码托管平台 CLI")
        result = self._run(Path.cwd(), "auth", "status", check=False)
        if result.returncode != 0:
            raise ForgeError(f"{self.command} 未登录: {result.stderr.strip()}")

    def open_pr(self, repo: Path | str, head: str, base: str, title: str, body: str) -> PRRef:
        cwd = Path(repo)
        # 先看这个分支上有没有开启中的 PR。`Forge` 协议要求 open_pr 幂等——
        # 崩溃恢复会重放,重复创建会在平台上留下两个 PR。
        existing = self._find_open_pr(cwd, head, base)
        if existing is not None:
            return existing
        result = self._run(
            cwd, "pr", "create", "--head", head, "--base", base, "--title", title, "--body", body
        )
        number = _parse_pr_number(result.stdout)
        return PRRef(repo=str(cwd), number=number, head=head, base=base)

    def merge_pr(self, pr: PRRef) -> Rev:
        status = self.pr_status(pr)
        if status.state is PRState.MERGED:
            # 幂等:已合并的再合一次返回同一 revision 而非报错。
            return status.merged_rev or ""
        self._run(Path(pr.repo), "pr", "merge", str(pr.number), "--merge")
        return self.pr_status(pr).merged_rev or ""

    def _find_open_pr(self, cwd: Path, head: str, base: str) -> PRRef | None:
        result = self._run(
            cwd,
            "pr",
            "list",
            "--head",
            head,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "number",
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        entries = json.loads(result.stdout)
        if not entries:
            return None
        return PRRef(repo=str(cwd), number=int(entries[0]["number"]), head=head, base=base)

    def pr_status(self, pr: PRRef) -> PRStatus:
        result = self._run(
            Path(pr.repo),
            "pr",
            "view",
            str(pr.number),
            "--json",
            "state,title,mergeCommit",
            check=False,
        )
        if result.returncode != 0:
            raise PRNotFound(f"PR 不存在: #{pr.number}({result.stderr.strip()})")
        payload = json.loads(result.stdout)
        merge_commit = payload.get("mergeCommit") or {}
        return PRStatus(
            state=_STATE_MAP.get(payload.get("state", ""), PRState.OPEN),
            title=payload.get("title", ""),
            merged_rev=merge_commit.get("oid"),
        )

    def is_protected(self, repo: Path | str, branch: str) -> bool:
        result = self._run(
            Path(repo),
            "api",
            f"repos/{{owner}}/{{repo}}/branches/{branch}/protection",
            check=False,
        )
        return result.returncode == 0

    def _run(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([self.command, *args], cwd=cwd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise ForgeError(f"{self.command} {' '.join(args)} 失败: {result.stderr.strip()}")
        return result


def _parse_pr_number(stdout: str) -> int:
    """从 `gh pr create` 的输出里取 PR 编号。输出是 PR 的 URL,编号是最后一段。"""
    url = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if not tail.isdigit():
        raise ForgeError(f"无法从输出中解析 PR 编号: {stdout!r}")
    return int(tail)
