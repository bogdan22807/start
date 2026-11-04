"""Vercel function: admin statistics. Only @Bogdan22807 can read them."""

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shop.checkout import handle_request
from shop.errors import ShopError


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self._send(400, {"error": "bad_request", "message": "Слишком большое тело запроса."})
            return
        if length < 0 or length > 65536:
            self._send(400, {"error": "bad_request", "message": "Слишком большое тело запроса."})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            status, body = handle_request("POST", "/api/stat", parsed.query, self.headers, raw)
        except ShopError as exc:
            status, body = exc.status, {"error": exc.code, "message": exc.message}
        self._send(status, body)

    def do_GET(self) -> None:
        self._send(405, {"error": "method_not_allowed", "message": "Статистика открывается из бота."})

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        return
