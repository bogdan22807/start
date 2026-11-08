import os
import unittest
from decimal import Decimal

from shop.checkout import create_invoice, invoice_status, purchases_blocked_reason
from shop.errors import ShopError
from shop.paystars import PaystarsClient, normalize_status
from tests.test_checkout import RecordingPay, Script, purchase_env

KEY = "paystars-test-key"
BASE = "https://paystars.test/api/v1"


def client(script):
    return PaystarsClient(KEY, BASE, opener=script)


def check_ok(username="alexdev"):
    return (200, {"valid": True, "username": username, "premium_active": False, "verification_token": "vt-1", "verification_expires_in": 180})


class PaystarsClientTests(unittest.TestCase):
    def test_headers_and_buy_body(self):
        script = Script([(200, {"ok": True, "order_id": 9001, "status": "processing", "quantity": 50, "cost": 0.76, "balance": 10, "currency": "USD"})])
        client(script).buy(
            kind="stars",
            username="alexdev",
            count=50,
            verification_token="vt-1",
            max_usd=Decimal("0.76"),
            idempotency_key="cryptopay-77",
        )
        call = script.calls[0]
        self.assertEqual(call["url"], BASE + "/stars/buy")
        self.assertEqual(call["headers"]["X-API-Key"], KEY)
        self.assertEqual(call["headers"]["Idempotency-Key"], "cryptopay-77")
        self.assertEqual(call["body"], {"username": "alexdev", "verification_token": "vt-1", "currency": "USD", "max_usd": 0.76, "quantity": 50})

    def test_premium_already_active(self):
        script = Script([(200, {"valid": False, "premium_active": True, "verification_token": None})])
        with self.assertRaises(ShopError) as caught:
            client(script).check("alexdev", "premium")
        self.assertEqual(caught.exception.code, "premium_active")

    def test_errors(self):
        cases = [
            ((402, {"detail": {"code": "insufficient_balance", "message": "StarKerak balance is insufficient."}}), "insufficient_balance"),
            ((409, {"detail": {"code": "price_changed", "message": "price changed"}}), "price_changed"),
            ((409, {"detail": {"code": "verification_used", "message": "used"}}), "idempotency_conflict"),
            ((401, {"detail": {"code": "bad_key", "message": "no"}}), "unauthorized"),
            ((422, {"detail": {"code": "username_unavailable", "message": "no user"}}), "recipient_not_found"),
            ((503, {}), "fragment_unavailable"),
        ]
        for response, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ShopError) as caught:
                    client(Script([response])).account()
                self.assertEqual(caught.exception.code, code)

    def test_status_words(self):
        self.assertEqual(normalize_status("completed"), "completed")
        self.assertEqual(normalize_status("DONE"), "completed")
        self.assertEqual(normalize_status("failed"), "failed")
        self.assertEqual(normalize_status("processing"), "processing")
        self.assertEqual(normalize_status(None), "processing")


class PaystarsCheckoutTests(unittest.TestCase):
    def test_paid_invoice_buys_once_then_only_reads_the_order(self):
        script = Script(
            [
                check_ok(),
                (200, {"usd": 0.76}),
                (200, {"ok": True, "order_id": 9001, "status": "processing", "quantity": 50, "cost": 0.76, "balance": 9.24, "currency": "USD"}),
                (200, {"order_id": 9001, "status": "processing"}),
                (200, {"order_id": 9001, "status": "completed"}),
            ]
        )
        stars = client(script)
        pay = RecordingPay()
        with purchase_env(PAYSTARS_API_KEY=KEY, FRAGMENT_API_KEY=None):
            created = create_invoice({"kind": "stars", "recipient": "@alexdev", "amount": 50}, cryptopay=pay)
            self.assertEqual(created["total"], "0.94")
            pay.invoice["status"] = "paid"
            first = invoice_status("77", cryptopay=pay, fragment=stars)
            self.assertTrue(first["paid"])
            self.assertFalse(first["delivered"])
            self.assertTrue(first["retry"])
            self.assertEqual(script.calls[2]["headers"]["Idempotency-Key"], "cryptopay-77")
            self.assertEqual(script.calls[2]["body"]["max_usd"], 0.76)
            second = invoice_status("77", cryptopay=pay, fragment=stars)
            self.assertFalse(second["delivered"])
            done = invoice_status("77", cryptopay=pay, fragment=stars)
        self.assertTrue(done["delivered"])
        self.assertEqual(done["message"], "Звёзды отправлены на @alexdev.")
        self.assertEqual([c["url"] for c in script.calls[3:]], [BASE + "/order/9001", BASE + "/order/9001"])

    def test_completed_right_away(self):
        script = Script(
            [
                check_ok(),
                (200, {"usd": 12.5}),
                (200, {"ok": True, "order_id": 42, "status": "completed"}),
            ]
        )
        pay = RecordingPay()
        with purchase_env(PAYSTARS_API_KEY=KEY, FRAGMENT_API_KEY=None):
            create_invoice({"kind": "premium", "recipient": "alexdev", "months": 3}, cryptopay=pay)
            pay.invoice["status"] = "paid"
            done = invoice_status("77", cryptopay=pay, fragment=client(script))
        self.assertTrue(done["delivered"])
        self.assertEqual(script.calls[2]["url"], BASE + "/premium/buy")
        self.assertEqual(script.calls[2]["body"]["months"], 3)

    def test_cost_above_price_is_not_bought(self):
        script = Script([check_ok(), (200, {"usd": 5.0})])
        pay = RecordingPay()
        with purchase_env(PAYSTARS_API_KEY=KEY, FRAGMENT_API_KEY=None):
            create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 50}, cryptopay=pay)
            pay.invoice["status"] = "paid"
            result = invoice_status("77", cryptopay=pay, fragment=client(script))
        self.assertFalse(result["delivered"])
        self.assertFalse(result["retry"])
        self.assertEqual(len(script.calls), 2)

    def test_empty_balance_retries(self):
        script = Script([check_ok(), (200, {"usd": 0.76}), (402, {"detail": {"code": "insufficient_balance", "message": "low"}})])
        pay = RecordingPay()
        with purchase_env(PAYSTARS_API_KEY=KEY, FRAGMENT_API_KEY=None):
            create_invoice({"kind": "stars", "recipient": "alexdev", "amount": 50}, cryptopay=pay)
            pay.invoice["status"] = "paid"
            result = invoice_status("77", cryptopay=pay, fragment=client(script))
        self.assertTrue(result["retry"])
        self.assertIn("PayStars", result["message"])
        self.assertIn("USD", result["message"])

    def test_paystars_key_alone_opens_purchases(self):
        with purchase_env(PAYSTARS_API_KEY=KEY, FRAGMENT_API_KEY=None):
            self.assertIsNone(purchases_blocked_reason())
        with purchase_env(PAYSTARS_API_KEY=None, FRAGMENT_API_KEY=None):
            self.assertIn("PAYSTARS_API_KEY", purchases_blocked_reason())


if __name__ == "__main__":
    unittest.main()
