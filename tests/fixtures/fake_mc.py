"""一个"真实的假 mc"——把存储前缀映射到本地目录的 MinIO 客户端替身。

与 `fake_agent` 同一条原则:不 mock subprocess,拉起的是真进程,于是真实的
参数构造、真实的退出码、真实的 stderr 全被测到,只有"对象存储"被换成了
本地目录。

环境变量:
- ``FAKE_MC_ROOT``:存储前缀映射到的本地根目录(必给)。
- ``FAKE_MC_PREFIX``:被翻译的前缀(如 ``myminio/agentteams``,必给)。
- ``FAKE_MC_FAIL``:非空时任何调用都以退出码 1 失败,stderr 回显它的值。
- ``FAKE_MC_LOG``:非空时把每次调用的参数逐行追加到该文件。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _translate(arg: str) -> Path:
    root = Path(os.environ["FAKE_MC_ROOT"])
    prefix = os.environ["FAKE_MC_PREFIX"]
    if arg == prefix:
        return root
    if arg.startswith(prefix + "/"):
        return root / arg[len(prefix) + 1 :]
    return Path(arg)


def main() -> int:
    log = os.environ.get("FAKE_MC_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as sink:
            sink.write(" ".join(sys.argv[1:]) + "\n")

    if os.environ.get("FAKE_MC_FAIL"):
        sys.stderr.write(os.environ["FAKE_MC_FAIL"] + "\n")
        return 1

    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    command = args[0]

    if command == "ls":
        source = _translate(args[1])
        if not source.exists():
            sys.stderr.write(f"mc: <ERROR> Unable to list `{args[1]}`. Object does not exist\n")
            return 1
        sys.stdout.write("\n".join(p.name for p in source.iterdir()) + "\n")
        return 0

    source, target = _translate(args[1]), _translate(args[2])

    if command == "mirror":
        if not source.is_dir():
            sys.stderr.write(f"mc: <ERROR> Unable to mirror `{args[1]}`. Object does not exist\n")
            return 1
        # 与真 mc 对齐:带 --remove 才删目标端多出来的东西,不带就只是叠加。
        if target.exists() and "--remove" in flags:
            shutil.rmtree(target)
        shutil.copytree(source, target, dirs_exist_ok=True)
        return 0

    if command == "cp":
        if not source.is_file():
            sys.stderr.write(f"mc: <ERROR> Unable to stat `{args[1]}`. Object does not exist\n")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, target)
        return 0

    sys.stderr.write(f"mc: unknown command {command}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
