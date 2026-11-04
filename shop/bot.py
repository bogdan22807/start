"""Telegram shop bot. Buyers pick a product; an admin confirms payment before Fragment is called."""

import os
import time

from shop.catalog import get_product
from shop.config import load_shop
from shop.errors import ShopError
from shop.orders import OrderStore
from shop.service import Shop
from shop.telegram_api import TelegramApi


class ShopBot:
    def __init__(self, shop: Shop, api, admin_ids: set[int]):
        self.shop = shop
        self.api = api
        self.admin_ids = admin_ids
        self.pending: dict[int, str] = {}

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            self._on_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        text = message.get("text") or ""
        chat = message.get("chat") or {}
        if not text or "id" not in chat:
            return
        chat_id = int(chat["id"])
        user = message.get("from") or {}
        user_id = int(user["id"]) if "id" in user else chat_id
        command, rest = _command(text)
        if command in {"/start", "/catalog"}:
            self.pending.pop(chat_id, None)
            self._show_catalog(chat_id, user_id)
            return
        if command == "/id":
            self.api.send_message(chat_id, f"Ваш Telegram id: {user_id}")
            return
        if command == "/cancel":
            self.pending.pop(chat_id, None)
            self.api.send_message(chat_id, "Выбор отменён. Нажмите /catalog, чтобы открыть товары.")
            return
        if command == "/paid":
            self._confirm(chat_id, user_id, rest)
            return
        if chat_id in self.pending and not command:
            self._create_for_username(chat_id, self.pending[chat_id], text)
            return
        self._show_catalog(chat_id, user_id)

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

    def _on_callback(self, query: dict) -> None:
        callback_id = query.get("id")
        if isinstance(callback_id, str):
            self.api.answer_callback(callback_id)
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        if "id" not in chat:
            return
        chat_id = int(chat["id"])
        user = query.get("from") or {}
        data = query.get("data") or ""
        if data.startswith("b:"):
            self._ask_recipient(chat_id, data[2:], user.get("username"))
            return
        if data.startswith("m:"):
            username = user.get("username")
            if not isinstance(username, str) or not username:
                self.pending[chat_id] = data[2:]
                self.api.send_message(chat_id, "У этого аккаунта нет username. Напишите username получателя.")
                return
            self._create_for_username(chat_id, data[2:], username)

    def _show_catalog(self, chat_id: int, user_id: int) -> None:
        catalog = self.shop.catalog()
        buttons = []
        for item in catalog["products"]:
            if not item["priced"]:
                continue
            label = f"{item['title']} — {item['customer_price']} {item['currency']}"
            buttons.append([(label, f"b:{item['id']}")])
        lines = [
            "Магазин звёзд и Premium.",
            "Выберите товар. Сумма придёт следующим сообщением.",
            "Выдача начнётся после подтверждения оплаты администратором.",
            f"Ваш Telegram id: {user_id}",
        ]
        if not buttons:
            lines.append("Цены ещё не заданы. Нужны SHOP_STAR_UNIT_COST и цены Premium.")
        self.api.send_message(chat_id, "\n".join(lines), buttons or None)

    def _ask_recipient(self, chat_id: int, product_id: str, own_username: object) -> None:
        product = get_product(product_id)
        if product is None:
            self.api.send_message(chat_id, "Такого товара нет. Откройте /catalog.")
            return
        self.pending[chat_id] = product_id
        buttons = None
        if isinstance(own_username, str) and own_username:
            buttons = [[(f"Себе (@{own_username})", f"m:{product_id}")]]
        self.api.send_message(
            chat_id,
            f"Кому отправить «{product.title}»?\nНапишите username получателя или нажмите кнопку ниже.",
            buttons,
        )

    def _create_for_username(self, chat_id: int, product_id: str, username: str) -> None:
        try:
            order = self.shop.create_order(product_id, username, buyer_chat_id=chat_id)
        except ShopError as exc:
            self.api.send_message(chat_id, exc.message)
            return
        self.pending.pop(chat_id, None)
        product = get_product(order.product_id)
        title = product.title if product else order.product_id
        text = (
            f"Заказ {order.id}\n"
            f"{title} для @{order.recipient_username}\n"
            f"К оплате {order.customer_amount} {order.currency}.\n"
            "После оплаты администратор подтвердит выдачу."
        )
        self.api.send_message(chat_id, text)
        notice = f"Новый заказ {order.id}\n{title} для @{order.recipient_username}\n{order.customer_amount} {order.currency}\n/paid {order.id}"
        for admin_id in sorted(self.admin_ids):
            if admin_id == chat_id:
                continue
            self.api.send_message(admin_id, notice)

    def _confirm(self, chat_id: int, user_id: int, rest: str) -> None:
        if user_id not in self.admin_ids:
            self.api.send_message(chat_id, "Подтверждать оплату может только администратор.")
            return
        parts = rest.split()
        if not parts:
            self.api.send_message(chat_id, "Формат: /paid id_заказа")
            return
        order_id = parts[0]
        charge_id = parts[1] if len(parts) > 1 else f"tg-{order_id}"
        try:
            order = self.shop.get_order(order_id)
            if order.status == "pending_payment":
                order = self.shop.confirm_payment(order_id, charge_id)
            if order.status == "failed":
                raise ShopError("fragment_failed", order.error_message or "Покупка на Fragment не выполнена.", 502)
            if order.status != "fulfilled":
                order, _delivery = self.shop.fulfill(order_id)
        except ShopError as exc:
            self.api.send_message(chat_id, exc.message)
            return
        if order.status == "fulfilled":
            result = f"Заказ {order.id} выдан @{order.recipient_username}."
        else:
            result = f"Заказ {order.id} принят Fragment, статус {order.status}. Повторите /paid {order.id}"
        self.api.send_message(chat_id, result)
        if order.buyer_chat_id and order.buyer_chat_id != chat_id:
            self.api.send_message(order.buyer_chat_id, result)


def main() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Для бота в Telegram нужен BOT_TOKEN от @BotFather.")
    admin_ids = set()
    for part in os.environ.get("TELEGRAM_ADMIN_IDS", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            admin_ids.add(int(part))
        except ValueError as exc:
            raise SystemExit("TELEGRAM_ADMIN_IDS должен быть списком числовых id через запятую.") from exc
    if not admin_ids:
        print("TELEGRAM_ADMIN_IDS пуст. Напишите боту /id и подставьте этот номер, иначе оплату некому подтвердить.")
    shop = load_shop(OrderStore(os.environ.get("SHOP_DB", "data/orders.db")))
    bot = ShopBot(shop, TelegramApi(token), admin_ids)
    print("telegram bot polling")
    try:
        bot.run()
    finally:
        shop.store.close()


def _command(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", stripped
    head, _, rest = stripped.partition(" ")
    name = head.split("@", 1)[0].lower()
    return name, rest.strip()
