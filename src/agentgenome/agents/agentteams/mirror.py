"""对 MinIO 的全部 I/O:一个走 `mc` 子进程的薄驱动。

走 `mc` 而不是 S3 SDK,与 AgentTeams 自己的 Worker 同步机制同一条路
(它们的 file-sync 走的就是 `mc mirror`),且不给本仓引入新的 Python 依赖。
驱动只搬字节,不含任何任务语义——那在 `taskdoc` 与传输装配层。

## "没有"与"失败"必须分开

拉一个还不存在的远端目录不算失败(产物没写完是常态,契约校验会用清晰的话
说"没有产物");凭证过期、网络断了才是失败,归因到**平台**并带上 stderr
摘要——"存储挂了"指向的下一步是查平台,不是查员工或工序。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

from agentgenome.agents.agentteams.transport import PlatformUnavailable

#: mc 用这句话表示**对象**不存在。靠 stderr 文本判定不体面,但 mc 的退出码
#: 不区分"没有"与"失败"——这是它的约定,假 mc 也按此回显。
#:
#: 刻意带上 "Object":别名配置错误时 mc 说的是 `alias "x" does not exist`,
#: 匹配过宽会把配置错误当成"对象还没写完",轮询就一直转到墙钟超时——
#: 而超时的归因指向 Worker,不指向配置。别名类错误由预检的 `ls` 探测兜底。
_MISSING_MARKER = "Object does not exist"


class MinioMirror:
    """一条到对象存储的通道。`prefix` 形如 ``myminio/agentteams``。"""

    def __init__(self, mc_argv: list[str], prefix: str) -> None:
        self._mc_argv = list(mc_argv)
        self._prefix = prefix.rstrip("/")

    def preflight(self) -> None:
        """mc 可执行 + 前缀可列。配置问题在启动期暴露,不留到第一次派发。

        `ls` 探测顺带把"别名没配 / 桶不存在"这类错误挡在这里——它们的 stderr
        与对象缺失同款措辞,进了运行期就会被误当成"还没写完"而空转。
        """
        executable = self._mc_argv[0]
        if shutil.which(executable) is None and not Path(executable).is_file():
            raise PlatformUnavailable(f"mc 客户端不存在: {executable}")
        probe = subprocess.run(
            [*self._mc_argv, "ls", self._prefix], capture_output=True, text=True, check=False
        )
        if probe.returncode != 0:
            raise PlatformUnavailable(self._render_failure("ls", probe))

    async def push_dir(self, local: Path, remote_rel: str) -> None:
        """把本地目录镜像到远端(远端多出来的会被删——现场以推送方为准)。"""
        await self._run("mirror", "--overwrite", "--remove", str(local), self._remote(remote_rel))

    async def pull_dir(self, remote_rel: str, local: Path) -> None:
        """把远端目录镜像到本地。远端不存在 → 本地留空,不算失败。"""
        local.mkdir(parents=True, exist_ok=True)
        result = await self._run_raw("mirror", "--overwrite", self._remote(remote_rel), str(local))
        if result.returncode != 0 and _MISSING_MARKER not in result.stderr:
            raise PlatformUnavailable(self._render_failure("mirror", result))

    async def read_file(self, remote_rel: str) -> str | None:
        """取回单个远端文件。不存在 → `None`,失败 → 抛。"""
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / "file"
            result = await self._run_raw("cp", self._remote(remote_rel), str(target))
            if result.returncode != 0:
                if _MISSING_MARKER in result.stderr:
                    return None
                raise PlatformUnavailable(self._render_failure("cp", result))
            return target.read_text(encoding="utf-8")

    # --- 内部 ----------------------------------------------------------------

    def _remote(self, rel: str) -> str:
        return f"{self._prefix}/{rel.strip('/')}"

    async def _run(self, *args: str) -> None:
        result = await self._run_raw(*args)
        if result.returncode != 0:
            raise PlatformUnavailable(self._render_failure(args[0], result))

    async def _run_raw(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return await asyncio.to_thread(
                subprocess.run,
                [*self._mc_argv, *args],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise PlatformUnavailable(f"无法拉起 mc: {exc}") from exc

    @staticmethod
    def _render_failure(command: str, result: subprocess.CompletedProcess[str]) -> str:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        summary = tail[-1] if tail else "(无输出)"
        return f"对象存储操作失败(mc {command}, 退出码 {result.returncode}): {summary}"


__all__ = ["MinioMirror"]
