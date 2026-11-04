import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from shop.catalog import CATALOG
from shop.fulfillment import ShopError
from shop.orders import OrderStore
from shop.server import make_server
from shop.service import Shop


PREMIUM_COSTS = {"premium_3m": (3, 1000), "premium_6m": (6, 1500), "premium_12m": (12, 2500)}


class CatalogTests(unittest.TestCase):
    def test_premium_matches_bot_api_contract(self):
        for product_id, (months, stars) in PREMIUM_COSTS.items():
            product = CATALOG[product_id]
            self.assertEqual(product.kind, "premium")
            self.assertEqual(product.month_count, months)
            self.assertEqual(product.star_count, stars)
            self.assertTrue(product.deliverable)

    def test_star_packs_are_not_deliverable(self):
        packs = [item for item in CATALOG.values() if item.kind == "stars"]
        self.assertGreaterEqual(len(packs), 1)
        for pack in packs:
            self.assertFalse(pack.deliverable)
            self.assertIsNone(pack.month_count)


class OrderFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shop = Shop(OrderStore(Path(self.tmp.name) / "orders.db"))

    def tearDown(self):
        self.shop.store.close()
        self.tmp.cleanup()

    def test_premium_dry_run_creates_gift_payload(self):
        order = self.shop.create_order("premium_3m", 424242)
        self.assertEqual(order.status, "pending_payment")
        paid = self.shop.confirm_payment(order.id, "pay-1")
        self.assertEqual(paid.status, "paid")
        fulfilled, delivery = self.shop.fulfill(order.id, mode="dry-run")
        self.assertEqual(fulfilled.status, "fulfilled")
        self.assertEqual(delivery.method, "giftPremiumSubscription")
        self.assertEqual(delivery.mode, "dry-run")
        self.assertIsNone(delivery.telegram_ok)
        self.assertEqual(
            delivery.params,
            {"user_id": 424242, "month_count": 3, "star_count": 1000, "text": "Premium на 3 мес."},
        )
        again, replayed = self.shop.fulfill(order.id, mode="dry-run")
        self.assertEqual(again.status, "fulfilled")
        self.assertEqual(replayed.params, delivery.params)

    def test_fulfill_before_payment_is_rejected(self):
        order = self.shop.create_order("premium_12m", 7)
        with self.assertRaises(ShopError) as caught:
            self.shop.fulfill(order.id, mode="dry-run")
        self.assertEqual(caught.exception.code, "not_paid")
        self.assertEqual(self.shop.get_order(order.id).status, "pending_payment")

    def test_stars_stay_unpaid_delivery(self):
        order = self.shop.create_order("stars_100", 7)
        self.shop.confirm_payment(order.id, "pay-stars")
        with self.assertRaises(ShopError) as caught:
            self.shop.fulfill(order.id, mode="dry-run")
        self.assertEqual(caught.exception.code, "stars_not_deliverable")
        saved = self.shop.get_order(order.id)
        self.assertEqual(saved.status, "paid")
        self.assertEqual(saved.error_code, "stars_not_deliverable")

    def test_live_premium_posts_gift_and_requires_token(self):
        order = self.shop.create_order("premium_6m", 99)
        self.shop.confirm_payment(order.id, "pay-live")
        calls = []

        def poster(token, method, params):
            calls.append((token, method, params))
            return {"ok": True, "result": True}

        previous = os.environ.get("BOT_TOKEN")
        os.environ["BOT_TOKEN"] = "test-token"
        try:
            _, delivery = self.shop.fulfill(order.id, mode="live", poster=poster)
        finally:
            if previous is None:
                os.environ.pop("BOT_TOKEN", None)
            else:
                os.environ["BOT_TOKEN"] = previous
        self.assertEqual(calls, [("test-token", "giftPremiumSubscription", delivery.params)])
        self.assertTrue(delivery.telegram_ok)
        self.assertEqual(delivery.params["month_count"], 6)
        self.assertEqual(delivery.params["star_count"], 1500)

    def test_live_without_token_does_not_mark_fulfilled(self):
        order = self.shop.create_order("premium_3m", 5)
        self.shop.confirm_payment(order.id, "pay-no-token")
        previous = os.environ.pop("BOT_TOKEN", None)
        try:
            with self.assertRaises(ShopError) as caught:
                self.shop.fulfill(order.id, mode="live", poster=lambda *args: {"ok": True})
        finally:
            if previous is not None:
                os.environ["BOT_TOKEN"] = previous
        self.assertEqual(caught.exception.code, "missing_token")
        self.assertEqual(self.shop.get_order(order.id).status, "paid")

    def test_unknown_product_and_bad_user(self):
        with self.assertRaises(ShopError) as missing:
            self.shop.create_order("nope", 1)
        self.assertEqual(missing.exception.status, 404)
        with self.assertRaises(ShopError):
            self.shop.create_order("premium_3m", 0)
        with self.assertRaises(ShopError):
            self.shop.create_order("premium_3m", True)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        shop = Shop(OrderStore(Path(self.tmp.name) / "orders.db"))
        self.server = make_server("127.0.0.1", 0, shop)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.server.shop.store.close()
        self.tmp.cleanup()

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_http_premium_purchase_and_star_refusal(self):
        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])

        status, catalog = self.request("GET", "/catalog")
        self.assertEqual(status, 200)
        ids = {item["id"] for item in catalog["products"]}
        self.assertIn("premium_12m", ids)
        self.assertIn("stars_500", ids)

        status, created = self.request(
            "POST", "/orders", {"product_id": "premium_12m", "recipient_user_id": 1001}
        )
        self.assertEqual(status, 201)
        order_id = created["id"]

        status, blocked = self.request("POST", f"/orders/{order_id}/fulfill", {})
        self.assertEqual(status, 409)
        self.assertEqual(blocked["error"], "not_paid")

        status, paid = self.request("POST", f"/orders/{order_id}/pay", {"charge_id": "demo-charge"})
        self.assertEqual(status, 200)
        self.assertEqual(paid["status"], "paid")

        status, done = self.request("POST", f"/orders/{order_id}/fulfill", {})
        self.assertEqual(status, 200)
        self.assertEqual(done["status"], "fulfilled")
        self.assertEqual(done["delivery"]["params"]["star_count"], 2500)
        self.assertEqual(done["delivery"]["params"]["month_count"], 12)
        self.assertEqual(done["delivery"]["mode"], "dry-run")

        status, stars = self.request(
            "POST", "/orders", {"product_id": "stars_50", "recipient_user_id": 1001}
        )
        self.assertEqual(status, 201)
        self.request("POST", f"/orders/{stars['id']}/pay", {"charge_id": "stars-charge"})
        status, refused = self.request("POST", f"/orders/{stars['id']}/fulfill", {})
        self.assertEqual(status, 409)
        self.assertEqual(refused["error"], "stars_not_deliverable")


if __name__ == "__main__":
    unittest.main()
