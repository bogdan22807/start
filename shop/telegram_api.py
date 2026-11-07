"""Telegram Bot API calls. The bot token stays in the request URL and is not logged."""

import json
import ssl
import urllib.error
import urllib.request

from shop.errors import ShopError

SHOP_MENU = "Открыть магазин"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TelegramApi:
    def __init__(self, token: str, opener=None):
        self.token = token.strip()
        self._opener = opener or _post

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[tuple[str, str]]] | None = None,
        keyboard: list[list[str]] | None = None,
        placeholder: str | None = None,
        app_button: tuple[str, str] | None = None,
        remove_keyboard: bool = False,
        open_button: tuple[str, str] | None = None,
        link_button: tuple[str, str] | None = None,
        link_buttons: list[tuple[str, str]] | None = None,
    ) -> dict:
        payload: dict = {"chat_id": chat_id, "text": text}
        if link_buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": label, "url": url}] for label, url in link_buttons]
            }
        elif open_button is not None:
            label, url = open_button
            payload["reply_markup"] = {"inline_keyboard": [[{"text": label, "web_app": {"url": url}}]]}
        elif link_button is not None:
            label, url = link_button
            payload["reply_markup"] = {"inline_keyboard": [[{"text": label, "url": url}]]}
        elif remove_keyboard:
            payload["reply_markup"] = {"remove_keyboard": True}
        elif app_button is not None:
            label, url = app_button
            payload["reply_markup"] = {
                "keyboard": [[{"text": label, "web_app": {"url": url}}]],
                "resize_keyboard": True,
                "is_persistent": True,
            }
        elif keyboard is not None:
            markup: dict = {
                "keyboard": [[{"text": label} for label in row] for row in keyboard],
                "resize_keyboard": True,
                "is_persistent": True,
            }
            if placeholder:
                markup["input_field_placeholder"] = placeholder
            payload["reply_markup"] = markup
        elif buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row] for row in buttons
                ]
            }
        result = self._call("sendMessage", payload)
        return result if isinstance(result, dict) else {}

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def set_chat_menu_button(self, chat_id: int, url: str) -> None:
        self._call(
            "setChatMenuButton",
            {
                "chat_id": chat_id,
                "menu_button": {"type": "web_app", "text": SHOP_MENU, "web_app": {"url": url}},
            },
        )

    def answer_callback(self, callback_id: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id})

    def get_updates(self, offset: int | None) -> list[dict]:
        payload: dict = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=35)
        if not isinstance(result, list):
            raise ShopError("telegram_rejected", "Telegram вернул апдейты в неожиданном виде.", 502)
        return result

    def get_me(self) -> dict:
        result = self._call("getMe", {})
        return result if isinstance(result, dict) else {}

    def delete_webhook(self) -> None:
        self._call("deleteWebhook", {"drop_pending_updates": False})

    def set_menu_button(self, url: str) -> None:
        self._call(
            "setChatMenuButton",
            {"menu_button": {"type": "web_app", "text": SHOP_MENU, "web_app": {"url": url}}},
        )

    def clear_menu_button(self) -> None:
        self._call("setChatMenuButton", {"menu_button": {"type": "default"}})

    def set_commands(self) -> None:
        self._call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Купить звезды"},
                ]
            },
        )

    def set_admin_commands(self, chat_id: int) -> None:
        self._call(
            "setMyCommands",
            {
                "scope": {"type": "chat", "chat_id": chat_id},
                "commands": [
                    {"command": "start", "description": "Купить звезды"},
                    {"command": "stat", "description": "Статистика"},
                ],
            },
        )

    def _call(self, method: str, payload: dict, timeout: int = 20):
        if not self.token:
            raise ShopError("missing_bot_token", "Для бота в Telegram нужен BOT_TOKEN от @BotFather.", 503)
        status, body = self._opener(self.token, method, payload, timeout)
        if status >= 300 or not isinstance(body, dict) or not body.get("ok"):
            description = ""
            if isinstance(body, dict) and isinstance(body.get("description"), str):
                description = body["description"][:300]
            raise ShopError("telegram_rejected", description or "Telegram отклонил запрос.", 502)
        return body.get("result")


def _post(token: str, method: str, payload: dict, timeout: int) -> tuple[int, dict]:
    try:
        return _open(token, method, payload, timeout, None)
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc.reason):
            raise ShopError("telegram_unavailable", f"Telegram не ответил: {exc.reason}", 502) from exc
        try:
            return _open(token, method, payload, timeout, ssl._create_unverified_context())
        except urllib.error.URLError as retry_exc:
            raise ShopError("telegram_unavailable", f"Telegram не ответил: {retry_exc.reason}", 502) from retry_exc


def _open(token: str, method: str, payload: dict, timeout: int, context) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    handlers = [_NoRedirect]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, _decode(response.read(1_000_000))
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read(1_000_000))


def _decode(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode(errors="replace"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
