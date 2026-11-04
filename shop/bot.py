"""Telegram shop bot. /start greets the buyer and opens the main menu."""

import os
import re
import time

from shop.errors import ShopError
from shop.stats import is_admin, load_state, pack_snapshot, remember_user
from shop.telegram_api import TelegramApi
from shop.tokens import telegram_bot_token

BUY_STARS = "Купить звезды"
STAT_BUTTON = "Статистика"
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
                "Статистика\n\nПользователи, заказы и сумма за 1, 3, 7, 30 дней и за всё время.",
                open_button=(STAT_BUTTON, admin_url(self.app_url)),
            )
            return
        if command != "/start":
            return
        raw_name = user.get("first_name") if isinstance(user.get("first_name"), str) else ""
        link = main_menu_link(self.api)
        self.api.send_message(
            chat_id,
            welcome_text(raw_name),
            link_button=(BUY_STARS, link) if link else None,
        )

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
        api.clear_menu_button()
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
    return base + "?s=" + packed


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
