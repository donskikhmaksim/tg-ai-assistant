"""Static tool catalog + tier-resolution logic for the manifest-policy admin.

Phase 1 scope (see app/policy/__init__.py): this module only *describes* tools
and *resolves* what tier currently applies to one, for the Mini App to display
and edit. It does not gate any tool call — no MCP server consumes this yet.

Schema (app/policy/catalog.json):
    {
      "defaults": {"destructive": tier, "external": tier, "mutating": tier, "read": tier},
      "tools": {
        "<server>.<tool>": {
          "class": "destructive" | "external" | "mutating" | "read",
          "recommended_tier": one of TIERS,
          "has_manifest": bool,   # has an existing plan_*/execute_* (or
                                  # automation_key) escalation path already
          "label": "human label (Russian, for the Mini App)",
          "note": "optional extra context (Mini App tooltip)",
        },
        ...
      }
    }

The *policy document* (app/repositories.py::get_policy/save_policy) is a
separate, much smaller thing: just the owner's class-wide `defaults` overrides
and per-tool `tools` overrides. Resolution order for one tool T (§1.2 of the
design doc):

    policy.tools[T]              (explicit owner override for this exact tool)
    -> policy.defaults[class(T)] (owner override for the tool's whole class)
    -> catalog.tools[T].recommended_tier  (this catalog's own per-tool seed)
    -> "soft_guard"               (fail-safe: an unknown tool is guarded, never bare)
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CATALOG_PATH = Path(__file__).parent / "catalog.json"

TIERS = ("hard_manifest", "soft_guard", "off")
CLASSES = ("destructive", "external", "mutating", "read")

# Fail-safe: an unknown tool (not in the catalog, no override) resolves here —
# guarded, never bare. Mirrors the design's resolution-order fallback.
FAIL_SAFE_TIER = "soft_guard"


@lru_cache
def load_catalog() -> dict[str, Any]:
    """The static catalog: {"defaults": {...}, "tools": {"<server>.<tool>": {...}}}.

    Cached (it's read-only, bundled data) — call `load_catalog.cache_clear()`
    in tests if you need to reload after editing the file on disk.
    """
    return json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))


def resolve_tier(
    tool_key: str, policy: dict[str, Any] | None, catalog: dict[str, Any] | None = None
) -> str:
    """Resolve the tier that currently applies to `tool_key`.

    `policy` is the stored policy doc (or {} / None for "never configured" —
    everything then falls back to the catalog). See the module docstring for
    the resolution order.
    """
    catalog = catalog if catalog is not None else load_catalog()
    policy = policy or {}

    overrides = policy.get("tools") or {}
    override = overrides.get(tool_key)
    if override in TIERS:
        return override

    entry = catalog.get("tools", {}).get(tool_key)
    owner_defaults = policy.get("defaults") or {}

    klass = entry.get("class") if entry else None
    if klass and owner_defaults.get(klass) in TIERS:
        return owner_defaults[klass]

    if entry and entry.get("recommended_tier") in TIERS:
        return entry["recommended_tier"]

    # Tool not in the catalog at all (or catalog entry missing a usable
    # tier) — still try the catalog's OWN class defaults before giving up,
    # then fail safe.
    catalog_defaults = catalog.get("defaults") or {}
    if klass and catalog_defaults.get(klass) in TIERS:
        return catalog_defaults[klass]
    return FAIL_SAFE_TIER


def merged_defaults(policy: dict[str, Any] | None, catalog: dict[str, Any] | None = None) -> dict[str, str]:
    """Catalog defaults overlaid by the owner's own class-wide overrides."""
    catalog = catalog if catalog is not None else load_catalog()
    policy = policy or {}
    return {**catalog.get("defaults", {}), **(policy.get("defaults") or {})}
