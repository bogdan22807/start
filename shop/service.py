"""Shop actions used by the HTTP server and by tests."""

import json

from shop.catalog import get_product, list_products
from shop.fulfillment import Delivery, ShopError, deliver
from shop.orders import Order, OrderStore


class Shop:
    def __init__(self, store: OrderStore):
        self.store = store

    def catalog(self) -> list[dict]:
        return [item.to_dict() for item in list_products()]

    def create_order(self, product_id: str, recipient_user_id: int) -> Order:
        if not isinstance(product_id, str) or not product_id:
            raise ShopError("bad_request", "Укажите product_id.", 400)
        if not isinstance(recipient_user_id, int) or isinstance(recipient_user_id, bool) or recipient_user_id <= 0:
            raise ShopError("bad_request", "recipient_user_id должен быть положительным числом Telegram.", 400)
        if get_product(product_id) is None:
            raise ShopError("unknown_product", "Такого товара нет в каталоге.", 404)
        return self.store.create(product_id, recipient_user_id)

    def get_order(self, order_id: str) -> Order:
        order = self.store.get(order_id)
        if order is None:
            raise ShopError("not_found", "Заказ не найден.", 404)
        return order

    def confirm_payment(self, order_id: str, charge_id: str) -> Order:
        if not isinstance(charge_id, str) or not charge_id.strip() or len(charge_id) > 128:
            raise ShopError("bad_request", "charge_id обязателен и не длиннее 128 символов.", 400)
        order = self.store.get(order_id)
        if order is None:
            raise ShopError("not_found", "Заказ не найден.", 404)
        if order.status in {"paid", "fulfilled"}:
            if order.charge_id == charge_id:
                return order
            raise ShopError("already_paid", "Заказ уже оплачен другим платежом.", 409)
        updated = self.store.mark_paid(order_id, charge_id.strip())
        if updated is None or updated.status != "paid":
            raise ShopError("pay_failed", "Не удалось подтвердить оплату.", 409)
        return updated

    def fulfill(self, order_id: str, *, mode: str | None = None, poster=None) -> tuple[Order, Delivery]:
        return deliver(self.store, order_id, mode=mode, poster=poster)

    @staticmethod
    def public_order(order: Order, delivery: Delivery | None = None) -> dict:
        body = order.to_dict()
        if order.delivery_json and delivery is None:
            body["delivery"] = json.loads(order.delivery_json)
        elif delivery is not None:
            body["delivery"] = delivery.to_dict()
        else:
            body["delivery"] = None
        body.pop("delivery_json", None)
        return body
