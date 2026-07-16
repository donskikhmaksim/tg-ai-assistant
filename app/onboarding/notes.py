"""Create self-destructing notes on the Self-Destroyed-Notes service.

Zero-knowledge by construction: this module encrypts the text locally with a
random URL key and only ever sends the *ciphertext* to the notes server. The
key travels in the share link's `#` fragment, which never reaches the server.
Opening the link once destroys the note (oneView).

Crypto contract mirrors the service's own client (public/index.html) and its
`read-note.mjs` reader, byte-for-byte:

    urlKey  = 32 random bytes  (base64url, in the link fragment)
    salt    = 16 random bytes  (base64, in the envelope)
    iv      = 12 random bytes  (base64, in the envelope)
    key     = PBKDF2-HMAC-SHA256(urlKey [+ password], salt, iter=100000, 32)
    data    = AES-256-GCM(iv, plaintext) = ciphertext || 16-byte tag  (base64)
    POST /api/notes {payload:{iv,data,salt,iter,pw}, oneView, ttl} -> {id}
    link    = <base>/#/n/<id>/<urlKey base64url, unpadded>
"""
from __future__ import annotations

import base64
import hashlib
import os

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Must match the service (public/index.html: `const ITER = 100000`).
PBKDF2_ITER = 100_000


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64url_nopad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


async def create_note(
    base_url: str,
    text: str,
    *,
    ttl_seconds: int = 3600,
    one_view: bool = True,
    password: str | None = None,
) -> str:
    """Encrypt `text`, store it as a one-time note, and return the share link.

    Raises on a non-2xx response or a missing id so callers can surface a clear
    "couldn't create the note" error instead of handing out a broken link.
    """
    base = base_url.rstrip("/")
    url_key = os.urandom(32)
    salt = os.urandom(16)
    iv = os.urandom(12)

    kdf_input = url_key + (password.encode("utf-8") if password else b"")
    aes_key = hashlib.pbkdf2_hmac("sha256", kdf_input, salt, PBKDF2_ITER, dklen=32)
    data = AESGCM(aes_key).encrypt(iv, text.encode("utf-8"), None)  # ct || tag

    payload = {
        "iv": _b64(iv),
        "data": _b64(data),
        "salt": _b64(salt),
        "iter": PBKDF2_ITER,
        "pw": bool(password),
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{base}/api/notes",
            json={"payload": payload, "oneView": one_view, "ttl": ttl_seconds},
        )
        resp.raise_for_status()
        note_id = (resp.json() or {}).get("id")
    if not note_id:
        raise RuntimeError("notes service did not return an id")

    return f"{base}/#/n/{note_id}/{_b64url_nopad(url_key)}"
