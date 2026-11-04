import unittest
from pathlib import Path

import os

from shop.bot import ShopBot, webapp_url, welcome_text
from shop.tokens import fragment_api_token, telegram_bot_token


class MemoryApi:
    def __init__(self):
        self.sent = []
        self.answered = []

    def send_message(
        self,
        chat_id,
        text,
        buttons=None,
        keyboard=None,
        placeholder=None,
        app_button=None,
        remove_keyboard=False,
        link_button=None,
    ):
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "buttons": buttons,
                "keyboard": keyboard,
                "placeholder": placeholder,
                "app_button": app_button,
                "remove_keyboard": remove_keyboard,
                "link_button": link_button,
            }
        )

    def answer_callback(self, callback_id):
        self.answered.append(callback_id)


class TokenTests(unittest.TestCase):
    def test_botfather_token_in_api_token_starts_the_bot(self):
        saved = {key: os.environ.get(key) for key in ("BOT_TOKEN", "API_TOKEN", "FRAGMENT_API_KEY")}
        try:
            os.environ.pop("BOT_TOKEN", None)
            os.environ.pop("FRAGMENT_API_KEY", None)
            os.environ["API_TOKEN"] = "123456789:AAFakeTokenForTestsOnly0001"
            self.assertEqual(telegram_bot_token(), os.environ["API_TOKEN"])
            self.assertEqual(fragment_api_token(), "")
            os.environ["BOT_TOKEN"] = "999:BBBBbbbbCCCCddddEEEEffffGGGGhhhhIII"
            self.assertEqual(telegram_bot_token(), os.environ["BOT_TOKEN"])
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class BotTests(unittest.TestCase):
    def setUp(self):
        self.api = MemoryApi()
        self.bot = ShopBot(self.api, "https://shop.example")

    def test_start_greets_by_name_and_links_the_shop(self):
        self.bot.handle(_message(10, "/start", first_name="Богдан"))
        sent = self.api.sent[-1]
        self.assertEqual(sent["text"], welcome_text("Богдан"))
        self.assertIn("кнопки ниже", sent["text"])
        self.assertEqual(sent["link_button"], ("Купить звезды", "https://shop.example"))
        self.assertIsNone(sent["app_button"])
        self.assertFalse(sent["remove_keyboard"])
        self.assertNotIn("Открыть магазин", sent["text"])

    def test_start_without_a_name_uses_a_fallback(self):
        self.bot.handle(_message(10, "/start", first_name=""))
        self.assertIn("Добро пожаловать, друг", self.api.sent[-1]["text"])

    def test_start_at_mention_uses_the_same_screen(self):
        self.bot.handle(_message(10, "/start@ShopBot", first_name="Анна"))
        sent = self.api.sent[-1]
        self.assertEqual(sent["link_button"][0], "Купить звезды")
        self.assertIsNone(sent["app_button"])

    def test_other_messages_do_nothing(self):
        for text in ("/catalog", "/id", "/cancel", "/paid 1", "⭐ Купить Звёзды", "привет"):
            self.bot.handle(_message(10, text, username="alexdev"))
        self.bot.handle(_callback(10, "alexdev", "p:self"))
        self.bot.handle(
            {
                "message": {
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 10, "username": "alexdev"},
                    "web_app_data": {"data": '{"kind":"stars","recipient":"alexdev","amount":1000}'},
                }
            }
        )
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.api.answered, [])

    def test_default_shop_url_is_the_vercel_app(self):
        saved = os.environ.get("WEBAPP_URL")
        try:
            os.environ.pop("WEBAPP_URL", None)
            self.assertEqual(webapp_url(), "https://start-bogdan22807s-projects.vercel.app")
            os.environ["WEBAPP_URL"] = "https://example.com/shop"
            self.assertEqual(webapp_url(), "https://example.com/shop")
        finally:
            if saved is None:
                os.environ.pop("WEBAPP_URL", None)
            else:
                os.environ["WEBAPP_URL"] = saved

    def test_mini_app_keeps_the_shop_and_adds_theme_and_payment(self):
        page = Path(__file__).parents[1].joinpath("public/index.html").read_text(encoding="utf-8")
        folded = page.lower()
        self.assertIn("telegram stars", folded)
        self.assertIn("18.75", page)
        self.assertIn("36.24", page)
        self.assertIn("14.99", page)
        self.assertIn("#1b86ee", folded)
        self.assertIn("#fffaf6", folded)
        self.assertIn("#face13", folded)
        self.assertIn("#000000", folded)
        self.assertIn("<svg", folded)
        self.assertIn("@keyframes", folded)
        self.assertIn("оплатить", folded)
        self.assertIn("банковская карта", folded)
        self.assertIn("visa и mastercard", folded)
        self.assertIn("crypto bot", folded)
        self.assertIn("heleket", folded)
        self.assertIn("оплата heleket временно недоступна", folded)
        self.assertIn("#f21818", folded)
        self.assertIn("data:image/png;base64,", page)
        self.assertIn('id="theme"', page)
        self.assertIn('data-theme="dark"', page)
        self.assertIn("/api/invoice", page)
        self.assertIn("init_data", page)
        self.assertIn("openTelegramLink", page)
        self.assertNotIn("деньги не списываются", folded)
        self.assertNotIn("украин", folded)
        self.assertNotIn("мир", folded)
        self.assertNotIn("сбп", folded)
        self.assertNotIn("способ оплаты", folded)


def _message(user_id, text, username="buyer", first_name="Покупатель"):
    user = {"id": user_id, "first_name": first_name}
    if username:
        user["username"] = username
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "text": text,
            "from": user,
            "chat": {"id": user_id, "type": "private"},
        },
    }


def _callback(user_id, username, data):
    user = {"id": user_id}
    if username:
        user["username"] = username
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb-1",
            "from": user,
            "data": data,
            "message": {"message_id": 3, "chat": {"id": user_id, "type": "private"}},
        },
    }


if __name__ == "__main__":
    unittest.main()
