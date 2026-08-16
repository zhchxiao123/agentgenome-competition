"""一个"真实的假子进程"——按剧本行事的测试用 CLI Agent 替身。

刻意不 mock `subprocess` / `asyncio.create_subprocess_exec`:那样测的是我们对
子进程 API 的想象。这里拉起的是一个真实进程,于是真实的进程管理、真实的流读取、
真实的超时与强杀、真实的产物落盘全都被测到,只有"被拉起的是谁"被替换了。

剧本用环境变量传,因为运行时构造 argv 的方式是被测对象之一,不该被测试劫持。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

#: 剧本环境变量。
SCRIPT_ENV = "FAKE_AGENT_SCRIPT"


def main() -> int:
    script = json.loads(os.environ.get(SCRIPT_ENV, "{}"))
    # 路径从剧本里拿。让生产代码去注入 FAKE_AGENT_* 会把测试细节漏进它。
    output_dir = Path(script["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    if script.get("child_pid_file"):
        child = subprocess.Popen(  # noqa: S603 - 固定解释器与固定测试脚本
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path(script["child_pid_file"]).write_text(str(child.pid), encoding="utf-8")

    for raw in script.get("raw_lines", []):
        sys.stdout.write(raw + "\n")
        sys.stdout.flush()

    for line in script.get("stream", []):
        sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if script.get("delay_per_line"):
            time.sleep(script["delay_per_line"])

    for relative, content in (script.get("files") or {}).items():
        target = Path(script["workdir"]) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    # 是不是重试那一次:用产物目录里的标记判断——两次尝试共用同一个 output_dir,
    # 这本身就是被测语义之一(重试是增量修复,产物保留在原地)。
    marker = output_dir / ".attempted"
    retrying = marker.exists()
    if not retrying:
        marker.write_text("1")

    # 首次写 `output_files`,重试只写 `retry_output_files`——剧本化"已通过的文件
    # 不要重写、第二次尝试只修被点名的那几个"。首次写下的文件在重试时依然在场,
    # 因为两次尝试共用同一个 output_dir。
    key = "retry_output_files" if retrying else "output_files"
    for relative, content in (script.get(key) or {}).items():
        target = output_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    result = script.get("result")
    if script.get("succeed_on_retry"):
        # 第一次故意不写结果,第二次才写——用来验证契约失败后的重试。
        if retrying:
            result = {
                "task_id": "ag-1",
                "producer": "dev-employee",
                "created_at": "2026-09-01T10:00:00Z",
                "passed": True,
            }
        else:
            result = None

    if result is not None:
        payload = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        (output_dir / "result.json").write_text(payload, encoding="utf-8")

    if script.get("hang"):
        # 挂住不退出——用来触发超时与强杀。
        while True:
            time.sleep(0.05)

    return int(script.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
