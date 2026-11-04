import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from decimal import Decimal

from shop.checkout import (
    create_invoice,
    cryptobot_webhook,
    invoice_status,
    parse_order,
    star_price,
    verify_webhook,
)
from shop.cryptopay import CryptoPay
from shop.errors import ShopError
from shop.fragment import FragmentClient
from shop.orders import OrderStore
from shop.pricing import PriceBook
from shop.server import make_server
from shop.service import Shop


TOKEN = "crypto-test-token"
FRAGMENT = "fragment-test-key"


@contextmanager
def purchase_env(**extra):
    keys = {
        "SHOP_MODE",
        "CRYPTO_PAY_TOKEN",
        "FRAGMENT_API_KEY",
        "API_TOKEN",
        "BOT_TOKEN",
        "SHOP_WALLET",
        "WEBAPP_URL",
        "CRYPTO_PAY_BASE",
        "FRAGMENT_API_BASE",
    }
    saved = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ["CRYPTO_PAY_TOKEN"] = TOKEN
        os.environ["FRAGMENT_API_KEY"] = FRAGMENT
        os.environ["SHOP_WALLET"] = "USDT"
        for key, value in extra.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Script:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if not self.responses:
            raise AssertionError(f"unexpected {method} {url}")
        return self.responses.pop(0)


class RecordingPay:
    def __init__(self):
        self.created = []
        self.invoice = None

    def create_invoice(self, amount, description, payload, *, paid_btn_url, hidden_message):
        self.created.append(
            {
                "amount": amount,
                "description": description,
                "payload": payload,
                "paid_btn_url": paid_btn_url,
                "hidden_message": hidden_message,
            }
        )
        self.invoice = {
            "invoice_id": 77,
            "status": "active",
            "asset": "USDT",
            "amount": amount,
            "payload": payload,
            "mini_app_invoice_url": "https://t.me/CryptoBot/app?startapp=IV77",
            "bot_invoice_url": "https://t.me/CryptoBot?start=IV77",
        }
        return dict(self.invoice)

    def get_invoice(self, invoice_id):
        if self.invoice is None or str(invoice_id) != "77":
            raise ShopError("invoice_not_found", "Счёт Crypto Bot не найден.", 404)
        return dict(self.invoice)


class PriceTests(unittest.TestCase):
    def test_screen_prices_are_recomputed(self):
        self.assertEqual(star_price(50), "0.94")
        self.assertEqual(star_price(1000), "18.75")
        self.assertEqual(star_price(75), "1.41")
        self.assertEqual(parse_order({"kind": "premium", "recipient": "@AlexDev", "months": 12})["total"], "36.24")
        self.assertEqual(parse_order({"kind": "stars", "recipient": "alexdev", "amount": "250"})["recipient"], "alexdev")

    def test_purchase_stays_off_without_the_fragment_key(self):
        with purchase_env(FRAGMENT_API_KEY=None, API_TOKEN="123456789:AAFakeTokenForTestsOnly0001"):
            with self.assertRaises(ShopError) as caught:
                create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 50})
        self.assertEqual(caught.exception.code, "purchases_off")

    def test_dry_run_does_not_create_an_invoice(self):
        with purchase_env(SHOP_MODE="dry-run"):
            with self.assertRaises(ShopError) as caught:
                create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 50}, cryptopay=RecordingPay())
        self.assertIn("dry-run", caught.exception.message)


class CheckoutTests(unittest.TestCase):
    def test_paid_invoice_sends_stars_once(self):
        script = Script(
            [
                (200, {"success": True, "recipient": "hash-1", "name": "Alex"}),
                (202, {"order_id": "550e8400-e29b-41d4-a716-446655440000", "status": "pending", "quantity": 50}),
                (200, {"order_id": "550e8400-e29b-41d4-a716-446655440000", "status": "pending", "quantity": 50}),
                (200, {"success": True, "recipient": "hash-1", "name": "Alex"}),
                (202, {"order_id": "550e8400-e29b-41d4-a716-446655440000", "status": "pending", "quantity": 50}),
                (200, {"order_id": "550e8400-e29b-41d4-a716-446655440000", "status": "completed", "quantity": 50}),
            ]
        )
        fragment = FragmentClient(FRAGMENT, "https://fragment.test", opener=script)
        pay = RecordingPay()
        with purchase_env():
            created = create_invoice({"kind": "stars", "recipient": "@alexdev", "amount": 50, "total": "0.01"}, cryptopay=pay)
            self.assertEqual(created["pay_url"], "https://t.me/CryptoBot/app?startapp=IV77")
            self.assertEqual(created["total"], "0.94")
            self.assertEqual(pay.created[0]["amount"], "0.94")
            self.assertIn("@alexdev", pay.created[0]["description"])
            self.assertTrue(pay.created[0]["paid_btn_url"].startswith("https://"))
            waiting = invoice_status(created["invoice_id"], cryptopay=pay, fragment=fragment)
            self.assertFalse(waiting["paid"])
            self.assertEqual(script.calls, [])
            pay.invoice["status"] = "paid"
            pending = invoice_status("77", cryptopay=pay, fragment=fragment)
            self.assertTrue(pending["paid"])
            self.assertFalse(pending["delivered"])
            self.assertTrue(pending["retry"])
            self.assertEqual(script.calls[1]["headers"]["Idempotency-Key"], "cryptopay-77")
            self.assertEqual(script.calls[1]["body"]["wallet_type"], "USDT")
            self.assertEqual(script.calls[1]["body"]["quantity"], 50)
            done = invoice_status(77, cryptopay=pay, fragment=fragment)
        self.assertTrue(done["delivered"])
        self.assertEqual(done["message"], "Звёзды отправлены на @alexdev.")
        self.assertEqual(script.calls[4]["headers"]["Idempotency-Key"], "cryptopay-77")

    def test_bad_signature_and_wrong_amount_do_not_call_fragment(self):
        script = Script([])
        fragment = FragmentClient(FRAGMENT, "https://fragment.test", opener=script)
        pay = RecordingPay()
        with purchase_env():
            create_invoice({"kind": "premium", "recipient": "alexdev", "months": 3}, cryptopay=pay)
            pay.invoice["status"] = "paid"
            tampered = json.loads(pay.invoice["payload"])
            tampered["sig"] = "0" * 64
            pay.invoice["payload"] = json.dumps(tampered)
            with self.assertRaises(ShopError) as bad_sig:
                invoice_status(77, cryptopay=pay, fragment=fragment)
            self.assertEqual(bad_sig.exception.code, "bad_invoice")
            pay.invoice["payload"] = json.dumps(json.loads(pay.created[0]["payload"]))
            pay.invoice["amount"] = "1.00"
            with self.assertRaises(ShopError) as mismatch:
                invoice_status(77, cryptopay=pay, fragment=fragment)
            self.assertEqual(mismatch.exception.code, "amount_mismatch")
        self.assertEqual(script.calls, [])

    def test_string_amount_in_payload_still_verifies(self):
        script = Script(
            [
                (200, {"success": True, "recipient": "hash-p", "name": "Alex"}),
                (202, {"order_id": "550e8400-e29b-41d4-a716-446655440001", "status": "completed", "months": 6}),
            ]
        )
        fragment = FragmentClient(FRAGMENT, "https://fragment.test", opener=script)
        pay = RecordingPay()
        with purchase_env(SHOP_WALLET="TON"):
            create_invoice({"kind": "premium", "recipient": "alexdev", "months": 6}, cryptopay=pay)
            stored = json.loads(pay.invoice["payload"])
            stored["order"]["months"] = "6"
            pay.invoice["payload"] = json.dumps(stored)
            pay.invoice["status"] = "paid"
            done = invoice_status(77, cryptopay=pay, fragment=fragment)
        self.assertTrue(done["delivered"])
        self.assertEqual(done["message"], "Premium отправлен на @alexdev.")
        self.assertEqual(script.calls[1]["body"]["months"], 6)
        self.assertEqual(script.calls[1]["body"]["wallet_type"], "TON")
        self.assertEqual(len(script.calls), 2)

    def test_low_fragment_balance_keeps_the_payment_for_retry(self):
        script = Script([(402, {"message": "Insufficient balance"})])
        fragment = FragmentClient(FRAGMENT, "https://fragment.test", opener=script)
        pay = RecordingPay()
        with purchase_env():
            create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 100}, cryptopay=pay)
            pay.invoice["status"] = "paid"
            pending = invoice_status(77, cryptopay=pay, fragment=fragment)
        self.assertTrue(pending["paid"])
        self.assertFalse(pending["delivered"])
        self.assertTrue(pending["retry"])
        self.assertIn("USDT", pending["message"])

    def test_webhook_refetches_the_invoice(self):
        raw = json.dumps({"update_id": 1, "update_type": "invoice_paid", "payload": {"invoice_id": 77, "status": "paid"}}).encode()
        signature = _webhook_signature(raw, TOKEN)
        self.assertTrue(verify_webhook(raw, signature, TOKEN))
        self.assertFalse(verify_webhook(raw + b" ", signature, TOKEN))
        pay = RecordingPay()
        script = Script([])
        fragment = FragmentClient(FRAGMENT, "https://fragment.test", opener=script)
        with purchase_env():
            create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 50}, cryptopay=pay)
            waiting = cryptobot_webhook(raw, signature, cryptopay=pay, fragment=fragment)
            self.assertFalse(waiting["paid"])
            pay.invoice["status"] = "paid"
            done_script = Script(
                [
                    (200, {"success": True, "recipient": "hash-1", "name": "Alex"}),
                    (202, {"order_id": "550e8400-e29b-41d4-a716-446655440000", "status": "completed", "quantity": 50}),
                ]
            )
            done = cryptobot_webhook(
                raw,
                signature,
                cryptopay=pay,
                fragment=FragmentClient(FRAGMENT, "https://fragment.test", opener=done_script),
            )
            with self.assertRaises(ShopError) as caught:
                cryptobot_webhook(raw, "bad", cryptopay=pay, fragment=fragment)
        self.assertTrue(done["delivered"])
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(script.calls, [])


class CryptoPayClientTests(unittest.TestCase):
    def test_create_and_lookup_use_the_token_header(self):
        invoice = {
            "invoice_id": 9,
            "status": "active",
            "asset": "USDT",
            "amount": "0.94",
            "bot_invoice_url": "https://t.me/CryptoBot?start=IV9",
        }
        script = Script(
            [
                (200, {"ok": True, "result": invoice}),
                (200, {"ok": True, "result": [invoice]}),
                (200, {"ok": False, "error": "AMOUNT_INVALID"}),
            ]
        )
        client = CryptoPay("secret-token", opener=script, base_url="https://pay.test/api")
        created = client.create_invoice("0.94", "50 звёзд", "{}", paid_btn_url="https://shop.example", hidden_message="ok")
        self.assertEqual(created["invoice_id"], 9)
        self.assertEqual(script.calls[0]["headers"]["Crypto-Pay-API-Token"], "secret-token")
        self.assertEqual(script.calls[0]["body"]["asset"], "USDT")
        found = client.get_invoice(9)
        self.assertEqual(found["amount"], "0.94")
        self.assertEqual(script.calls[1]["body"]["invoice_ids"], "9")
        with self.assertRaises(ShopError) as caught:
            client.create_invoice("0", "x", "{}", paid_btn_url="https://shop.example", hidden_message="ok")
        self.assertEqual(caught.exception.code, "cryptopay_rejected")


class HttpCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shop = Shop(
            OrderStore(Path(self.tmp.name) / "orders.db"),
            PriceBook(Decimal("0"), "USDT", {}),
            FragmentClient("", "https://fragment.test"),
        )
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

    def request(self, method, path, payload=None, headers=None, raw=None):
        data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None and raw is None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_invoice_route_is_off_until_both_keys_exist(self):
        with purchase_env(CRYPTO_PAY_TOKEN=None, FRAGMENT_API_KEY=None):
            status, body = self.request("POST", "/api/invoice", {"kind": "stars", "recipient": "alexdev", "amount": 50})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "purchases_off")

    def test_http_invoice_pays_and_webhook_delivers(self):
        with purchase_env(), patch("shop.checkout.CryptoPay", FakePay), patch("shop.checkout.FragmentClient", FakeFragment):
            status, created = self.request("POST", "/api/invoice", {"kind": "stars", "recipient": "alexdev", "amount": 50})
            self.assertEqual(status, 201)
            self.assertEqual(created["total"], "0.94")
            self.assertTrue(created["pay_url"].startswith("https://t.me/"))
            paid, state = self.request("GET", "/api/invoice?id=77")
            self.assertEqual(paid, 200)
            self.assertTrue(state["delivered"])
            self.assertEqual(FakeFragment.created[0]["idempotency_key"], "cryptopay-77")
            raw = json.dumps({"update_type": "invoice_paid", "payload": {"invoice_id": 77}}).encode()
            signature = _webhook_signature(raw, TOKEN)
            code, hooked = self.request("POST", "/api/cryptobot", raw=raw, headers={"crypto-pay-api-signature": signature})
        self.assertEqual(code, 200)
        self.assertTrue(hooked["delivered"])
        self.assertEqual(len(FakeFragment.created), 2)


class FakePay:
    invoice = None

    def __init__(self, token, opener=None, base_url=None):
        self.token = token

    def create_invoice(self, amount, description, payload, *, paid_btn_url, hidden_message):
        FakePay.invoice = {
            "invoice_id": 77,
            "status": "active",
            "asset": "USDT",
            "amount": amount,
            "payload": payload,
            "mini_app_invoice_url": "https://t.me/CryptoBot/app?startapp=IV77",
        }
        return dict(FakePay.invoice)

    def get_invoice(self, invoice_id):
        data = dict(FakePay.invoice)
        data["status"] = "paid"
        return data


class FakeFragment:
    created = []

    def __init__(self, token, base_url=None, opener=None):
        self.token = token

    def search_stars(self, username, quantity):
        return "hash-1"

    def create_stars(self, **kwargs):
        FakeFragment.created.append(kwargs)
        return {"order_id": "ord-1", "status": "completed", "quantity": kwargs["quantity"]}

    def search_premium(self, username, months):
        raise AssertionError(months)

    def create_premium(self, **kwargs):
        raise AssertionError(kwargs)

    def get_order(self, order_id):
        raise AssertionError(order_id)


def _webhook_signature(raw: bytes, token: str) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, raw, hashlib.sha256).hexdigest()


if __name__ == "__main__":
    unittest.main()
