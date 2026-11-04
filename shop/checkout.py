"""Customer checkout. Crypto Bot takes USDT, Monobank takes the card.

The browser never sees payment tokens or the Fragment key. A purchase is sent
only after the provider itself confirms the payment and the signed order still
matches the price the server calculated.
"""

import hashlib
import hmac
import json
import os
import re
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qs

from shop.cryptopay import PRODUCTION_BASE as CRYPTO_PAY_BASE
from shop.cryptopay import CryptoPay
from shop.errors import ShopError
from shop.fragment import PRODUCTION_BASE as FRAGMENT_BASE
from shop.fragment import FragmentClient
from shop.monobank import CARD_ID
from shop.monobank import Monobank
from shop.monobank import card_currency, monobank_token, nbu_usd_rate, verify_sign
from shop.receipt import send_receipt
from shop.tokens import fragment_api_token, telegram_bot_token
from shop.username import normalize_username
from shop.webapp import webapp_profile

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


def create_invoice(body: dict, *, cryptopay: CryptoPay | None = None, monobank: Monobank | None = None, rate: Decimal | None = None) -> dict:
    if not isinstance(body, dict):
        raise ShopError("bad_request", "JSON должен быть объектом.", 400)
    pay = body.get("pay")
    if pay in (None, "", "cryptobot"):
        return _create_crypto(body, cryptopay=cryptopay)
    if pay == "card":
        return _create_card(body, monobank=monobank, rate=rate)
    raise ShopError("bad_pay", "Этот способ оплаты ещё не подключён.", 400)


def _create_crypto(body: dict, *, cryptopay: CryptoPay | None = None) -> dict:
    require_purchases()
    order = _order_with_buyer(body)
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
    _note_invoice(invoice["invoice_id"], order, "cryptobot", paid=False)
    return _created(invoice["invoice_id"], pay_url, invoice.get("status") or "active", order, description)


def _create_card(body: dict, *, monobank: Monobank | None = None, rate: Decimal | None = None) -> dict:
    token = _require_card()
    order = _order_with_buyer(body)
    ccy, minor, charge = _card_amount(order["total"], rate)
    order["ccy"] = ccy
    order["minor"] = minor
    fields = _signed_fields(order)
    reference = json.dumps(
        {"order": fields, "sig": _sign(fields, token)},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if len(reference) > 500:
        raise ShopError("bad_invoice", "Счёт не собрался. Покупка не создана.", 500)
    description = f"{order['label']} для @{order['recipient']}"
    client = monobank or Monobank(token)
    invoice = client.create_invoice(minor, ccy, reference, description, _shop_url(), _shop_url().rstrip("/") + "/api/monobank")
    created = _created(invoice["invoiceId"], invoice["pageUrl"], "created", order, description)
    created["pay"] = "card"
    created["charge"] = charge
    _note_invoice(invoice["invoiceId"], order, "card", paid=False)
    return created


def invoice_status(
    invoice_id: object,
    *,
    pay: str = "cryptobot",
    cryptopay: CryptoPay | None = None,
    monobank: Monobank | None = None,
    fragment: FragmentClient | None = None,
    sender=None,
) -> dict:
    if pay == "card":
        return _card_status(invoice_id, monobank=monobank, fragment=fragment, sender=sender)
    if pay not in {"", "cryptobot"}:
        raise ShopError("bad_pay", "Этот способ оплаты ещё не подключён.", 400)
    require_purchases()
    text = str(invoice_id).strip()
    if not text.isdigit() or len(text) > 20:
        raise ShopError("bad_request", "Неизвестный счёт.", 400)
    token = os.environ["CRYPTO_PAY_TOKEN"].strip()
    client = cryptopay or CryptoPay(token, base_url=_crypto_base())
    return settle(client.get_invoice(text), token, fragment=fragment, sender=sender)


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


def monobank_webhook(
    raw: bytes,
    signature: str,
    *,
    monobank: Monobank | None = None,
    pubkey: bytes | None = None,
    fragment: FragmentClient | None = None,
    sender=None,
) -> dict:
    token = _require_card()
    client = monobank or Monobank(token)
    pem = pubkey if pubkey is not None else client.public_key()
    if isinstance(pem, str):
        pem = pem.encode()
    if not signature or not verify_sign(pem, signature, raw):
        raise ShopError("unauthorized", "Подпись Monobank не сходится.", 401)
    try:
        update = json.loads(raw.decode())
    except json.JSONDecodeError as exc:
        raise ShopError("bad_json", "Тело запроса должно быть JSON.", 400) from exc
    if not isinstance(update, dict):
        raise ShopError("bad_invoice", "В уведомлении нет счёта.", 400)
    invoice_id = update.get("invoiceId")
    if not isinstance(invoice_id, str) or not re.fullmatch(CARD_ID, invoice_id):
        raise ShopError("bad_invoice", "В уведомлении нет счёта.", 400)
    return settle_card(client.get_invoice(invoice_id), token, fragment=fragment, sender=sender)


def settle_card(invoice: dict, token: str, *, fragment: FragmentClient | None = None, sender=None) -> dict:
    invoice_id = invoice.get("invoiceId") or invoice.get("invoice_id")
    status = str(invoice.get("status") or "")
    if status != "success":
        return _card_waiting(invoice_id, status, invoice)
    order = _verified_order({"payload": invoice.get("reference")}, token)
    if "minor" not in order or "ccy" not in order:
        raise ShopError("bad_invoice", "Счёт без суммы карты.", 409)
    amount = _whole(invoice.get("amount"))
    ccy = _whole(invoice.get("ccy"))
    final = invoice.get("finalAmount")
    final_amount = amount if final in (None, "") else _whole(final)
    if amount != order["minor"] or ccy != order["ccy"] or final_amount != amount:
        raise ShopError("amount_mismatch", "Сумма счёта не совпала с ценой. Покупка не отправлена.", 409)
    _note_invoice(invoice_id, order, "card", paid=True)
    return _deliver(order, invoice_id, fragment, sender=sender, prefix="monobank")


def settle(invoice: dict, token: str, *, fragment: FragmentClient | None = None, sender=None) -> dict:
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
    _note_invoice(invoice_id, order, "cryptobot", paid=True)
    return _deliver(order, invoice_id, fragment, sender=sender)


def deliver_paid_invoices(*, cryptopay: CryptoPay | None = None, fragment: FragmentClient | None = None, sender=None) -> dict:
    require_purchases()
    token = os.environ["CRYPTO_PAY_TOKEN"].strip()
    client = cryptopay or CryptoPay(token, base_url=_crypto_base())
    items = []
    for invoice in client.list_invoices(status="paid", count=100):
        if str(invoice.get("status") or "") != "paid":
            continue
        try:
            items.append(settle(invoice, token, fragment=fragment, sender=sender))
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
            params = parse_qs(query)
            invoice_id = (params.get("id") or [""])[0]
            pay = (params.get("pay") or ["cryptobot"])[0]
            if pay not in {"", "cryptobot", "card"}:
                return 400, {"error": "bad_pay", "message": "Этот способ оплаты ещё не подключён."}
            return 200, invoice_status(invoice_id, pay=pay or "cryptobot")
        if method == "GET" and path == "/api/sync":
            return 200, deliver_paid_invoices()
        if method == "POST" and path == "/api/invoice":
            body = json.loads(raw.decode() or "{}")
            return 201, create_invoice(body)
        if method == "POST" and path == "/api/cryptobot":
            signature = _header(headers, "crypto-pay-api-signature")
            return 200, cryptobot_webhook(raw, signature)
        if method == "POST" and path == "/api/monobank":
            return 200, monobank_webhook(raw, _header(headers, "x-sign"))
        if method == "POST" and path == "/api/stat":
            body = json.loads(raw.decode() or "{}")
            return stat_response(body)
        return 404, {"error": "not_found", "message": "Неизвестный адрес."}
    except ShopError as exc:
        return exc.status, {"error": exc.code, "message": exc.message}
    except json.JSONDecodeError:
        return 400, {"error": "bad_json", "message": "Тело запроса должно быть JSON."}


def _header(headers, name: str) -> str:
    if not hasattr(headers, "get"):
        return ""
    titled = "-".join(part.capitalize() for part in name.split("-"))
    for candidate in (name, name.lower(), name.upper(), titled):
        value = headers.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _order_with_buyer(body: dict) -> dict:
    order = parse_order(body)
    init_data = body.get("init_data") if isinstance(body.get("init_data"), str) else ""
    profile = webapp_profile(init_data, telegram_bot_token())
    if profile:
        order["buyer_id"] = profile["id"]
        order["buyer_username"] = profile["username"]
    return order


def _note_invoice(invoice_id: object, order: dict, source: str, *, paid: bool) -> None:
    try:
        from shop.stats import remember_invoice, remember_user

        if order.get("buyer_id"):
            remember_user(order.get("buyer_id"), order.get("buyer_username") or "")
        remember_invoice(invoice_id, user_id=order.get("buyer_id"), amount=order.get("total"), source=source, paid=paid)
    except Exception:
        return


def stat_response(body: dict) -> tuple[int, dict]:
    from shop.stats import build_report, is_admin, load_state, unpack_snapshot

    if not isinstance(body, dict):
        raise ShopError("bad_request", "JSON должен быть объектом.", 400)
    token = telegram_bot_token()
    if not token:
        raise ShopError("missing_bot_token", "Для статистики нужен токен бота.", 503)
    init_data = body.get("init_data") if isinstance(body.get("init_data"), str) else ""
    profile = webapp_profile(init_data, token)
    if not is_admin(profile):
        raise ShopError("forbidden", "Эта панель только для администратора.", 403)
    snapshot = None
    packed = body.get("snapshot") if isinstance(body.get("snapshot"), str) else ""
    if packed:
        snapshot = unpack_snapshot(packed, token)
        if snapshot is None:
            raise ShopError("bad_snapshot", "Снимок статистики не сходится.", 400)
    from shop.stats import remember_user

    remember_user(profile["id"], profile.get("username") or "")
    users, orders = load_state()
    crypto: list[dict] = []
    official: dict = {}
    crypto_token = os.environ.get("CRYPTO_PAY_TOKEN", "").strip()
    if crypto_token:
        client = CryptoPay(crypto_token, base_url=_crypto_base())
        try:
            crypto = _crypto_history(client)
        except ShopError:
            crypto = []
        official = _official_stats(client)
    return 200, build_report(users, orders, crypto=crypto, snapshot=snapshot, official=official)


def _crypto_history(client: CryptoPay) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for status in (None, "paid"):
        offset = 0
        while len(found) < 2000:
            page = client.list_invoices(status=status, count=100, offset=offset)
            if not page:
                break
            fresh = []
            for item in page:
                marker = str(item.get("invoice_id"))
                if marker in seen:
                    continue
                seen.add(marker)
                fresh.append(item)
            if not fresh:
                break
            found.extend(fresh)
            if len(page) < 100:
                break
            offset += len(page)
    return found[:2000]


def _official_stats(client: CryptoPay) -> dict:
    from datetime import datetime, timezone

    from shop.stats import period_start

    now = datetime.now(timezone.utc)
    official: dict = {}
    for name in ("1", "3", "7", "30", "all"):
        start = period_start(name, now)
        try:
            stamp = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if start else None
            payload = client.get_stats(start_at=stamp)
        except ShopError:
            continue
        if isinstance(payload, dict):
            official[name] = payload
    return official


def _created(invoice_id: object, pay_url: str, status: str, order: dict, description: str) -> dict:
    return {
        "invoice_id": invoice_id,
        "pay_url": pay_url,
        "status": status,
        "total": order["total"],
        "label": description,
        "kind": order["kind"],
        "recipient": order["recipient"],
        "amount": order.get("amount"),
        "months": order.get("months"),
    }


def _require_card() -> str:
    if os.environ.get("SHOP_MODE", "").strip().lower() == "dry-run":
        raise ShopError(
            "purchases_off",
            "Оплата выключена: в настройках стоит SHOP_MODE=dry-run. Уберите её или поставьте live.",
            503,
        )
    token = monobank_token()
    if not token:
        raise ShopError(
            "purchases_off",
            "Оплата картой пока не готова: в настройках магазина нет токена Monobank. Назовите его MONOBANK_TOKEN.",
            503,
        )
    if not fragment_api_token():
        raise ShopError("purchases_off", "Оплата пока не готова: в настройках магазина нет ключа Fragment.", 503)
    return token


def _card_status(invoice_id: object, *, monobank: Monobank | None, fragment: FragmentClient | None, sender) -> dict:
    token = _require_card()
    text = str(invoice_id).strip()
    if not re.fullmatch(CARD_ID, text):
        raise ShopError("bad_request", "Неизвестный счёт.", 400)
    client = monobank or Monobank(token)
    return settle_card(client.get_invoice(text), token, fragment=fragment, sender=sender)


def _card_amount(total: str, rate: Decimal | None) -> tuple[int, int, str]:
    ccy = card_currency()
    if ccy == 840:
        minor = int(_cents(total) * 100)
        return 840, minor, f"{_cents(total):.2f} USD"
    rate = nbu_usd_rate() if rate is None else rate
    if not isinstance(rate, Decimal):
        rate = Decimal(str(rate))
    if rate <= 0 or rate > Decimal("1000"):
        raise ShopError("rate_unavailable", "Не удалось узнать курс доллара. Оплата картой сейчас недоступна.", 503)
    uah = (_cents(total) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    minor = int(uah * 100)
    if minor <= 0:
        raise ShopError("bad_amount", "Сумма к оплате получилась нулевой.", 500)
    return 980, minor, f"{uah:.2f} UAH"


def _card_waiting(invoice_id: object, status: str, invoice: dict) -> dict:
    if status == "created":
        message = "Оплатите картой. После оплаты покупка уйдёт сама."
        try:
            message = (
                "Оплатите картой. Сумма "
                + _charge_text(_whole(invoice.get("amount")), _whole(invoice.get("ccy")))
                + ". После оплаты покупка уйдёт сама."
            )
        except ShopError:
            pass
    else:
        message = {
            "processing": "Банк ещё проводит оплату.",
            "hold": "Банк удерживает оплату. Жду подтверждение.",
            "failure": "Банк отклонил оплату.",
            "reversed": "Оплата возвращена. Покупка не отправлена.",
            "expired": "Счёт истёк. Нажмите карту ещё раз.",
        }.get(status, "Жду ответ банка.")
    retry = status in {"created", "processing", "hold"} or status not in {"failure", "reversed", "expired"}
    return {
        "invoice_id": invoice_id,
        "status": status or "created",
        "paid": False,
        "delivered": False,
        "retry": retry,
        "message": message,
    }


def _charge_text(minor: int, ccy: int) -> str:
    amount = (Decimal(minor) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    code = {980: "UAH", 840: "USD"}.get(ccy, str(ccy))
    return f"{amount:.2f} {code}"


def _deliver(order: dict, invoice_id: object, fragment: FragmentClient | None, sender=None, prefix: str = "cryptopay") -> dict:
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
        key = f"{prefix}-{invoice_id}" if wallet == _wallet() else f"{prefix}-{invoice_id}-{wallet}"
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
        send_receipt(order, invoice_id, sender=sender)
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
    if fields.get("buyer_id") is not None:
        order["buyer_id"] = _whole(fields["buyer_id"])
    if fields.get("minor") not in (None, ""):
        order["minor"] = _whole(fields["minor"])
    if fields.get("ccy") not in (None, ""):
        order["ccy"] = _whole(fields["ccy"])
    if _cents(fields.get("total")) != _cents(order["total"]):
        raise ShopError("bad_invoice", "Цена в счёте не сходится.", 409)
    return order


def _signed_fields(order: dict) -> dict:
    fields = {"kind": order["kind"], "recipient": order["recipient"], "total": order["total"]}
    if order["kind"] == "stars":
        fields["amount"] = order["amount"]
    else:
        fields["months"] = order["months"]
    if order.get("buyer_id"):
        fields["buyer_id"] = order["buyer_id"]
    if order.get("minor") not in (None, ""):
        fields["minor"] = order["minor"]
    if order.get("ccy") not in (None, ""):
        fields["ccy"] = order["ccy"]
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
    if fields.get("buyer_id") not in (None, ""):
        data["buyer_id"] = _whole(fields["buyer_id"])
    if fields.get("minor") not in (None, ""):
        data["minor"] = _whole(fields["minor"])
    if fields.get("ccy") not in (None, ""):
        data["ccy"] = _whole(fields["ccy"])
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
