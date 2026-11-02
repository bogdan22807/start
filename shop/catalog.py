"""Product catalog.

Premium plans match the Bot API contract for giftPremiumSubscription:
3 months cost the bot 1000 Stars, 6 months cost 1500, 12 months cost 2500.

Star packs are listed because that is what the storefront sells, but the Bot API
has no method that credits Stars onto a user's balance. sendGift explicitly
cannot be converted into Stars by the receiver.
"""

from dataclasses import dataclass
from typing import Literal

ProductKind = Literal["premium", "stars"]


@dataclass(frozen=True)
class Product:
    id: str
    kind: ProductKind
    title: str
    description: str
    star_count: int
    month_count: int | None = None

    @property
    def deliverable(self) -> bool:
        return self.kind == "premium"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "star_count": self.star_count,
            "month_count": self.month_count,
            "deliverable": self.deliverable,
        }


PREMIUM_PLANS = (
    Product(
        id="premium_3m",
        kind="premium",
        title="Premium 3 мес.",
        description="Telegram Premium на 3 месяца. Бот оплачивает подарок со своего баланса: 1000 звёзд.",
        star_count=1000,
        month_count=3,
    ),
    Product(
        id="premium_6m",
        kind="premium",
        title="Premium 6 мес.",
        description="Telegram Premium на 6 месяцев. Бот оплачивает подарок со своего баланса: 1500 звёзд.",
        star_count=1500,
        month_count=6,
    ),
    Product(
        id="premium_12m",
        kind="premium",
        title="Premium 12 мес.",
        description="Telegram Premium на 12 месяцев. Бот оплачивает подарок со своего баланса: 2500 звёзд.",
        star_count=2500,
        month_count=12,
    ),
)

STAR_PACKS = tuple(
    Product(
        id=f"stars_{amount}",
        kind="stars",
        title=f"{amount} звёзд",
        description=(
            "Пакет Telegram Stars. Bot API не умеет зачислять звёзды на баланс пользователя, "
            "поэтому автоматической выдачи нет."
        ),
        star_count=amount,
    )
    for amount in (50, 100, 250, 500, 1000)
)

CATALOG: dict[str, Product] = {item.id: item for item in (*PREMIUM_PLANS, *STAR_PACKS)}


def list_products() -> list[Product]:
    return list(CATALOG.values())


def get_product(product_id: str) -> Product | None:
    return CATALOG.get(product_id)
