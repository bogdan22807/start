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

    def send_message(
        self, chat_id, text, buttons=None, keyboard=None, placeholder=None, app_button=None, remove_keyboard=False
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
                ["🌟 Купить Premium"],
                ["🧸 Купить Подарки"],
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
        self.bot.handle(_message(20, "🌟 Купить Premium", username="alexdev"))
        screen = self.api.sent[-1]
        self.assertEqual(screen["text"], "🌟 Выберите кому отправить Premium:")
        self.assertEqual(
            [(button[0], button[1]) for row in screen["buttons"] for button in row],
            [("👤 На мой аккаунт", "p:self"), ("👥 На другой аккаунт", "p:other")],
        )

        self.bot.handle(_callback(20, "alexdev", "p:self"))
        terms = self.api.sent[-1]
        self.assertEqual(terms["text"], "🌟 Выберите срок подписки:")
        self.assertEqual(
            [row[0][0] for row in terms["buttons"]],
            ["3 месяца - 1390₽", "6 месяцев - 1990₽", "12 месяцев - 3190₽", "↩️ Назад"],
        )

        self.bot.handle(_callback(20, "alexdev", "p:back"))
        self.assertEqual(self.api.sent[-1]["text"], "🌟 Выберите кому отправить Premium:")

        self.bot.handle(_callback(20, "alexdev", "p:other"))
        ask = self.api.sent[-1]
        self.assertEqual(
            ask["text"],
            "Введите @юзернейм аккаунта, на который нужно отправить Premium\n\n"
            "Убедитесь что вводите юзернейм правильно.",
        )
        self.assertEqual(ask["buttons"], [[("↩️ Вернуться назад", "p:back")]])
        self.bot.handle(_message(20, "buddy", username="alexdev"))
        self.assertEqual(self.api.sent[-1]["text"], "🌟 Выберите срок подписки:")
        self.bot.handle(_callback(20, "alexdev", "p:6"))
        done = self.api.sent[-1]
        self.assertEqual(done["text"], "6 месяцев - 1990₽\n👤 Получатель: @buddy")
        self.assertIsNone(done["buttons"])
        self._assert_payment_is_left_out(done["text"])

    def test_self_without_username_asks_to_type_one(self):
        self.bot.handle(_message(20, "⭐ Купить Звёзды", username=None))
        self.assertIn("Напишите свой ник", self.api.sent[-1]["text"])
        self.bot.handle(_message(20, "ownnick", username=None))
        self.assertIn("👤 Получатель: @ownnick", self.api.sent[-1]["text"])

    def test_web_app_choice_is_not_an_order(self):
        self.bot.app_url = "https://example.com/app"
        self.bot.handle(_message(10, "/start"))
        self.assertIsNone(self.api.sent[-1]["app_button"])
        self.assertTrue(self.api.sent[-1]["remove_keyboard"])
        self.assertNotIn("Открыть магазин", self.api.sent[-1]["text"])
        self.bot.handle(
            {
                "message": {
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 10, "username": "alexdev"},
                    "web_app_data": {"data": '{"kind":"stars","recipient":"alexdev","amount":1000,"total":"18.75"}'},
                }
            }
        )
        done = self.api.sent[-1]
        self.assertEqual(done["text"], "1000 звёзд для @alexdev.\nК оплате 18.75 USDT.")
        self._assert_payment_is_left_out(done["text"])
        self.bot.handle(
            {
                "message": {
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 10},
                    "web_app_data": {"data": '{"kind":"premium","recipient":"buddy","months":12,"total":"1.00"}'},
                }
            }
        )
        self.assertEqual(self.api.sent[-1]["text"], "Premium на 12 мес. для @buddy.\nК оплате 36.24 USDT.")
        self.bot.handle(
            {
                "message": {
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 10},
                    "web_app_data": {
                        "data": '{"kind":"stars","recipient":"alexdev","amount":50,"pay":"heleket"}'
                    },
                }
            }
        )
        self.assertIn("Heleket", self.api.sent[-1]["text"])
        self.assertIn("0.94 USDT", self.api.sent[-1]["text"])

    def test_mini_app_keeps_the_chosen_colors(self):
        page = Path(__file__).parents[1].joinpath("shop/webapp/index.html").read_text(encoding="utf-8")
        self.assertIn("#2AABEE", page)
        self.assertIn("#FFCC00", page)
        self.assertIn("Банковская карта", page)
        self.assertIn("Crypto Bot", page)
        self.assertIn("Heleket", page)
        self.assertIn('id="theme"', page)

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
