"""Telegram shop bot. The menu matches the reference screen and stops before payment."""

import json
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from shop.config import load_shop
from shop.errors import ShopError
from shop.orders import OrderStore
from shop.service import Shop
from shop.telegram_api import TelegramApi
from shop.tokens import telegram_bot_token
from shop.username import normalize_username

_STAR_MIN = 50
_STAR_MAX = 1_000_000

BUY_STARS = "⭐ Купить Звёзды"
BUY_PREMIUM = "🌟 Купить Premium"
BUY_GIFTS = "🧸 Купить Подарки"
REFERRALS = "👥 Рефералы"
BUY_TON = "💎 Купить TON"
GIFT_STARS = "🎁 Подарить звёзды"

_MENU = [
    [BUY_STARS],
    [BUY_PREMIUM],
    [BUY_GIFTS],
    [REFERRALS, BUY_TON],
]
_PREMIUM_PLANS = (
    (3, "3 месяца - 1390₽"),
    (6, "6 месяцев - 1990₽"),
    (12, "12 месяцев - 3190₽"),
)
_PREMIUM_PRICE = dict(_PREMIUM_PLANS)


@dataclass
class Draft:
    kind: str
    step: str
    recipient: str | None = None


_DEFAULT_APP = "https://cdn.jsdelivr.net/gh/bogdan22807/start@main/shop/webapp/index.html"
_STAR_PACKS = {50: "0.94", 100: "1.88", 250: "4.69", 500: "9.38", 1000: "18.75", 2500: "46.88"}
_APP_PREMIUM = {3: "14.99", 6: "19.99", 12: "36.24"}


class ShopBot:
    def __init__(self, shop: Shop, api, admin_ids: set[int], app_url: str = ""):
        self.shop = shop
        self.api = api
        self.admin_ids = admin_ids
        self.app_url = app_url
        self.drafts: dict[int, Draft] = {}

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            self._on_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if "id" not in chat:
            return
        chat_id = int(chat["id"])
        web_app = message.get("web_app_data") or {}
        if isinstance(web_app.get("data"), str):
            self._on_app(chat_id, web_app["data"])
            return
        text = message.get("text") or ""
        if not text:
            return
        user = message.get("from") or {}
        user_id = int(user["id"]) if "id" in user else chat_id
        command, rest = _command(text)
        if command in {"/start", "/catalog"}:
            self.drafts.pop(chat_id, None)
            self._show_menu(chat_id)
            return
        if command == "/id":
            self.api.send_message(chat_id, f"Ваш Telegram id: {user_id}", keyboard=_MENU)
            return
        if command == "/cancel":
            self.drafts.pop(chat_id, None)
            self._show_menu(chat_id, "Выбор отменён.")
            return
        if command == "/paid":
            self._confirm(chat_id, user_id, rest)
            return
        if text == BUY_STARS:
            self._open_stars(chat_id, user.get("username"))
            return
        if text == BUY_PREMIUM:
            self._open_premium(chat_id)
            return
        if text in {BUY_GIFTS, REFERRALS, BUY_TON}:
            self.drafts.pop(chat_id, None)
            self._show_menu(chat_id, "Этот раздел пока не открыт.")
            return
        if text == GIFT_STARS:
            self._ask_friend(chat_id, "stars")
            return
        draft = self.drafts.get(chat_id)
        if draft is not None and not command:
            self._on_text(chat_id, draft, text)
            return
        self._show_menu(chat_id)

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
        if data == "p:self":
            self._premium_self(chat_id, user.get("username"))
            return
        if data == "p:other":
            self._premium_other(chat_id)
            return
        if data == "p:back":
            self._open_premium(chat_id)
            return
        if data.startswith("p:"):
            self._choose_months(chat_id, data[2:])

    def _show_menu(self, chat_id: int, text: str = "Выберите действие:") -> None:
        if self.app_url:
            shown = "Магазин звёзд и Premium." if text == "Выберите действие:" else text
            self.api.send_message(chat_id, shown, app_button=("Открыть магазин", self.app_url))
            return
        self.api.send_message(chat_id, text, keyboard=_MENU)

    def _on_app(self, chat_id: int, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        text = _app_summary(payload)
        if self.app_url:
            self.api.send_message(chat_id, text, app_button=("Открыть магазин", self.app_url))
            return
        self.api.send_message(chat_id, text, keyboard=_MENU)

    def _open_stars(self, chat_id: int, own_username: object) -> None:
        self._open_for_self(chat_id, "stars", own_username)

    def _open_premium(self, chat_id: int) -> None:
        self.drafts[chat_id] = Draft(kind="premium", step="whom")
        self.api.send_message(
            chat_id,
            "🌟 Выберите кому отправить Premium:",
            [[("👤 На мой аккаунт", "p:self")], [("👥 На другой аккаунт", "p:other")]],
        )

    def _premium_self(self, chat_id: int, own_username: object) -> None:
        username = own_username if isinstance(own_username, str) else ""
        try:
            recipient = normalize_username(username)
        except ShopError:
            self.drafts[chat_id] = Draft(kind="premium", step="nick")
            self.api.send_message(
                chat_id,
                "У аккаунта нет username. Напишите свой ник.",
                [[("↩️ Вернуться назад", "p:back")]],
            )
            return
        self._show_premium_terms(chat_id, recipient)

    def _premium_other(self, chat_id: int) -> None:
        self.drafts[chat_id] = Draft(kind="premium", step="gift")
        self.api.send_message(
            chat_id,
            "Введите @юзернейм аккаунта, на который нужно отправить Premium\n\n"
            "Убедитесь что вводите юзернейм правильно.",
            [[("↩️ Вернуться назад", "p:back")]],
        )

    def _show_premium_terms(self, chat_id: int, recipient: str) -> None:
        self.drafts[chat_id] = Draft(kind="premium", step="quantity", recipient=recipient)
        rows = [[(label, f"p:{months}")] for months, label in _PREMIUM_PLANS]
        rows.append([("↩️ Назад", "p:back")])
        self.api.send_message(chat_id, "🌟 Выберите срок подписки:", rows)

    def _open_for_self(self, chat_id: int, kind: str, own_username: object) -> None:
        username = own_username if isinstance(own_username, str) else ""
        try:
            recipient = normalize_username(username)
        except ShopError:
            self.drafts[chat_id] = Draft(kind=kind, step="nick")
            self.api.send_message(chat_id, "У аккаунта нет username. Напишите свой ник.", keyboard=_MENU)
            return
        self._ask_amount(chat_id, kind, recipient)

    def _ask_friend(self, chat_id: int, kind: str) -> None:
        self.drafts[chat_id] = Draft(kind=kind, step="gift")
        self.api.send_message(chat_id, "Напишите ник друга.", keyboard=_gift_keyboard(kind))

    def _on_text(self, chat_id: int, draft: Draft, text: str) -> None:
        if draft.step in {"nick", "gift"}:
            try:
                recipient = normalize_username(text)
            except ShopError as exc:
                if draft.kind == "premium":
                    self.api.send_message(chat_id, exc.message, [[("↩️ Вернуться назад", "p:back")]])
                else:
                    keyboard = _gift_keyboard(draft.kind) if draft.step == "gift" else _MENU
                    self.api.send_message(chat_id, exc.message, keyboard=keyboard)
                return
            if draft.kind == "premium":
                self._show_premium_terms(chat_id, recipient)
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
            self.api.send_message(
                chat_id,
                f"🚀 Введите количество звёзд для покупки (от 50 ⭐ до 1000000 ⭐)\n\n👤 Получатель: @{recipient}",
                keyboard=_gift_keyboard(kind),
                placeholder="Введите количество",
            )
            return

    def _finish_stars(self, chat_id: int, draft: Draft, text: str) -> None:
        try:
            amount = int(text.strip().replace(" ", ""))
        except ValueError:
            amount = 0
        if amount < _STAR_MIN or amount > _STAR_MAX:
            self.api.send_message(
                chat_id,
                f"🚀 Введите количество звёзд для покупки (от 50 ⭐ до 1000000 ⭐)\n\n👤 Получатель: @{draft.recipient}",
                keyboard=_gift_keyboard("stars"),
                placeholder="Введите количество",
            )
            return
        self.drafts.pop(chat_id, None)
        self.api.send_message(
            chat_id,
            (
                "⭐ Оплатите указ Сегодня эличество\n"
                "звёзд\n\n"
                f"☆ Количество: {amount} ⭐\n"
                f"👤 Получатель: @{draft.recipient}"
            ),
            keyboard=_MENU,
        )

    def _choose_months(self, chat_id: int, raw: str) -> None:
        draft = self.drafts.get(chat_id)
        if draft is None or draft.step != "quantity" or draft.kind != "premium":
            self._show_menu(chat_id)
            return
        self._finish_premium(chat_id, draft, raw)

    def _finish_premium(self, chat_id: int, draft: Draft, text: str) -> None:
        months = _months(text)
        label = _PREMIUM_PRICE.get(months)
        if label is None:
            self._show_premium_terms(chat_id, draft.recipient or "")
            return
        self.drafts.pop(chat_id, None)
        self.api.send_message(
            chat_id,
            f"{label}\n👤 Получатель: @{draft.recipient}",
            keyboard=_MENU,
        )

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
    token = telegram_bot_token()
    if not token:
        raise SystemExit(
            "Бот не запущен, поэтому /start в Telegram никуда не доходит. "
            "Нужен токен BotFather в BOT_TOKEN или в API_TOKEN (вид 123456789:AA...)."
        )
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
    app_url = webapp_url()
    _start_app_server(shop)
    try:
        api.delete_webhook()
        api.set_commands()
        if app_url:
            api.set_menu_button(app_url)
    except ShopError as exc:
        print(f"telegram setup: {exc.message}")
    bot = ShopBot(shop, api, admin_ids, app_url)
    print("telegram bot polling")
    try:
        bot.run()
    finally:
        shop.store.close()


def webapp_url() -> str:
    value = os.environ.get("WEBAPP_URL", "").strip()
    return value or _DEFAULT_APP


def _start_app_server(shop: Shop) -> None:
    from shop.server import make_server

    host = os.environ.get("SHOP_HOST", "0.0.0.0")
    port = int(os.environ.get("SHOP_PORT", "8000"))
    try:
        server = make_server(host, port, shop)
    except OSError as exc:
        print(f"mini app server: {exc}")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"mini app http://127.0.0.1:{port}/app")


def _app_summary(payload: object) -> str:
    if not isinstance(payload, dict):
        return "Не получилось прочитать выбор."
    try:
        recipient = normalize_username(payload.get("recipient"))
    except ShopError as exc:
        return exc.message
    kind = payload.get("kind")
    if kind == "stars":
        try:
            amount = int(payload.get("amount"))
        except (TypeError, ValueError):
            amount = 0
        if amount < _STAR_MIN or amount > 100_000:
            return "Для звёзд нужно число от 50 до 100 000."
        return f"{amount} звёзд для @{recipient}.\nК оплате {_star_total(amount)} USDT."
    if kind == "premium":
        try:
            months = int(payload.get("months"))
        except (TypeError, ValueError):
            months = 0
        total = _APP_PREMIUM.get(months)
        if total is None:
            return "Для Premium доступны 3, 6 или 12 месяцев."
        return f"Premium на {months} мес. для @{recipient}.\nК оплате {total} USDT."
    return "Неизвестный выбор."


def _star_total(amount: int) -> str:
    known = _STAR_PACKS.get(amount)
    if known:
        return known
    cents = (Decimal(amount) * Decimal("1.875")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{Decimal(cents) / Decimal(100):.2f}"


def _gift_keyboard(kind: str) -> list[list[str]]:
    return [[GIFT_STARS]]


def _months(text: str) -> int:
    digits = "".join(char for char in text if char.isdigit())
    if not digits:
        return 0
    try:
        return int(digits)
    except ValueError:
        return 0


def _command(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", stripped
    head, _, rest = stripped.partition(" ")
    name = head.split("@", 1)[0].lower()
    return name, rest.strip()


if __name__ == "__main__":
    main()
