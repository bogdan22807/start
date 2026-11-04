"""Shop statistics for the admin panel.

Users are people who wrote to the bot. Orders are payments the provider
confirmed. Amounts stay in USDT, including card payments, so the periods
can be compared. Crypto Pay invoices are merged in when the panel is built,
because that list survives a restart of the server.
"""

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

ADMIN_USERNAME = "bogdan22807"
ZONE = ZoneInfo("Europe/Bratislava")
PERIODS = ("1", "3", "7", "30", "all")
_SNAPSHOT_LIMIT = 1400
_LOCK = threading.Lock()
_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
_MONTHS_FULL = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def is_admin(user: dict | None) -> bool:
    if not isinstance(user, dict):
        return False
    username = user.get("username")
    if not isinstance(username, str):
        return False
    return username.lstrip("@").lower() == ADMIN_USERNAME


def db_path() -> Path:
    raw = os.environ.get("STATS_DB", "").strip() or "data/stats.sqlite"
    return Path(raw)


def remember_user(user_id: object, username: str = "") -> None:
    uid = _user_id(user_id)
    if uid is None:
        return
    name = username.lstrip("@")[:32] if isinstance(username, str) else ""
    now = _iso(datetime.now(timezone.utc))
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT id FROM users WHERE id = ?", (uid,)).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE users
                    SET last_seen = ?, username = CASE WHEN ? != '' THEN ? ELSE username END
                    WHERE id = ?
                    """,
                    (now, name, name, uid),
                )
            else:
                conn.execute(
                    "INSERT INTO users (id, username, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                    (uid, name, now, now),
                )
            conn.commit()
        finally:
            conn.close()


def remember_invoice(invoice_id: object, *, user_id: object, amount: object, source: str, paid: bool) -> None:
    text = _invoice_id(invoice_id)
    price = _amount(amount)
    if not text or price is None:
        return
    uid = _user_id(user_id)
    now = _iso(datetime.now(timezone.utc))
    origin = source if source in {"card", "cryptobot"} else "cryptobot"
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT status, paid_at, user_id FROM orders WHERE id = ?", (text,)).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO orders (id, user_id, amount, status, source, created_at, paid_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (text, uid, price, "paid" if paid else "created", origin, now, now if paid else None),
                )
            elif paid and row["status"] != "paid":
                conn.execute(
                    """
                    UPDATE orders
                    SET status = 'paid', paid_at = ?, user_id = COALESCE(user_id, ?)
                    WHERE id = ?
                    """,
                    (now, uid, text),
                )
            elif uid and row["user_id"] is None:
                conn.execute("UPDATE orders SET user_id = ? WHERE id = ?", (uid, text))
            conn.commit()
        finally:
            conn.close()


def load_state() -> tuple[list[dict], list[dict]]:
    try:
        with _LOCK:
            conn = _connect()
            try:
                users = [dict(row) for row in conn.execute("SELECT id, username, first_seen FROM users")]
                orders = [
                    dict(row)
                    for row in conn.execute("SELECT id, user_id, amount, status, created_at, paid_at FROM orders")
                ]
            finally:
                conn.close()
    except sqlite3.Error:
        return [], []
    return users, orders


def pack_snapshot(users: list[dict], orders: list[dict], token: str) -> str:
    if not token:
        return ""
    prepared_users = []
    for item in users:
        uid = _user_id(item.get("id"))
        seen = _parse_time(item.get("first_seen"))
        if uid and seen:
            prepared_users.append((uid, int(seen.timestamp())))
    prepared_users.sort(key=lambda item: item[1])
    prepared_orders = []
    for item in orders:
        if item.get("status") != "paid" and not item.get("paid_at"):
            continue
        paid = _parse_time(item.get("paid_at") or item.get("created_at"))
        price = _amount(item.get("amount"))
        text = _invoice_id(item.get("id"))
        if not text or paid is None or price is None:
            continue
        prepared_orders.append((text, int(paid.timestamp()), price, _user_id(item.get("user_id")) or 0))
    caps = [len(prepared_users)]
    for cap in (200, 80, 0):
        if cap < caps[0]:
            caps.append(cap)
    for cap in caps:
        chosen = prepared_users[-cap:] if cap else []
        extra = len(prepared_users) - len(chosen)
        for keep_orders in (True, False):
            payload = {
                "u": [list(item) for item in chosen],
                "o": [list(item) for item in prepared_orders] if keep_orders else [],
                "x": extra,
            }
            packed = _sign_payload(payload, token)
            if len(packed) <= _SNAPSHOT_LIMIT:
                return packed
    return ""


def unpack_snapshot(packed: str, token: str) -> dict | None:
    if not token or not isinstance(packed, str) or packed.count(".") != 1:
        return None
    body, signature = packed.split(".", 1)
    if not body or not signature or len(body) > _SNAPSHOT_LIMIT:
        return None
    expected = base64.urlsafe_b64encode(hmac.new(token.encode(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        compressed = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, 100_000)
        if decoder.unconsumed_tail or not decoder.eof:
            return None
        payload = json.loads(raw.decode())
    except (zlib.error, json.JSONDecodeError, ValueError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def build_report(
    users: list[dict],
    orders: list[dict],
    *,
    crypto: list[dict] | None = None,
    snapshot: dict | None = None,
    now: datetime | None = None,
) -> dict:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    known: dict[int, datetime] = {}
    paid: dict[str, dict] = {}
    extra = 0
    _add_ledger_users(known, users)
    _add_ledger_orders(paid, orders)
    if isinstance(snapshot, dict):
        _add_snapshot(known, paid, snapshot)
        raw_extra = snapshot.get("x")
        if isinstance(raw_extra, int) and not isinstance(raw_extra, bool) and raw_extra > 0:
            extra = raw_extra
    _add_crypto(known, paid, crypto or [], include_users=extra == 0)
    local_now = moment.astimezone(ZONE)
    periods = {}
    for name in PERIODS:
        periods[name] = _period(name, known, paid, extra, local_now)
    return {"currency": "USDT", "periods": periods}


def _add_ledger_users(known: dict[int, datetime], users: list[dict]) -> None:
    for item in users:
        uid = _user_id(item.get("id"))
        seen = _parse_time(item.get("first_seen"))
        if uid and seen:
            _earliest(known, uid, seen)


def _add_ledger_orders(paid: dict[str, dict], orders: list[dict]) -> None:
    for item in orders:
        if item.get("status") != "paid" and not item.get("paid_at"):
            continue
        text = _invoice_id(item.get("id"))
        when = _parse_time(item.get("paid_at") or item.get("created_at"))
        price = _amount(item.get("amount"))
        if text and when and price is not None:
            paid[text] = {"at": when, "amount": Decimal(price), "user_id": _user_id(item.get("user_id"))}


def _add_snapshot(known: dict[int, datetime], paid: dict[str, dict], snapshot: dict) -> None:
    entries = snapshot.get("u") if isinstance(snapshot.get("u"), list) else []
    for item in entries:
        if not isinstance(item, list) or len(item) < 2:
            continue
        uid = _user_id(item[0])
        seen = _parse_time(item[1])
        if uid and seen:
            _earliest(known, uid, seen)
    entries = snapshot.get("o") if isinstance(snapshot.get("o"), list) else []
    for item in entries:
        if not isinstance(item, list) or len(item) < 3:
            continue
        text = _invoice_id(item[0])
        when = _parse_time(item[1])
        price = _amount(item[2])
        if text and when and price is not None and text not in paid:
            paid[text] = {"at": when, "amount": Decimal(price), "user_id": _user_id(item[3]) if len(item) > 3 else None}


def _add_crypto(known: dict[int, datetime], paid: dict[str, dict], invoices: list[dict], *, include_users: bool) -> None:
    for item in invoices:
        if not isinstance(item, dict):
            continue
        if str(item.get("asset") or "USDT").upper() != "USDT":
            continue
        created = _parse_time(item.get("created_at"))
        buyer = _payload_buyer(item.get("payload"))
        if include_users and buyer and created:
            _earliest(known, buyer, created)
        if str(item.get("status") or "") != "paid":
            continue
        text = _invoice_id(item.get("invoice_id"))
        when = _parse_time(item.get("paid_at")) or created
        price = _amount(item.get("amount"))
        if text and when and price is not None and text not in paid:
            paid[text] = {"at": when, "amount": Decimal(price), "user_id": buyer}


def _period(name: str, known: dict[int, datetime], paid: dict[str, dict], extra: int, local_now: datetime) -> dict:
    start = _window_start(name, local_now)
    users = [seen for seen in known.values() if _inside(seen, start, local_now)]
    orders = [item for item in paid.values() if _inside(item["at"], start, local_now)]
    total = sum((item["amount"] for item in orders), Decimal("0.00"))
    points = _points(name, known, paid, start, local_now)
    count = len(users) + (extra if name == "all" else 0)
    return {"users": count, "orders": len(orders), "sum": _money(total), "points": points}


def _window_start(name: str, local_now: datetime) -> datetime | None:
    if name == "all":
        return None
    days = int(name)
    start_day = local_now.date() - timedelta(days=days - 1)
    return datetime(start_day.year, start_day.month, start_day.day, tzinfo=ZONE)


def _points(name: str, known: dict[int, datetime], paid: dict[str, dict], start: datetime | None, local_now: datetime) -> list[dict]:
    if name == "1":
        buckets = _hour_buckets(local_now)
    elif name == "all":
        buckets = _all_buckets(known, paid, local_now)
    else:
        buckets = _day_buckets(start, local_now)
    return [_bucket(label, title, opening, closing, known, paid) for label, title, opening, closing in buckets]


def _hour_buckets(local_now: datetime) -> list[tuple]:
    opening = datetime(local_now.year, local_now.month, local_now.day, tzinfo=ZONE)
    buckets = []
    for hour in range(24):
        start = opening + timedelta(hours=hour)
        end = start + timedelta(hours=1)
        buckets.append((f"{hour:02d}", f"{start.day} {_MONTHS_FULL[start.month - 1]}, {hour:02d}:00", start, end))
    return buckets


def _day_buckets(start: datetime | None, local_now: datetime) -> list[tuple]:
    if start is None:
        return []
    buckets = []
    day = start.date()
    while day <= local_now.date():
        opening = datetime(day.year, day.month, day.day, tzinfo=ZONE)
        buckets.append((f"{day.day:02d}.{day.month:02d}", f"{day.day} {_MONTHS_FULL[day.month - 1]}", opening, opening + timedelta(days=1)))
        day += timedelta(days=1)
    return buckets


def _all_buckets(known: dict[int, datetime], paid: dict[str, dict], local_now: datetime) -> list[tuple]:
    moments = [item.astimezone(ZONE) for item in known.values()]
    moments.extend(item["at"].astimezone(ZONE) for item in paid.values())
    if not moments:
        start = local_now.date() - timedelta(days=6)
        return _day_buckets(datetime(start.year, start.month, start.day, tzinfo=ZONE), local_now)
    first = min(moments).date()
    span = (local_now.date() - first).days + 1
    if span <= 31:
        return _day_buckets(datetime(first.year, first.month, first.day, tzinfo=ZONE), local_now)
    if span <= 186:
        return _week_buckets(first, local_now)
    return _month_buckets(first, local_now)


def _week_buckets(first, local_now: datetime) -> list[tuple]:
    start = first - timedelta(days=first.weekday())
    buckets = []
    while start <= local_now.date():
        end = start + timedelta(days=7)
        opening = datetime(start.year, start.month, start.day, tzinfo=ZONE)
        closing = datetime(end.year, end.month, end.day, tzinfo=ZONE)
        finish = end - timedelta(days=1)
        label = f"{start.day}–{finish.day} {_MONTHS[finish.month - 1]}"
        title = f"{start.day} {_MONTHS[start.month - 1]} – {finish.day} {_MONTHS[finish.month - 1]}"
        buckets.append((label, title, opening, closing))
        start = end
    return buckets


def _month_buckets(first, local_now: datetime) -> list[tuple]:
    year, month = first.year, first.month
    buckets = []
    while (year, month) <= (local_now.year, local_now.month):
        opening = datetime(year, month, 1, tzinfo=ZONE)
        next_month = 1 if month == 12 else month + 1
        next_year = year + 1 if month == 12 else year
        closing = datetime(next_year, next_month, 1, tzinfo=ZONE)
        buckets.append((f"{_MONTHS[month - 1]} {str(year)[2:]}", f"{_MONTHS_FULL[month - 1]} {year}", opening, closing))
        year, month = next_year, next_month
    return buckets


def _bucket(label: str, title: str, opening: datetime, closing: datetime, known: dict[int, datetime], paid: dict[str, dict]) -> dict:
    users = sum(1 for seen in known.values() if opening <= seen.astimezone(ZONE) < closing)
    chosen = [item for item in paid.values() if opening <= item["at"].astimezone(ZONE) < closing]
    total = sum((item["amount"] for item in chosen), Decimal("0.00"))
    return {"label": label, "title": title, "users": users, "orders": len(chosen), "sum": _money(total)}


def _inside(moment: datetime, start: datetime | None, local_now: datetime) -> bool:
    local = moment.astimezone(ZONE)
    if local > local_now:
        return False
    return start is None or local >= start


def _earliest(known: dict[int, datetime], uid: int, seen: datetime) -> None:
    current = known.get(uid)
    if current is None or seen < current:
        known[uid] = seen


def _sign_payload(payload: dict, token: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    body = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(token.encode(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return f"{body}.{signature}"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            amount TEXT NOT NULL,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            paid_at TEXT
        )
        """
    )
    return conn


def _payload_buyer(payload: object) -> int | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    order = payload.get("order")
    if isinstance(order, dict):
        return _user_id(order.get("buyer_id"))
    return _user_id(payload.get("buyer_id"))


def _user_id(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit() and int(value.strip()) > 0:
        return int(value.strip())
    return None


def _invoice_id(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).strip()
    if not text or len(text) > 80:
        return ""
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-")
    if any(char not in allowed for char in text):
        return ""
    return text


def _amount(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value).strip())
    except Exception:
        return None
    if amount <= 0 or amount > Decimal("1000000"):
        return None
    return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _money(amount: Decimal) -> str:
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    whole, frac = f"{abs(quantized):.2f}".split(".")
    groups = []
    while whole:
        groups.append(whole[-3:])
        whole = whole[:-3]
    return sign + " ".join(reversed(groups)) + "." + frac


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()
