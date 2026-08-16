"""工作区的推送快照与事务性取回。

## 不变量:返回时本地工作区即为最终现场

越权检查、后续工序接力、提交流水线都工作在**本地** workdir 上。所以远端改动
必须在 `run_job` 返回前完整落回本地;落不回来就整体作废,工作区逐字节保持
开工前状态——半份改动落地的现场没有任何下游能正确处理。

## 事务性怎么保证

先把**全部**路径校验完(越界、.git),一个不合法就整体拒绝、一个字节不写;
写入过程中出错则按记下的原样逐个恢复。校验先行让最常见的失败类(坏路径)
根本走不到写入。
"""

from __future__ import annotations

from pathlib import Path


class WorkspaceSyncError(RuntimeError):
    """取回无法完整落地。调用方要把它判成过程失败,且保证工作区未被触碰。"""


def snapshot_workspace(workdir: Path) -> dict[str, str]:
    """相对路径 → 内容的工作区快照,随任务推给 Worker。

    - `.git` 不进快照:git 内部状态不是现场的一部分,Worker 侧有自己的仓。
    - 符号链接不进快照:它可能指向工作区之外,跟着读等于把边界让出去。
    - 解不出 UTF-8 的文件不进快照:传输契约是文本映射。二进制资产要过去,
      是真实传输实现换共享存储引用时的优化,不改这份契约。
    """
    snapshot: dict[str, str] = {}
    for path in sorted(workdir.rglob("*")):
        if not path.is_file() or path.is_symlink() or ".git" in path.parts:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        snapshot[str(path.relative_to(workdir))] = content
    return snapshot


def ensure_inside(root: Path, relative: str, what: str) -> Path:
    """校验一条相对路径落在 `root` 之内,返回解析后的目标。

    产物落地与工作区取回共用这一道闸——同一个失败模式写两遍,迟早有一处更宽,
    而更宽的那一处就是漏洞。
    """
    target = (root / relative).resolve()
    if root != target and root not in target.parents:
        raise WorkspaceSyncError(f"{what}越出边界: {relative}")
    return target


def apply_changes(workdir: Path, changes: dict[str, str | None]) -> None:
    """把 Worker 的改动落到本地工作区。值为 `None` 表示删除。

    全部路径校验通过之前一个字节都不写;写入中途失败按原样恢复。

    原样快照存**字节**不存文本:被覆盖的可能是二进制文件,按 UTF-8 读它会炸——
    而炸在快照那一步意味着前面的写入没人回滚,恰好击穿事务性承诺。
    """
    root = workdir.resolve()
    targets: dict[str, Path] = {}
    for relative in changes:
        target = ensure_inside(root, relative, "Worker 的改动")
        if ".git" in target.relative_to(root).parts:
            # 改 .git 能让后续的 diff 说谎——越权检查与提交流水线全建立在
            # git 可信之上,这里必须拒绝。
            raise WorkspaceSyncError(f"Worker 的改动试图触碰 git 内部状态: {relative}")
        targets[relative] = target

    originals: dict[Path, bytes | None] = {}
    try:
        for relative, content in changes.items():
            target = targets[relative]
            originals[target] = target.read_bytes() if target.is_file() else None
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
    except OSError as exc:
        _restore(originals)
        raise WorkspaceSyncError(f"取回中途失败,已恢复开工前状态: {exc}") from exc


def _restore(originals: dict[Path, bytes | None]) -> None:
    for path, content in originals.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


__all__ = ["WorkspaceSyncError", "apply_changes", "ensure_inside", "snapshot_workspace"]
