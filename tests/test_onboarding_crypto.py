"""Ported crypto for the per-user vault: encrypt/decrypt, tamper, HMAC, tokens."""
import base64, os
os.environ.setdefault("TOKEN_ENC_KEY", base64.b64encode(b"k"*32).decode())
import pytest
from app.onboarding import crypto


def test_roundtrip():
    for s in ["hello", "", "юникод 🔒", "x"*4000]:
        assert crypto.decrypt_secret(crypto.encrypt_secret(s)) == s

def test_versioned_and_random():
    a, b = crypto.encrypt_secret("same"), crypto.encrypt_secret("same")
    assert a.startswith("v1:") and a != b

def test_tamper_raises():
    ct = crypto.encrypt_secret("secret")
    raw = bytearray(base64.b64decode(ct[3:])); raw[-1] ^= 1
    with pytest.raises(Exception):
        crypto.decrypt_secret("v1:" + base64.b64encode(bytes(raw)).decode())

def test_sign_verify():
    sig = crypto.sign("u=1")
    assert crypto.verify("u=1", sig) and not crypto.verify("u=2", sig)

def test_new_token_unique():
    assert len({crypto.new_token() for _ in range(50)}) == 50
