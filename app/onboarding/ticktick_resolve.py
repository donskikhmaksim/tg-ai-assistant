"""Per-user TickTick resolution (Variant B: each user has their own ticktick-mcp).

Multi-tenant Большой Брат doesn't talk to TickTick directly — each user runs
their own ticktick-mcp "adapter" (which already handles both the official API
and the v2 cookie features). The bot just needs each user's adapter URL. Those
are stored per-user in the vault (encrypted); this module reads one back and
builds a TickTickMCP client for it.

There is NO shared/global TickTick account. Everyone — including the owner — is
resolved purely from their own per-user URL. The legacy global TICKTICK_MCP_URL
env var (the owner's single-tenant deploy) is treated as a one-time migration
seed: the first time we resolve the owner and they have no vault URL yet, we
copy the env value into their vault entry. After that the env var is dead weight
and can be removed. New users never touch it.
"""
from __future__ import annotations

import logging
from typing import Optional

from .. import repositories as repo
from ..config import get_settings
from ..ticktick.mcp_client import TickTickMCP
from . import vault

logger = logging.getLogger(__name__)

_PROVIDER = "ticktick"
_OWNER_ID_KEY = "owner_id"


async def set_user_mcp_url(user_id: str, mcp_url: str) -> None:
    """Store a user's own ticktick-mcp connector URL (the full /mcp/<secret>
    URL — it is itself the credential)."""
    await vault.save_credential(user_id, _PROVIDER, extra={"mcp_url": mcp_url.strip()})


async def get_user_mcp_url(user_id: str) -> Optional[str]:
    cred = await vault.get_credential(user_id, _PROVIDER)
    url = (cred.extra.get("mcp_url") if cred else None) or None
    return url


async def _is_owner(user_id: Optional[str]) -> bool:
    if user_id is None:
        return False
    owner_id = await repo.get_bot_state(_OWNER_ID_KEY)
    return owner_id is not None and str(user_id) == str(owner_id)


async def seed_owner_from_env(user_id: str) -> Optional[str]:
    """One-time migration: if the owner has no per-user URL yet but the legacy
    global TICKTICK_MCP_URL env is set, copy it into their vault so their
    existing task flow keeps working after the global fallback is removed.

    Returns the seeded URL (or the already-stored one), or None if nothing to
    seed. Safe to call repeatedly — it never overwrites an existing vault URL.
    """
    existing = await get_user_mcp_url(user_id)
    if existing:
        return existing
    if not await _is_owner(user_id):
        return None
    env_url = get_settings().ticktick_mcp_url or None
    if not env_url:
        return None
    await set_user_mcp_url(user_id, env_url)
    logger.info("Seeded owner %s TickTick URL from legacy env into vault", user_id)
    return env_url


async def get_user_ticktick(user_id: Optional[str]) -> Optional[TickTickMCP]:
    """Return a TickTickMCP client for THIS user's own adapter, or None.

    Resolution is purely per-user: there is no shared account. The only special
    case is a self-healing one-time seed of the owner's legacy global URL into
    their own vault entry (see seed_owner_from_env) — after which even the owner
    is a plain per-user lookup. A user with no connector gets None (nothing is
    pushed until they connect their own).
    """
    url = await get_user_mcp_url(user_id) if user_id else None
    if not url and user_id is not None:
        url = await seed_owner_from_env(user_id)
    if not url:
        return None
    return TickTickMCP(url=url)
