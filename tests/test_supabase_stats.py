import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from shop import stats

ENV = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_KEY": "sb_publishable_test",
    "STATS_SECRET": "s" * 40,
}


class SupabaseStorageTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def fake_post(self, reply=None, status=200):
        def post(url, headers, body):
            self.calls.append((url, headers, body))
            return status, reply

        return post

    def test_user_and_order_go_to_supabase_with_the_secret(self):
        with mock.patch.object(stats, "_post_json", self.fake_post()):
            stats.remember_user(42, "@Buyer")
            stats.remember_invoice(
                "77", user_id=42, amount="18.75", source="cryptobot", paid=True,
                kind="stars", quantity=1000, recipient="@friend",
            )
        user_url, headers, user_body = self.calls[0]
        self.assertEqual(user_url, "https://example.supabase.co/rest/v1/rpc/shop_remember_user")
        self.assertEqual(headers["apikey"], "sb_publishable_test")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(user_body, {"p_id": 42, "p_username": "Buyer", "p_secret": "s" * 40})
        order_body = self.calls[1][2]
        self.assertEqual(order_body["p_amount"], "18.75")
        self.assertEqual(order_body["p_kind"], "stars")
        self.assertEqual(order_body["p_quantity"], 1000)
        self.assertEqual(order_body["p_recipient"], "friend")
        self.assertTrue(order_body["p_paid"])

    def test_load_state_reads_supabase(self):
        reply = {
            "users": [{"id": 1, "username": "a", "first_seen": "2026-11-04T10:00:00+00:00"}],
            "orders": [{"id": "9", "user_id": 1, "amount": "14.99", "status": "paid", "paid_at": "2026-11-04T11:00:00+00:00", "kind": "premium", "quantity": 3, "source": "card"}],
        }
        with mock.patch.object(stats, "_post_json", self.fake_post(reply)):
            users, orders = stats.load_state()
        self.assertEqual(users[0]["username"], "a")
        self.assertEqual(orders[0]["kind"], "premium")

    def test_supabase_errors_never_break_a_purchase(self):
        def broken(url, headers, body):
            raise OSError("down")

        with mock.patch.object(stats, "_post_json", broken):
            stats.remember_user(42, "buyer")
            self.assertEqual(stats.load_state(), ([], []))
        with mock.patch.object(stats, "_post_json", self.fake_post(status=401)):
            self.assertEqual(stats.load_state(), ([], []))

    def test_short_secret_keeps_sqlite(self):
        with mock.patch.dict(os.environ, {"STATS_SECRET": "short"}):
            self.assertFalse(stats.supabase_ready())


class AdminTests(unittest.TestCase):
    def test_username_is_case_insensitive(self):
        with mock.patch.dict(os.environ, {"ADMIN_TELEGRAM_ID": ""}):
            self.assertTrue(stats.is_admin({"id": 5, "username": "BogdaN22807"}))
            self.assertFalse(stats.is_admin({"id": 5, "username": "bogdan228077"}))

    def test_pinned_id_wins_over_username(self):
        with mock.patch.dict(os.environ, {"ADMIN_TELEGRAM_ID": "555"}):
            self.assertTrue(stats.is_admin({"id": 555, "username": "renamed"}))
            self.assertFalse(stats.is_admin({"id": 6, "username": "Bogdan22807"}))


class ReportTests(unittest.TestCase):
    def test_report_splits_products_sources_and_lists_recent(self):
        users = [{"id": 1, "username": "anna", "first_seen": "2026-11-04T09:00:00+00:00"}]
        orders = [
            {"id": "a", "user_id": 1, "amount": "18.75", "status": "paid", "paid_at": "2026-11-04T10:00:00+00:00", "kind": "stars", "quantity": 1000, "source": "card", "recipient": "bob"},
            {"id": "b", "user_id": 1, "amount": "14.99", "status": "paid", "paid_at": "2026-11-04T11:00:00+00:00", "kind": "premium", "quantity": 3, "source": "cryptobot", "recipient": "anna"},
        ]
        crypto = [{"invoice_id": "c", "status": "paid", "asset": "USDT", "amount": "0.94", "paid_at": "2026-11-04T12:00:00Z",
                   "created_at": "2026-11-04T11:59:00Z", "payload": '{"order":{"kind":"stars","amount":50,"recipient":"zed","buyer_id":1}}'}]
        report = stats.build_report(users, orders, crypto=crypto, now=datetime(2026, 11, 4, 13, tzinfo=timezone.utc))
        today = report["periods"]["1"]
        self.assertEqual(today["orders"], 3)
        self.assertEqual(today["sum"], "34.68")
        self.assertEqual(today["average"], "11.56")
        self.assertEqual(today["products"]["stars"], {"orders": 2, "sum": "19.69"})
        self.assertEqual(today["products"]["premium"], {"orders": 1, "sum": "14.99"})
        self.assertEqual(today["sources"]["card"]["orders"], 1)
        self.assertEqual(today["stars"], 1050)
        recent = report["recent"]
        self.assertEqual([item["id"] for item in recent], ["c", "b", "a"])
        self.assertEqual(recent[0]["recipient"], "zed")
        self.assertEqual(recent[0]["buyer"], "anna")
        self.assertEqual(recent[2]["when"], "4 ноя, 11:00")


if __name__ == "__main__":
    unittest.main()
