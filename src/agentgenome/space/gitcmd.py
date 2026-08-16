"""git 子进程调用的薄封装。

只做三件事:统一工作目录、统一异常类型、把 stdout 收干净。刻意不抽象 git 的语义
——上层需要什么 git 行为就直接写什么 git 命令,这样看代码的人不必先学一层 DSL。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: 编排器自身的提交身份。员工产出 commit,推送由编排器执行,两者身份分开——
#: 这样提交历史里人、员工、编排器的贡献可区分。以 `-c` 传入,不依赖机器上的全局配置。
ORCHESTRATOR_IDENTITY = (
    "-c",
    "user.name=AgentGenome",
    "-c",
    "user.email=orchestrator@agentgenome.local",
)

#: 数字员工的提交身份。与编排器分开:提交历史里"这行代码是员工写的"与"这个指针是
#: 编排器前移的"要能一眼分辨——出事时第一个问题就是它。
EMPLOYEE_IDENTITY = (
    "-c",
    "user.name=AgentGenome Employee",
    "-c",
    "user.email=employee@agentgenome.local",
)


#: 一个 git revision(40 位 SHA)。整个空间层共用这一个别名,不各定各的。
Rev = str


class GitError(RuntimeError):
    """git 命令以非零退出码结束。"""

    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str) -> None:
        self.args_ = args
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(f"git {' '.join(args)} 失败({returncode}): {self.stderr}")


def is_local_url(url: str) -> bool:
    """判断远端是否是本地文件系统路径。

    本地远端只出现在测试夹具与离线场景里。git 2.38 起默认禁止子模块走 file 协议
    (CVE-2022-39253),所以这类 URL 需要显式放行——但只对本地 URL 放行,不全局开。
    """
    if "://" in url:
        return url.startswith("file://")
    return "@" not in url and Path(url).exists()


def git(
    cwd: Path,
    *args: str,
    check: bool = True,
    allow_file_protocol: bool = False,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    prefix = ["-c", "protocol.file.allow=always"] if allow_file_protocol else []
    result = subprocess.run(
        ["git", *prefix, *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise GitError(args, result.returncode, result.stderr)
    return result


def git_out(cwd: Path, *args: str, allow_file_protocol: bool = False) -> str:
    return git(cwd, *args, allow_file_protocol=allow_file_protocol).stdout.strip()


def git_lines(cwd: Path, *args: str) -> list[str]:
    out = git_out(cwd, *args)
    return out.splitlines() if out else []


def git_z(cwd: Path, *args: str) -> list[str]:
    """读 `-z` 形式的输出,按 NUL 切分且**不做 strip**。

    porcelain 的状态列以空格开头(` M path`),strip 掉会把路径削去首字母;
    路径本身也可以带前后空格。凡是 `-z` 输出一律走这里。
    """
    raw = git(cwd, *args).stdout
    return [field for field in raw.split("\0") if field]


def is_repo_root(path: Path) -> bool:
    """这个目录**自己**是不是一个仓库的根。

    不能用 `rev-parse --git-dir`:它会一路往上找,于是一个放在别人仓库里的目录会被判成
    "是个仓库",而后续的 `add`/`commit` 全落进外层那个仓库——症状是外层历史里凭空多出一堆
    没人认领的提交,而本目录的版本面其实一直是空的。
    """
    found = git(path, "rev-parse", "--show-toplevel", check=False)
    if found.returncode != 0:
        return False
    return Path(found.stdout.strip()).resolve() == Path(path).resolve()


def ref_exists(cwd: Path, ref: str) -> bool:
    """引用是否存在。全限定 ref,如 `refs/heads/main`。"""
    return git(cwd, "show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def branch_exists(cwd: Path, branch: str) -> bool:
    return ref_exists(cwd, f"refs/heads/{branch}")


def published_branch_exists(repository: Path, branch: str) -> bool:
    """任务分支是否已经发布到仓库。

    生产合并拿到的是普通 checkout:任务分支由隔离 worktree 推到 ``origin`` 后,当前
    checkout 未必保留对应的本地 ref。只查 ``refs/heads`` 会把已经发布的变更误判为
    "这个仓没改"并静默跳过。LocalForge 使用 bare repository,没有 origin,此时本地
    ``refs/heads`` 本身就是发布事实。
    """
    origin = git(repository, "remote", "get-url", "origin", check=False)
    if origin.returncode != 0 or not origin.stdout.strip():
        return branch_exists(repository, branch)
    return (
        git(
            repository,
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
            check=False,
        ).returncode
        == 0
    )
