"""Token encryption (AES-256-GCM) and tamper-proof HMAC signing for OAuth
state/links. Mirrors the proven scheme from the Google MCP hub.

- Secrets at rest: encrypted with AES-256-GCM, stored as "v1:" + base64(iv|tag|ct).
- OAuth links: HMAC-SHA256 signatures so a return target / user id carried
  through an OAuth round-trip can't be forged.

Both use one key from TOKEN_ENC_KEY (64 hex chars, 32-byte base64, or a
passphrase stretched with scrypt). Rotating the key makes existing ciphertext
unreadable — keep it stable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_CIPHER_PREFIX = "v1:"
_SCRYPT_SALT = b"tg-ai-assistant.token.v1"


@lru_cache(maxsize=1)
def _key() -> bytes:
    """Derive the 32-byte key from TOKEN_ENC_KEY. Cached for the process."""
    secret = os.environ.get("TOKEN_ENC_KEY", "").strip()
    if not secret:
        raise RuntimeError("TOKEN_ENC_KEY is not set.")
    # 64 hex chars -> raw 32 bytes
    if len(secret) == 64:
        try:
            return bytes.fromhex(secret)
        except ValueError:
            pass
    # base64 that decodes to exactly 32 bytes
    try:
        raw = base64.b64decode(secret, validate=True)
        if len(raw) == 32:
            return raw
    except Exception:
        pass
    # otherwise treat as a passphrase and stretch it
    return hashlib.scrypt(secret.encode(), salt=_SCRYPT_SALT, n=2**14, r=8, p=1, dklen=32)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a UTF-8 secret. Returns 'v1:' + base64(iv(12) | tag(16) | ct)."""
    if plaintext is None:
        raise ValueError("cannot encrypt None")
    iv = os.urandom(12)
    ct_with_tag = AESGCM(_key()).encrypt(iv, plaintext.encode("utf-8"), None)
    return _CIPHER_PREFIX + base64.b64encode(iv + ct_with_tag).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Reverse encrypt_secret. Raises on tamper / wrong key / bad format."""
    if not token or not token.startswith(_CIPHER_PREFIX):
        raise ValueError("not a valid ciphertext")
    blob = base64.b64decode(token[len(_CIPHER_PREFIX):])
    iv, ct_with_tag = blob[:12], blob[12:]
    return AESGCM(_key()).decrypt(iv, ct_with_tag, None).decode("utf-8")


def sign(payload: str) -> str:
    """HMAC-SHA256 of payload (base64url, no padding), keyed by TOKEN_ENC_KEY."""
    mac = hmac.new(_key(), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def verify(payload: str, signature: str) -> bool:
    """Constant-time verification of sign()."""
    return hmac.compare_digest(sign(payload), signature)


def new_token(nbytes: int = 24) -> str:
    """A URL-safe random bearer token (per-user MCP connector key)."""
    return base64.urlsafe_b64encode(os.urandom(nbytes)).rstrip(b"=").decode("ascii")
