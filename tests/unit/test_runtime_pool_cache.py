"""服务端任务池缓存要随影响装配的配置变化而更新。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

from agentgenome.config import Config
from agentgenome.server import app as server_app


def _config(*, agentteams: bool = False) -> Config:
    runtime: dict[str, Any] = {
        "default": "claude-code",
        "claude-code": {"cmd": "claude"},
    }
    if agentteams:
        runtime["agentteams"] = {
            "transport": "matrix-minio",
            "endpoint": "http://controller.example.com",
            "consumer_token_env": "AGENTTEAMS_CONSUMER_TOKEN",
            "matrix_homeserver": "http://matrix.example.com",
            "matrix_token_env": "AGENTTEAMS_MATRIX_TOKEN",
            "storage_prefix": "agentteams/agentteams-storage/shared",
        }
    return Config.model_validate({"runtime": runtime})


def test_a_runtime_config_change_rebuilds_the_cached_task_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设置页保存成功后,下一次推进不能继续拿配置变更前的运行时注册表。"""
    root = tmp_path / "ws"
    root.mkdir()
    app = server_app.create_app(root)
    request = Request({"type": "http", "app": app})
    assembled: list[tuple[str, ...]] = []

    def build(config: Config, _root: Path | None = None) -> dict[str, Any]:
        assembled.append(tuple(sorted(config.runtime.runtimes)))
        return {}

    monkeypatch.setattr(server_app, "build_runtimes", build)

    before = server_app._task_pool(request, root, _config())
    after = server_app._task_pool(request, root, _config(agentteams=True))
    reused = server_app._task_pool(request, root, _config(agentteams=True))

    assert assembled == [("claude-code",), ("agentteams", "claude-code")]
    assert after is not before
    assert reused is after
