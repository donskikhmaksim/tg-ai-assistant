"""Phase-2 Mini App: an aiohttp server running alongside bot polling.

Serves a Telegram WebApp (`/app`) and a small JSON API to bind chats to
TickTick projects. Every API call is authenticated with the Telegram WebApp
`initData` signature (HMAC-SHA256 keyed by the bot token) and restricted to
the bot owner once the owner id is known (set on business_connection).

The WebApp is served from the same origin as the API, so requests are
same-origin and need no CORS.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from .. import repositories as repo
from ..config import get_settings
from ..ticktick.mcp_client import get_ticktick
from .auth import validate_init_data

logger = logging.getLogger(__name__)

OWNER_ID_KEY = "owner_id"
_STATIC = Path(__file__).parent / "static"


async def _require_owner(request: web.Request) -> dict[str, Any]:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    data = validate_init_data(init_data, get_settings().bot_token)
    if not data:
        raise web.HTTPUnauthorized(text="invalid initData")
    owner_id = await repo.get_bot_state(OWNER_ID_KEY)
    uid = data["user"].get("id")
    # Enforce owner-only once we know the owner; before that (bot never
    # connected to Business yet) any validly-signed user is allowed to bootstrap.
    if owner_id is not None and uid != int(owner_id):
        raise web.HTTPForbidden(text="not the owner")
    return data


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def serve_app(_: web.Request) -> web.Response:
    html = (_STATIC / "app.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


async def api_data(request: web.Request) -> web.Response:
    await _require_owner(request)
    try:
        projects = await get_ticktick().get_projects()
    except Exception:  # noqa: BLE001
        logger.exception("get_projects failed")
        return web.json_response({"error": "ticktick_unreachable"}, status=502)

    chats = await repo.list_known_chats()
    bindings = {b["chatId"]: b for b in await repo.list_project_bindings()}
    out_chats = [
        {
            "chatId": c["chatId"],
            "title": c.get("title") or c["chatId"],
            "kind": "group" if c["chatId"].startswith("group_") else "dm",
            "boundProjectId": bindings.get(c["chatId"], {}).get("ticktickProjectId"),
        }
        for c in chats
    ]
    return web.json_response(
        {"projects": projects, "chats": out_chats, "botUsername": await _bot_username(request)}
    )


async def _bot_username(request: web.Request) -> str:
    """Cached bot username, for the WebApp's "add to group" deep link."""
    cached = request.app.get("bot_username")
    if cached:
        return cached
    try:
        me = await request.app["bot"].get_me()
        request.app["bot_username"] = me.username or ""
        return request.app["bot_username"]
    except Exception:  # noqa: BLE001
        return ""


async def api_bind(request: web.Request) -> web.Response:
    await _require_owner(request)
    body = await request.json()
    chat_id = (body or {}).get("chatId")
    project_id = (body or {}).get("projectId")
    if not chat_id or not project_id:
        return web.json_response({"error": "chatId and projectId required"}, status=400)

    # Resolve the project name so bindings stay readable without a TickTick call.
    projects = await get_ticktick().get_projects()
    name = next((p["name"] for p in projects if p["id"] == project_id), "")
    if not name:
        return web.json_response({"error": "unknown project"}, status=400)

    await repo.set_project_binding(chat_id, project_id, name)
    logger.info("Mini App: bound %s -> %s (%s)", chat_id, name, project_id)
    return web.json_response({"ok": True, "projectName": name})


async def api_unbind(request: web.Request) -> web.Response:
    await _require_owner(request)
    body = await request.json()
    chat_id = (body or {}).get("chatId")
    if not chat_id:
        return web.json_response({"error": "chatId required"}, status=400)
    removed = await repo.delete_project_binding(chat_id)
    logger.info("Mini App: unbound %s (removed=%s)", chat_id, removed)
    return web.json_response({"ok": True, "removed": removed})


def build_app(bot: Any) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.add_routes(
        [
            web.get("/", health),
            web.get("/health", health),
            web.get("/app", serve_app),
            web.post("/api/data", api_data),
            web.post("/api/bind", api_bind),
            web.post("/api/unbind", api_unbind),
        ]
    )
    return app


async def start_web(bot: Any) -> web.AppRunner:
    """Bind the aiohttp server on settings.port (Railway's $PORT)."""
    s = get_settings()
    runner = web.AppRunner(build_app(bot))
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=s.port)
    await site.start()
    logger.info("Mini App web server started on :%d", s.port)
    return runner
