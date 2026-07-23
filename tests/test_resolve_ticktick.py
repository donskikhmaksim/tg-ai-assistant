"""resolve_ticktick() precedence: bot_state override > env TICKTICK_MCP_URL > None."""
import asyncio
from types import SimpleNamespace

import app.repositories as repo
import app.ticktick.mcp_client as mcp


def _patch(monkeypatch, *, override, env):
    async def fake_get_bot_state(key):
        assert key == "ticktick_mcp_url"
        return override

    monkeypatch.setattr(repo, "get_bot_state", fake_get_bot_state)
    monkeypatch.setattr(mcp, "get_settings", lambda: SimpleNamespace(ticktick_mcp_url=env))


def test_bot_state_override_wins_over_env(monkeypatch):
    _patch(monkeypatch, override="https://override/mcp", env="https://env/mcp")
    tt = asyncio.run(mcp.resolve_ticktick())
    assert tt is not None and tt.url == "https://override/mcp"


def test_env_used_when_no_override(monkeypatch):
    _patch(monkeypatch, override=None, env="https://env/mcp")
    tt = asyncio.run(mcp.resolve_ticktick())
    assert tt is not None and tt.url == "https://env/mcp"


def test_none_when_neither_set(monkeypatch):
    _patch(monkeypatch, override=None, env="")
    assert asyncio.run(mcp.resolve_ticktick()) is None
