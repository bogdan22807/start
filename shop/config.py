"""Environment for the local shop process. Secrets stay in the environment."""

import os
from decimal import Decimal

from shop.errors import ShopError
from shop.fragment import PRODUCTION_BASE, FragmentClient
from shop.orders import OrderStore
from shop.pricing import PriceBook, costs_from_sources, format_percent, parse_decimal
from shop.service import Shop
from shop.tokens import fragment_api_token


def load_shop(store: OrderStore) -> Shop:
    mode = os.environ.get("SHOP_MODE", "dry-run").strip().lower()
    if mode not in {"dry-run", "live"}:
        raise SystemExit("SHOP_MODE должен быть dry-run или live.")
    currency = os.environ.get("SHOP_WALLET", "USDT").strip().upper()
    if currency not in {"TON", "USDT"}:
        raise SystemExit("SHOP_WALLET должен быть TON или USDT.")

    markup = _decimal_env("SHOP_MARKUP_PERCENT", os.environ.get("SHOP_MARKUP_PERCENT", "0"))
    star_raw = os.environ.get("SHOP_STAR_UNIT_COST", "").strip()
    star_unit = _decimal_env("SHOP_STAR_UNIT_COST", star_raw) if star_raw else None
    if star_unit is not None and star_unit <= 0:
        raise SystemExit("SHOP_STAR_UNIT_COST должен быть больше нуля.")

    premium_env: dict[int, Decimal] = {}
    for months, name in ((3, "SHOP_PREMIUM_COST_3M"), (6, "SHOP_PREMIUM_COST_6M"), (12, "SHOP_PREMIUM_COST_12M")):
        raw = os.environ.get(name, "").strip()
        if raw:
            premium_env[months] = _decimal_env(name, raw)

    token = fragment_api_token()
    base = os.environ.get("FRAGMENT_API_BASE", PRODUCTION_BASE).strip() or PRODUCTION_BASE
    client = FragmentClient(token, base)
    packages = None
    if token:
        try:
            packages = client.premium_packages()
            print("цены Premium взяты из Fragment API")
        except ShopError as exc:
            print(f"цены Premium из Fragment API недоступны: {exc.message}")

    try:
        costs = costs_from_sources(
            star_unit=star_unit,
            premium_env=premium_env,
            packages=packages,
            currency=currency,
        )
        prices = PriceBook(markup, currency, costs)
    except ShopError as exc:
        raise SystemExit(exc.message) from exc

    admin = os.environ.get("SHOP_ADMIN_TOKEN", "").strip()
    if mode == "live" and not admin:
        print("live без SHOP_ADMIN_TOKEN: оплата, выдача и баланс закрыты")
    print(f"markup={format_percent(markup)}% wallet={currency} priced_products={len(costs)}")
    return Shop(store, prices, client, mode=mode, admin_token=admin)


def _decimal_env(name: str, raw: str):
    try:
        return parse_decimal(raw)
    except ShopError as exc:
        raise SystemExit(f"{name}: {exc.message}") from exc
