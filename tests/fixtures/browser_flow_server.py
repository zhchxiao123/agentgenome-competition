"""给控制台 Playwright 主流程提供一个确定性的回放后端。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from typer.testing import CliRunner

from agentgenome.cli import app as cli_app
from agentgenome.server.app import create_app
from tests.fixtures.git import commit_all
from tests.fixtures.mall import materialize_mall


def main() -> None:
    with TemporaryDirectory(prefix="agentgenome-browser-") as temporary:
        root = Path(temporary)
        os.environ["AGENTGENOME_GLOBAL_PROCEDURES"] = str(root / "global")
        os.environ["AGENTGENOME_WORKTREES_HOME"] = str(root / "worktrees")
        (root / "global").mkdir()
        mall = materialize_mall(root / "upstream")
        workspace = root / "workspace"
        result = CliRunner().invoke(
            cli_app,
            [
                "init", "--local-only",
                str(workspace),
                "--name",
                "browser-flow",
                "--repo",
                mall["order-service"].remote_url,
            ],
        )
        if result.exit_code != 0:
            raise RuntimeError(result.output)

        # 主流程会先做需求解析，再派发开发员工。两者都必须走回放，避免浏览器测试
        # 在本地或 CI 悄悄启动真实外部 Agent；开发回放缺失时会确定性停在失败事实上。
        for employee_id in ("decision-employee", "dev-employee"):
            employee = workspace / "employees" / f"{employee_id}.yaml"
            employee.write_text(
                employee.read_text("utf-8").replace("runtime: claude-code", "runtime: replay"),
                encoding="utf-8",
            )
        commit_all(workspace, "chore: browser flow uses replay")

        recordings = root / "recordings"
        recording = recordings / "decision-employee__requirement-analysis__r1"
        recording.mkdir(parents=True)
        (recording / "result.json").write_text(
            json.dumps(
                {
                    "task_id": "",
                    "producer": "decision-employee",
                    "created_at": "2026-08-14T10:00:00Z",
                    "passed": True,
                    "modules": ["order-service"],
                    "cross_module": False,
                    "acceptance": ["主流程可观察"],
                    "risks": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.environ["AGENTGENOME_RECORDINGS"] = str(recordings)
        uvicorn.run(create_app(workspace), host="127.0.0.1", port=18081, log_level="warning")


if __name__ == "__main__":
    main()
