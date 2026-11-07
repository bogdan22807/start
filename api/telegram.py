"""Telegram webhook. /start sends the welcome and the shop link.

GET /api/telegram?setup=1 points the bot at this webhook, sets the commands and the
«Открыть магазин» menu button, and shows where Telegram delivers updates. It only ever
sets the shop's own address, so calling it again does no harm.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from urllib.parse import parse_qs, urlparse

from shop.bot import ShopBot, webapp_url
from shop.errors import ShopError
from shop.telegram_api import TelegramApi
from shop.tokens import telegram_bot_token


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        if query.get("setup") or query.get("check"):
            self._send(200, setup_webhook(apply=bool(query.get("setup"))))
            return
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
        token = telegram_bot_token()
        if token and isinstance(update, dict):
            try:
                ShopBot(TelegramApi(token)).handle(update)
            except ShopError as exc:
                print(f"telegram update: {exc.message}")
        self._ok()

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _ok(self) -> None:
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def setup_webhook(apply: bool, api=None) -> dict:
    token = telegram_bot_token()
    if api is None:
        if not token:
            return {"ok": False, "message": "В Vercel нет BOT_TOKEN."}
        api = TelegramApi(token)
    hook = webapp_url().rstrip("/") + "/api/telegram"
    report: dict = {"ok": True, "webhook": hook}
    try:
        from shop.stats import storage_health

        report["stats"] = storage_health()
    except Exception:
        report["stats"] = {"ok": False}
    try:
        before = api.get_webhook_info()
        report["before"] = _webhook_summary(before, token)
        if apply:
            api.set_webhook(hook, os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""))
            api.set_commands()
            api.set_menu_button(webapp_url())
            report["after"] = _webhook_summary(api.get_webhook_info(), token)
    except ShopError as exc:
        report.update({"ok": False, "message": exc.message})
    return report


def _webhook_summary(info: dict, token: str = "") -> dict:
    keys = ("url", "pending_update_count", "last_error_date", "last_error_message")
    summary = {key: info.get(key) for key in keys if key in info}
    url = summary.get("url")
    if isinstance(url, str):
        if token:
            url = url.replace(token, "***")
        summary["url"] = url or "(пусто: вебхука нет, бот работает через getUpdates где-то ещё)"
    return summary
