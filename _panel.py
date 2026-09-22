"""猫娘插件通用面板服务模板（本地 127.0.0.1，零第三方依赖）

每个插件复制本文件并注入：
- html_provider() -> str            面板页 HTML
- endpoints: {("GET"|"POST", 路由): 处理函数(json_body)->dict}
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

# 静态资源用得上的一小张 MIME 表；面板靠它发字体、背景图与图标
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".mp3": "audio/mpeg",
}


def guess_mime(name: str) -> str:
    return _MIME.get(Path(name).suffix.lower(), "application/octet-stream")


class PanelServer:
    def __init__(
        self,
        port: int,
        html_provider: Callable[[], str],
        endpoints: dict[tuple[str, str], Callable[[dict[str, Any]], dict[str, Any]]],
        static_dir: Optional[Path] = None,
        asset_provider: Optional[Callable[[str], Optional[tuple[bytes, str]]]] = None,
    ):
        self.port = int(port)
        self._html_provider = html_provider
        self._endpoints = endpoints
        # 面板自己起的端口只能发 "/" 和 API，字体/背景图要靠这里；
        # 不给 static_dir 时行为与以前完全一致。
        self._static_dir = Path(static_dir).resolve() if static_dir else None
        # 自定义背景图之类的资源不在 static/ 里，交给插件自己给（传路径 -> (bytes, mime)）
        self._asset_provider = asset_provider
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _reply(self, body: bytes, ctype: str):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                # 图片与字体可以缓存；不然每次开面板都要重下背景图
                head = ctype.split(";")[0].strip().lower()
                if head.startswith(("image/", "font/")):
                    self.send_header("Cache-Control", "public, max-age=86400")
                else:
                    self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._reply(b"{}", "application/json")

            def do_GET(self):
                route = self.path.split("?", 1)[0]
                if route in ("/", "/index.html"):
                    self._reply(outer._html_provider().encode("utf-8"), "text/html; charset=utf-8")
                    return
                fn = outer._endpoints.get(("GET", route))
                if fn is None:
                    rel = route.lstrip("/").split("?", 1)[0]
                    hit = None
                    if outer._asset_provider is not None:
                        try:
                            hit = outer._asset_provider(rel)
                        except Exception:
                            hit = None
                    if hit is None:
                        hit = outer._static_asset(rel)
                    if hit is not None:
                        self._reply(hit[0], hit[1])
                        return
                    self.send_error(404)
                    return
                try:
                    payload = fn({})
                    self._reply(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                except Exception as exc:
                    self._reply(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def do_POST(self):
                route = self.path.split("?", 1)[0]
                fn = outer._endpoints.get(("POST", route))
                if fn is None:
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    body = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                try:
                    payload = fn(body if isinstance(body, dict) else {})
                    self._reply(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                except Exception as exc:
                    self._reply(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            return False
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name=f"neko-panel-{self.port}"
        )
        self._thread.start()
        return True

    def _static_asset(self, rel: str) -> Optional[tuple[bytes, str]]:
        """从 static/ 里取文件（只读、防目录穿越）。"""
        if self._static_dir is None:
            return None
        rel = (rel or "").strip().lstrip("/")
        if not rel or rel.endswith("/"):
            rel = "index.html"
        try:
            target = (self._static_dir / rel).resolve()
        except Exception:
            return None
        if target != self._static_dir and self._static_dir not in target.parents:
            return None
        if not target.is_file():
            return None
        try:
            return target.read_bytes(), guess_mime(target.name)
        except Exception:
            return None

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None


def find_open_port(preferred: int, attempts: int = 5) -> int:
    """从 preferred 开始找一个可绑定端口（+1 递增）。"""
    import socket

    for offset in range(attempts):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    return preferred


_PAGE_CSS = """
body{margin:0;font-family:'Microsoft YaHei',sans-serif;background:#1b1826;color:#e8e4f2;display:flex;justify-content:center;padding:24px}
.wrap{max-width:860px;width:100%}
h1{font-size:24px;margin:8px 0 4px}
.sub{color:#9a93b5;font-size:14px;margin-bottom:16px}
.card{background:#241f33;border:1px solid #3a3352;border-radius:16px;padding:16px;margin-bottom:16px}
.card h2{font-size:17px;margin:2px 0 12px;color:#c9b9ff}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:6px;border-bottom:1px solid #322b4a}
button{background:#6c5ce7;color:#fff;border:0;border-radius:10px;padding:7px 16px;font-size:13px;cursor:pointer;margin-right:8px}
button:hover{background:#7d6ef0}
input[type=text],input[type=number]{background:#2a2440;color:#e8e4f2;border:1px solid #3a3352;border-radius:8px;padding:6px 10px;font-size:14px;width:110px}
.toggle{display:inline-block;padding:4px 12px;border-radius:20px;font-size:13px}
.on{background:#1f4a2e;color:#7be3a2}
.off{background:#4a1f2a;color:#e37b8f}
#status{color:#9a93b5;font-size:13px;margin-left:8px}
.kv td:first-child{color:#9a93b5;width:180px}
"""
