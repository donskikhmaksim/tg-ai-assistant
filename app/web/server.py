"""Phase-2 Mini App: an aiohttp server running alongside bot polling.

Serves a Telegram WebApp (`/app`) and a small JSON API to bind chats to
TickTick projects. Every API call is authenticated with the Telegram WebApp
`initData` signature (HMAC-SHA256 keyed by the bot token) and restricted to
the bot owner once the owner id is known (set on business_connection).

The WebApp is served from the same origin as the API, so requests are
same-origin and need no CORS.
"""
from __future__ import annotations

import html
import logging
from datetime import timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

from .. import repositories as repo
from ..config import get_settings
from ..telegram.notify import group_watch_announcement
from ..ticktick.mcp_client import get_ticktick
from .auth import validate_init_data, verify_chat_token

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
    page = (_STATIC / "app.html").read_text(encoding="utf-8")
    return web.Response(text=page, content_type="text/html")


def _local_dt(dt: Any, tz_name: str) -> str:
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
        zone = timezone.utc
    try:
        return dt.astimezone(zone).strftime("%d.%m.%Y %H:%M")
    except (ValueError, OSError, AttributeError):
        return ""


async def serve_chat(request: web.Request) -> web.Response:
    """Render a chat's stored transcript. Auth via the signed token in the URL
    (so a plain link from a TickTick task works without Telegram initData)."""
    chat_id = request.query.get("c", "")
    token = request.query.get("t", "")
    if not verify_chat_token(chat_id, token, get_settings().bot_token):
        raise web.HTTPForbidden(text="invalid or expired link")

    title = await repo.get_chat_title(chat_id)
    messages = await repo.get_chat_messages(chat_id)
    tz_name = get_settings().default_timezone

    rows = []
    for m in messages:
        when = _local_dt(m.get("date"), tz_name)
        who = html.escape(m.get("senderName") or ("Я" if m.get("direction") == "out" else "—"))
        cls = "out" if m.get("direction") == "out" else "in"
        text = html.escape(m.get("text") or "")
        rows.append(
            f'<div class="msg {cls}"><div class="meta">{html.escape(when)} · {who}</div>'
            f'<div class="text">{text}</div></div>'
        )
    body = "\n".join(rows) or '<div class="empty">Сообщений пока нет.</div>'
    page = _CHAT_TEMPLATE.format(title=html.escape(title), body=body, count=len(messages))
    return web.Response(text=page, content_type="text/html")


_CHAT_TEMPLATE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Переписка — {title}</title>
<style>
  body {{ margin:0; padding:16px; max-width:760px; margin:0 auto;
    font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    background:#0f1115; color:#e8e8ea; }}
  h1 {{ font-size:18px; margin:4px 0 2px; }}
  .sub {{ color:#8a8f98; font-size:12px; margin-bottom:16px; }}
  .msg {{ padding:8px 12px; margin:6px 0; border-radius:12px; background:#1b1e26; }}
  .msg.out {{ background:#1d2b3a; }}
  .meta {{ color:#8a8f98; font-size:11px; margin-bottom:3px; }}
  .text {{ white-space:pre-wrap; word-break:break-word; }}
  .empty {{ color:#8a8f98; text-align:center; margin-top:40px; }}
</style></head>
<body>
  <h1>{title}</h1>
  <div class="sub">Сохранённая переписка · сообщений: {count}</div>
  {body}
</body></html>"""


async def api_data(request: web.Request) -> web.Response:
    await _require_owner(request)
    try:
        projects = await get_ticktick().get_projects()
    except Exception:  # noqa: BLE001
        logger.exception("get_projects failed")
        return web.json_response({"error": "ticktick_unreachable"}, status=502)

    chats = await repo.list_known_chats()
    bindings = {b["chatId"]: b for b in await repo.list_project_bindings()}
    msg_counts = await repo.count_messages_per_chat()
    out_chats = []
    for c in chats:
        chat_id = c["chatId"]
        settings_doc = await repo.get_chat_settings(chat_id)
        last_msg_at = c.get("lastMessageAt")
        out_chats.append(
            {
                "chatId": chat_id,
                "title": c.get("title") or chat_id,
                "kind": "group" if chat_id.startswith("group_") else "dm",
                "boundProjectId": bindings.get(chat_id, {}).get("ticktickProjectId"),
                "boundSectionId": bindings.get(chat_id, {}).get("ticktickSectionId"),
                "boundSectionName": bindings.get(chat_id, {}).get("sectionName"),
                "lastMessageAt": last_msg_at.isoformat() if last_msg_at else None,
                "alias": settings_doc.get("alias") or None,
                "messageCount": msg_counts.get(chat_id, 0),
            }
        )
    out_chats.sort(key=lambda c: c["messageCount"], reverse=True)
    return web.json_response(
        {"projects": projects, "chats": out_chats, "botUsername": await _bot_username(request)}
    )


async def api_sections(request: web.Request) -> web.Response:
    """List a project's sections (columns) for the section picker."""
    await _require_owner(request)
    body = await request.json()
    project_id = (body or {}).get("projectId")
    if not project_id:
        return web.json_response({"error": "projectId required"}, status=400)
    try:
        sections = await get_ticktick().get_sections(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("get_sections failed")
        return web.json_response({"error": "ticktick_unreachable"}, status=502)
    return web.json_response({"sections": sections})


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
    section_id = (body or {}).get("sectionId") or None
    if not chat_id or not project_id:
        return web.json_response({"error": "chatId and projectId required"}, status=400)

    # Resolve the project name so bindings stay readable without a TickTick call.
    projects = await get_ticktick().get_projects()
    name = next((p["name"] for p in projects if p["id"] == project_id), "")
    if not name:
        return web.json_response({"error": "unknown project"}, status=400)

    # Resolve the section name too (best-effort) for a readable binding.
    section_name = None
    if section_id:
        try:
            for s in await get_ticktick().get_sections(project_id):
                if s["id"] == section_id:
                    section_name = s["name"]
                    break
        except Exception:  # noqa: BLE001
            logger.exception("section name lookup failed")

    await repo.set_project_binding(chat_id, project_id, name, section_id, section_name)
    logger.info("Mini App: bound %s -> %s / %s (%s)", chat_id, name, section_name, project_id)

    # Announce in the group that surveillance now feeds this project/section.
    if chat_id.startswith("group_"):
        try:
            gid = int(chat_id[len("group_"):])
            await request.app["bot"].send_message(
                gid, group_watch_announcement(name, section_name)
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to announce binding in group %s", chat_id)

    return web.json_response({"ok": True, "projectName": name, "sectionName": section_name})


async def api_unbind(request: web.Request) -> web.Response:
    await _require_owner(request)
    body = await request.json()
    chat_id = (body or {}).get("chatId")
    if not chat_id:
        return web.json_response({"error": "chatId required"}, status=400)
    removed = await repo.delete_project_binding(chat_id)
    logger.info("Mini App: unbound %s (removed=%s)", chat_id, removed)
    return web.json_response({"ok": True, "removed": removed})


_SETTINGS_FIELDS = ("alias", "who", "topics", "task_side", "importance", "people", "filter_rules", "extract_rules")


async def api_get_settings(request: web.Request) -> web.Response:
    """GET /api/settings?chatId=... — вернуть настройки чата (или __global__)"""
    await _require_owner(request)
    chat_id = request.rel_url.query.get("chatId", "__global__")
    doc = await repo.get_chat_settings(chat_id)
    # Strip non-serializable MongoDB fields (_id, datetimes)
    safe = {k: v for k, v in doc.items() if k in _SETTINGS_FIELDS}
    return web.json_response(safe)


async def api_save_settings(request: web.Request) -> web.Response:
    """POST /api/settings — сохранить поля настроек"""
    await _require_owner(request)
    body = await request.json()
    chat_id = body.get("chatId", "__global__")
    fields = {k: v for k, v in body.items() if k in _SETTINGS_FIELDS}
    await repo.update_chat_settings(chat_id, fields)
    return web.json_response({"ok": True})


async def api_bulk_settings(request: web.Request) -> web.Response:
    """POST /api/settings/bulk — скопировать настройки из одного чата в несколько"""
    await _require_owner(request)
    body = await request.json()
    source_id = body.get("sourceId", "__global__")
    target_ids = body.get("targetIds", [])
    source_doc = await repo.get_chat_settings(source_id)
    fields = {k: v for k, v in source_doc.items() if k in _SETTINGS_FIELDS and v}
    for tid in target_ids:
        await repo.update_chat_settings(tid, fields)
    return web.json_response({"ok": True, "count": len(target_ids)})


def build_app(bot: Any) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.add_routes(
        [
            web.get("/", health),
            web.get("/health", health),
            web.get("/app", serve_app),
            web.get("/chat", serve_chat),
            web.post("/api/data", api_data),
            web.get("/api/data", api_data),
            web.post("/api/sections", api_sections),
            web.post("/api/bind", api_bind),
            web.post("/api/unbind", api_unbind),
            web.get("/api/settings", api_get_settings),
            web.post("/api/settings", api_save_settings),
            web.post("/api/settings/bulk", api_bulk_settings),
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
