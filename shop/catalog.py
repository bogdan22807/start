"""Stars and Premium sold by buying them on Fragment."""

from dataclasses import dataclass
from typing import Literal

ProductKind = Literal["premium", "stars"]

STAR_QUANTITIES = (50, 100, 250, 500, 1000, 2500)
PREMIUM_MONTHS = (3, 6, 12)


@dataclass(frozen=True)
class Product:
    id: str
    kind: ProductKind
    title: str
    description: str
    quantity: int | None = None
    months: int | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "quantity": self.quantity,
            "months": self.months,
        }


STAR_PACKS = tuple(
    Product(
        id=f"stars_{amount}",
        kind="stars",
        title=f"{amount} звёзд",
        description=(
            f"{amount} Telegram Stars. Магазин покупает их на Fragment для указанного username. "
            "Цена для покупателя — стоимость Fragment плюс наценка магазина."
        ),
        quantity=amount,
    )
    for amount in STAR_QUANTITIES
)

PREMIUM_PLANS = tuple(
    Product(
        id=f"premium_{months}m",
        kind="premium",
        title=f"Premium {months} мес.",
        description=(
            f"Telegram Premium на {months} мес. Магазин дарит его через Fragment на указанный username. "
            "Цена для покупателя — стоимость Fragment плюс наценка магазина."
        ),
        months=months,
    )
    for months in PREMIUM_MONTHS
)

CATALOG: dict[str, Product] = {item.id: item for item in (*STAR_PACKS, *PREMIUM_PLANS)}


def list_products() -> list[Product]:
    return list(CATALOG.values())


def get_product(product_id: str) -> Product | None:
    return CATALOG.get(product_id)
