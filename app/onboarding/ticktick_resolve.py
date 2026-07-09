"""Per-user TickTick resolution (Variant B: each user has their own ticktick-mcp).

Multi-tenant Большой Брат doesn't talk to TickTick directly — each user runs
their own ticktick-mcp "adapter" (which already handles both the official API
and the v2 cookie features). The bot just needs each user's adapter URL. Those
are stored per-user in the vault (encrypted); this module reads one back and
builds a TickTickMCP client for it.

Falls back to the single global TICKTICK_MCP_URL (the current single-owner
deploy) when a user has no per-user URL yet — so nothing breaks during the
transition to multi-tenant.
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


async def get_user_ticktick(user_id: Optional[str]) -> Optional[TickTickMCP]:
    """Return a TickTickMCP client for THIS user's own adapter.

    Safety: the global TICKTICK_MCP_URL is the OWNER's own TickTick account.
    We fall back to it ONLY for the owner — NEVER for anyone else, so a new
    user without their own adapter can never dump tasks into the owner's
    TickTick. Such users get None (nothing is pushed until they connect their
    own connector).
    """
    url = await get_user_mcp_url(user_id) if user_id else None
    if not url:
        owner_id = await repo.get_bot_state(_OWNER_ID_KEY)
        is_owner = (
            user_id is not None and owner_id is not None
            and str(user_id) == str(owner_id)
        )
        if is_owner:
            url = get_settings().ticktick_mcp_url or None
    if not url:
        return None
    return TickTickMCP(url=url)
