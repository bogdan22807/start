"""Customer price = Fragment cost plus the shop's own markup.

Fragment API already includes its service margin in package values and in the
order amount. This markup is only the shop's spread on top of that cost.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from shop.catalog import Product, list_products
from shop.errors import ShopError


def parse_decimal(raw: str) -> Decimal:
    text = raw.strip().replace(" ", "").replace(",", ".")
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        return Decimal(text)
    except Exception as exc:
        raise ShopError("bad_price", f"Не удалось прочитать число {raw!r}.", 500) from exc


def apply_markup(cost: Decimal, percent: Decimal) -> Decimal:
    customer = cost * (Decimal(1) + (percent / Decimal(100)))
    return customer.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def money(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    whole, frac = format(quantized, "f").split(".")
    frac = frac.rstrip("0")
    if len(frac) < 2:
        frac = frac.ljust(2, "0")
    return f"{whole}.{frac}"


def format_percent(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = format(quantized, "f").rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class Quote:
    cost: Decimal
    customer: Decimal
    currency: str
    markup_percent: Decimal

    @property
    def cost_text(self) -> str:
        return money(self.cost)

    @property
    def customer_text(self) -> str:
        return money(self.customer)

    @property
    def markup_text(self) -> str:
        return format_percent(self.markup_percent)


class PriceBook:
    def __init__(self, markup_percent: Decimal, currency: str, costs: dict[str, Decimal]):
        if markup_percent < 0 or markup_percent > Decimal(1000):
            raise ShopError("bad_markup", "SHOP_MARKUP_PERCENT должен быть от 0 до 1000.", 500)
        self.markup_percent = markup_percent
        self.currency = currency
        self.costs = costs

    def quote(self, product: Product) -> Quote | None:
        cost = self.costs.get(product.id)
        if cost is None:
            return None
        return Quote(
            cost=cost,
            customer=apply_markup(cost, self.markup_percent),
            currency=self.currency,
            markup_percent=self.markup_percent,
        )


def costs_from_sources(
    *,
    star_unit: Decimal | None,
    premium_env: dict[int, Decimal],
    packages: list[dict] | None,
    currency: str,
) -> dict[str, Decimal]:
    """Build per-product Fragment costs.

    Live Premium packages replace env fallbacks. Stars have no public price
    list, so their cost is the configured unit cost times quantity.
    """
    premium_costs = dict(premium_env)
    if packages is not None:
        field = "usd_value" if currency == "USDT" else "ton_value"
        for package in packages:
            try:
                months = int(package["months"])
                premium_costs[months] = parse_decimal(str(package[field]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ShopError("fragment_rejected", "Пакет Premium от Fragment неполный.", 502) from exc

    costs: dict[str, Decimal] = {}
    for product in list_products():
        if product.kind == "stars":
            if star_unit is None or product.quantity is None:
                continue
            cost = (star_unit * product.quantity).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        else:
            if product.months not in premium_costs:
                continue
            cost = premium_costs[product.months]
        if cost <= 0:
            raise ShopError("bad_price", f"Себестоимость {product.id} должна быть больше нуля.", 500)
        costs[product.id] = cost
    return costs
