"""Tests for Telegram WebApp initData validation (app/web/auth.py)."""
import hashlib
import hmac
import json
import urllib.parse

from app.web.auth import validate_init_data

TOKEN = "123456:TEST-BOT-TOKEN"


def _sign(fields: dict, token: str) -> str:
    """Build a correctly-signed initData query string for `fields`."""
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": h})


def test_valid_init_data_returns_user():
    user = {"id": 42, "first_name": "Maksim"}
    init = _sign({"auth_date": "1700000000", "user": json.dumps(user)}, TOKEN)
    out = validate_init_data(init, TOKEN)
    assert out is not None
    assert out["user"]["id"] == 42


def test_tampered_payload_rejected():
    # Change the (url-encoded) user id without re-signing — hash must no longer match.
    init = _sign({"auth_date": "1700000000", "user": '{"id":42}'}, TOKEN)
    tampered = init.replace("42", "43")
    assert tampered != init
    assert validate_init_data(tampered, TOKEN) is None


def test_corrupted_hash_rejected():
    init = _sign({"auth_date": "1", "user": '{"id":1}'}, TOKEN)
    flipped = init[:-1] + ("0" if init[-1] != "0" else "1")
    assert validate_init_data(flipped, TOKEN) is None


def test_wrong_token_rejected():
    init = _sign({"auth_date": "1", "user": '{"id":1}'}, TOKEN)
    assert validate_init_data(init, "999:OTHER-TOKEN") is None


def test_empty_and_missing_hash():
    assert validate_init_data("", TOKEN) is None
    assert validate_init_data("auth_date=1&user=%7B%7D", TOKEN) is None
