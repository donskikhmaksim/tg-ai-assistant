"""Telegram WebApp initData validation (stdlib-only, so it is unit-testable
without the web/runtime dependencies).

Algorithm: https://core.telegram.org/bots/webapps#validating-data
  secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
  hash       = HMAC_SHA256(key=secret_key, msg=data_check_string)
where data_check_string is every field except `hash`, sorted by key and
joined by newlines as "key=value".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import urllib.parse
from typing import Any


def validate_init_data(init_data: str, bot_token: str) -> dict[str, Any] | None:
    """Verify Telegram WebApp initData; return the parsed payload or None."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    user: dict[str, Any] = {}
    if "user" in pairs:
        try:
            user = json.loads(pairs["user"])
        except json.JSONDecodeError:
            pass
    return {"user": user, "auth_date": pairs.get("auth_date")}
