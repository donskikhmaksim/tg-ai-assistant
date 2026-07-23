"""Manifest-policy admin — Phase 1.

Per-tool tri-state enforcement policy ({tool -> hard_manifest|soft_guard|off}),
stored centrally in this bot's own Mongo and edited from the owner-only Mini
App. This package is the STORAGE + UI plane only: nothing here enforces the
policy. Enforcement is a separate, later phase that lives in each MCP server's
own repo (e.g. ticktick-mcp), which will pull this policy over HTTPS (see the
`GET /policy` machine endpoint in app/web/server.py) and cache it locally.

See app/policy/catalog.py for the static tool catalog + resolution logic, and
app/policy/catalog.json for the seed data.
"""
