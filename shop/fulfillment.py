"""Turn a paid order into a Telegram delivery, or refuse one the API cannot do."""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from shop.catalog import Product, get_product
from shop.orders import Order, OrderStore


class ShopError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Delivery:
    method: str
    mode: str
    params: dict
    telegram_ok: bool | None = None

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "mode": self.mode,
            "params": self.params,
            "telegram_ok": self.telegram_ok,
        }


def shop_mode() -> str:
    mode = os.environ.get("SHOP_MODE", "dry-run").strip().lower()
    if mode not in {"dry-run", "live"}:
        raise ShopError("bad_mode", "SHOP_MODE must be dry-run or live.", 500)
    return mode


def deliver(store: OrderStore, order_id: str, *, mode: str | None = None, poster=None) -> tuple[Order, Delivery]:
    order = store.get(order_id)
    if order is None:
        raise ShopError("not_found", "Заказ не найден.", 404)
    if order.status == "fulfilled" and order.delivery_json:
        payload = json.loads(order.delivery_json)
        return order, Delivery(
            method=payload["method"],
            mode=payload["mode"],
            params=payload["params"],
            telegram_ok=payload.get("telegram_ok"),
        )
    if order.status != "paid":
        raise ShopError("not_paid", "Сначала подтвердите оплату. Выдача до оплаты запрещена.", 409)

    product = get_product(order.product_id)
    if product is None:
        raise ShopError("unknown_product", "Товар заказа больше не существует.", 409)
    if product.kind != "premium":
        store.mark_rejected(order_id, "stars_not_deliverable")
        raise ShopError(
            "stars_not_deliverable",
            "Bot API не зачисляет Telegram Stars на баланс пользователя. "
            "sendGift тоже не подходит: получатель не может обменять такой подарок на звёзды. "
            "Автоматически продавать пакеты звёзд через бота нельзя.",
            409,
        )

    mode = mode or shop_mode()
    delivery = _deliver_premium(order, product, mode=mode, poster=poster)
    saved = store.mark_fulfilled(order_id, json.dumps(delivery.to_dict(), ensure_ascii=False))
    if saved is None or saved.status != "fulfilled":
        raise ShopError("fulfill_failed", "Не удалось сохранить выдачу.", 500)
    return saved, delivery


def _deliver_premium(order: Order, product: Product, *, mode: str, poster) -> Delivery:
    if product.month_count not in (3, 6, 12):
        raise ShopError("bad_plan", "Срок Premium должен быть 3, 6 или 12 месяцев.", 500)
    expected = {3: 1000, 6: 1500, 12: 2500}[product.month_count]
    if product.star_count != expected:
        raise ShopError("bad_plan", "Стоимость Premium не совпадает с контрактом Bot API.", 500)

    params = {
        "user_id": order.recipient_user_id,
        "month_count": product.month_count,
        "star_count": product.star_count,
        "text": f"Premium на {product.month_count} мес.",
    }
    if mode == "dry-run":
        return Delivery(method="giftPremiumSubscription", mode="dry-run", params=params, telegram_ok=None)
    if mode != "live":
        raise ShopError("bad_mode", "SHOP_MODE must be dry-run or live.", 500)

    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise ShopError("missing_token", "Для живой выдачи нужен BOT_TOKEN.", 503)
    send = poster or _post_telegram
    try:
        body = send(token, "giftPremiumSubscription", params)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        description = parsed.get("description") or raw or str(exc.reason)
        raise ShopError("telegram_rejected", description, 502) from exc
    except urllib.error.URLError as exc:
        raise ShopError("telegram_unavailable", f"Telegram не ответил: {exc.reason}", 502) from exc
    if not body.get("ok"):
        description = body.get("description") or "Telegram отклонил giftPremiumSubscription."
        raise ShopError("telegram_rejected", description, 502)
    return Delivery(method="giftPremiumSubscription", mode="live", params=params, telegram_ok=True)


def _post_telegram(token: str, method: str, params: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode())
