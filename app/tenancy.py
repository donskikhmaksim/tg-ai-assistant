"""Single-tenant lock (see Settings.multi_tenant_enabled).

The distribution model is SELF-HOST: every person runs their OWN fully-isolated
instance, so this instance must only ever serve the PRIMARY OWNER. The
multi-tenant / multihub machinery stays in the code but is gated behind one
config flag; while it's False, only the primary owner (or the very first user on
a fresh deploy, who becomes the primary owner) is served — no SECOND tenant.

`tenant_allowed()` is the single pure decision used at every gate; keeping it
free of I/O makes it trivially testable.
"""
from __future__ import annotations

from .config import get_settings


def tenant_allowed(
    *, multi_tenant_enabled: bool, is_primary_owner: bool, owner_exists: bool
) -> bool:
    """Whether a user may be served / onboarded as a tenant of this instance.

    - Multi-tenant ON  -> anyone may be a tenant (full multihub).
    - Multi-tenant OFF (default, single-tenant lock):
        * the primary owner is always allowed;
        * if no owner exists yet, allow bootstrap — this user becomes the
          primary owner (the self-host owner setting up their own instance);
        * otherwise block (no SECOND tenant on a private instance).
    """
    if multi_tenant_enabled:
        return True
    if is_primary_owner:
        return True
    if not owner_exists:
        return True
    return False


def is_multi_tenant_allowed() -> bool:
    """True when the multi-tenant/multihub path is explicitly turned on."""
    return get_settings().multi_tenant_enabled
