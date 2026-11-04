"""Small JSON API for the Fragment shop."""

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

_WEB_ROOT = (Path(__file__).resolve().parents[1] / "public").resolve()

from shop.checkout import handle_request
from shop.config import load_shop
from shop.errors import ShopError
from shop.orders import OrderStore
from shop.pricing import format_percent
from shop.service import Shop

MAX_BODY = 8192


class ShopHandler(BaseHTTPRequestHandler):
    shop: Shop

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.rstrip("/") == "/api/invoice":
            status, body = handle_request("GET", path, parsed.query, self.headers, b"")
            self._send(status, body)
            return
        page = _web_page(path)
        if page is not None:
            self._send_file(page)
            return
        try:
            if path == "/health":
                self._send(
                    200,
                    {
                        "ok": True,
                        "mode": self.shop.mode,
                        "currency": self.shop.prices.currency,
                        "markup_percent": format_percent(self.shop.prices.markup_percent),
                    },
                )
                return
            if path == "/catalog":
                self._send(200, self.shop.catalog())
                return
            if path == "/wallet":
                self._require_admin()
                self._send(200, self.shop.wallet())
                return
            order_id = _order_id(path)
            if order_id and path == f"/orders/{order_id}":
                self._send(200, self.shop.public_order(self.shop.get_order(order_id)))
                return
            self._send(404, {"error": "not_found", "message": "Неизвестный адрес."})
        except ShopError as exc:
            self._send(exc.status, {"error": exc.code, "message": exc.message})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.rstrip("/") in {"/api/invoice", "/api/cryptobot"}:
            try:
                raw = self._read_raw(65536)
            except ShopError as exc:
                self._send(exc.status, {"error": exc.code, "message": exc.message})
                return
            status, body = handle_request("POST", path, "", self.headers, raw)
            self._send(status, body)
            return
        try:
            payload = self._read_json()
            if path == "/orders":
                if "recipient_username" not in payload and "recipient_user_id" in payload:
                    raise ShopError(
                        "bad_username",
                        "Fragment покупает на username. Укажите recipient_username, не user id.",
                        400,
                    )
                order = self.shop.create_order(payload.get("product_id"), payload.get("recipient_username"))
                self._send(201, self.shop.public_order(order))
                return
            order_id = _order_id(path)
            if order_id and path == f"/orders/{order_id}/pay":
                self._require_admin()
                order = self.shop.confirm_payment(order_id, payload.get("charge_id", ""))
                self._send(200, self.shop.public_order(order))
                return
            if order_id and path == f"/orders/{order_id}/fulfill":
                self._require_admin()
                order, delivery = self.shop.fulfill(order_id)
                code = 200 if order.status == "fulfilled" else 202
                self._send(code, self.shop.public_order(order, delivery))
                return
            self._send(404, {"error": "not_found", "message": "Неизвестный адрес."})
        except ShopError as exc:
            self._send(exc.status, {"error": exc.code, "message": exc.message})
        except json.JSONDecodeError:
            self._send(400, {"error": "bad_json", "message": "Тело запроса должно быть JSON."})

    def _require_admin(self) -> None:
        if self.shop.mode == "live" and not self.shop.admin_token:
            raise ShopError(
                "missing_admin_token",
                "В live задайте SHOP_ADMIN_TOKEN. Без него магазин не принимает оплату и не покупает на Fragment.",
                503,
            )
        expected = self.shop.admin_token
        if not expected:
            return
        provided = self.headers.get("X-Admin-Token", "")
        if not provided:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                provided = auth[7:].strip()
        if not hmac.compare_digest(provided.encode(), expected.encode()):
            raise ShopError("unauthorized", "Нужен верный X-Admin-Token.", 401)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")

    def _read_raw(self, limit: int = MAX_BODY) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ShopError("bad_request", "Слишком большое тело запроса.", 400) from exc
        if length < 0 or length > limit:
            raise ShopError("bad_request", "Слишком большое тело запроса.", 400)
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict:
        raw = self._read_raw() or b"{}"
        body = json.loads(raw.decode())
        if not isinstance(body, dict):
            raise ShopError("bad_request", "JSON должен быть объектом.", 400)
        return body

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        kind = "text/html; charset=utf-8" if path.suffix == ".html" else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def _web_page(path: str) -> Path | None:
    if path in {"/", "/app", "/app/"}:
        relative = "index.html"
    elif path.startswith("/app/"):
        relative = path[len("/app/") :]
    else:
        return None
    target = (_WEB_ROOT / relative).resolve()
    if target != _WEB_ROOT and _WEB_ROOT not in target.parents:
        return None
    if target.is_file():
        return target
    return None


def _order_id(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "orders" and parts[1] not in {"pay", "fulfill"}:
        return parts[1]
    return None


def make_server(host: str, port: int, shop: Shop) -> ThreadingHTTPServer:
    handler = type("BoundShopHandler", (ShopHandler,), {"shop": shop})
    server = ThreadingHTTPServer((host, port), handler)
    server.shop = shop
    return server


def main() -> None:
    host = os.environ.get("SHOP_HOST", "0.0.0.0")
    port = int(os.environ.get("SHOP_PORT", "8000"))
    shop = load_shop(OrderStore(os.environ.get("SHOP_DB", "data/orders.db")))
    server = make_server(host, port, shop)
    print(f"shop listening on http://{host}:{port} mode={shop.mode}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        shop.store.close()
