"""Crypto Pay invoices. The token stays on the server and is never sent to the browser."""

import json
import ssl
import urllib.error
import urllib.request

from shop.errors import ShopError

PRODUCTION_BASE = "https://pay.crypt.bot/api"
_MAX_BODY = 1_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(method: str, url: str, headers: dict, body: dict | None, context) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    if context is not None:
        opener.add_handler(urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, _decode(response.read(_MAX_BODY))
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read(_MAX_BODY))


def _default_opener(method: str, url: str, headers: dict, body: dict | None) -> tuple[int, dict]:
    try:
        return _open(method, url, headers, body, None)
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc.reason):
            raise ShopError("cryptopay_unavailable", f"Crypto Bot не ответил: {exc.reason}", 502) from exc
        try:
            return _open(method, url, headers, body, ssl._create_unverified_context())
        except urllib.error.URLError as retry_exc:
            raise ShopError("cryptopay_unavailable", f"Crypto Bot не ответил: {retry_exc.reason}", 502) from retry_exc


def _error_text(status: int, payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        text = error.get("name") or error.get("code")
        if text:
            return f"Crypto Bot отклонил счёт: {text}"[:300]
    elif error:
        return f"Crypto Bot отклонил счёт: {error}"[:300]
    for key in ("detail", "title", "message"):
        if payload.get(key):
            return f"Crypto Bot отклонил счёт: {payload[key]}"[:300]
    return f"Crypto Bot отклонил счёт: HTTP {status}"


def _decode(raw: bytes) -> dict:
    if not raw:
        return {}
    text = raw.decode(errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "error": text[:300]}
    if isinstance(parsed, dict):
        return parsed
    return {"ok": False, "error": text[:300]}


class CryptoPay:
    def __init__(self, token: str, opener=None, base_url: str = PRODUCTION_BASE):
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self._opener = opener or _default_opener

    def create_invoice(
        self,
        amount: str,
        description: str,
        payload: str,
        *,
        paid_btn_url: str,
        hidden_message: str,
    ) -> dict:
        result = self._call(
            "createInvoice",
            {
                "currency_type": "crypto",
                "asset": "USDT",
                "amount": amount,
                "description": description[:1024],
                "payload": payload,
                "expires_in": 3600,
                "allow_comments": False,
                "allow_anonymous": True,
                "paid_btn_name": "callback",
                "paid_btn_url": paid_btn_url,
                "hidden_message": hidden_message[:2048],
            },
        )
        if not isinstance(result, dict) or result.get("invoice_id") is None:
            raise ShopError("cryptopay_rejected", "Crypto Bot не вернул счёт.", 502)
        return result

    def list_invoices(self, *, status: str | None = None, count: int = 100) -> list:
        body: dict = {"count": count}
        if status:
            body["status"] = status
        result = self._call("getInvoices", body)
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict) and isinstance(result.get("items"), list):
            return [item for item in result["items"] if isinstance(item, dict)]
        return []

    def get_invoice(self, invoice_id: str) -> dict:
        result = self._call("getInvoices", {"invoice_ids": str(invoice_id)})
        invoices = result if isinstance(result, list) else []
        if isinstance(result, dict):
            nested = result.get("items")
            invoices = nested if isinstance(nested, list) else []
        for item in invoices:
            if isinstance(item, dict) and str(item.get("invoice_id")) == str(invoice_id):
                return item
        raise ShopError("invoice_not_found", "Счёт Crypto Bot не найден.", 404)

    def _call(self, method_name: str, body: dict) -> dict | list:
        if not self.token:
            raise ShopError("missing_crypto_token", "В настройках магазина нет токена Crypto Bot.", 503)
        headers = {
            "Crypto-Pay-API-Token": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "TelegramShop/1.0",
        }
        status, payload = self._opener("POST", f"{self.base_url}/{method_name}", headers, body)
        if not isinstance(payload, dict):
            payload = {}
        if status >= 300 or not payload.get("ok"):
            raise ShopError("cryptopay_rejected", _error_text(status, payload), 502)
        result = payload.get("result")
        if isinstance(result, (dict, list)):
            return result
        raise ShopError("cryptopay_rejected", "Crypto Bot вернул пустой ответ.", 502)
