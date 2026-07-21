"""Single-tenant lock gating decision (app/tenancy.tenant_allowed)."""
import pytest

from app.tenancy import tenant_allowed


# (multi_tenant_enabled, is_primary_owner, owner_exists) -> allowed
CASES = [
    # Locked (default): only the primary owner, or bootstrap on a fresh deploy.
    (False, True, True, True),    # primary owner, owner known -> served
    (False, False, True, False),  # some other user, owner already exists -> BLOCKED
    (False, False, False, True),  # fresh deploy, no owner yet -> bootstrap allowed
    (False, True, False, True),   # (primary flag with no owner) -> allowed
    # Multi-tenant explicitly ON: anyone may be a tenant.
    (True, False, True, True),    # other user served when multihub is on
    (True, False, False, True),
    (True, True, True, True),
]


@pytest.mark.parametrize("flag,is_primary,owner_exists,expected", CASES)
def test_tenant_allowed(flag, is_primary, owner_exists, expected):
    assert (
        tenant_allowed(
            multi_tenant_enabled=flag,
            is_primary_owner=is_primary,
            owner_exists=owner_exists,
        )
        is expected
    )


def test_second_tenant_blocked_by_default():
    """The core lock: a non-owner is blocked once an owner exists, with the
    default (locked) flag."""
    assert not tenant_allowed(
        multi_tenant_enabled=False, is_primary_owner=False, owner_exists=True
    )


def test_lock_never_blocks_primary_owner():
    """The primary owner is served regardless of the flag."""
    for flag in (False, True):
        assert tenant_allowed(
            multi_tenant_enabled=flag, is_primary_owner=True, owner_exists=True
        )
