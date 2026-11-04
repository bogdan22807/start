"""Buy a paid order on Fragment, or record the same purchase in dry-run."""

import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from shop.catalog import Product, get_product
from shop.errors import ShopError
from shop.fragment import FragmentClient
from shop.orders import Order, OrderStore
from shop.pricing import money


@dataclass(frozen=True)
class Delivery:
    provider: str
    mode: str
    kind: str
    username: str
    quantity: int | None
    months: int | None
    wallet_type: str
    fragment_order_id: str | None
    fragment_status: str | None
    quoted_cost: str
    fragment_amount: str | None
    customer_price: str
    currency: str

    def to_dict(self) -> dict:
        return asdict(self)


def deliver(store: OrderStore, fragment: FragmentClient, order_id: str, *, mode: str) -> tuple[Order, Delivery]:
    if mode not in {"dry-run", "live"}:
        raise ShopError("bad_mode", "SHOP_MODE должен быть dry-run или live.", 500)
    order = store.get(order_id)
    if order is None:
        raise ShopError("not_found", "Заказ не найден.", 404)
    if order.status == "fulfilled" and order.delivery_json:
        return order, Delivery(**json.loads(order.delivery_json))
    if order.status == "failed":
        raise ShopError("fragment_failed", order.error_message or "Покупка на Fragment не выполнена.", 502)
    if order.status not in {"paid", "delivering"}:
        raise ShopError("not_paid", "Сначала подтвердите оплату. Покупка до оплаты запрещена.", 409)

    product = get_product(order.product_id)
    if product is None:
        raise ShopError("unknown_product", "Товар заказа больше не существует.", 409)
    _check_product(product)

    if mode == "dry-run":
        delivery = _delivery(order, product, mode="dry-run", fragment_order_id=None, fragment_status=None, amount=order.fragment_cost)
        return _save(store, order.id, "fulfilled", delivery, error_code=None, error_message=None)

    if order.fragment_order_id:
        remote = fragment.get_order(order.fragment_order_id)
    elif product.kind == "stars":
        recipient_hash = fragment.search_stars(order.recipient_username, product.quantity or 0)
        remote = fragment.create_stars(
            username=order.recipient_username,
            recipient_hash=recipient_hash,
            quantity=product.quantity or 0,
            wallet_type=order.currency,
            idempotency_key=order.id,
        )
    else:
        recipient_hash = fragment.search_premium(order.recipient_username, product.months or 0)
        remote = fragment.create_premium(
            username=order.recipient_username,
            recipient_hash=recipient_hash,
            months=product.months or 0,
            wallet_type=order.currency,
            idempotency_key=order.id,
        )
    return _apply_remote(store, order, product, remote)


def _check_product(product: Product) -> None:
    if product.kind == "stars" and (product.quantity is None or product.quantity < 50):
        raise ShopError("bad_product", "Пакет звёзд должен быть от 50.", 500)
    if product.kind == "premium" and product.months not in {3, 6, 12}:
        raise ShopError("bad_product", "Срок Premium должен быть 3, 6 или 12 месяцев.", 500)


def _apply_remote(store: OrderStore, order: Order, product: Product, remote: dict) -> tuple[Order, Delivery]:
    status = remote.get("status")
    if status not in {"pending", "processing", "completed", "failed"}:
        raise ShopError("fragment_rejected", "Fragment вернул неизвестный статус заказа.", 502)
    fragment_id = remote.get("order_id")
    if not isinstance(fragment_id, str) or not fragment_id:
        raise ShopError("fragment_rejected", "Fragment не вернул order_id.", 502)
    try:
        amount = money(Decimal(str(remote.get("amount"))))
    except Exception as exc:
        raise ShopError("fragment_rejected", "Fragment не назвал сумму заказа.", 502) from exc

    delivery = _delivery(
        order,
        product,
        mode="live",
        fragment_order_id=fragment_id,
        fragment_status=status,
        amount=amount,
    )
    if status == "failed":
        message = remote.get("error") if isinstance(remote.get("error"), str) else "Fragment не выполнил покупку."
        _save(store, order.id, "failed", delivery, error_code="fragment_failed", error_message=message[:300])
        raise ShopError("fragment_failed", message[:300], 502)
    new_status = "fulfilled" if status == "completed" else "delivering"
    return _save(store, order.id, new_status, delivery, error_code=None, error_message=None)


def _delivery(
    order: Order,
    product: Product,
    *,
    mode: str,
    fragment_order_id: str | None,
    fragment_status: str | None,
    amount: str,
) -> Delivery:
    return Delivery(
        provider="fragment",
        mode=mode,
        kind=product.kind,
        username=order.recipient_username,
        quantity=product.quantity,
        months=product.months,
        wallet_type=order.currency,
        fragment_order_id=fragment_order_id,
        fragment_status=fragment_status,
        quoted_cost=order.fragment_cost,
        fragment_amount=amount,
        customer_price=order.customer_amount,
        currency=order.currency,
    )


def _save(
    store: OrderStore,
    order_id: str,
    status: str,
    delivery: Delivery,
    *,
    error_code: str | None,
    error_message: str | None,
) -> tuple[Order, Delivery]:
    saved = store.transition(
        order_id,
        ("paid", "delivering"),
        status=status,
        fragment_order_id=delivery.fragment_order_id,
        fragment_amount=delivery.fragment_amount,
        delivery_json=json.dumps(delivery.to_dict(), ensure_ascii=False),
        error_code=error_code,
        error_message=error_message,
    )
    if saved is None:
        raise ShopError("not_found", "Заказ не найден.", 404)
    order, changed = saved
    if not changed and order.status == "fulfilled" and order.delivery_json:
        return order, Delivery(**json.loads(order.delivery_json))
    if order.status != status:
        raise ShopError("fulfill_failed", "Не удалось сохранить выдачу.", 500)
    return order, delivery
