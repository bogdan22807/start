"""Check the Telegram mini app signature and read the buyer from it."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def webapp_profile(init_data: str, bot_token: str, *, now: float | None = None) -> dict | None:
    if not init_data or not bot_token:
        return None
    parsed = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = parsed.pop("hash", "")
    if not received or "auth_date" not in parsed:
        return None
    try:
        auth_date = int(parsed["auth_date"])
    except (TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    if auth_date > current + 30 or current - auth_date > 24 * 60 * 60:
        return None
    check = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        user = json.loads(parsed.get("user") or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict):
        return None
    user_id = _user_id(user.get("id"))
    if user_id is None:
        return None
    username = user.get("username") if isinstance(user.get("username"), str) else ""
    first_name = user.get("first_name") if isinstance(user.get("first_name"), str) else ""
    return {"id": user_id, "username": username.lstrip("@"), "first_name": first_name}


def webapp_user_id(init_data: str, bot_token: str, *, now: float | None = None) -> int | None:
    profile = webapp_profile(init_data, bot_token, now=now)
    return profile["id"] if profile else None


def _user_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None
