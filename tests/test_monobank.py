import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from shop.checkout import create_invoice, handle_request, invoice_status, monobank_webhook
from shop.errors import ShopError
from shop.fragment import FragmentClient
from shop.monobank import Monobank, _verify_openssl, nbu_usd_rate, verify_sign
from shop.orders import OrderStore
from shop.pricing import PriceBook
from shop.server import make_server
from shop.service import Shop


TOKEN = "crypto-test-token"
FRAGMENT = "fragment-test-key"
MONO = "mono-test-token"
SAMPLE_PUBKEY = base64.b64decode(
    "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUZrd0V3WUhLb1pJemowQ0FRWUlLb1pJemowREFRY0RRZ0FFQUc1LzZ3NnZubGJZb0ZmRHlYWE4vS29CbVVjTgo3NWJSUWg4MFBhaEdldnJoanFCQnI3OXNSS0JSbnpHODFUZVQ5OEFOakU1c0R3RmZ5Znhub0ZJcmZBPT0KLS0tLS1FTkQgUFVCTElDIEtFWS0tLS0tCg=="
)
SAMPLE_SIGN = "MEUCIQC/mVKhi8FKoayul2Mim3E2oaIOCNJk5dEXxTqbkeJSOQIgOM0hsW0qcP2H8iXy1aQYpmY0SJWEaWur7nQXlKDCFxA="
SAMPLE_BODY = b"""{
  "invoiceId": "p2_9ZgpZVsl3",
  "status": "created",
  "failureReason": "string",
  "amount": 4200,
  "ccy": 980,
  "finalAmount": 4200,
  "createdDate": "2019-08-24T14:15:22Z",
  "modifiedDate": "2019-08-24T14:15:22Z",
  "reference": "84d0070ee4e44667b31371d8f8813947",
  "cancelList": [
    {
      "status": "processing",
      "amount": 4200,
      "ccy": 980,
      "createdDate": "2019-08-24T14:15:22Z",
      "modifiedDate": "2019-08-24T14:15:22Z",
      "approvalCode": "662476",
      "rrn": "060189181768",
      "extRef": "635ace02599849e981b2cd7a65f417fe"
    }
  ]
}"""


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
        "SHOP_NOTIFY_FILE",
        "MONOBANK_TOKEN",
        "MONO_TOKEN",
        "MONOBANK_API_KEY",
        "MONOBANK_API_TOKEN",
        "MONOBANK_CCY",
    }
    saved = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ["CRYPTO_PAY_TOKEN"] = TOKEN
        os.environ["FRAGMENT_API_KEY"] = FRAGMENT
        os.environ["MONOBANK_TOKEN"] = MONO
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


class RecordingMono:
    def __init__(self):
        self.created = []
        self.invoice = None
        self.fetched = 0

    def create_invoice(self, minor, ccy, reference, destination, redirect_url, webhook_url):
        self.created.append(
            {
                "minor": minor,
                "ccy": ccy,
                "reference": reference,
                "destination": destination,
                "redirect_url": redirect_url,
                "webhook_url": webhook_url,
            }
        )
        self.invoice = {
            "invoiceId": "p2_test",
            "status": "created",
            "amount": minor,
            "ccy": ccy,
            "reference": reference,
            "pageUrl": "https://pay.mbnk.biz/p2_test",
        }
        return {"invoiceId": "p2_test", "pageUrl": "https://pay.mbnk.biz/p2_test"}

    def get_invoice(self, invoice_id):
        self.fetched += 1
        if self.invoice is None or invoice_id != "p2_test":
            raise ShopError("invoice_not_found", "Счёт не найден.", 404)
        return dict(self.invoice)

    def public_key(self):
        raise AssertionError("public key")


def _fragment(responses):
    return FragmentClient(FRAGMENT, "https://fragment.test", opener=Script(responses))


class _QuietTelegram:
    def __init__(self, token, opener=None):
        self.token = token

    def send_message(self, chat_id, text, link_button=None, **kwargs):
        return None


def _sign(private_key, body: bytes) -> str:
    signature = private_key.sign(body, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode()


def _init_data(token: str, user_id: int) -> str:
    pairs = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id}, separators=(",", ":"))}
    check = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class MonobankSignatureTests(unittest.TestCase):
    def test_published_sample_verifies(self):
        self.assertTrue(verify_sign(SAMPLE_PUBKEY, SAMPLE_SIGN, SAMPLE_BODY))
        self.assertTrue(_verify_openssl(SAMPLE_PUBKEY, base64.b64decode(SAMPLE_SIGN), SAMPLE_BODY))
        self.assertFalse(verify_sign(SAMPLE_PUBKEY, SAMPLE_SIGN, SAMPLE_BODY + b" "))
        self.assertFalse(verify_sign(SAMPLE_PUBKEY, "%%%", SAMPLE_BODY))


class MonobankClientTests(unittest.TestCase):
    def test_create_posts_minor_units_and_reads_status(self):
        pem = SAMPLE_PUBKEY
        script = Script(
            [
                (200, {"invoiceId": "p2_test", "pageUrl": "https://pay.mbnk.biz/p2_test"}),
                (200, {"invoiceId": "p2_test", "status": "success", "amount": 3760, "ccy": 980}),
                (200, {"key": base64.b64encode(pem).decode()}),
            ]
        )
        client = Monobank("secret-token", opener=script, base_url="https://mono.test")
        created = client.create_invoice(3760, 980, "ref", "50 звёзд для @alexdev", "https://shop.example", "https://shop.example/api/monobank")
        self.assertEqual(created["pageUrl"], "https://pay.mbnk.biz/p2_test")
        sent = script.calls[0]
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["url"], "https://mono.test/api/merchant/invoice/create")
        self.assertEqual(sent["headers"]["X-Token"], "secret-token")
        self.assertEqual(sent["headers"]["User-Agent"], "TelegramShop/1.0")
        self.assertEqual(sent["body"]["amount"], 3760)
        self.assertEqual(sent["body"]["ccy"], 980)
        self.assertEqual(sent["body"]["merchantPaymInfo"]["reference"], "ref")
        self.assertEqual(sent["body"]["merchantPaymInfo"]["destination"], "50 звёзд для @alexdev")
        self.assertEqual(sent["body"]["webHookUrl"], "https://shop.example/api/monobank")
        self.assertNotIn("secret-token", sent["body"]["merchantPaymInfo"]["destination"])
        status = client.get_invoice("p2_test")
        self.assertEqual(status["status"], "success")
        self.assertIn("invoiceId=p2_test", script.calls[1]["url"])
        self.assertEqual(client.public_key(), pem)

    def test_nbu_rate_is_read_from_the_directory(self):
        script = Script([(200, [{"rate": 41.25, "cc": "USD"}])])
        self.assertEqual(nbu_usd_rate(script), Decimal("41.25"))

    def test_api_error_text_is_returned_without_the_token(self):
        script = Script([(400, {"errCode": "BAD", "errText": "amount is invalid"})])
        client = Monobank("secret-token", opener=script, base_url="https://mono.test")
        with self.assertRaises(ShopError) as caught:
            client.create_invoice(1, 980, "ref", "text", "https://shop.example", "https://shop.example/api/monobank")
        self.assertEqual(caught.exception.code, "monobank_rejected")
        self.assertIn("amount is invalid", caught.exception.message)
        self.assertNotIn("secret-token", caught.exception.message)
        denied = Monobank("secret-token", opener=Script([(403, {"errCode": "FORBIDDEN", "errText": "forbidden"})]), base_url="https://mono.test")
        with self.assertRaises(ShopError) as forbidden:
            denied.create_invoice(1, 980, "ref", "text", "https://shop.example", "https://shop.example/api/monobank")
        self.assertIn("MONOBANK_TOKEN", forbidden.exception.message)
        self.assertNotIn("secret-token", forbidden.exception.message)


class CardCheckoutTests(unittest.TestCase):
    def test_uah_invoice_uses_the_given_rate_and_success_sends_stars(self):
        script = Script(
            [
                (200, {"success": True, "recipient": "hash-1", "name": "Alex"}),
                (202, {"order_id": "550e8400-e29b-41d4-a716-446655440000", "status": "completed", "quantity": 50}),
            ]
        )
        fragment = FragmentClient(FRAGMENT, "https://fragment.test", opener=script)
        mono = RecordingMono()
        with purchase_env(CRYPTO_PAY_TOKEN=None):
            created = create_invoice(
                {"kind": "stars", "recipient": "alexdev", "amount": 50, "pay": "card"},
                monobank=mono,
                rate=Decimal("40"),
            )
            self.assertEqual(created["charge"], "37.60 UAH")
            self.assertEqual(created["pay_url"], "https://pay.mbnk.biz/p2_test")
            self.assertEqual(mono.created[0]["minor"], 3760)
            self.assertEqual(mono.created[0]["ccy"], 980)
            self.assertTrue(mono.created[0]["webhook_url"].endswith("/api/monobank"))
            self.assertNotIn("{", mono.created[0]["destination"])
            waiting = invoice_status("p2_test", pay="card", monobank=mono, fragment=fragment)
            self.assertFalse(waiting["paid"])
            self.assertTrue(waiting["retry"])
            self.assertIn("37.60 UAH", waiting["message"])
            self.assertEqual(script.calls, [])
            mono.invoice["status"] = "success"
            done = invoice_status("p2_test", pay="card", monobank=mono, fragment=fragment)
        self.assertTrue(done["delivered"])
        self.assertEqual(done["message"], "Звёзды отправлены на @alexdev.")
        self.assertEqual(script.calls[1]["headers"]["Idempotency-Key"], "monobank-p2_test")
        self.assertEqual(script.calls[1]["body"]["quantity"], 50)
        self.assertEqual(script.calls[1]["body"]["wallet_type"], "USDT")

    def test_dollar_invoice_skips_the_exchange_rate(self):
        mono = RecordingMono()
        with purchase_env(MONOBANK_CCY="840"):
            created = create_invoice(
                {"kind": "stars", "recipient": "alexdev", "amount": 50, "pay": "card"},
                monobank=mono,
            )
        self.assertEqual(created["charge"], "0.94 USD")
        self.assertEqual(mono.created[0]["minor"], 94)
        self.assertEqual(mono.created[0]["ccy"], 840)

    def test_token_alias_is_accepted(self):
        mono = RecordingMono()
        with purchase_env(MONOBANK_TOKEN=None, MONO_TOKEN="alias-token", MONOBANK_CCY="840"):
            created = create_invoice(
                {"kind": "premium", "recipient": "alexdev", "months": 3, "pay": "card"},
                monobank=mono,
            )
        self.assertEqual(created["charge"], "14.99 USD")
        self.assertIn("Premium", mono.created[0]["destination"])

    def test_processing_wrong_amount_and_refund_do_not_send_stars(self):
        fragment = _fragment([])
        mono = RecordingMono()
        with purchase_env(MONOBANK_CCY="840"):
            create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 50, "pay": "card"}, monobank=mono)
            mono.invoice["status"] = "processing"
            pending = invoice_status("p2_test", pay="card", monobank=mono, fragment=fragment)
            self.assertFalse(pending["paid"])
            self.assertTrue(pending["retry"])
            mono.invoice["status"] = "success"
            mono.invoice["amount"] = 1
            with self.assertRaises(ShopError) as mismatch:
                invoice_status("p2_test", pay="card", monobank=mono, fragment=fragment)
            self.assertEqual(mismatch.exception.code, "amount_mismatch")
            mono.invoice["amount"] = 94
            mono.invoice["finalAmount"] = 10
            with self.assertRaises(ShopError) as refunded:
                invoice_status("p2_test", pay="card", monobank=mono, fragment=fragment)
            self.assertEqual(refunded.exception.code, "amount_mismatch")
            mono.invoice["status"] = "failure"
            failed = invoice_status("p2_test", pay="card", monobank=mono, fragment=fragment)
        self.assertFalse(failed["retry"])
        self.assertIn("отклонил", failed["message"])
        self.assertEqual(fragment._opener.calls, [])

    def test_bad_signature_does_not_ask_monobank_for_the_invoice(self):
        mono = RecordingMono()
        with purchase_env(MONOBANK_CCY="840"):
            create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 50, "pay": "card"}, monobank=mono)
            before = mono.fetched
            with self.assertRaises(ShopError) as caught:
                monobank_webhook(b'{"invoiceId":"p2_test","amount":1}', "bm9wZQ==", monobank=mono, pubkey=SAMPLE_PUBKEY)
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(mono.fetched, before)

    def test_valid_webhook_trusts_the_refetched_invoice(self):
        private = ec.generate_private_key(ec.SECP256R1())
        public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        script = Script(
            [
                (200, {"success": True, "recipient": "hash-1", "name": "Alex"}),
                (202, {"order_id": "550e8400-e29b-41d4-a716-446655440000", "status": "completed", "quantity": 50}),
            ]
        )
        fragment = FragmentClient(FRAGMENT, "https://fragment.test", opener=script)
        mono = RecordingMono()
        bot = "123456:AAFakeTokenForTestsOnly0001"
        sent = []
        folder = tempfile.TemporaryDirectory()
        try:
            with purchase_env(MONOBANK_CCY="840", BOT_TOKEN=bot, SHOP_NOTIFY_FILE=str(Path(folder.name) / "sent.txt")), patch(
                "shop.checkout.TelegramApi", _QuietTelegram
            ):
                create_invoice(
                    {
                        "kind": "stars",
                        "recipient": "alexdev",
                        "amount": 50,
                        "pay": "card",
                        "init_data": _init_data(bot, 4242),
                    },
                    monobank=mono,
                )
                mono.invoice["status"] = "success"
                raw = b'{"invoiceId":"p2_test","status":"created","amount":1}'
                done = monobank_webhook(
                    raw,
                    _sign(private, raw),
                    monobank=mono,
                    pubkey=public,
                    fragment=fragment,
                    sender=lambda chat, text: sent.append((chat, text)),
                )
        finally:
            folder.cleanup()
        self.assertTrue(done["delivered"])
        self.assertEqual(script.calls[1]["body"]["quantity"], 50)
        self.assertEqual(sent, [(4242, "⭐ 50 звёзд → @alexdev")])

    def test_missing_token_dry_run_and_heleket_do_not_charge(self):
        mono = RecordingMono()
        with purchase_env(MONOBANK_TOKEN=None):
            with self.assertRaises(ShopError) as missing:
                create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 50, "pay": "card"}, monobank=mono)
        self.assertIn("MONOBANK_TOKEN", missing.exception.message)
        with purchase_env(SHOP_MODE="dry-run"):
            with self.assertRaises(ShopError) as dry:
                create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 50, "pay": "card"}, monobank=mono)
        self.assertIn("dry-run", dry.exception.message)
        with purchase_env():
            with self.assertRaises(ShopError) as heleket:
                create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 50, "pay": "heleket"}, monobank=mono)
        self.assertEqual(heleket.exception.code, "bad_pay")
        self.assertEqual(mono.created, [])

    def test_route_rejects_a_bad_signature_before_delivery(self):
        class Idle:
            def __init__(self, token, opener=None, base_url=None):
                self.token = token

            def public_key(self):
                return SAMPLE_PUBKEY

            def get_invoice(self, invoice_id):
                raise AssertionError(invoice_id)

        with purchase_env(), patch("shop.checkout.Monobank", Idle):
            status, body = handle_request(
                "POST",
                "/api/monobank",
                "",
                {"X-Sign": "bm9wZQ=="},
                b'{"invoiceId":"p2_test"}',
            )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")


class CardRouteTests(unittest.TestCase):
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

    def request(self, method, path, raw=None, headers=None):
        request = urllib.request.Request(self.base + path, data=raw, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_missing_token_is_a_clear_error(self):
        with purchase_env(MONOBANK_TOKEN=None):
            status, body = self.request("POST", "/api/monobank", raw=b"{}", headers={"X-Sign": "bm9wZQ=="})
            code, invoice = self.request("GET", "/api/invoice?id=p2_test&pay=card")
        self.assertEqual(status, 503)
        self.assertIn("MONOBANK_TOKEN", body["message"])
        self.assertEqual(code, 503)
        self.assertEqual(invoice["error"], "purchases_off")


if __name__ == "__main__":
    unittest.main()
