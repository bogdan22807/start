"""Fragment API client for Stars purchases and Premium gifts.

The key is sent only as the API-Key header and is never written into orders.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from shop.errors import ShopError

PRODUCTION_BASE = "https://v1.fragmentapi.com/api/v1/partner"
_MAX_BODY = 1_000_000


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
        raise ShopError("fragment_unavailable", f"Fragment API не ответил: {exc.reason}", 502) from exc


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


class FragmentClient:
    def __init__(self, token: str, base_url: str = PRODUCTION_BASE, opener=None):
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self._opener = opener or _default_opener

    def premium_packages(self) -> list[dict]:
        payload = self._call("GET", "/premium/packages")
        packages = payload.get("items")
        if not isinstance(packages, list):
            raise ShopError("fragment_rejected", "Fragment API вернул пакеты Premium в неожиданном виде.", 502)
        return packages

    def search_stars(self, username: str, quantity: int) -> str:
        payload = self._call(
            "GET",
            "/star/recipient/search",
            {"username": username, "quantity": str(quantity)},
        )
        return self._recipient(payload)

    def search_premium(self, username: str, months: int) -> str:
        payload = self._call(
            "GET",
            "/premium/recipient/search",
            {"username": username, "months": str(months)},
        )
        return self._recipient(payload)

    def create_stars(self, *, username: str, recipient_hash: str, quantity: int, wallet_type: str, idempotency_key: str) -> dict:
        return self._call(
            "POST",
            "/orders/star",
            body={
                "username": username,
                "recipient_hash": recipient_hash,
                "quantity": quantity,
                "wallet_type": wallet_type,
            },
            idempotency_key=idempotency_key,
        )

    def create_premium(self, *, username: str, recipient_hash: str, months: int, wallet_type: str, idempotency_key: str) -> dict:
        return self._call(
            "POST",
            "/orders/premium",
            body={
                "username": username,
                "recipient_hash": recipient_hash,
                "months": months,
                "wallet_type": wallet_type,
            },
            idempotency_key=idempotency_key,
        )

    def get_order(self, order_id: str) -> dict:
        return self._call("GET", f"/orders/{order_id}")

    def wallet(self) -> dict:
        return self._call("GET", "/wallet/balance")

    def _recipient(self, payload: dict) -> str:
        recipient = payload.get("recipient")
        if payload.get("success") is False or not isinstance(recipient, str) or not recipient:
            raise ShopError("recipient_not_found", "Fragment не нашёл такого пользователя для этой покупки.", 400)
        return recipient

    def _call(self, method: str, path: str, query: dict | None = None, body: dict | None = None, idempotency_key: str | None = None) -> dict:
        if not self.token:
            raise ShopError("missing_token", "Для покупки на Fragment нужен API_TOKEN.", 503)
        url = self.base_url + path
        if query:
            pairs = "&".join(
                f"{key}={urllib.parse.quote(str(value), safe='')}" for key, value in query.items()
            )
            url = f"{url}?{pairs}"
        headers = {"API-Key": self.token, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        status, payload = self._opener(method, url, headers, body)
        if not isinstance(payload, dict):
            payload = {"message": "Fragment API вернул не объект."}
        if status >= 300:
            raise self._error(status, payload)
        return payload

    @staticmethod
    def _error(status: int, payload: dict) -> ShopError:
        message = payload.get("message") or payload.get("error") or "Fragment отклонил запрос."
        if not isinstance(message, str):
            message = "Fragment отклонил запрос."
        message = message[:300]
        if status == 401:
            return ShopError("unauthorized", "API_TOKEN отклонён Fragment API.", 401)
        if status == 402:
            return ShopError("insufficient_balance", message, 402)
        if status == 404:
            return ShopError("fragment_not_found", message, 404)
        if status == 409:
            return ShopError("idempotency_conflict", message, 409)
        if status == 429:
            return ShopError("rate_limited", "Fragment API ограничил частоту запросов.", 429)
        if status >= 500 or status in {301, 302, 303, 307, 308}:
            return ShopError("fragment_unavailable", message, 502)
        return ShopError("fragment_rejected", message, 400)
