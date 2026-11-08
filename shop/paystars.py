"""PayStars API client: Telegram Stars and Premium bought from a USD balance.

Docs: https://paystars.uz/en/docs-api/

Flow for one purchase:
1. POST /check-username gives a verification token for this recipient (180 s, one order).
2. POST /usd/quote gives the exact USD price.
3. POST /stars/buy or /premium/buy with that token, max_usd and an Idempotency-Key.
4. GET /order/{id} until the order is completed or failed.

The key is sent only as the X-API-Key header and is never written into orders.
"""

import json
import os
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

from shop.errors import ShopError

PRODUCTION_BASE = "https://paystars.uz/api/v1"
_MAX_BODY = 1_000_000

_DONE = {"completed", "complete", "done", "success", "succeeded", "delivered", "finished"}
_FAILED = {"failed", "fail", "error", "cancelled", "canceled", "refunded", "rejected", "expired"}


def paystars_api_key() -> str:
    return os.environ.get("PAYSTARS_API_KEY", "").strip()


def paystars_base() -> str:
    value = os.environ.get("PAYSTARS_API_BASE", "").strip().rstrip("/")
    return value if value.startswith("https://") else PRODUCTION_BASE


def normalize_status(value: object) -> str:
    """completed, failed or processing, whatever exact word PayStars uses."""
    text = str(value or "").strip().lower()
    if text in _DONE:
        return "completed"
    if text in _FAILED:
        return "failed"
    return "processing"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_opener(method: str, url: str, headers: dict, body: dict | None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, _decode(response.read(_MAX_BODY))
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read(_MAX_BODY))
    except urllib.error.URLError as exc:
        raise ShopError("fragment_unavailable", f"PayStars не ответил: {exc.reason}", 502) from exc


def _decode(raw: bytes) -> dict:
    if not raw:
        return {}
    text = raw.decode(errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"message": text[:300]}
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return {"items": parsed}
    return {"message": text[:300]}


class PaystarsClient:
    provider = "paystars"

    def __init__(self, token: str, base_url: str = PRODUCTION_BASE, opener=None):
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self._opener = opener or _default_opener

    def check(self, username: str, kind: str) -> str:
        """Verification token for one purchase, or a clear error."""
        payload = self._call("POST", "/check-username", {"username": username, "kind": kind})
        if kind == "premium" and payload.get("premium_active") is True:
            raise ShopError("premium_active", f"У @{username} Premium уже активен, подарить ещё нельзя.", 400)
        token = payload.get("verification_token")
        if payload.get("valid") is not True or not isinstance(token, str) or not token:
            raise ShopError("recipient_not_found", "PayStars не нашёл такого пользователя для этой покупки.", 400)
        return token

    def quote(self, kind: str, username: str, count: int) -> Decimal:
        body = {"kind": kind, "username": username}
        body["quantity" if kind == "stars" else "months"] = count
        payload = self._call("POST", "/usd/quote", body)
        try:
            usd = Decimal(str(payload.get("usd")))
        except (InvalidOperation, ValueError) as exc:
            raise ShopError("fragment_rejected", "PayStars не назвал цену.", 502) from exc
        if not usd.is_finite() or usd <= 0:
            raise ShopError("fragment_rejected", "PayStars не назвал цену.", 502)
        return usd

    def buy(self, *, kind: str, username: str, count: int, verification_token: str, max_usd: Decimal, idempotency_key: str) -> dict:
        body = {
            "username": username,
            "verification_token": verification_token,
            "currency": "USD",
            "max_usd": float(max_usd),
        }
        if kind == "stars":
            body["quantity"] = count
            path = "/stars/buy"
        else:
            body["months"] = count
            path = "/premium/buy"
        return self._call("POST", path, body, idempotency_key=idempotency_key[:128])

    def get_order(self, order_id: str) -> dict:
        payload = self._call("GET", f"/order/{order_id}")
        order = payload.get("order")
        return order if isinstance(order, dict) else payload

    def account(self) -> dict:
        return self._call("GET", "/account")

    def _call(self, method: str, path: str, body: dict | None = None, idempotency_key: str | None = None) -> dict:
        if not self.token:
            raise ShopError("missing_token", "Для покупки нужен PAYSTARS_API_KEY.", 503)
        headers = {"X-API-Key": self.token, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        status, payload = self._opener(method, self.base_url + path, headers, body)
        if not isinstance(payload, dict):
            payload = {"message": "PayStars вернул не объект."}
        if status >= 300:
            raise self._error(status, payload)
        if payload.get("ok") is False:
            raise self._error(400, payload)
        return payload

    @staticmethod
    def _error(status: int, payload: dict) -> ShopError:
        detail = payload.get("detail")
        code = ""
        message = ""
        if isinstance(detail, dict):
            code = str(detail.get("code") or "")
            message = str(detail.get("message") or "")
        elif isinstance(detail, str):
            message = detail
        if not message:
            raw = payload.get("message") or payload.get("error")
            message = raw if isinstance(raw, str) else "PayStars отклонил запрос."
        message = message[:300]
        code = code.lower()
        if status == 401:
            return ShopError("unauthorized", "PAYSTARS_API_KEY отклонён PayStars.", 401)
        if status == 402 or code == "insufficient_balance":
            return ShopError("insufficient_balance", message, 402)
        if code == "price_changed":
            return ShopError("price_changed", message, 409)
        if status == 409:
            return ShopError("idempotency_conflict", message, 409)
        if status == 422:
            return ShopError("recipient_not_found", message, 400)
        if status == 429:
            return ShopError("rate_limited", "PayStars ограничил частоту запросов.", 429)
        if status >= 500 or status in {301, 302, 303, 307, 308}:
            return ShopError("fragment_unavailable", message, 502)
        return ShopError("fragment_rejected", message, 400)
