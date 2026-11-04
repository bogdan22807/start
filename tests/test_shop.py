import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

from shop.catalog import get_product
from shop.config import load_shop
from shop.errors import ShopError
from shop.fragment import FragmentClient
from shop.orders import OrderStore
from shop.pricing import PriceBook, apply_markup, costs_from_sources, money
from shop.server import make_server
from shop.service import Shop


def price_book(markup="25") -> PriceBook:
    costs = costs_from_sources(
        star_unit=Decimal("0.01"),
        premium_env={3: Decimal("10"), 6: Decimal("18"), 12: Decimal("30")},
        packages=None,
        currency="USDT",
    )
    return PriceBook(Decimal(markup), "USDT", costs)


def _no_network(*_args):
    raise AssertionError("network")


def make_shop(store: OrderStore, *, mode="dry-run", fragment=None, admin="") -> Shop:
    client = fragment or FragmentClient("", "https://fragment.test", opener=_no_network)
    return Shop(store, price_book(), client, mode=mode, admin_token=admin)


class Script:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if not self.responses:
            raise AssertionError(f"unexpected {method} {url}")
        return self.responses.pop(0)


class PricingTests(unittest.TestCase):
    def test_markup_is_added_on_top_of_fragment_cost(self):
        self.assertEqual(apply_markup(Decimal("10"), Decimal("25")), Decimal("12.500000"))
        self.assertEqual(money(Decimal("12.5")), "12.50")
        self.assertEqual(money(Decimal("0.015") * 100), "1.50")

    def test_live_premium_packages_replace_fallback_costs(self):
        costs = costs_from_sources(
            star_unit=Decimal("0.02"),
            premium_env={3: Decimal("9"), 6: Decimal("18"), 12: Decimal("30")},
            packages=[{"months": 3, "usd_value": 11, "ton_value": 4}, {"months": 6, "usd_value": 20, "ton_value": 8}, {"months": 12, "usd_value": 36, "ton_value": 14}],
            currency="USDT",
        )
        book = PriceBook(Decimal("10"), "USDT", costs)
        premium = book.quote(get_product("premium_3m"))
        stars = book.quote(get_product("stars_50"))
        self.assertEqual(premium.cost_text, "11.00")
        self.assertEqual(premium.customer_text, "12.10")
        self.assertEqual(stars.cost_text, "1.00")
        self.assertEqual(stars.customer_text, "1.10")

        ton_costs = costs_from_sources(
            star_unit=Decimal("0.02"),
            premium_env={},
            packages=[{"months": 3, "usd_value": 11, "ton_value": 4}, {"months": 6, "usd_value": 20, "ton_value": 8}, {"months": 12, "usd_value": 36, "ton_value": 14}],
            currency="TON",
        )
        self.assertEqual(ton_costs["premium_3m"], Decimal("4"))


class OrderFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shop = make_shop(OrderStore(Path(self.tmp.name) / "orders.db"))

    def tearDown(self):
        self.shop.store.close()
        self.tmp.cleanup()

    def test_catalog_prices_stars_and_premium(self):
        catalog = self.shop.catalog()
        self.assertEqual(catalog["markup_percent"], "25")
        by_id = {item["id"]: item for item in catalog["products"]}
        self.assertEqual(by_id["stars_100"]["customer_price"], "1.25")
        self.assertEqual(by_id["stars_100"]["fragment_cost"], "1.00")
        self.assertEqual(by_id["premium_3m"]["customer_price"], "12.50")
        self.assertTrue(by_id["stars_100"]["priced"])
        self.assertTrue(by_id["premium_12m"]["priced"])

    def test_dry_run_buys_stars_and_premium_without_fragment(self):
        stars = self._paid("stars_100", "@AlexDev", "pay-stars")
        done, delivery = self.shop.fulfill(stars.id)
        self.assertEqual(done.status, "fulfilled")
        self.assertEqual(delivery.provider, "fragment")
        self.assertEqual(delivery.mode, "dry-run")
        self.assertEqual(delivery.username, "alexdev")
        self.assertEqual(delivery.quantity, 100)
        self.assertEqual(delivery.customer_price, "1.25")
        self.assertIsNone(delivery.fragment_order_id)

        premium = self._paid("premium_6m", "premium_user", "pay-premium")
        _, gift = self.shop.fulfill(premium.id)
        self.assertEqual(gift.kind, "premium")
        self.assertEqual(gift.months, 6)
        self.assertEqual(gift.customer_price, "22.50")
        again, replayed = self.shop.fulfill(premium.id)
        self.assertEqual(again.status, "fulfilled")
        self.assertEqual(replayed.months, 6)

    def test_fulfill_before_payment_is_rejected(self):
        order = self.shop.create_order("premium_12m", "buyer_one")
        with self.assertRaises(ShopError) as caught:
            self.shop.fulfill(order.id)
        self.assertEqual(caught.exception.code, "not_paid")
        self.assertEqual(self.shop.get_order(order.id).status, "pending_payment")

    def test_missing_price_blocks_the_order(self):
        self.shop.prices = PriceBook(Decimal("0"), "USDT", {})
        with self.assertRaises(ShopError) as caught:
            self.shop.create_order("stars_50", "buyer_one")
        self.assertEqual(caught.exception.code, "price_unavailable")
        self.assertFalse(self.shop.catalog()["products"][0]["priced"])

    def test_username_rules(self):
        with self.assertRaises(ShopError):
            self.shop.create_order("stars_50", "ab")
        with self.assertRaises(ShopError):
            self.shop.create_order("stars_50", 12345)
        with self.assertRaises(ShopError) as missing:
            self.shop.create_order("nope", "buyer_one")
        self.assertEqual(missing.exception.status, 404)

    def _paid(self, product_id, username, charge_id):
        order = self.shop.create_order(product_id, username)
        return self.shop.confirm_payment(order.id, charge_id)


class FragmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OrderStore(Path(self.tmp.name) / "orders.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_live_stars_poll_until_completed(self):
        script = Script(
            [
                (200, {"success": True, "recipient": "hash-1", "name": "Alex"}),
                (202, {"order_id": "550e8400-e29b-41d4-a716-446655440000", "status": "pending", "username": "alexdev", "quantity": 100, "amount": 1.08, "created_at": "2026-11-04T00:00:00Z"}),
                (200, {"order_id": "550e8400-e29b-41d4-a716-446655440000", "status": "completed", "username": "alexdev", "quantity": 100, "amount": 1.08, "created_at": "2026-11-04T00:00:00Z"}),
            ]
        )
        shop = make_shop(
            self.store,
            mode="live",
            fragment=FragmentClient("test-token", "https://fragment.test/api", opener=script),
        )
        order = shop.confirm_payment(shop.create_order("stars_100", "alexdev").id, "pay-1")
        delivering, pending = shop.fulfill(order.id)
        self.assertEqual(delivering.status, "delivering")
        self.assertEqual(pending.fragment_status, "pending")
        self.assertEqual(pending.fragment_amount, "1.08")
        self.assertEqual(script.calls[0]["url"], "https://fragment.test/api/star/recipient/search?username=alexdev&quantity=100")
        self.assertEqual(script.calls[1]["body"]["quantity"], 100)
        self.assertEqual(script.calls[1]["body"]["wallet_type"], "USDT")
        self.assertEqual(script.calls[1]["body"]["recipient_hash"], "hash-1")
        self.assertEqual(script.calls[1]["headers"]["API-Key"], "test-token")
        self.assertEqual(script.calls[1]["headers"]["Idempotency-Key"], order.id)
        done, delivery = shop.fulfill(order.id)
        self.assertEqual(done.status, "fulfilled")
        self.assertEqual(delivery.fragment_status, "completed")
        self.assertEqual(script.calls[2]["method"], "GET")
        self.assertTrue(script.calls[2]["url"].endswith("/orders/550e8400-e29b-41d4-a716-446655440000"))
        self.assertEqual(len(script.calls), 3)

    def test_live_premium_completes_immediately(self):
        script = Script(
            [
                (200, {"success": True, "recipient": "hash-p", "name": "Alex"}),
                (202, {"order_id": "550e8400-e29b-41d4-a716-446655440001", "status": "completed", "username": "alexdev", "months": 3, "amount": 10.5, "created_at": "2026-11-04T00:00:00Z"}),
            ]
        )
        shop = make_shop(self.store, mode="live", fragment=FragmentClient("test-token", "https://fragment.test", opener=script))
        order = shop.confirm_payment(shop.create_order("premium_3m", "alexdev").id, "pay-p")
        done, delivery = shop.fulfill(order.id)
        self.assertEqual(done.status, "fulfilled")
        self.assertEqual(delivery.months, 3)
        self.assertEqual(delivery.fragment_amount, "10.50")
        self.assertEqual(script.calls[1]["url"], "https://fragment.test/orders/premium")
        self.assertEqual(script.calls[1]["body"]["months"], 3)

    def test_missing_token_and_insufficient_balance_do_not_fulfill(self):
        shop = make_shop(self.store, mode="live", fragment=FragmentClient("", "https://fragment.test"))
        order = shop.confirm_payment(shop.create_order("stars_50", "alexdev").id, "pay-no")
        with self.assertRaises(ShopError) as missing:
            shop.fulfill(order.id)
        self.assertEqual(missing.exception.code, "missing_token")
        self.assertEqual(shop.get_order(order.id).status, "paid")

        script = Script([(402, {"message": "Insufficient balance"})])
        shop.fragment = FragmentClient("test-token", "https://fragment.test", opener=script)
        with self.assertRaises(ShopError) as broke:
            shop.fulfill(order.id)
        self.assertEqual(broke.exception.code, "insufficient_balance")
        self.assertEqual(shop.get_order(order.id).status, "paid")

    def test_failed_fragment_order_is_not_delivered(self):
        script = Script(
            [
                (200, {"success": True, "recipient": "hash-1", "name": "Alex"}),
                (202, {"order_id": "550e8400-e29b-41d4-a716-446655440002", "status": "processing", "username": "alexdev", "quantity": 50, "amount": 0.5, "created_at": "2026-11-04T00:00:00Z"}),
                (200, {"order_id": "550e8400-e29b-41d4-a716-446655440002", "status": "failed", "username": "alexdev", "quantity": 50, "amount": 0.5, "error": "recipient left", "created_at": "2026-11-04T00:00:00Z"}),
            ]
        )
        shop = make_shop(self.store, mode="live", fragment=FragmentClient("test-token", "https://fragment.test", opener=script))
        order = shop.confirm_payment(shop.create_order("stars_50", "alexdev").id, "pay-f")
        delivering, _ = shop.fulfill(order.id)
        self.assertEqual(delivering.status, "delivering")
        with self.assertRaises(ShopError) as caught:
            shop.fulfill(order.id)
        self.assertEqual(caught.exception.code, "fragment_failed")
        saved = shop.get_order(order.id)
        self.assertEqual(saved.status, "failed")
        self.assertEqual(saved.error_message, "recipient left")

    def test_old_user_id_schema_is_replaced(self):
        path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE orders (id TEXT PRIMARY KEY, recipient_user_id INTEGER)")
        conn.commit()
        conn.close()
        store = OrderStore(path)
        shop = make_shop(store)
        order = shop.create_order("stars_50", "alexdev")
        self.assertEqual(order.recipient_username, "alexdev")
        store.close()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shop = make_shop(OrderStore(Path(self.tmp.name) / "orders.db"))
        self.server = make_server("127.0.0.1", 0, self.shop)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.shop.store.close()
        self.tmp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_mini_app_page_is_served(self):
        request = urllib.request.Request(self.base + "/app")
        with urllib.request.urlopen(request) as response:
            body = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers.get("Content-Type", ""))
        self.assertIn("Telegram Stars", body)
        self.assertIn("К оплате", body)

    def test_http_dry_run_purchase(self):
        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["markup_percent"], "25")

        status, catalog = self.request("GET", "/catalog")
        self.assertEqual(status, 200)
        ids = {item["id"] for item in catalog["products"]}
        self.assertIn("premium_12m", ids)
        self.assertIn("stars_500", ids)

        status, created = self.request(
            "POST",
            "/orders",
            {"product_id": "stars_100", "recipient_username": "@AlexDev"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["customer_amount"], "1.25")
        self.assertEqual(created["recipient_username"], "alexdev")
        order_id = created["id"]

        status, blocked = self.request("POST", f"/orders/{order_id}/fulfill", {})
        self.assertEqual(blocked["error"], "not_paid")

        status, paid = self.request("POST", f"/orders/{order_id}/pay", {"charge_id": "demo-charge"})
        self.assertEqual(paid["status"], "paid")

        status, done = self.request("POST", f"/orders/{order_id}/fulfill", {})
        self.assertEqual(status, 200)
        self.assertEqual(done["status"], "fulfilled")
        self.assertEqual(done["delivery"]["provider"], "fragment")
        self.assertEqual(done["delivery"]["quantity"], 100)
        self.assertEqual(done["delivery"]["mode"], "dry-run")

        status, rejected = self.request("POST", "/orders", {"product_id": "premium_3m", "recipient_user_id": 1001})
        self.assertEqual(status, 400)
        self.assertEqual(rejected["error"], "bad_username")

    def test_live_http_does_not_spend_without_admin_token(self):
        script = Script([])
        self.shop.mode = "live"
        self.shop.fragment = FragmentClient("test-token", "https://fragment.test", opener=script)
        status, created = self.request("POST", "/orders", {"product_id": "stars_50", "recipient_username": "alexdev"})
        self.assertEqual(status, 201)
        status, denied = self.request("POST", f"/orders/{created['id']}/pay", {"charge_id": "x"})
        self.assertEqual(status, 503)
        self.assertEqual(denied["error"], "missing_admin_token")
        self.assertEqual(script.calls, [])

        self.shop.admin_token = "admin-secret"
        status, unauthorized = self.request("POST", f"/orders/{created['id']}/pay", {"charge_id": "x"})
        self.assertEqual(status, 401)
        status, paid = self.request(
            "POST",
            f"/orders/{created['id']}/pay",
            {"charge_id": "x"},
            {"X-Admin-Token": "admin-secret"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(paid["status"], "paid")
        self.assertEqual(script.calls, [])


class ConfigTests(unittest.TestCase):
    def test_load_shop_reads_markup_without_calling_fragment(self):
        keys = [
            "SHOP_MODE",
            "SHOP_WALLET",
            "SHOP_MARKUP_PERCENT",
            "SHOP_STAR_UNIT_COST",
            "SHOP_PREMIUM_COST_3M",
            "SHOP_PREMIUM_COST_6M",
            "SHOP_PREMIUM_COST_12M",
            "SHOP_ADMIN_TOKEN",
            "API_TOKEN",
            "FRAGMENT_API_KEY",
            "FRAGMENT_API_BASE",
        ]
        saved = {key: os.environ.get(key) for key in keys}
        tmp = tempfile.TemporaryDirectory()
        try:
            os.environ["SHOP_MODE"] = "dry-run"
            os.environ["SHOP_WALLET"] = "USDT"
            os.environ["SHOP_MARKUP_PERCENT"] = "15"
            os.environ["SHOP_STAR_UNIT_COST"] = "0,02"
            os.environ["SHOP_PREMIUM_COST_3M"] = "10"
            os.environ["SHOP_PREMIUM_COST_6M"] = "18"
            os.environ["SHOP_PREMIUM_COST_12M"] = "30"
            os.environ["SHOP_ADMIN_TOKEN"] = ""
            os.environ.pop("API_TOKEN", None)
            os.environ.pop("FRAGMENT_API_KEY", None)
            shop = load_shop(OrderStore(Path(tmp.name) / "orders.db"))
            quote = shop.prices.quote(get_product("stars_50"))
            self.assertEqual(shop.prices.markup_percent, Decimal("15"))
            self.assertEqual(quote.cost_text, "1.00")
            self.assertEqual(quote.customer_text, "1.15")
            shop.store.close()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
