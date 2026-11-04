"""Local orders. Customer payment is recorded separately from the Fragment purchase."""

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Order:
    id: str
    product_id: str
    recipient_username: str
    status: str
    charge_id: str | None
    currency: str
    markup_percent: str
    fragment_cost: str
    customer_amount: str
    fragment_amount: str | None
    fragment_order_id: str | None
    delivery_json: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "product_id": self.product_id,
            "recipient_username": self.recipient_username,
            "status": self.status,
            "charge_id": self.charge_id,
            "currency": self.currency,
            "markup_percent": self.markup_percent,
            "fragment_cost": self.fragment_cost,
            "customer_amount": self.customer_amount,
            "fragment_amount": self.fragment_amount,
            "fragment_order_id": self.fragment_order_id,
            "delivery_json": self.delivery_json,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_COLUMNS = (
    "id",
    "product_id",
    "recipient_username",
    "status",
    "charge_id",
    "currency",
    "markup_percent",
    "fragment_cost",
    "customer_amount",
    "fragment_amount",
    "fragment_order_id",
    "delivery_json",
    "error_code",
    "error_message",
    "created_at",
    "updated_at",
)

_MUTABLE = {
    "status",
    "charge_id",
    "fragment_amount",
    "fragment_order_id",
    "delivery_json",
    "error_code",
    "error_message",
}


class OrderStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(orders)").fetchall()
        columns = {row[1] for row in rows}
        if columns and "recipient_username" not in columns:
            # Earlier skeleton stored a numeric Telegram id and could not buy Stars.
            self._conn.execute("DROP TABLE orders")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                recipient_username TEXT NOT NULL,
                status TEXT NOT NULL,
                charge_id TEXT,
                currency TEXT NOT NULL,
                markup_percent TEXT NOT NULL,
                fragment_cost TEXT NOT NULL,
                customer_amount TEXT NOT NULL,
                fragment_amount TEXT,
                fragment_order_id TEXT,
                delivery_json TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create(
        self,
        *,
        product_id: str,
        recipient_username: str,
        currency: str,
        markup_percent: str,
        fragment_cost: str,
        customer_amount: str,
    ) -> Order:
        now = utc_now()
        order = Order(
            id=str(uuid.uuid4()),
            product_id=product_id,
            recipient_username=recipient_username,
            status="pending_payment",
            charge_id=None,
            currency=currency,
            markup_percent=markup_percent,
            fragment_cost=fragment_cost,
            customer_amount=customer_amount,
            fragment_amount=None,
            fragment_order_id=None,
            delivery_json=None,
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._conn.execute(
                f"INSERT INTO orders ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})",
                tuple(getattr(order, column) for column in _COLUMNS),
            )
            self._conn.commit()
        return order

    def get(self, order_id: str) -> Order | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return self._row(row) if row else None

    def mark_paid(self, order_id: str, charge_id: str) -> Order | None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE orders
                SET status = 'paid', charge_id = ?, error_code = NULL, error_message = NULL, updated_at = ?
                WHERE id = ? AND status = 'pending_payment'
                """,
                (charge_id, now, order_id),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return self._row(row) if row else None

    def transition(self, order_id: str, allowed: tuple[str, ...], **changes) -> tuple[Order, bool] | None:
        unknown = set(changes) - _MUTABLE
        if unknown:
            raise ValueError(f"unexpected order fields: {sorted(unknown)}")
        sets = ["updated_at = ?"]
        params: list = [utc_now()]
        for key, value in changes.items():
            sets.append(f"{key} = ?")
            params.append(value)
        params.append(order_id)
        params.extend(allowed)
        marks = ", ".join("?" for _ in allowed)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE orders SET {', '.join(sets)} WHERE id = ? AND status IN ({marks})",
                params,
            )
            self._conn.commit()
            changed = cursor.rowcount == 1
            row = self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            return None
        return self._row(row), changed

    @staticmethod
    def _row(row: sqlite3.Row) -> Order:
        return Order(**{column: row[column] for column in _COLUMNS})
