"""`Forge` 的本地实现——全仓的次测试缝。

它不是 mock:开分支、合并、冲突检测全都走真实 git,只是没有网络。全仓后续所有
涉及 PR 与合并的测试都建在它上面,所以它的行为忠实度直接决定那些测试的可信度。

PR 元数据存在 bare 仓自己的引用命名空间下(`refs/agentgenome/pull/<n>`),
不是进程内状态——崩溃恢复要能把它读回来,换一个 Forge 实例也要看得见。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from agentgenome.space.forge import (
    MergeConflict,
    PRNotFound,
    PRRef,
    PRState,
    PRStatus,
    Rev,
)
from agentgenome.space.gitcmd import (
    ORCHESTRATOR_IDENTITY,
    branch_exists,
    git,
    git_lines,
    git_out,
    ref_exists,
)

PR_NAMESPACE = "refs/agentgenome/pull"
PROTECTED_NAMESPACE = "refs/agentgenome/protected"


class LocalForge:
    """对本地 bare 仓库的 Forge 实现。"""

    # --- PR ------------------------------------------------------------------

    def open_pr(self, repo: Path | str, head: str, base: str, title: str, body: str) -> PRRef:
        bare = Path(repo)
        self._require_branch(bare, head)
        self._require_branch(bare, base)

        existing = self._find_open_pr(bare, head, base)
        if existing is not None:
            # 崩溃恢复会重放,重复开 PR 不能造出两个。
            return existing

        number = self._next_number(bare)
        ref = PRRef(repo=str(bare), number=number, head=head, base=base)
        self._write_record(
            bare,
            number,
            {
                "number": number,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
                "state": PRState.OPEN.value,
                "merged_rev": None,
            },
        )
        return ref

    def merge_pr(self, pr: PRRef) -> Rev:
        bare = Path(pr.repo)
        record = self._read_record(bare, pr.number)

        if record["state"] == PRState.MERGED.value:
            # 幂等:已合并的再合一次返回同一 revision。
            merged: Rev = record["merged_rev"]
            return merged

        # 这里刻意**不**检查目标分支是否受保护:分支保护挡的是直接 push,
        # 而合并 PR 恰恰是变更合进受保护 main 的正当途径。在这里拦下来会让
        # 次缝的行为偏离真实平台(默认 protected_branch 就是 main)。
        rev = self._merge_in_scratch_clone(bare, head=record["head"], base=record["base"], pr=pr)
        record["state"] = PRState.MERGED.value
        record["merged_rev"] = rev
        self._write_record(bare, pr.number, record)
        return rev

    def pr_status(self, pr: PRRef) -> PRStatus:
        record = self._read_record(Path(pr.repo), pr.number)
        return PRStatus(
            state=PRState(record["state"]),
            title=record.get("title", ""),
            merged_rev=record.get("merged_rev"),
        )

    # --- 保护分支 ------------------------------------------------------------

    def is_protected(self, repo: Path | str, branch: str) -> bool:
        return ref_exists(Path(repo), f"{PROTECTED_NAMESPACE}/{branch}")

    def protect(self, repo: Path | str, branch: str) -> None:
        """把一个分支标记为受保护。生产实现里这一步在平台上做。"""
        bare = Path(repo)
        rev = git_out(bare, "rev-parse", branch)
        git(bare, "update-ref", f"{PROTECTED_NAMESPACE}/{branch}", rev)

    def unprotect(self, repo: Path | str, branch: str) -> None:
        git(Path(repo), "update-ref", "-d", f"{PROTECTED_NAMESPACE}/{branch}", check=False)

    # --- 内部 ----------------------------------------------------------------

    def _merge_in_scratch_clone(self, bare: Path, head: str, base: str, pr: PRRef) -> Rev:
        """在一个临时 clone 里做真实合并,成功才推回 bare。

        不在 bare 仓上直接操作:bare 没有工作树,冲突检测需要一个。临时目录用完即删,
        合并失败时 bare 完全不受影响——"冲突不能污染基线"这条靠它保证。
        """
        scratch = Path(tempfile.mkdtemp(prefix="agentgenome-forge-"))
        try:
            work = scratch / "work"
            git(scratch, "clone", "--quiet", str(bare), str(work))
            git(work, "checkout", "--quiet", base)
            result = git(
                work,
                *ORCHESTRATOR_IDENTITY,
                "merge",
                "--no-ff",
                "-m",
                f"Merge PR #{pr.number}: {head} -> {base}",
                f"origin/{head}",
                check=False,
            )
            if result.returncode != 0:
                raise MergeConflict(files=self._conflicting_files(work), pr=pr)
            git(work, "push", "--quiet", "origin", f"{base}:{base}")
            return git_out(work, "rev-parse", "HEAD")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    def _conflicting_files(work: Path) -> list[str]:
        return sorted(set(git_lines(work, "diff", "--name-only", "--diff-filter=U")))

    def _require_branch(self, bare: Path, branch: str) -> None:
        if not branch_exists(bare, branch):
            raise PRNotFound(f"分支不存在: {branch}(仓库 {bare})")

    def _record_ref(self, number: int) -> str:
        return f"{PR_NAMESPACE}/{number}"

    def _write_record(self, bare: Path, number: int, record: dict[str, Any]) -> None:
        """把 PR 元数据存成一个 blob,并让引用指向它。

        用 git 对象而非旁路文件,这样元数据跟着仓库走——clone 不带、但 bare 仓本身
        始终是唯一事实来源。
        """
        payload = json.dumps(record, ensure_ascii=False, indent=2)
        blob = git(bare, "hash-object", "-w", "--stdin", check=True, stdin=payload).stdout.strip()
        git(bare, "update-ref", self._record_ref(number), blob)

    def _read_record(self, bare: Path, number: int) -> dict[str, Any]:
        ref = self._record_ref(number)
        if not ref_exists(bare, ref):
            raise PRNotFound(f"PR 不存在: #{number}(仓库 {bare})")
        return dict(json.loads(git_out(bare, "cat-file", "blob", ref)))

    def _all_records(self, bare: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line in git_lines(bare, "for-each-ref", "--format=%(refname)", PR_NAMESPACE):
            records.append(dict(json.loads(git_out(bare, "cat-file", "blob", line))))
        return records

    def _find_open_pr(self, bare: Path, head: str, base: str) -> PRRef | None:
        for record in self._all_records(bare):
            if (
                record["head"] == head
                and record["base"] == base
                and record["state"] == PRState.OPEN.value
            ):
                return PRRef(repo=str(bare), number=record["number"], head=head, base=base)
        return None

    def _next_number(self, bare: Path) -> int:
        numbers = [record["number"] for record in self._all_records(bare)]
        return max(numbers, default=0) + 1
