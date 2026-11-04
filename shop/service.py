"""Shop actions used by the HTTP server and by tests."""

import json

from shop.catalog import get_product, list_products
from shop.errors import ShopError
from shop.fragment import FragmentClient
from shop.fulfillment import Delivery, deliver
from shop.orders import Order, OrderStore
from shop.pricing import PriceBook, format_percent
from shop.username import normalize_username


class Shop:
    def __init__(
        self,
        store: OrderStore,
        prices: PriceBook,
        fragment: FragmentClient,
        mode: str = "dry-run",
        admin_token: str = "",
    ):
        self.store = store
        self.prices = prices
        self.fragment = fragment
        self.mode = mode
        self.admin_token = admin_token

    def catalog(self) -> dict:
        markup = format_percent(self.prices.markup_percent)
        products = []
        for product in list_products():
            item = product.to_dict()
            quote = self.prices.quote(product)
            if quote is None:
                item.update(
                    priced=False,
                    currency=self.prices.currency,
                    markup_percent=markup,
                    fragment_cost=None,
                    customer_price=None,
                )
            else:
                item.update(
                    priced=True,
                    currency=quote.currency,
                    markup_percent=quote.markup_text,
                    fragment_cost=quote.cost_text,
                    customer_price=quote.customer_text,
                )
            products.append(item)
        return {"currency": self.prices.currency, "markup_percent": markup, "products": products}

    def create_order(self, product_id: object, recipient_username: object) -> Order:
        if isinstance(recipient_username, int) and not isinstance(recipient_username, bool):
            raise ShopError(
                "bad_username",
                "Fragment покупает на username. Укажите recipient_username, не числовой user id.",
                400,
            )
        username = normalize_username(recipient_username)
        if not isinstance(product_id, str) or not product_id:
            raise ShopError("bad_request", "Укажите product_id.", 400)
        product = get_product(product_id)
        if product is None:
            raise ShopError("unknown_product", "Такого товара нет в каталоге.", 404)
        quote = self.prices.quote(product)
        if quote is None:
            raise ShopError(
                "price_unavailable",
                "Нет себестоимости Fragment для этого товара. "
                "Для звёзд задайте SHOP_STAR_UNIT_COST. Для Premium задайте SHOP_PREMIUM_COST_* или API_TOKEN.",
                409,
            )
        return self.store.create(
            product_id=product.id,
            recipient_username=username,
            currency=quote.currency,
            markup_percent=quote.markup_text,
            fragment_cost=quote.cost_text,
            customer_amount=quote.customer_text,
        )

    def get_order(self, order_id: str) -> Order:
        order = self.store.get(order_id)
        if order is None:
            raise ShopError("not_found", "Заказ не найден.", 404)
        return order

    def confirm_payment(self, order_id: str, charge_id: object) -> Order:
        if not isinstance(charge_id, str) or not charge_id.strip() or len(charge_id) > 128:
            raise ShopError("bad_request", "charge_id обязателен и не длиннее 128 символов.", 400)
        charge_id = charge_id.strip()
        existing = self.get_order(order_id)
        if existing.status != "pending_payment":
            if existing.charge_id == charge_id:
                return existing
            raise ShopError("already_paid", "Заказ уже оплачен другим платежом.", 409)
        updated = self.store.mark_paid(order_id, charge_id)
        if updated is None or updated.status != "paid":
            raise ShopError("pay_failed", "Не удалось подтвердить оплату.", 409)
        return updated

    def fulfill(self, order_id: str) -> tuple[Order, Delivery]:
        return deliver(self.store, self.fragment, order_id, mode=self.mode)

    def wallet(self) -> dict:
        return self.fragment.wallet()

    @staticmethod
    def public_order(order: Order, delivery: Delivery | None = None) -> dict:
        body = order.to_dict()
        if delivery is not None:
            body["delivery"] = delivery.to_dict()
        elif order.delivery_json:
            body["delivery"] = json.loads(order.delivery_json)
        else:
            body["delivery"] = None
        body.pop("delivery_json", None)
        return body
