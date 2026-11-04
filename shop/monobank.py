"""Monobank acquiring. The token stays on the server and is never sent to the browser.

A paid card is accepted only after Monobank reports status success and the
signed order still matches the amount. The webhook signature is ECDSA-SHA256
over the raw body, checked with the merchant public key.
"""

import base64
import json
import os
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from decimal import Decimal

from shop.errors import ShopError

PRODUCTION_BASE = "https://api.monobank.ua"
NBU_USD_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"
_TOKEN_NAMES = ("MONOBANK_TOKEN", "MONO_TOKEN", "MONOBANK_API_KEY", "MONOBANK_API_TOKEN")
_MAX_BODY = 1_000_000
CARD_ID = r"^[A-Za-z0-9_-]{4,80}$"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def monobank_token() -> str:
    for name in _TOKEN_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def card_currency() -> int:
    raw = os.environ.get("MONOBANK_CCY", "980").strip() or "980"
    if raw in {"980", "840"}:
        return int(raw)
    raise ShopError("bad_currency", "Валюта карты может быть только гривной или долларом.", 500)


class Monobank:
    def __init__(self, token: str, opener=None, base_url: str = PRODUCTION_BASE):
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self._opener = opener or _default_opener
        self._pubkey: bytes | None = None

    def create_invoice(
        self,
        minor: int,
        ccy: int,
        reference: str,
        destination: str,
        redirect_url: str,
        webhook_url: str,
    ) -> dict:
        payload = self._call(
            "POST",
            "/api/merchant/invoice/create",
            body={
                "amount": minor,
                "ccy": ccy,
                "merchantPaymInfo": {"reference": reference, "destination": destination},
                "redirectUrl": redirect_url,
                "webHookUrl": webhook_url,
                "validity": 3600,
                "paymentType": "debit",
            },
        )
        invoice_id = payload.get("invoiceId")
        page = payload.get("pageUrl")
        if not isinstance(invoice_id, str) or not _card_id(invoice_id):
            raise ShopError("monobank_rejected", "Monobank не дал номер счёта.", 502)
        if not isinstance(page, str) or not page.startswith("https://"):
            raise ShopError("monobank_rejected", "Monobank не дал ссылку на оплату.", 502)
        return {"invoiceId": invoice_id, "pageUrl": page}

    def get_invoice(self, invoice_id: str) -> dict:
        payload = self._call("GET", "/api/merchant/invoice/status", query={"invoiceId": invoice_id})
        if payload.get("invoiceId") is None:
            payload = dict(payload)
            payload["invoiceId"] = invoice_id
        return payload

    def public_key(self) -> bytes:
        if self._pubkey is not None:
            return self._pubkey
        payload = self._call("GET", "/api/merchant/pubkey")
        encoded = payload.get("key")
        if not isinstance(encoded, str) or not encoded.strip():
            raise ShopError("monobank_rejected", "Monobank не дал ключ подписи.", 502)
        try:
            pem = base64.b64decode(encoded.strip(), validate=False)
        except Exception as exc:
            raise ShopError("monobank_rejected", "Monobank не дал ключ подписи.", 502) from exc
        if b"BEGIN PUBLIC KEY" not in pem:
            raise ShopError("monobank_rejected", "Monobank не дал ключ подписи.", 502)
        self._pubkey = pem
        return pem

    def _call(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        url = self.base_url + path
        if query:
            pairs = "&".join(
                f"{key}={urllib.parse.quote(str(value), safe='')}" for key, value in query.items()
            )
            url = f"{url}?{pairs}"
        headers = {
            "X-Token": self.token,
            "Accept": "application/json",
            "User-Agent": "TelegramShop/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, payload = self._opener(method, url, headers, body)
        if status in {401, 403} or (isinstance(payload, dict) and str(payload.get("errCode") or "").upper() == "FORBIDDEN"):
            raise ShopError(
                "monobank_rejected",
                "Monobank не принял токен. Проверьте, что в настройках он записан как MONOBANK_TOKEN.",
                502,
            )
        if status >= 300 or not isinstance(payload, dict) or payload.get("errCode"):
            text = ""
            if isinstance(payload, dict):
                text = str(payload.get("errText") or payload.get("errorDescription") or "")
            raise ShopError("monobank_rejected", (text or "Monobank отклонил запрос.")[:300], 502)
        return payload


def nbu_usd_rate(opener=None) -> Decimal:
    fetch = opener or _default_opener
    try:
        status, payload = fetch(
            "GET",
            NBU_USD_URL,
            {"Accept": "application/json", "User-Agent": "TelegramShop/1.0"},
            None,
        )
    except ShopError as exc:
        raise ShopError(
            "rate_unavailable",
            "Не удалось узнать курс доллара. Оплата картой сейчас недоступна.",
            503,
        ) from exc
    if status >= 300 or not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ShopError("rate_unavailable", "Не удалось узнать курс доллара. Оплата картой сейчас недоступна.", 503)
    try:
        rate = Decimal(str(payload[0].get("rate")))
    except Exception as exc:
        raise ShopError("rate_unavailable", "Не удалось узнать курс доллара. Оплата картой сейчас недоступна.", 503) from exc
    if rate <= 0 or rate > Decimal("1000"):
        raise ShopError("rate_unavailable", "Не удалось узнать курс доллара. Оплата картой сейчас недоступна.", 503)
    return rate


def verify_sign(pem: bytes, signature: str | bytes, body: bytes) -> bool:
    if isinstance(signature, str):
        try:
            raw_sig = base64.b64decode(signature.strip(), validate=False)
        except Exception:
            return False
    elif isinstance(signature, bytes):
        raw_sig = signature
    else:
        return False
    if not raw_sig or not isinstance(pem, bytes) or b"BEGIN PUBLIC KEY" not in pem or not isinstance(body, bytes):
        return False
    try:
        return _verify_cryptography(pem, raw_sig, body)
    except ImportError:
        return _verify_openssl(pem, raw_sig, body)


def _verify_cryptography(pem: bytes, signature: bytes, body: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    try:
        key = load_pem_public_key(pem)
        key.verify(signature, body, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    except Exception:
        return False
    return True


def _verify_openssl(pem: bytes, signature: bytes, body: bytes) -> bool:
    try:
        with tempfile.TemporaryDirectory() as folder:
            pub = os.path.join(folder, "pub.pem")
            sig = os.path.join(folder, "sig.bin")
            data = os.path.join(folder, "body.bin")
            with open(pub, "wb") as handle:
                handle.write(pem)
            with open(sig, "wb") as handle:
                handle.write(signature)
            with open(data, "wb") as handle:
                handle.write(body)
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", pub, "-signature", sig, data],
                capture_output=True,
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _card_id(value: str) -> bool:
    import re

    return re.fullmatch(CARD_ID, value) is not None


def _default_opener(method: str, url: str, headers: dict, body: dict | None) -> tuple[int, object]:
    try:
        return _open(method, url, headers, body, None)
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc.reason):
            raise ShopError("monobank_unavailable", "Monobank сейчас не отвечает.", 503) from exc
        try:
            return _open(method, url, headers, body, ssl._create_unverified_context())
        except urllib.error.URLError as retry_exc:
            raise ShopError("monobank_unavailable", "Monobank сейчас не отвечает.", 503) from retry_exc


def _open(method: str, url: str, headers: dict, body: dict | None, context) -> tuple[int, object]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    if context is not None:
        opener.add_handler(urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, _decode(response.read(_MAX_BODY))
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read(_MAX_BODY))


def _decode(raw: bytes) -> object:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode())
    except json.JSONDecodeError as exc:
        raise ShopError("monobank_rejected", "Monobank ответил не JSON.", 502) from exc
