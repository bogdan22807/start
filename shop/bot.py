"""Telegram shop bot. /start opens the mini app and nothing else."""

import os
import time

from shop.errors import ShopError
from shop.telegram_api import TelegramApi
from shop.tokens import telegram_bot_token

BUY_STARS = "Купить звезды"
_DEFAULT_APP = "https://start-bogdan22807s-projects.vercel.app"


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
        if "id" not in chat:
            return
        text = message.get("text") or ""
        command, _rest = _command(text)
        if command != "/start":
            return
        user = message.get("from") or {}
        raw_name = user.get("first_name") if isinstance(user.get("first_name"), str) else ""
        self.api.send_message(
            int(chat["id"]),
            welcome_text(raw_name),
            link_button=(BUY_STARS, self.app_url),
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
