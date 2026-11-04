"""Telegram usernames as Fragment expects them."""

import re

from shop.errors import ShopError

# Fragment addresses a recipient by username, not by numeric user id.
_USERNAME = re.compile(r"^[a-z][a-z0-9_]{4,31}$")


def normalize_username(value: object) -> str:
    if not isinstance(value, str):
        raise ShopError("bad_username", "Укажите recipient_username.", 400)
    name = value.strip().lstrip("@").lower()
    if not _USERNAME.fullmatch(name):
        raise ShopError(
            "bad_username",
            "Нужен Telegram username: 5–32 символа, латиница, цифры и _. Ссылка не подходит.",
            400,
        )
    return name
