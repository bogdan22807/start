"""Telegram shop bot. /start greets the buyer and opens the main menu."""

import os
import re
import time

from shop.errors import ShopError
from shop.stats import build_report, is_admin, load_state, pack_snapshot, remember_user
from shop.telegram_api import TelegramApi
from shop.tokens import telegram_bot_token

BUY_STARS = "⭐ Купить звезды"
BUY_PREMIUM = "💎 Купить премиум"
STAT_BUTTON = "📊 Статистика"
PRIVACY_BUTTON = "Открыть политику"
_DEFAULT_APP = "https://start-bogdan22807s-projects.vercel.app"
_ADMIN_COMMANDS: set[int] = set()


def welcome_text(name: str) -> str:
    shown = name.strip() or "друг"
    return (
        f"🌟 Добро пожаловать, {shown}\n\n"
        "Здесь вы можете быстро и выгодно приобрести Telegram Звезды, Премиум - для себя или в подарок 🎁\n\n"
        "Для взаимодействия с ботом, используйте кнопки ниже 👇"
    )


class ShopBot:
    def __init__(self, api, app_url: str = ""):
        self.api = api
        self.app_url = app_url or webapp_url()

    def handle(self, update: dict) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if "id" not in chat or chat.get("type") not in (None, "private"):
            return
        user = message.get("from") if isinstance(message.get("from"), dict) else {}
        _remember(user)
        chat_id = int(chat["id"])
        if is_admin(user) and chat_id not in _ADMIN_COMMANDS:
            setter = getattr(self.api, "set_admin_commands", None)
            if setter:
                try:
                    setter(chat_id)
                except ShopError:
                    pass
                else:
                    _ADMIN_COMMANDS.add(chat_id)
        text = message.get("text") or ""
        command, _rest = _command(text)
        if command == "/stat":
            if not is_admin(user):
                return
            self.api.send_message(
                chat_id,
                stat_text(),
                open_button=(STAT_BUTTON, admin_url(self.app_url)),
            )
            return
        if command == "/privacy":
            self.api.send_message(
                chat_id,
                "🔒 Политика конфиденциальности: какие данные мы получаем, зачем и как их удалить.",
                link_buttons=[(PRIVACY_BUTTON, privacy_url(self.app_url))],
            )
            return
        if command != "/start":
            return
        raw_name = user.get("first_name") if isinstance(user.get("first_name"), str) else ""
        self._reset_shop_buttons(chat_id)
        link = main_menu_link(self.api)
        buttons = [(BUY_STARS, link + "=stars"), (BUY_PREMIUM, link + "=premium")] if link else None
        self.api.send_message(chat_id, welcome_text(raw_name), link_buttons=buttons)

    def _reset_shop_buttons(self, chat_id: int) -> None:
        """Drop the old «Открыть магазин» keyboard and point the menu button at the working shop.

        A keyboard web_app button opens the shop without the buyer's Telegram data, so payment
        never starts there. Telegram keeps that keyboard until the bot removes it.
        """
        try:
            sent = self.api.send_message(chat_id, "⏳", remove_keyboard=True)
            message_id = sent.get("message_id") if isinstance(sent, dict) else None
            deleter = getattr(self.api, "delete_message", None)
            if deleter and isinstance(message_id, int):
                deleter(chat_id, message_id)
        except ShopError:
            pass
        setter = getattr(self.api, "set_chat_menu_button", None)
        if setter:
            try:
                setter(chat_id, self.app_url)
            except ShopError:
                pass

    def run(self) -> None:
        offset = None
        while True:
            try:
                updates = self.api.get_updates(offset)
            except ShopError as exc:
                print(f"telegram: {exc.message}")
                time.sleep(3)
                continue
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    self.handle(update)
                except ShopError as exc:
                    print(f"telegram update: {exc.message}")


def main() -> None:
    token = telegram_bot_token()
    if not token:
        raise SystemExit(
            "Бот не запущен, поэтому /start в Telegram никуда не доходит. "
            "Нужен токен BotFather в BOT_TOKEN или в API_TOKEN (вид 123456789:AA...)."
        )
    api = TelegramApi(token)
    app_url = webapp_url()
    try:
        api.delete_webhook()
        api.set_commands()
        api.set_menu_button(app_url)
    except ShopError as exc:
        print(f"telegram setup: {exc.message}")
    print("telegram bot polling")
    ShopBot(api, app_url).run()


def main_menu_link(api) -> str:
    me = api.get_me() if hasattr(api, "get_me") else {}
    username = me.get("username") if isinstance(me, dict) else ""
    if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return f"https://t.me/{username}?startapp"
    return ""


def privacy_url(app_url: str) -> str:
    return app_url.rstrip("/") + "/privacy"


def admin_url(app_url: str) -> str:
    base = app_url.rstrip("/") + "/admin.html"
    token = telegram_bot_token()
    if not token:
        return base
    try:
        users, orders = load_state()
        packed = pack_snapshot(users, orders, token)
    except Exception:
        return base
    if not packed:
        return base
    return base + "?s=" + packed + "#" + packed


def stat_text() -> str:
    try:
        report = build_report(*load_state())
        entire = report["periods"]["all"]
    except Exception:
        entire = {"users": 0, "orders": 0, "sum": "0.00"}
    return (
        "📊 Статистика\n\n"
        f"👤 Пользователи: {entire['users']}\n"
        f"⭐ Заказы: {entire['orders']}\n"
        f"💰 Сумма: {entire['sum']} USDT"
    )


def _remember(user: dict) -> None:
    try:
        username = user.get("username") if isinstance(user.get("username"), str) else ""
        remember_user(user.get("id"), username)
    except Exception:
        return


def webapp_url() -> str:
    value = os.environ.get("WEBAPP_URL", "").strip()
    return value or _DEFAULT_APP


def _command(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", stripped
    head, _, rest = stripped.partition(" ")
    name = head.split("@", 1)[0].lower()
    return name, rest.strip()


if __name__ == "__main__":
    main()
