"""One-time onboarding invites.

The owner mints an invite (a random token); it is redeemed exactly once when a
person opens the bot via the `?start=inv_<token>` deep link. Redeeming grants
that Telegram user onboarding access, which is what gates the connector-setup
buttons (so the owner's shared secrets never go to an uninvited stranger).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from ..db import get_db


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create_invite() -> str:
    """Mint an unused invite token."""
    token = secrets.token_urlsafe(16)
    db = get_db()
    await db.onboarding_invites.insert_one(
        {"token": token, "createdAt": _now(), "usedBy": None, "usedAt": None}
    )
    return token


async def redeem_invite(token: str, user_id: str) -> bool:
    """Consume `token` for `user_id`. Atomic and one-time: only the first caller
    matching an unused token wins. Grants onboarding access on success."""
    if not token:
        return False
    db = get_db()
    claimed = await db.onboarding_invites.find_one_and_update(
        {"token": token, "usedBy": None},
        {"$set": {"usedBy": str(user_id), "usedAt": _now()}},
    )
    if claimed is None:
        return False
    await db.onboarding_access.update_one(
        {"userId": str(user_id)},
        {"$setOnInsert": {"userId": str(user_id), "grantedAt": _now()}},
        upsert=True,
    )
    return True


async def has_access(user_id: str) -> bool:
    """Whether this user has redeemed a valid invite."""
    db = get_db()
    return await db.onboarding_access.find_one({"userId": str(user_id)}) is not None
