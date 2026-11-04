import tempfile
import unittest
from pathlib import Path

from shop.bot import ShopBot
from shop.orders import OrderStore
from test_shop import make_shop


class MemoryApi:
    def __init__(self):
        self.sent = []
        self.answered = []

    def send_message(self, chat_id, text, buttons=None):
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})

    def answer_callback(self, callback_id):
        self.answered.append(callback_id)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shop = make_shop(OrderStore(Path(self.tmp.name) / "orders.db"))
        self.api = MemoryApi()
        self.bot = ShopBot(self.shop, self.api, {7})

    def tearDown(self):
        self.shop.store.close()
        self.tmp.cleanup()

    def test_start_lists_priced_products_and_user_id(self):
        self.bot.handle(_message(10, "/start"))
        sent = self.api.sent[-1]
        self.assertEqual(sent["chat_id"], 10)
        self.assertIn("Ваш Telegram id: 10", sent["text"])
        labels = [row[0][0] for row in sent["buttons"]]
        self.assertIn("100 звёзд — 1.25 USDT", labels)
        self.assertIn("Premium 3 мес. — 12.50 USDT", labels)

    def test_buyer_creates_order_and_admin_fulfills_it(self):
        self.bot.handle(_callback(20, "alexdev", "b:stars_100"))
        self.assertEqual(self.api.answered, ["cb-1"])
        self.bot.handle(_message(20, "@Friend_Name", username="alexdev"))
        buyer = self.api.sent[-2]
        admin = self.api.sent[-1]
        self.assertIn("К оплате 1.25 USDT", buyer["text"])
        self.assertIn("для @friend_name", buyer["text"])
        self.assertEqual(admin["chat_id"], 7)
        order_id = buyer["text"].splitlines()[0].removeprefix("Заказ ")

        self.bot.handle(_message(20, f"/paid {order_id}"))
        self.assertIn("только администратор", self.api.sent[-1]["text"])
        self.assertEqual(self.shop.get_order(order_id).status, "pending_payment")

        self.bot.handle(_message(7, f"/paid {order_id}", username="owner"))
        self.assertIn("выдан @friend_name", self.api.sent[-2]["text"])
        self.assertEqual(self.api.sent[-1]["chat_id"], 20)
        self.assertEqual(self.shop.get_order(order_id).status, "fulfilled")

    def test_self_button_uses_telegram_username(self):
        self.bot.handle(_callback(20, "alexdev", "m:premium_3m"))
        buyer = next(item for item in self.api.sent if item["text"].startswith("Заказ"))
        self.assertIn("для @alexdev", buyer["text"])
        self.assertIn("К оплате 12.50 USDT", buyer["text"])
        order_id = buyer["text"].splitlines()[0].removeprefix("Заказ ")
        self.assertEqual(self.shop.store.get(order_id).buyer_chat_id, 20)


def _message(user_id, text, username="buyer"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "text": text,
            "from": {"id": user_id, "username": username},
            "chat": {"id": user_id, "type": "private"},
        },
    }


def _callback(user_id, username, data):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": user_id, "username": username},
            "data": data,
            "message": {"message_id": 3, "chat": {"id": user_id, "type": "private"}},
        },
    }


if __name__ == "__main__":
    unittest.main()
