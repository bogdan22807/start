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

    def send_message(self, chat_id, text, buttons=None, keyboard=None, placeholder=None):
        self.sent.append(
            {"chat_id": chat_id, "text": text, "buttons": buttons, "keyboard": keyboard, "placeholder": placeholder}
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
        self.assertEqual(
            sent["keyboard"],
            [
                ["⭐ Купить Звёзды"],
                ["☆ Купить Premium", "🧸 Купить Подарки"],
                ["👥 Рефералы", "💎 Купить TON"],
            ],
        )
        self.assertIsNone(sent["buttons"])

    def test_stars_for_self_then_quantity_stops(self):
        self.bot.handle(_message(20, "⭐ Купить Звёзды", username="alexdev"))
        screen = self.api.sent[-1]
        self.assertEqual(
            screen["text"],
            "🚀 Введите количество звёзд для покупки (от 50 ⭐ до 1000000 ⭐)\n\n👤 Получатель: @alexdev",
        )
        self.assertEqual(screen["keyboard"], [["🎁 Подарить звёзды"]])

        self.bot.handle(_message(20, "40", username="alexdev"))
        self.assertIn("от 50 ⭐ до 1000000 ⭐", self.api.sent[-1]["text"])

        self.bot.handle(_message(20, "50", username="alexdev"))
        done = self.api.sent[-1]
        self.assertEqual(
            done["text"],
            "⭐ Оплатите указ Сегодня эличество\nзвёзд\n\n☆ Количество: 50 ⭐\n👤 Получатель: @alexdev",
        )
        self.assertIsNone(done["buttons"])
        self._assert_payment_is_left_out(done["text"])

    def test_stars_for_friend_asks_for_a_nick(self):
        self.bot.handle(_message(20, "⭐ Купить Звёзды", username="alexdev"))
        self.bot.handle(_message(20, "🎁 Подарить звёзды", username="alexdev"))
        self.assertEqual(self.api.sent[-1]["text"], "Напишите ник друга.")
        self.bot.handle(_message(20, "@Friend_Name", username="alexdev"))
        self.assertIn("👤 Получатель: @friend_name", self.api.sent[-1]["text"])
        self.bot.handle(_message(20, "100", username="alexdev"))
        done = self.api.sent[-1]
        self.assertIn("☆ Количество: 100 ⭐", done["text"])
        self.assertIn("👤 Получатель: @friend_name", done["text"])
        self._assert_payment_is_left_out(done["text"])

    def test_premium_repeats_the_same_steps(self):
        self.bot.handle(_message(20, "☆ Купить Premium", username="alexdev"))
        screen = self.api.sent[-1]
        self.assertIn("3, 6 или 12 месяцев", screen["text"])
        self.assertIn("👤 Получатель: @alexdev", screen["text"])
        self.assertEqual(screen["keyboard"][0], ["3 месяца", "6 месяцев", "12 месяцев"])
        self.assertEqual(screen["keyboard"][1], ["🎁 Подарить Premium"])

        self.bot.handle(_message(20, "🎁 Подарить Premium", username="alexdev"))
        self.bot.handle(_message(20, "buddy", username="alexdev"))
        self.assertIn("👤 Получатель: @buddy", self.api.sent[-1]["text"])
        self.bot.handle(_message(20, "6 месяцев", username="alexdev"))
        done = self.api.sent[-1]
        self.assertEqual(done["text"], "☆ Срок: 6 мес.\n👤 Получатель: @buddy")
        self._assert_payment_is_left_out(done["text"])

        self.bot.handle(_message(20, "☆ Купить Premium", username="alexdev"))
        self.bot.handle(_message(20, "12", username="alexdev"))
        self.assertEqual(self.api.sent[-1]["text"], "☆ Срок: 12 мес.\n👤 Получатель: @alexdev")

    def test_self_without_username_asks_to_type_one(self):
        self.bot.handle(_message(20, "⭐ Купить Звёзды", username=None))
        self.assertIn("Напишите свой ник", self.api.sent[-1]["text"])
        self.bot.handle(_message(20, "ownnick", username=None))
        self.assertIn("👤 Получатель: @ownnick", self.api.sent[-1]["text"])

    def _assert_payment_is_left_out(self, text: str) -> None:
        lowered = text.lower()
        self.assertNotIn("соглашаетесь", lowered)
        self.assertNotIn("сбп", lowered)
        self.assertNotIn("вывод", lowered)
        self.assertNotIn("способ оплаты", lowered)

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
