"""Per-user encrypted credential vault (MongoDB).

The foundation for multi-tenant "Большой Брат": each Telegram user connects
their OWN services (TickTick, Google), and their refresh tokens live here
encrypted (AES-256-GCM), keyed by telegram user id. This is additive — it does
not touch the existing single-owner flow; the pipeline can start reading a
per-user token from here once wired.

Collection `user_credentials`:
  userId       str   — telegram user id (the tenant key)
  provider     str   — "ticktick" | "google"
  label        str   — account label (default "default")
  accessEnc    str   — encrypted access token
  refreshEnc   str   — encrypted refresh token
  extraEnc     str   — encrypted JSON blob (e.g. TickTick v2 cookie, mcp url)
  updatedAt    datetime
  UNIQUE (userId, provider, label)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..db import get_db
from . import crypto


@dataclass
class Credential:
    provider: str
    label: str = "default"
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    extra: dict = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enc(v: Optional[str]) -> Optional[str]:
    return crypto.encrypt_secret(v) if v else None


def _dec(v: Optional[str]) -> Optional[str]:
    return crypto.decrypt_secret(v) if v else None


def _enc_json(v: Optional[dict]) -> Optional[str]:
    return crypto.encrypt_secret(json.dumps(v, ensure_ascii=False)) if v else None


def _dec_json(v: Optional[str]) -> dict:
    if not v:
        return {}
    try:
        return json.loads(crypto.decrypt_secret(v))
    except Exception:
        return {}


async def ensure_indexes() -> None:
    db = get_db()
    await db.user_credentials.create_index(
        [("userId", 1), ("provider", 1), ("label", 1)], unique=True, name="user_provider_label"
    )


async def save_credential(
    user_id: str,
    provider: str,
    access_token: Optional[str] = None,
    refresh_token: Optional[str] = None,
    extra: Optional[dict] = None,
    label: str = "default",
) -> None:
    """Upsert one provider credential for a user. `extra` is merged with any
    existing extra (so e.g. adding a v2 cookie doesn't drop the mcp url)."""
    db = get_db()
    existing = await db.user_credentials.find_one(
        {"userId": user_id, "provider": provider, "label": label}
    )
    merged_extra = {**_dec_json(existing.get("extraEnc") if existing else None), **(extra or {})}
    doc: dict[str, Any] = {
        "userId": user_id,
        "provider": provider,
        "label": label,
        "updatedAt": _now(),
        "extraEnc": _enc_json(merged_extra) if merged_extra else None,
    }
    if access_token is not None:
        doc["accessEnc"] = _enc(access_token)
    if refresh_token is not None:
        doc["refreshEnc"] = _enc(refresh_token)
    elif existing and existing.get("refreshEnc"):
        doc["refreshEnc"] = existing["refreshEnc"]  # preserve rotated refresh
    await db.user_credentials.update_one(
        {"userId": user_id, "provider": provider, "label": label},
        {"$set": doc},
        upsert=True,
    )


async def get_credential(
    user_id: str, provider: str, label: str = "default"
) -> Optional[Credential]:
    db = get_db()
    doc = await db.user_credentials.find_one(
        {"userId": user_id, "provider": provider, "label": label}
    )
    if not doc:
        return None
    return Credential(
        provider=provider,
        label=label,
        access_token=_dec(doc.get("accessEnc")),
        refresh_token=_dec(doc.get("refreshEnc")),
        extra=_dec_json(doc.get("extraEnc")),
    )


async def list_credentials(user_id: str) -> list[Credential]:
    db = get_db()
    out: list[Credential] = []
    async for doc in db.user_credentials.find({"userId": user_id}):
        out.append(Credential(
            provider=doc["provider"],
            label=doc.get("label", "default"),
            access_token=_dec(doc.get("accessEnc")),
            refresh_token=_dec(doc.get("refreshEnc")),
            extra=_dec_json(doc.get("extraEnc")),
        ))
    return out


async def delete_credential(user_id: str, provider: str, label: str = "default") -> bool:
    db = get_db()
    r = await db.user_credentials.delete_one(
        {"userId": user_id, "provider": provider, "label": label}
    )
    return r.deleted_count > 0
