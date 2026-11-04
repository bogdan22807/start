import tempfile
import unittest
from pathlib import Path

import os

from shop.bot import ShopBot
from shop.orders import OrderStore
from shop.tokens import fragment_api_token, telegram_bot_token
from test_shop import make_shop


class MemoryApi:
    def __init__(self):
        self.sent = []
        self.answered = []

    def send_message(self, chat_id, text, buttons=None):
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})

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
        self.tmp = tempfile.TemporaryDirectory()
        self.shop = make_shop(OrderStore(Path(self.tmp.name) / "orders.db"))
        self.shop.create_order = self._order_is_not_created
        self.api = MemoryApi()
        self.bot = ShopBot(self.shop, self.api, {7})

    def tearDown(self):
        self.shop.store.close()
        self.tmp.cleanup()

    def test_start_shows_stars_and_premium(self):
        self.bot.handle(_message(10, "/start"))
        sent = self.api.sent[-1]
        labels = [button[0] for row in sent["buttons"] for button in row]
        self.assertEqual(labels, ["Купить звезды", "Телеграм премиум"])
        self.assertEqual([button[1] for row in sent["buttons"] for button in row], ["k:stars", "k:premium"])

    def test_stars_for_self_then_quantity_stops(self):
        self.bot.handle(_callback(20, "alexdev", "k:stars"))
        screen = self.api.sent[-1]
        self.assertIn("Минимальный заказ от 50 звёзд.", screen["text"])
        self.assertEqual(
            [(button[0], button[1]) for button in screen["buttons"][0]],
            [("Себе", "w:self:stars"), ("Другу", "w:friend:stars")],
        )

        self.bot.handle(_callback(20, "AlexDev", "w:self:stars"))
        self.assertIn("Сколько звёзд отправить @alexdev?", self.api.sent[-1]["text"])

        self.bot.handle(_message(20, "40", username="alexdev"))
        self.assertIn("Минимальный заказ от 50", self.api.sent[-1]["text"])

        self.bot.handle(_message(20, "50", username="alexdev"))
        self.assertEqual(self.api.sent[-1]["text"], "Выбрано: 50 звёзд для @alexdev.")
        self.assertIsNone(self.api.sent[-1]["buttons"])

    def test_stars_for_friend_asks_for_a_nick(self):
        self.bot.handle(_callback(20, "alexdev", "k:stars"))
        self.bot.handle(_callback(20, "alexdev", "w:friend:stars"))
        self.assertEqual(self.api.sent[-1]["text"], "Напишите ник друга.")
        self.bot.handle(_message(20, "@Friend_Name", username="alexdev"))
        self.assertIn("@friend_name", self.api.sent[-1]["text"])
        self.bot.handle(_message(20, "100", username="alexdev"))
        self.assertEqual(self.api.sent[-1]["text"], "Выбрано: 100 звёзд для @friend_name.")

    def test_premium_repeats_the_same_steps(self):
        self.bot.handle(_callback(20, "alexdev", "k:premium"))
        screen = self.api.sent[-1]
        self.assertIn("3, 6 или 12 месяцев", screen["text"])
        self.assertEqual(
            [(button[0], button[1]) for button in screen["buttons"][0]],
            [("Себе", "w:self:premium"), ("Другу", "w:friend:premium")],
        )

        self.bot.handle(_callback(20, "alexdev", "w:friend:premium"))
        self.bot.handle(_message(20, "buddy", username="alexdev"))
        months = self.api.sent[-1]
        self.assertIn("@buddy", months["text"])
        self.assertEqual(
            [button[0] for button in months["buttons"][0]],
            ["3 месяца", "6 месяцев", "12 месяцев"],
        )
        self.bot.handle(_callback(20, "alexdev", "q:6"))
        self.assertEqual(self.api.sent[-1]["text"], "Выбрано: Premium на 6 мес. для @buddy.")

        self.bot.handle(_callback(20, "alexdev", "w:self:premium"))
        self.bot.handle(_callback(20, "alexdev", "q:12"))
        self.assertEqual(self.api.sent[-1]["text"], "Выбрано: Premium на 12 мес. для @alexdev.")

    def test_self_without_username_asks_to_type_one(self):
        self.bot.handle(_callback(20, None, "k:stars"))
        self.bot.handle(_callback(20, None, "w:self:stars"))
        self.assertIn("Напишите свой ник", self.api.sent[-1]["text"])
        self.bot.handle(_message(20, "ownnick", username=None))
        self.assertIn("@ownnick", self.api.sent[-1]["text"])

    def _order_is_not_created(self, *args, **kwargs):
        raise AssertionError("заказ создавать пока не нужно")


def _message(user_id, text, username="buyer"):
    user = {"id": user_id}
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
