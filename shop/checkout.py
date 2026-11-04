"""Crypto Bot checkout. The customer pays USDT; Fragment then sends the purchase.

The browser never sees CRYPTO_PAY_TOKEN or FRAGMENT_API_KEY. A paid invoice is
accepted only after Crypto Pay itself says it is paid and the signed order
inside it still matches the price table.
"""

import hashlib
import hmac
import json
import os
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qs

from shop.cryptopay import PRODUCTION_BASE as CRYPTO_PAY_BASE
from shop.cryptopay import CryptoPay
from shop.errors import ShopError
from shop.fragment import PRODUCTION_BASE as FRAGMENT_BASE
from shop.fragment import FragmentClient
from shop.tokens import fragment_api_token
from shop.username import normalize_username

_STAR_PRICES = {
    50: "0.94",
    100: "1.88",
    250: "4.69",
    500: "9.38",
    1000: "18.75",
    2500: "46.88",
}
_PREMIUM_PRICES = {3: "14.99", 6: "19.99", 12: "36.24"}
_STAR_MIN = 50
_STAR_MAX = 100_000
_RETRYABLE = {"insufficient_balance", "fragment_unavailable", "rate_limited", "idempotency_conflict", "missing_token"}
_PUBLIC_SHOP = "https://start-bogdan22807s-projects.vercel.app"


def star_price(amount: int) -> str:
    known = _STAR_PRICES.get(amount)
    if known:
        return known
    cents = (Decimal(amount) * Decimal("1.875")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{cents / Decimal(100):.2f}"


def purchases_blocked_reason() -> str | None:
    if os.environ.get("SHOP_MODE", "").strip().lower() == "dry-run":
        return "Оплата выключена: в настройках стоит SHOP_MODE=dry-run. Уберите её или поставьте live."
    if not os.environ.get("CRYPTO_PAY_TOKEN", "").strip():
        return "Оплата пока не готова: в настройках магазина нет токена Crypto Bot."
    if not fragment_api_token():
        return "Оплата пока не готова: в настройках магазина нет ключа Fragment."
    return None


def require_purchases() -> None:
    reason = purchases_blocked_reason()
    if reason:
        raise ShopError("purchases_off", reason, 503)


def parse_order(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ShopError("bad_request", "JSON должен быть объектом.", 400)
    kind = body.get("kind")
    recipient = normalize_username(body.get("recipient"))
    if kind == "stars":
        amount = _whole(body.get("amount"))
        if amount < _STAR_MIN or amount > _STAR_MAX:
            raise ShopError("bad_amount", "Количество звёзд от 50 до 100000.", 400)
        total = star_price(amount)
        return {
            "kind": "stars",
            "recipient": recipient,
            "amount": amount,
            "total": total,
            "label": f"{amount} звёзд",
        }
    if kind == "premium":
        months = _whole(body.get("months"))
        total = _PREMIUM_PRICES.get(months)
        if total is None:
            raise ShopError("bad_amount", "Premium продаётся на 3, 6 или 12 месяцев.", 400)
        return {
            "kind": "premium",
            "recipient": recipient,
            "months": months,
            "total": total,
            "label": f"Premium на {months} мес.",
        }
    raise ShopError("bad_product", "Выберите звёзды или Premium.", 400)


def create_invoice(body: dict, *, cryptopay: CryptoPay | None = None) -> dict:
    require_purchases()
    order = parse_order(body)
    token = os.environ["CRYPTO_PAY_TOKEN"].strip()
    client = cryptopay or CryptoPay(token, base_url=_crypto_base())
    fields = _signed_fields(order)
    payload = json.dumps(
        {"order": fields, "sig": _sign(fields, token)},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    description = f"{order['label']} для @{order['recipient']}"
    invoice = client.create_invoice(
        order["total"],
        description,
        payload,
        paid_btn_url=_shop_url(),
        hidden_message="Оплата получена. Вернитесь в магазин: покупка отправится на username.",
    )
    pay_url = invoice.get("mini_app_invoice_url") or invoice.get("bot_invoice_url") or invoice.get("web_app_invoice_url")
    if not isinstance(pay_url, str) or not pay_url.startswith("https://"):
        raise ShopError("cryptopay_rejected", "Crypto Bot не дал ссылку на оплату.", 502)
    return {
        "invoice_id": invoice["invoice_id"],
        "pay_url": pay_url,
        "status": invoice.get("status") or "active",
        "total": order["total"],
        "label": description,
        "kind": order["kind"],
        "recipient": order["recipient"],
        "amount": order.get("amount"),
        "months": order.get("months"),
    }


def invoice_status(invoice_id: object, *, cryptopay: CryptoPay | None = None, fragment: FragmentClient | None = None) -> dict:
    require_purchases()
    text = str(invoice_id).strip()
    if not text.isdigit() or len(text) > 20:
        raise ShopError("bad_request", "Неизвестный счёт.", 400)
    token = os.environ["CRYPTO_PAY_TOKEN"].strip()
    client = cryptopay or CryptoPay(token, base_url=_crypto_base())
    return settle(client.get_invoice(text), token, fragment=fragment)


def cryptobot_webhook(raw: bytes, signature: str, *, cryptopay: CryptoPay | None = None, fragment: FragmentClient | None = None) -> dict:
    require_purchases()
    token = os.environ["CRYPTO_PAY_TOKEN"].strip()
    if not signature or not verify_webhook(raw, signature, token):
        raise ShopError("unauthorized", "Подпись Crypto Bot не сходится.", 401)
    try:
        update = json.loads(raw.decode())
    except json.JSONDecodeError as exc:
        raise ShopError("bad_json", "Тело запроса должно быть JSON.", 400) from exc
    if not isinstance(update, dict) or update.get("update_type") != "invoice_paid":
        return {"ok": True}
    payload = update.get("payload")
    if not isinstance(payload, dict) or payload.get("invoice_id") is None:
        raise ShopError("bad_invoice", "В уведомлении нет счёта.", 400)
    client = cryptopay or CryptoPay(token, base_url=_crypto_base())
    fresh = client.get_invoice(str(payload["invoice_id"]))
    return settle(fresh, token, fragment=fragment)


def settle(invoice: dict, token: str, *, fragment: FragmentClient | None = None) -> dict:
    invoice_id = invoice.get("invoice_id")
    status = str(invoice.get("status") or "")
    if status != "paid":
        message = "Счёт истёк. Нажмите Crypto Bot ещё раз." if status == "expired" else "Жду оплату в Crypto Bot."
        return {
            "invoice_id": invoice_id,
            "status": status,
            "paid": False,
            "delivered": False,
            "retry": status == "active",
            "message": message,
        }
    order = _verified_order(invoice, token)
    if str(invoice.get("asset") or "") != "USDT" or _cents(invoice.get("amount")) != _cents(order["total"]):
        raise ShopError("amount_mismatch", "Сумма счёта не совпала с ценой. Покупка не отправлена.", 409)
    return _deliver(order, invoice_id, fragment)


def deliver_paid_invoices(*, cryptopay: CryptoPay | None = None, fragment: FragmentClient | None = None) -> dict:
    require_purchases()
    token = os.environ["CRYPTO_PAY_TOKEN"].strip()
    client = cryptopay or CryptoPay(token, base_url=_crypto_base())
    items = []
    for invoice in client.list_invoices(status="paid", count=100):
        if str(invoice.get("status") or "") != "paid":
            continue
        try:
            items.append(settle(invoice, token, fragment=fragment))
        except ShopError as exc:
            if exc.code in {"bad_invoice", "amount_mismatch"}:
                continue
            items.append(_progress(invoice.get("invoice_id"), exc.message, retry=exc.status >= 500))
    return {"ok": True, "items": items}


def verify_webhook(raw: bytes, signature: str, token: str) -> bool:
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, signature.strip())
    except (TypeError, ValueError):
        return False


def handle_request(method: str, path: str, query: str, headers, raw: bytes) -> tuple[int, dict]:
    path = path.rstrip("/") or "/"
    try:
        if method == "GET" and path == "/api/invoice":
            invoice_id = (parse_qs(query).get("id") or [""])[0]
            return 200, invoice_status(invoice_id)
        if method == "GET" and path == "/api/sync":
            return 200, deliver_paid_invoices()
        if method == "POST" and path == "/api/invoice":
            body = json.loads(raw.decode() or "{}")
            return 201, create_invoice(body)
        if method == "POST" and path == "/api/cryptobot":
            signature = ""
            if hasattr(headers, "get"):
                signature = headers.get("crypto-pay-api-signature") or ""
            return 200, cryptobot_webhook(raw, signature)
        return 404, {"error": "not_found", "message": "Неизвестный адрес."}
    except ShopError as exc:
        return exc.status, {"error": exc.code, "message": exc.message}
    except json.JSONDecodeError:
        return 400, {"error": "bad_json", "message": "Тело запроса должно быть JSON."}


def _deliver(order: dict, invoice_id: object, fragment: FragmentClient | None) -> dict:
    client = fragment or FragmentClient(fragment_api_token(), _fragment_base())
    try:
        if order["kind"] == "stars":
            recipient_hash = client.search_stars(order["recipient"], order["amount"])
        else:
            recipient_hash = client.search_premium(order["recipient"], order["months"])
    except ShopError as exc:
        return _delivery_error(order, invoice_id, exc)
    remote = None
    for wallet in _wallet_order():
        key = f"cryptopay-{invoice_id}" if wallet == _wallet() else f"cryptopay-{invoice_id}-{wallet}"
        try:
            if order["kind"] == "stars":
                remote = client.create_stars(
                    username=order["recipient"],
                    recipient_hash=recipient_hash,
                    quantity=order["amount"],
                    wallet_type=wallet,
                    idempotency_key=key,
                )
            else:
                remote = client.create_premium(
                    username=order["recipient"],
                    recipient_hash=recipient_hash,
                    months=order["months"],
                    wallet_type=wallet,
                    idempotency_key=key,
                )
            break
        except ShopError as exc:
            if exc.code == "insufficient_balance":
                remote = None
                continue
            return _delivery_error(order, invoice_id, exc)
    if remote is None:
        return _progress(invoice_id, "Оплата прошла. На балансе Fragment нет средств в TON и USDT.", retry=True)
    try:
        status = str(remote.get("status") or "")
        remote_id = remote.get("order_id")
        if remote_id and status not in {"completed", "failed"}:
            remote = client.get_order(str(remote_id))
            status = str(remote.get("status") or "")
    except ShopError as exc:
        return _delivery_error(order, invoice_id, exc)
    if status == "completed":
        if order["kind"] == "stars":
            message = f"Звёзды отправлены на @{order['recipient']}."
        else:
            message = f"Premium отправлен на @{order['recipient']}."
        return _progress(invoice_id, message, retry=False, delivered=True)
    if status == "failed":
        detail = remote.get("error") if isinstance(remote, dict) else None
        if not isinstance(detail, str) or not detail.strip():
            detail = "Fragment не смог отправить покупку."
        return _progress(invoice_id, "Оплата прошла. " + detail[:300], retry=False)
    return _progress(invoice_id, f"Оплата прошла. Покупка ещё отправляется на @{order['recipient']}.", retry=True)


def _delivery_error(order: dict, invoice_id: object, exc: ShopError) -> dict:
    if exc.code == "insufficient_balance":
        return _progress(invoice_id, f"Оплата прошла. На балансе Fragment не хватает {_wallet()}.", retry=True)
    if exc.code in _RETRYABLE:
        return _progress(invoice_id, "Оплата прошла. Покупка ещё отправляется.", retry=True)
    if exc.code == "recipient_not_found":
        return _progress(invoice_id, f"Оплата прошла, но получатель @{order['recipient']} не найден.", retry=False)
    return _progress(invoice_id, "Оплата прошла. " + exc.message, retry=False)


def _wallet_order() -> list[str]:
    first = _wallet()
    second = "TON" if first == "USDT" else "USDT"
    return [first, second]


def _progress(invoice_id: object, message: str, *, retry: bool, delivered: bool = False) -> dict:
    return {
        "invoice_id": invoice_id,
        "status": "paid",
        "paid": True,
        "delivered": delivered,
        "retry": retry,
        "message": message,
    }


def _verified_order(invoice: dict, token: str) -> dict:
    raw = invoice.get("payload")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ShopError("bad_invoice", "Счёт без данных заказа.", 502) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("order"), dict) or not isinstance(raw.get("sig"), str):
        raise ShopError("bad_invoice", "Счёт без данных заказа.", 502)
    fields = raw["order"]
    expected = _sign(fields, token)
    if not verify_signature(expected, raw["sig"]):
        raise ShopError("bad_invoice", "Подпись счёта не сходится. Покупка не отправлена.", 409)
    order = parse_order(fields)
    if _cents(fields.get("total")) != _cents(order["total"]):
        raise ShopError("bad_invoice", "Цена в счёте не сходится.", 409)
    return order


def _signed_fields(order: dict) -> dict:
    fields = {"kind": order["kind"], "recipient": order["recipient"], "total": order["total"]}
    if order["kind"] == "stars":
        fields["amount"] = order["amount"]
    else:
        fields["months"] = order["months"]
    return fields


def _sign(fields: dict, token: str) -> str:
    raw = json.dumps(_canonical(fields), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(token.encode(), raw, hashlib.sha256).hexdigest()


def _canonical(fields: dict) -> dict:
    if not isinstance(fields, dict):
        raise ShopError("bad_invoice", "Счёт без данных заказа.", 502)
    kind = fields.get("kind")
    data = {"kind": kind, "recipient": fields.get("recipient"), "total": str(fields.get("total"))}
    if kind == "stars":
        data["amount"] = _whole(fields.get("amount"))
    elif kind == "premium":
        data["months"] = _whole(fields.get("months"))
    else:
        raise ShopError("bad_product", "Выберите звёзды или Premium.", 400)
    return data


def verify_signature(expected: str, actual: str) -> bool:
    try:
        return hmac.compare_digest(expected, actual)
    except (TypeError, ValueError):
        return False


def _whole(value: object) -> int:
    if isinstance(value, bool):
        raise ShopError("bad_amount", "Нужно целое число.", 400)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ShopError("bad_amount", "Нужно целое число.", 400)


def _cents(value: object) -> Decimal:
    try:
        amount = Decimal(str(value).strip())
    except Exception as exc:
        raise ShopError("amount_mismatch", "Сумма счёта не читается.", 409) from exc
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _wallet() -> str:
    value = os.environ.get("SHOP_WALLET", "USDT").strip().upper()
    return value if value in {"TON", "USDT"} else "USDT"


def _shop_url() -> str:
    value = os.environ.get("WEBAPP_URL", _PUBLIC_SHOP).strip()
    if value.startswith("https://") or value.startswith("http://"):
        return value
    return _PUBLIC_SHOP


def _crypto_base() -> str:
    return os.environ.get("CRYPTO_PAY_BASE", CRYPTO_PAY_BASE).strip() or CRYPTO_PAY_BASE


def _fragment_base() -> str:
    return os.environ.get("FRAGMENT_API_BASE", FRAGMENT_BASE).strip() or FRAGMENT_BASE
