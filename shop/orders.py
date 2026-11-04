"""Order records. Payment confirmation is explicit and separate from delivery."""

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
    recipient_user_id: int
    status: str
    charge_id: str | None
    delivery_json: str | None
    error_code: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "product_id": self.product_id,
            "recipient_user_id": self.recipient_user_id,
            "status": self.status,
            "charge_id": self.charge_id,
            "delivery_json": self.delivery_json,
            "error_code": self.error_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class OrderStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                recipient_user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                charge_id TEXT,
                delivery_json TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create(self, product_id: str, recipient_user_id: int) -> Order:
        now = utc_now()
        order = Order(
            id=uuid.uuid4().hex,
            product_id=product_id,
            recipient_user_id=recipient_user_id,
            status="pending_payment",
            charge_id=None,
            delivery_json=None,
            error_code=None,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO orders (
                    id, product_id, recipient_user_id, status, charge_id,
                    delivery_json, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.id,
                    order.product_id,
                    order.recipient_user_id,
                    order.status,
                    order.charge_id,
                    order.delivery_json,
                    order.error_code,
                    order.created_at,
                    order.updated_at,
                ),
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
            cursor = self._conn.execute(
                """
                UPDATE orders
                SET status = 'paid', charge_id = ?, error_code = NULL, updated_at = ?
                WHERE id = ? AND status = 'pending_payment'
                """,
                (charge_id, now, order_id),
            )
            self._conn.commit()
            if cursor.rowcount != 1:
                row = self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                return self._row(row) if row else None
            row = self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return self._row(row)

    def mark_fulfilled(self, order_id: str, delivery_json: str) -> Order | None:
        now = utc_now()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE orders
                SET status = 'fulfilled', delivery_json = ?, error_code = NULL, updated_at = ?
                WHERE id = ? AND status = 'paid'
                """,
                (delivery_json, now, order_id),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            return None
        order = self._row(row)
        if cursor.rowcount != 1 and order.status != "fulfilled":
            return order
        return order

    def mark_rejected(self, order_id: str, error_code: str) -> Order | None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE orders
                SET error_code = ?, updated_at = ?
                WHERE id = ? AND status = 'paid'
                """,
                (error_code, now, order_id),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row: sqlite3.Row) -> Order:
        return Order(
            id=row["id"],
            product_id=row["product_id"],
            recipient_user_id=row["recipient_user_id"],
            status=row["status"],
            charge_id=row["charge_id"],
            delivery_json=row["delivery_json"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
