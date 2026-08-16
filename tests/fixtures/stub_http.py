"""本地 stub HTTP 服务器:记录每个请求,按配置应答。

站在 Matrix homeserver 与 AgentTeams controller 的位置上——真实的 HTTP 栈、
真实的鉴权头传递,只有"平台"是假的。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class StubServer:
    """`with StubServer() as stub:` 后用 `stub.url` 当平台入口。"""

    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self._status = status
        self._body = json.dumps(body if body is not None else {}).encode()
        #: 每个请求一条:method/path/headers/body。
        self.records: list[dict[str, Any]] = []

        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                stub.records.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": self.rfile.read(length).decode("utf-8") if length else "",
                    }
                )
                self.send_response(stub._status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(stub._body)))
                self.end_headers()
                self.wfile.write(stub._body)

            do_GET = do_POST = do_PUT = do_DELETE = _serve

            def log_message(self, *args: Any) -> None:  # 静音
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> StubServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


__all__ = ["StubServer"]
