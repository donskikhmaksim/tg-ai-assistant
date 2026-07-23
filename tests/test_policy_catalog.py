"""Tests for the static manifest-policy tool catalog + tier-resolution logic
(app/policy/catalog.py, app/policy/catalog.json). Phase 1 — this catalog only
seeds data for the Mini App; nothing here enforces anything (see
app/policy/__init__.py)."""
from app.policy import catalog as policy_catalog


def _catalog():
    return policy_catalog.load_catalog()


# ── catalog data structure ──────────────────────────────────────────────────

def test_catalog_has_defaults_for_every_class():
    cat = _catalog()
    defaults = cat["defaults"]
    for klass in policy_catalog.CLASSES:
        assert klass in defaults, f"missing default for class {klass}"
        assert defaults[klass] in policy_catalog.TIERS


def test_catalog_contains_known_ticktick_tools():
    tools = _catalog()["tools"]
    for key in (
        "ticktick-mcp.create_tasks",
        "ticktick-mcp.update_tasks",
        "ticktick-mcp.delete_tasks",
        "ticktick-mcp.delete_task_with_subtasks",
        "ticktick-mcp.get_projects",
        "ticktick-mcp.plan_task_creation",
        "ticktick-mcp.execute_task_creation",
    ):
        assert key in tools, f"catalog missing {key}"


def test_every_tool_entry_has_a_valid_class_and_tier():
    tools = _catalog()["tools"]
    assert len(tools) > 0
    for key, meta in tools.items():
        assert meta["class"] in policy_catalog.CLASSES, key
        assert meta["recommended_tier"] in policy_catalog.TIERS, key
        assert isinstance(meta["has_manifest"], bool), key
        assert meta.get("label"), key  # every tool has a human label


def test_hard_manifest_tools_all_have_a_manifest_path():
    """A tool this catalog recommends `hard_manifest` for must actually have
    somewhere to route to (has_manifest=True) — otherwise the Mini App would
    offer a "hard" setting with nothing behind it."""
    tools = _catalog()["tools"]
    for key, meta in tools.items():
        if meta["recommended_tier"] == "hard_manifest":
            assert meta["has_manifest"] is True, f"{key} recommends hard_manifest but has no manifest path"


def test_read_class_tools_recommend_off():
    """Read-only tools are listed for completeness but are never enforceable."""
    tools = _catalog()["tools"]
    for key, meta in tools.items():
        if meta["class"] == "read":
            assert meta["recommended_tier"] == "off", key


def test_catalog_covers_roughly_the_known_tool_count():
    """Sanity check against the ~72-tool figure known for ticktick-mcp this
    session (70 live + plan_declutter/execute_declutter seeded ahead of time)."""
    tools = _catalog()["tools"]
    assert 60 <= len(tools) <= 90


# ── resolve_tier: resolution order ──────────────────────────────────────────

def test_resolve_explicit_tool_override_wins_over_everything():
    policy = {"defaults": {"mutating": "off"}, "tools": {"ticktick-mcp.update_tasks": "hard_manifest"}}
    assert policy_catalog.resolve_tier("ticktick-mcp.update_tasks", policy) == "hard_manifest"


def test_resolve_falls_back_to_owner_class_default():
    policy = {"defaults": {"mutating": "off"}, "tools": {}}
    # update_tasks is class "mutating" with recommended_tier soft_guard, but
    # the owner's class-wide default for "mutating" should win.
    assert policy_catalog.resolve_tier("ticktick-mcp.update_tasks", policy) == "off"


def test_resolve_falls_back_to_catalog_recommended_tier_when_no_owner_default():
    policy = {"defaults": {}, "tools": {}}
    assert policy_catalog.resolve_tier("ticktick-mcp.update_tasks", policy) == "soft_guard"
    assert policy_catalog.resolve_tier("ticktick-mcp.create_tasks", policy) == "hard_manifest"
    assert policy_catalog.resolve_tier("ticktick-mcp.create_project", policy) == "off"


def test_resolve_unknown_tool_fails_safe_to_soft_guard():
    """A tool absent from the catalog entirely (e.g. brand-new, audit hasn't
    caught up) must never resolve to bare/off — soft_guard is the fail-safe."""
    policy = {"defaults": {}, "tools": {}}
    assert policy_catalog.resolve_tier("some-new-server.mystery_tool", policy) == "soft_guard"


def test_resolve_handles_missing_policy_document():
    """A totally empty/None policy (never configured) still resolves via the
    catalog's own recommended tiers, not a crash."""
    assert policy_catalog.resolve_tier("ticktick-mcp.create_tasks", None) == "hard_manifest"
    assert policy_catalog.resolve_tier("ticktick-mcp.update_tasks", {}) == "soft_guard"


def test_resolve_ignores_garbage_override_values():
    """An invalid stored tier (e.g. from a corrupted/older doc) is ignored,
    falling through to the next resolution step rather than being returned
    as-is."""
    policy = {"defaults": {}, "tools": {"ticktick-mcp.update_tasks": "delete_everything"}}
    assert policy_catalog.resolve_tier("ticktick-mcp.update_tasks", policy) == "soft_guard"


def test_merged_defaults_overlays_owner_overrides_on_catalog():
    cat = policy_catalog.load_catalog()
    policy = {"defaults": {"destructive": "off"}}
    merged = policy_catalog.merged_defaults(policy, cat)
    assert merged["destructive"] == "off"          # owner override wins
    assert merged["mutating"] == cat["defaults"]["mutating"]  # untouched class keeps catalog default
