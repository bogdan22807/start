"""Telegram shop bot. The start screen asks who receives Stars or Premium, then stops."""

import os
import time
from dataclasses import dataclass

from shop.config import load_shop
from shop.errors import ShopError
from shop.orders import OrderStore
from shop.service import Shop
from shop.telegram_api import TelegramApi
from shop.username import normalize_username

_STAR_MIN = 50
_STAR_MAX = 1_000_000
_PREMIUM_MONTHS = (3, 6, 12)


@dataclass
class Draft:
    kind: str
    step: str
    recipient: str | None = None


class ShopBot:
    def __init__(self, shop: Shop, api, admin_ids: set[int]):
        self.shop = shop
        self.api = api
        self.admin_ids = admin_ids
        self.drafts: dict[int, Draft] = {}

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
            self.drafts.pop(chat_id, None)
            self._show_start(chat_id)
            return
        if command == "/id":
            self.api.send_message(chat_id, f"Ваш Telegram id: {user_id}")
            return
        if command == "/cancel":
            self.drafts.pop(chat_id, None)
            self.api.send_message(chat_id, "Выбор отменён. Нажмите /start, чтобы открыть меню.")
            return
        if command == "/paid":
            self._confirm(chat_id, user_id, rest)
            return
        draft = self.drafts.get(chat_id)
        if draft is not None and not command:
            self._on_text(chat_id, draft, text)
            return
        self._show_start(chat_id)

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
        if data == "k:stars":
            self._ask_who(chat_id, "stars")
            return
        if data == "k:premium":
            self._ask_who(chat_id, "premium")
            return
        if data.startswith("w:"):
            _prefix, who, kind = data.split(":", 2)
            self._choose_who(chat_id, kind, who, user.get("username"))
            return
        if data.startswith("q:"):
            self._choose_months(chat_id, data[2:])

    def _show_start(self, chat_id: int) -> None:
        self.api.send_message(
            chat_id,
            "Выберите, что купить.",
            [[("Купить звезды", "k:stars")], [("Телеграм премиум", "k:premium")]],
        )

    def _ask_who(self, chat_id: int, kind: str) -> None:
        self.drafts[chat_id] = Draft(kind=kind, step="who")
        if kind == "stars":
            text = "Минимальный заказ от 50 звёзд."
        else:
            text = "Telegram Premium на 3, 6 или 12 месяцев."
        self.api.send_message(
            chat_id,
            text,
            [[("Себе", f"w:self:{kind}"), ("Другу", f"w:friend:{kind}")]],
        )

    def _choose_who(self, chat_id: int, kind: str, who: str, own_username: object) -> None:
        if kind not in {"stars", "premium"} or who not in {"self", "friend"}:
            self._show_start(chat_id)
            return
        if who == "friend":
            self.drafts[chat_id] = Draft(kind=kind, step="nick")
            self.api.send_message(chat_id, "Напишите ник друга.")
            return
        username = own_username if isinstance(own_username, str) else ""
        try:
            recipient = normalize_username(username)
        except ShopError:
            self.drafts[chat_id] = Draft(kind=kind, step="nick")
            self.api.send_message(chat_id, "У аккаунта нет username. Напишите свой ник.")
            return
        self._ask_amount(chat_id, kind, recipient)

    def _on_text(self, chat_id: int, draft: Draft, text: str) -> None:
        if draft.step == "nick":
            try:
                recipient = normalize_username(text)
            except ShopError as exc:
                self.api.send_message(chat_id, exc.message)
                return
            self._ask_amount(chat_id, draft.kind, recipient)
            return
        if draft.step == "quantity" and draft.kind == "stars":
            self._finish_stars(chat_id, draft, text)
            return
        if draft.step == "quantity" and draft.kind == "premium":
            self._finish_premium(chat_id, draft, text)

    def _ask_amount(self, chat_id: int, kind: str, recipient: str) -> None:
        self.drafts[chat_id] = Draft(kind=kind, step="quantity", recipient=recipient)
        if kind == "stars":
            self.api.send_message(chat_id, f"Сколько звёзд отправить @{recipient}? Минимум 50.")
            return
        self.api.send_message(
            chat_id,
            f"Выберите срок Premium для @{recipient}.",
            [[("3 месяца", "q:3"), ("6 месяцев", "q:6"), ("12 месяцев", "q:12")]],
        )

    def _finish_stars(self, chat_id: int, draft: Draft, text: str) -> None:
        try:
            amount = int(text.strip())
        except ValueError:
            amount = 0
        if amount < _STAR_MIN or amount > _STAR_MAX:
            self.api.send_message(chat_id, "Минимальный заказ от 50 звёзд. Напишите число.")
            return
        self.drafts.pop(chat_id, None)
        self.api.send_message(chat_id, f"Выбрано: {amount} звёзд для @{draft.recipient}.")

    def _choose_months(self, chat_id: int, raw: str) -> None:
        draft = self.drafts.get(chat_id)
        if draft is None or draft.step != "quantity" or draft.kind != "premium":
            self._show_start(chat_id)
            return
        self._finish_premium(chat_id, draft, raw)

    def _finish_premium(self, chat_id: int, draft: Draft, text: str) -> None:
        try:
            months = int(text.strip())
        except ValueError:
            months = 0
        if months not in _PREMIUM_MONTHS:
            self.api.send_message(chat_id, "Для Premium доступны 3, 6 или 12 месяцев.")
            return
        self.drafts.pop(chat_id, None)
        self.api.send_message(chat_id, f"Выбрано: Premium на {months} мес. для @{draft.recipient}.")

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
    api = TelegramApi(token)
    try:
        api.delete_webhook()
        api.set_commands()
    except ShopError as exc:
        print(f"telegram setup: {exc.message}")
    bot = ShopBot(shop, api, admin_ids)
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


if __name__ == "__main__":
    main()
