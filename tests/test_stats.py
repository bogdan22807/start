import hashlib
import time
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from shop.checkout import create_invoice, invoice_status, stat_response
from shop.errors import ShopError
from shop.fragment import FragmentClient
from shop.stats import (
    build_report,
    is_admin,
    load_state,
    pack_snapshot,
    remember_invoice,
    remember_user,
    unpack_snapshot,
)
from shop.telegram_api import TelegramApi


TOKEN = "123456:AAFakeTokenForTestsOnly0001"
NOW = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)


class Script:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, method, url, headers, body):
        if not self.responses:
            raise AssertionError(f"unexpected {method} {url}")
        return self.responses.pop(0)


class Pay:
    def __init__(self):
        self.payload = ""

    def create_invoice(self, amount, description, payload, *, paid_btn_url, hidden_message):
        self.payload = payload
        return {
            "invoice_id": 77,
            "status": "active",
            "mini_app_invoice_url": "https://t.me/CryptoBot/app?startapp=IV77",
        }

    def get_invoice(self, invoice_id):
        return {
            "invoice_id": 77,
            "status": "paid",
            "asset": "USDT",
            "amount": "0.94",
            "payload": self.payload,
        }


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.stats_dir = tempfile.TemporaryDirectory()
        self.saved = {key: os.environ.get(key) for key in ("STATS_DB", "BOT_TOKEN", "CRYPTO_PAY_TOKEN", "FRAGMENT_API_KEY", "SHOP_MODE")}
        os.environ["STATS_DB"] = str(Path(self.stats_dir.name) / "stats.sqlite")
        os.environ.pop("BOT_TOKEN", None)
        os.environ.pop("CRYPTO_PAY_TOKEN", None)
        os.environ.pop("FRAGMENT_API_KEY", None)
        os.environ.pop("SHOP_MODE", None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.stats_dir.cleanup()

    def test_only_bogdan_is_the_admin(self):
        self.assertTrue(is_admin({"username": "Bogdan22807"}))
        self.assertTrue(is_admin({"username": "@bogdan22807"}))
        self.assertFalse(is_admin({"username": "alexdev"}))
        self.assertFalse(is_admin({"first_name": "Богдан"}))
        self.assertFalse(is_admin(None))

    def test_periods_split_users_orders_and_sums(self):
        users = [
            {"id": 1, "first_seen": "2026-11-04T10:00:00+00:00"},
            {"id": 2, "first_seen": "2026-11-02T10:00:00+00:00"},
            {"id": 3, "first_seen": "2026-10-01T12:00:00+00:00"},
        ]
        orders = [
            {"id": "today", "status": "paid", "paid_at": "2026-11-04T10:00:00+00:00", "amount": "18.75", "user_id": 1},
            {"id": "recent", "status": "paid", "paid_at": "2026-11-02T10:00:00+00:00", "amount": "14.99", "user_id": 2},
            {"id": "old", "status": "paid", "paid_at": "2026-10-01T12:00:00+00:00", "amount": "1234.50", "user_id": 3},
            {"id": "open", "status": "created", "paid_at": None, "amount": "9.38", "user_id": 1},
        ]
        report = build_report(users, orders, now=NOW)
        today = report["periods"]["1"]
        three = report["periods"]["3"]
        month = report["periods"]["30"]
        entire = report["periods"]["all"]
        self.assertEqual(today["users"], 1)
        self.assertEqual(today["orders"], 1)
        self.assertEqual(today["sum"], "18.75")
        self.assertEqual(len(today["points"]), 24)
        self.assertEqual(today["points"][11]["orders"], 1)
        self.assertEqual(three["users"], 2)
        self.assertEqual(three["orders"], 2)
        self.assertEqual(three["sum"], "33.74")
        self.assertEqual(month["users"], 2)
        self.assertEqual(month["orders"], 2)
        self.assertEqual(entire["users"], 3)
        self.assertEqual(entire["orders"], 3)
        self.assertEqual(entire["sum"], "1 268.24")
        self.assertEqual(sum(point["orders"] for point in three["points"]), three["orders"])

    def test_crypto_invoices_fill_gaps_without_double_counting(self):
        invoices = [
            {
                "invoice_id": 77,
                "status": "paid",
                "asset": "USDT",
                "amount": "9.99",
                "created_at": "2026-11-04T09:00:00Z",
                "paid_at": "2026-11-04T09:05:00Z",
                "payload": json.dumps({"order": {"buyer_id": 5}}),
            },
            {
                "invoice_id": 88,
                "status": "paid",
                "asset": "USDT",
                "amount": "2.00",
                "created_at": "2026-11-03T09:00:00Z",
                "paid_at": "2026-11-03T09:05:00Z",
                "payload": json.dumps({"order": {"buyer_id": 6}}),
            },
            {
                "invoice_id": 89,
                "status": "active",
                "asset": "USDT",
                "amount": "50.00",
                "created_at": "2026-11-04T08:00:00Z",
                "payload": json.dumps({"order": {"buyer_id": 7}}),
            },
            {"invoice_id": 90, "status": "paid", "asset": "TON", "amount": "3", "paid_at": "2026-11-04T09:00:00Z"},
        ]
        report = build_report(
            [{"id": 5, "first_seen": "2026-11-01T00:00:00+00:00"}],
            [{"id": "77", "status": "paid", "paid_at": "2026-11-04T09:05:00+00:00", "amount": "18.75", "user_id": 5}],
            crypto=invoices,
            now=NOW,
        )
        entire = report["periods"]["all"]
        self.assertEqual(entire["orders"], 2)
        self.assertEqual(entire["sum"], "20.75")
        self.assertEqual(entire["users"], 3)

    def test_snapshot_roundtrip_and_rejection(self):
        users = [{"id": 5, "first_seen": "2026-11-04T10:00:00+00:00", "status": ""}]
        orders = [{"id": "card-1", "status": "paid", "paid_at": "2026-11-04T11:00:00+00:00", "amount": "4.69", "user_id": 5}]
        packed = pack_snapshot(users, orders, TOKEN)
        self.assertLessEqual(len(packed), 1400)
        unpacked = unpack_snapshot(packed, TOKEN)
        self.assertEqual(unpacked["u"][0][0], 5)
        self.assertIsNone(unpack_snapshot(packed, "other-token"))
        self.assertIsNone(unpack_snapshot(packed + "x", TOKEN))
        report = build_report([], [], snapshot=unpacked, now=NOW)
        self.assertEqual(report["periods"]["1"]["orders"], 1)
        self.assertEqual(report["periods"]["1"]["sum"], "4.69")
        self.assertEqual(report["periods"]["1"]["users"], 1)

    def test_ledger_keeps_the_first_visit_and_counts_only_paid_orders(self):
        remember_user(5, "Alex")
        remember_user(5, "alexdev")
        remember_invoice("p2_test", user_id=5, amount="0.94", source="card", paid=False)
        users, orders = load_state()
        self.assertEqual(users[0]["username"], "alexdev")
        self.assertEqual(orders[0]["status"], "created")
        self.assertEqual(build_report(users, orders, now=datetime.now(timezone.utc))["periods"]["all"]["orders"], 0)
        first_seen = users[0]["first_seen"]
        remember_user(5, "")
        remember_invoice("p2_test", user_id=5, amount="0.94", source="card", paid=True)
        users, orders = load_state()
        self.assertEqual(users[0]["first_seen"], first_seen)
        self.assertEqual(users[0]["username"], "alexdev")
        self.assertEqual(orders[0]["status"], "paid")
        report = build_report(users, orders, now=datetime.now(timezone.utc))
        self.assertEqual(report["periods"]["all"]["orders"], 1)
        self.assertEqual(report["periods"]["all"]["sum"], "0.94")
        self.assertEqual(report["periods"]["all"]["users"], 1)

    def test_checkout_records_a_paid_order(self):
        os.environ["CRYPTO_PAY_TOKEN"] = "crypto-test-token"
        os.environ["FRAGMENT_API_KEY"] = "fragment-test-key"
        pay = Pay()
        fragment = FragmentClient(
            "fragment-test-key",
            "https://fragment.test",
            opener=Script([(404, {"error": "no"})]),
        )
        created = create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 50}, cryptopay=pay)
        self.assertEqual(created["invoice_id"], 77)
        _users, created_orders = load_state()
        self.assertEqual(created_orders[0]["status"], "created")
        state = invoice_status(77, cryptopay=pay, fragment=fragment)
        self.assertTrue(state["paid"])
        _users, paid_orders = load_state()
        self.assertEqual(paid_orders[0]["status"], "paid")
        report = build_report(*load_state(), now=datetime.now(timezone.utc))
        self.assertEqual(report["periods"]["all"]["sum"], "0.94")

    def test_api_refuses_everyone_except_the_admin(self):
        os.environ["BOT_TOKEN"] = TOKEN
        remember_invoice("card-9", user_id=5, amount="19.99", source="card", paid=True)
        with self.assertRaises(ShopError) as denied:
            stat_response({"init_data": _init_data(TOKEN, 5, "alexdev")})
        self.assertEqual(denied.exception.status, 403)
        self.assertEqual(denied.exception.message, "Эта панель только для администратора.")
        with self.assertRaises(ShopError) as broken:
            stat_response({"init_data": _init_data(TOKEN, 7, "Bogdan22807"), "snapshot": "broken.signature"})
        self.assertEqual(broken.exception.status, 400)
        status, body = stat_response({"init_data": _init_data(TOKEN, 7, "Bogdan22807")})
        self.assertEqual(status, 200)
        self.assertEqual(body["periods"]["all"]["orders"], 1)
        self.assertEqual(body["periods"]["all"]["sum"], "19.99")
        self.assertEqual(set(body["periods"]), {"1", "3", "7", "30", "all"})

    def test_public_commands_hide_stat_and_the_admin_chat_shows_it(self):
        capture = Capture()
        api = TelegramApi(TOKEN, opener=capture)
        api.set_commands()
        api.set_admin_commands(7)
        public = capture.calls[0][1]["commands"]
        private = capture.calls[1][1]
        self.assertEqual([item["command"] for item in public], ["start"])
        self.assertEqual(private["scope"], {"type": "chat", "chat_id": 7})
        self.assertEqual([item["command"] for item in private["commands"]], ["start", "stat"])


class Capture:
    def __init__(self):
        self.calls = []

    def __call__(self, token, method, payload, timeout):
        self.calls.append((method, payload))
        return 200, {"ok": True, "result": True}


def _init_data(token: str, user_id: int, username: str) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "username": username}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


if __name__ == "__main__":
    unittest.main()
