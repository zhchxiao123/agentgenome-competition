"""一个"真实的假平台"——按 Worker 资源语义行事的控制面替身。

与 `fake_agent` / `fake_mc` 同一条原则:不 mock 供应实现内部,拉起的是真的 HTTP
服务、走的是真的请求构造与状态码,只有"平台"这一端被换掉。它记得创建过什么,
于是"创建之后再查就存在"这类时序才测得出来——静态桩表达不了这件事。

旋钮(都是真实撞见过的形态):
- `never_ready`:受理了但永远起不来。
- `no_room`:起来了但没分到房间。
- `fail_status`:平台整体出事。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeWorkerPlatform:
    """`with FakeWorkerPlatform() as platform:` 后用 `platform.url` 当控制面入口。"""

    def __init__(
        self,
        never_ready: bool = False,
        no_room: bool = False,
        fail_status: int | None = None,
        wake_after: int = 0,
    ) -> None:
        #: Worker 名 → 资源。测试可以预置(模拟平台上已有的、包括不属于我们的)。
        self.workers: dict[str, dict[str, Any]] = {}
        #: 每个请求一条:method/path/body。断言"平台上发生了什么"用。
        self.records: list[dict[str, Any]] = []
        self._never_ready = never_ready
        self._no_room = no_room
        self._fail_status = fail_status
        self._wake_after = wake_after

        platform = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                platform.records.append(
                    {"method": self.command, "path": self.path, "body": raw,
                     "headers": dict(self.headers)}
                )
                status, body = platform._handle(self.command, self.path, raw)
                encoded = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = do_POST = do_PUT = do_DELETE = _serve

            def log_message(self, *args: Any) -> None:  # 静音
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # --- 资源语义 --------------------------------------------------------------

    def _handle(self, method: str, path: str, raw: str) -> tuple[int, dict[str, Any]]:
        if self._fail_status is not None:
            return self._fail_status, {"message": "platform is having a bad day"}

        # 控制面健康与 Matrix 身份:就绪检查探的就是这两条。同一个替身兼任两边,
        # 测试里把两个入口指到同一个地址即可。
        if path == "/api/v1/status":
            return 200, {"kubeMode": "embedded", "totalWorkers": len(self.workers)}
        if path.endswith("/_matrix/client/v3/account/whoami"):
            return 200, {"user_id": "@admin:example.com"}

        if path == "/api/v1/workers" and method == "POST":
            payload = json.loads(raw or "{}")
            name = str(payload.get("name", ""))
            self.workers[name] = self._materialize(name, payload)
            return 201, self.workers[name]

        if path.startswith("/api/v1/workers/"):
            name = path.rsplit("/", 1)[-1]
            if name.endswith("/wake") or name in ("wake", "sleep"):
                return self._lifecycle(path)
            if method == "GET":
                found = self.workers.get(name)
                return (200, found) if found else (404, {"message": "not found"})
            if method == "PUT":
                if name not in self.workers:
                    return 404, {"message": "not found"}
                payload = json.loads(raw or "{}")
                self.workers[name] = self._materialize(name, payload)
                return 200, self.workers[name]
            if method == "DELETE":
                if self.workers.pop(name, None) is None:
                    return 404, {"message": "not found"}
                return 204, {}

        if method == "POST" and (path.endswith("/wake") or path.endswith("/sleep")):
            return self._lifecycle(path)

        return 404, {"message": f"no route: {method} {path}"}

    def _lifecycle(self, path: str) -> tuple[int, dict[str, Any]]:
        action = path.rsplit("/", 1)[-1]
        name = path.rsplit("/", 2)[-2]
        worker = self.workers.get(name)
        if worker is None:
            return 404, {"message": "not found"}
        if action == "sleep":
            worker["state"] = "Sleeping"
            worker["phase"] = "Sleeping"
        else:
            worker["state"] = "Running"
            worker["phase"] = "Running"
        return 200, {"name": name, "phase": worker["phase"]}

    def _materialize(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """平台侧真实会做的事:分配房间与 Matrix 身份,把提交的字段存下来。"""
        return {
            **payload,
            "name": name,
            "phase": "Pending" if self._never_ready else "Running",
            "state": "Running",
            "roomID": "" if self._no_room else f"!room-{name}:example.com",
            "matrixUserID": f"@{name}:example.com",
        }

    def preset(self, name: str, **fields: Any) -> None:
        """预置一个平台上已经存在的 Worker(比如不属于我们的那些)。"""
        self.workers[name] = self._materialize(name, {"name": name, **fields})

    # --- 生命周期 --------------------------------------------------------------

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeWorkerPlatform:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


__all__ = ["FakeWorkerPlatform"]
