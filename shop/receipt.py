"""One short chat message after Fragment has actually delivered the order."""

import os
from pathlib import Path

from shop.errors import ShopError
from shop.telegram_api import TelegramApi
from shop.tokens import telegram_bot_token


def receipt_text(order: dict) -> str:
    who = f"@{order['recipient']}"
    if order.get("kind") == "stars":
        amount = int(order["amount"])
        return f"⭐ {_grouped(amount)} {_star_word(amount)} → {who}"
    months = int(order["months"])
    return f"💎 Premium на {_month_phrase(months)} → {who}"


def send_receipt(order: dict, invoice_id: object, *, sender=None) -> None:
    buyer = order.get("buyer_id")
    if isinstance(buyer, bool) or not isinstance(buyer, int) or buyer <= 0:
        return
    if not _claim(invoice_id):
        return
    text = receipt_text(order)
    try:
        if sender is not None:
            sender(buyer, text)
            return
        token = telegram_bot_token()
        if not token:
            _forget(invoice_id)
            return
        TelegramApi(token).send_message(buyer, text)
    except ShopError as exc:
        if exc.code == "telegram_unavailable":
            _forget(invoice_id)
    except Exception:
        _forget(invoice_id)


def _claim(invoice_id: object) -> bool:
    key = str(invoice_id).strip()
    if not key:
        return False
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            known = {line.strip() for line in handle if line.strip()}
            if key in known:
                return False
            handle.write(key + "\n")
            handle.flush()
            return True
    except OSError:
        return True


def _forget(invoice_id: object) -> None:
    key = str(invoice_id).strip()
    path = _path()
    if not path.is_file():
        return
    try:
        with path.open("a+", encoding="utf-8") as handle:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            kept = [line for line in handle if line.strip() and line.strip() != key]
            handle.seek(0)
            handle.truncate()
            handle.writelines(kept)
            handle.flush()
    except OSError:
        return


def _path() -> Path:
    return Path(os.environ.get("SHOP_NOTIFY_FILE", "/tmp/shop-notified-invoices.txt"))


def _grouped(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def _star_word(amount: int) -> str:
    tail = abs(amount) % 100
    if 11 <= tail <= 14:
        return "звёзд"
    last = tail % 10
    if last == 1:
        return "звезда"
    if 2 <= last <= 4:
        return "звезды"
    return "звёзд"


def _month_phrase(months: int) -> str:
    tail = abs(months) % 100
    if 11 <= tail <= 14:
        return f"{months} месяцев"
    last = tail % 10
    if last == 1:
        return f"{months} месяц"
    if 2 <= last <= 4:
        return f"{months} месяца"
    return f"{months} месяцев"
