"""Telegram webhook. /start sends the welcome and the shop link."""

import json
import os
from http.server import BaseHTTPRequestHandler

from shop.bot import ShopBot
from shop.errors import ShopError
from shop.telegram_api import TelegramApi


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._ok()

    def do_POST(self) -> None:
        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        provided = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secret and provided != secret:
            self._ok()
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 1_000_000:
            self._ok()
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            update = json.loads(raw.decode())
        except json.JSONDecodeError:
            update = {}
        token = os.environ.get("BOT_TOKEN", "").strip()
        if token and isinstance(update, dict):
            try:
                ShopBot(TelegramApi(token)).handle(update)
            except ShopError as exc:
                print(f"telegram update: {exc.message}")
        self._ok()

    def _ok(self) -> None:
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
