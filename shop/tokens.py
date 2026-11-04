"""Separate the BotFather token from the Fragment API key.

A BotFather token looks like 123456789:AA.... The shop previously required
BOT_TOKEN and also sent API_TOKEN to Fragment. If the only saved value was a
bot token under API_TOKEN, the process exited before it could answer /start.
"""

import os
import re

_TELEGRAM_BOT = re.compile(r"\d+:[A-Za-z0-9_-]{20,}")


def telegram_bot_token() -> str:
    explicit = os.environ.get("BOT_TOKEN", "").strip()
    if explicit:
        return explicit
    api_token = os.environ.get("API_TOKEN", "").strip()
    if _TELEGRAM_BOT.fullmatch(api_token):
        return api_token
    return ""


def fragment_api_token() -> str:
    explicit = os.environ.get("FRAGMENT_API_KEY", "").strip()
    if explicit:
        return explicit
    api_token = os.environ.get("API_TOKEN", "").strip()
    if _TELEGRAM_BOT.fullmatch(api_token):
        return ""
    return api_token
