"""Small JSON API so the shop can be exercised without Telegram."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from shop.fulfillment import ShopError
from shop.orders import OrderStore
from shop.service import Shop

MAX_BODY = 8192


class ShopHandler(BaseHTTPRequestHandler):
    shop: Shop

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/health":
                self._send(200, {"ok": True})
                return
            if path == "/catalog":
                self._send(200, {"products": self.shop.catalog()})
                return
            order_id = _order_id(path)
            if order_id and path == f"/orders/{order_id}":
                order = self.shop.get_order(order_id)
                self._send(200, self.shop.public_order(order))
                return
            self._send(404, {"error": "not_found", "message": "Неизвестный адрес."})
        except ShopError as exc:
            self._send(exc.status, {"error": exc.code, "message": exc.message})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/orders":
                order = self.shop.create_order(payload.get("product_id"), payload.get("recipient_user_id"))
                self._send(201, self.shop.public_order(order))
                return
            order_id = _order_id(path)
            if order_id and path == f"/orders/{order_id}/pay":
                order = self.shop.confirm_payment(order_id, payload.get("charge_id", ""))
                self._send(200, self.shop.public_order(order))
                return
            if order_id and path == f"/orders/{order_id}/fulfill":
                order, delivery = self.shop.fulfill(order_id)
                self._send(200, self.shop.public_order(order, delivery))
                return
            self._send(404, {"error": "not_found", "message": "Неизвестный адрес."})
        except ShopError as exc:
            self._send(exc.status, {"error": exc.code, "message": exc.message})
        except json.JSONDecodeError:
            self._send(400, {"error": "bad_json", "message": "Тело запроса должно быть JSON."})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_BODY:
            raise ShopError("bad_request", "Слишком большое тело запроса.", 400)
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw.decode())
        if not isinstance(body, dict):
            raise ShopError("bad_request", "JSON должен быть объектом.", 400)
        return body

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


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
    db_path = os.environ.get("SHOP_DB", "data/orders.db")
    shop = Shop(OrderStore(db_path))
    server = make_server(host, port, shop)
    print(f"shop listening on http://{host}:{port} mode={os.environ.get('SHOP_MODE', 'dry-run')}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        shop.store.close()
