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
from ..ticktick.mcp_client import TickTickMCP, resolve_ticktick
from .auth import validate_init_data, verify_chat_token
from .transcript import group_messages, initials, sender_color

logger = logging.getLogger(__name__)

OWNER_ID_KEY = "owner_id"
_STATIC = Path(__file__).parent / "static"


async def _require_owner(request: web.Request) -> dict[str, Any]:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    data = validate_init_data(init_data, get_settings().bot_token)
    if not data:
        raise web.HTTPUnauthorized(text="invalid initData")
    uid = data["user"].get("id")
    if not await _is_owner(uid):
        raise web.HTTPForbidden(text="not the owner")
    return data


async def _is_owner(uid: int | None) -> bool:
    """True for the single owner. Before any owner is known (fresh bot) any
    validly-signed user may bootstrap as that owner."""
    if uid is None:
        return False
    owner = await repo.get_bot_state(OWNER_ID_KEY)
    if owner is None:
        return True  # fresh bot — allow bootstrap
    return int(owner) == uid


async def _tt_for(_data: dict[str, Any]) -> TickTickMCP | None:
    """The single global TickTick client (or None if not configured)."""
    return await resolve_ticktick()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def serve_app(_: web.Request) -> web.Response:
    page = (_STATIC / "app.html").read_text(encoding="utf-8")
    return web.Response(text=page, content_type="text/html")


def _zone_of(tz_name: str) -> Any:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
        return timezone.utc


def _fmt(dt: Any, zone: Any, pattern: str) -> str:
    """Format a (UTC) datetime in `zone`; '' if it can't be formatted."""
    try:
        return dt.astimezone(zone).strftime(pattern)
    except (ValueError, OSError, AttributeError):
        return ""


def _parse_ids(raw: str) -> set[str]:
    """Parse the `&m=1,2,3` source-message-id list into a set of strings."""
    return {p for p in (raw or "").replace(" ", "").split(",") if p}


def _render_group(grp: dict[str, Any], zone: Any, highlight: set[str]) -> tuple[str, str | None]:
    """One Telegram-style message group (same side + sender). Returns the HTML
    and the DOM id of the first highlighted bubble in it (or None)."""
    side = "out" if grp["direction"] == "out" else "in"
    sender = grp["senderName"]
    color = sender_color(sender)
    first_hl: str | None = None
    msgs = grp["messages"]
    bubbles = []
    for idx, m in enumerate(msgs):
        mid = str(m.get("messageId"))
        is_hl = mid in highlight
        if is_hl and first_hl is None:
            first_hl = f"msg-{mid}"
        cls = "msg " + side
        if idx == len(msgs) - 1:
            cls += " tail"
        if is_hl:
            cls += " highlight"
        name_html = ""
        if side == "in" and idx == 0 and sender:
            name_html = f'<div class="name" style="color:{color}">{html.escape(sender)}</div>'
        text = html.escape(m.get("text") or "")
        when = _fmt(m.get("date"), zone, "%H:%M")
        bubbles.append(
            f'<div class="{cls}" id="msg-{html.escape(mid)}">{name_html}'
            f'<div class="text">{text}</div>'
            f'<div class="time">{html.escape(when)}</div></div>'
        )
    stack = f'<div class="stack">{"".join(bubbles)}</div>'
    if side == "in":
        display = sender or "—"
        avatar = (
            f'<div class="avatar" style="background:{color}" title="{html.escape(display)}">'
            f'{html.escape(initials(sender))}</div>'
        )
        inner = avatar + stack
    else:
        inner = stack
    return f'<div class="group {side}">{inner}</div>', first_hl


def _render_transcript(
    messages: list[dict[str, Any]], tz_name: str, highlight: set[str]
) -> tuple[str, str | None]:
    """Render the whole transcript: date dividers, then grouped bubbles.

    All timestamps are shown in `tz_name` (the owner's zone). Returns the body
    HTML and the DOM id to scroll to (first highlighted message, if any)."""
    zone = _zone_of(tz_name)
    # Bucket by local day first so a date divider never splits a message group.
    buckets: list[tuple[str, list[dict[str, Any]]]] = []
    for m in messages:
        day = _fmt(m.get("date"), zone, "%d.%m.%Y")
        if not buckets or buckets[-1][0] != day:
            buckets.append((day, []))
        buckets[-1][1].append(m)

    first_hl: str | None = None
    parts: list[str] = []
    for day, day_msgs in buckets:
        parts.append(f'<div class="day"><span>{html.escape(day)}</span></div>')
        for grp in group_messages(day_msgs):
            grp_html, hl = _render_group(grp, zone, highlight)
            parts.append(grp_html)
            if hl and first_hl is None:
                first_hl = hl
    body = "\n".join(parts) or '<div class="empty">Сообщений пока нет.</div>'
    return body, first_hl


async def serve_chat(request: web.Request) -> web.Response:
    """Render a chat's stored transcript, Telegram-style. Auth via the signed
    token in the URL (so a plain link from a TickTick task works without
    Telegram initData). An optional `&m=<id1>,<id2>` list highlights the task's
    source messages and scrolls the page to the first of them."""
    chat_id = request.query.get("c", "")
    token = request.query.get("t", "")
    if not verify_chat_token(chat_id, token, get_settings().bot_token):
        raise web.HTTPForbidden(text="invalid or expired link")

    title = await repo.get_chat_title(chat_id)
    messages = await repo.get_chat_messages(chat_id)
    tz_name = get_settings().default_timezone
    highlight = _parse_ids(request.query.get("m", ""))

    body, anchor = _render_transcript(messages, tz_name, highlight)
    page = _CHAT_TEMPLATE.format(
        title=html.escape(title),
        body=body,
        count=len(messages),
        anchor=anchor or "",
    )
    return web.Response(text=page, content_type="text/html")


_CHAT_TEMPLATE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Переписка — {title}</title>
<style>
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#0e1621; color:#e9edf0;
    font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  .wrap {{ max-width:820px; margin:0 auto; padding:0 12px 48px; }}
  .header {{ position:sticky; top:0; z-index:5; background:#17212b;
    margin:0 -12px 6px; padding:12px 16px; border-bottom:1px solid #0b1219; }}
  .header h1 {{ font-size:16px; margin:0; }}
  .header .sub {{ color:#6d8296; font-size:12px; margin-top:2px; }}
  .day {{ text-align:center; margin:16px 0 8px; }}
  .day span {{ background:#1c2b3a; color:#c6d3de; font-size:12px;
    padding:3px 10px; border-radius:12px; }}
  .group {{ display:flex; align-items:flex-end; gap:8px; margin:2px 0; }}
  .group.out {{ flex-direction:row-reverse; }}
  .avatar {{ width:34px; height:34px; flex:0 0 34px; border-radius:50%;
    display:flex; align-items:center; justify-content:center;
    color:#fff; font-size:13px; font-weight:600; }}
  .stack {{ display:flex; flex-direction:column; max-width:76%; min-width:0; }}
  .group.out .stack {{ align-items:flex-end; }}
  .msg {{ position:relative; padding:6px 46px 7px 12px; margin:1px 0;
    border-radius:14px; background:#182533; box-shadow:0 1px 1px rgba(0,0,0,.25); }}
  .msg.out {{ background:#2b5278; }}
  .msg.in.tail {{ border-bottom-left-radius:5px; }}
  .msg.out.tail {{ border-bottom-right-radius:5px; }}
  .msg .name {{ font-size:13px; font-weight:600; margin-bottom:2px; }}
  .msg .text {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
  .msg .time {{ position:absolute; right:10px; bottom:5px; font-size:10px;
    color:#a7bccd; opacity:.8; }}
  .msg.out .time {{ color:#cfe0f0; }}
  .msg.highlight {{ background:#5a4b1c; box-shadow:0 0 0 2px #d8ad3e inset;
    animation:flash 1.2s ease-out 1; }}
  @keyframes flash {{ from {{ box-shadow:0 0 0 3px #ffdf7e inset; }}
    to {{ box-shadow:0 0 0 2px #d8ad3e inset; }} }}
  .empty {{ color:#6d8296; text-align:center; margin-top:48px; }}
</style></head>
<body>
  <div class="header">
    <h1>{title}</h1>
    <div class="sub">Сохранённая переписка · сообщений: {count}</div>
  </div>
  <div class="wrap">
  {body}
  </div>
  <script>
    (function() {{
      var a = "{anchor}";
      if (!a) return;
      var el = document.getElementById(a);
      if (el) el.scrollIntoView({{block: "center"}});
    }})();
  </script>
</body></html>"""


async def api_data(request: web.Request) -> web.Response:
    data = await _require_owner(request)
    tt = await _tt_for(data)

    # Degrade instead of failing the whole endpoint: if TickTick is down the
    # Mini App still shows chats/settings, with a "projects unavailable" banner
    # (projectsError) instead of a blank page.
    projects: list[Any] = []
    projects_error = False
    if tt is not None:
        try:
            projects = await tt.get_projects()
        except Exception:  # noqa: BLE001
            logger.exception("get_projects failed")
            projects_error = True

    chats = await repo.list_known_chats()
    bindings = {b["chatId"]: b for b in await repo.list_project_bindings()}
    msg_counts = await repo.chat_activity_scores()
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
                "activityScore": msg_counts.get(chat_id, 0),
            }
        )
    out_chats.sort(key=lambda c: c["activityScore"], reverse=True)
    return web.json_response(
        {
            "projects": projects,
            "chats": out_chats,
            "botUsername": await _bot_username(request),
            "needsTickTick": tt is None,
            "projectsError": projects_error,
        }
    )


async def api_sections(request: web.Request) -> web.Response:
    """List a project's sections (columns) for the section picker."""
    data = await _require_owner(request)
    tt = await _tt_for(data)
    if tt is None:
        return web.json_response({"error": "ticktick_not_connected"}, status=409)
    body = await request.json()
    project_id = (body or {}).get("projectId")
    if not project_id:
        return web.json_response({"error": "projectId required"}, status=400)
    try:
        sections = await tt.get_sections(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("get_sections failed")
        return web.json_response({"error": "ticktick_unreachable"}, status=502)
    return web.json_response({"sections": sections})


async def api_create_project(request: web.Request) -> web.Response:
    """POST /api/project {name} — create a new TickTick project (inline create
    from the project picker). Returns the refreshed project list + the new id so
    the Mini App can auto-select it as the binding."""
    data = await _require_owner(request)
    tt = await _tt_for(data)
    if tt is None:
        return web.json_response({"error": "ticktick_not_connected"}, status=409)
    body = await request.json()
    name = ((body or {}).get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    try:
        project_id = await tt.create_project(name)
        projects = await tt.get_projects()
    except Exception:  # noqa: BLE001
        logger.exception("create_project failed for %r", name)
        return web.json_response({"error": "ticktick_unreachable"}, status=502)
    if not project_id:
        return web.json_response({"error": "create_failed"}, status=502)
    logger.info("Mini App: created project %r -> %s", name, project_id)
    return web.json_response({"ok": True, "projectId": project_id, "projects": projects})


async def api_create_section(request: web.Request) -> web.Response:
    """POST /api/section {projectId, name} — create a new section (kanban column)
    inside a project (inline create from the section picker). Returns the
    refreshed section list + the new id so the Mini App can auto-select it."""
    data = await _require_owner(request)
    tt = await _tt_for(data)
    if tt is None:
        return web.json_response({"error": "ticktick_not_connected"}, status=409)
    body = await request.json()
    project_id = (body or {}).get("projectId")
    name = ((body or {}).get("name") or "").strip()
    if not project_id or not name:
        return web.json_response({"error": "projectId and name required"}, status=400)
    try:
        section_id = await tt.create_project_column(project_id, name)
        sections = await tt.get_sections(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("create_project_column failed for %r in %s", name, project_id)
        return web.json_response({"error": "ticktick_unreachable"}, status=502)
    if not section_id:
        return web.json_response({"error": "create_failed"}, status=502)
    logger.info("Mini App: created section %r -> %s in %s", name, section_id, project_id)
    return web.json_response({"ok": True, "sectionId": section_id, "sections": sections})


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
    data = await _require_owner(request)
    tt = await _tt_for(data)
    if tt is None:
        return web.json_response({"error": "ticktick_not_connected"}, status=409)
    body = await request.json()
    chat_id = (body or {}).get("chatId")
    project_id = (body or {}).get("projectId")
    section_id = (body or {}).get("sectionId") or None
    if not chat_id or not project_id:
        return web.json_response({"error": "chatId and projectId required"}, status=400)

    # Resolve the project name so bindings stay readable without a TickTick call.
    projects = await tt.get_projects()
    name = next((p["name"] for p in projects if p["id"] == project_id), "")
    if not name:
        return web.json_response({"error": "unknown project"}, status=400)

    # Resolve the section name too (best-effort) for a readable binding.
    section_name = None
    if section_id:
        try:
            for s in await tt.get_sections(project_id):
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


_SETTINGS_FIELDS = ("alias", "who", "topics", "task_side", "importance", "people", "filter_rules", "extract_rules", "section_map", "control_mode", "control_marker", "control_tag", "extract_model", "extract_effort", "system_prompt", "qwen_base_url", "daily_summary", "default_project_id", "default_section_id", "routes")


async def api_default_prompt(request: web.Request) -> web.Response:
    """GET /api/default-prompt — the built-in base extraction prompt, so the Mini
    App can show "Посмотреть дефолтный" without hardcoding it."""
    await _require_owner(request)
    from ..llm import claude  # local import to avoid a heavy import at module load
    return web.json_response({"prompt": claude.SYSTEM_PROMPT})


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


async def api_cre_notify(request: web.Request) -> web.Response:
    """POST /cre/notify — приём алярмов/сводок CRE-парсера (cre-parser/reporter.py).

    Auth: Bearer CRE_NOTIFY_SECRET — отдельный лёгкий секрет, НЕ бот-токен.
    Смысл: mac mini / CRE-сервис шлют отчёты в Telegram, вообще не касаясь
    BOT_TOKEN — бот сам отправляет текст своим токеном в CRE_REPORT_CHAT_ID
    (личка Максима, позже — группа). Только отправка (send-only): конфликтов
    getUpdates это не создаёт. Пустые env → 503, чтобы сбой был явным.
    """
    import os

    secret = os.environ.get("CRE_NOTIFY_SECRET", "")
    chat_id = os.environ.get("CRE_REPORT_CHAT_ID", "")
    auth = request.headers.get("Authorization", "")
    if not secret or auth != f"Bearer {secret}":
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    if not chat_id:
        return web.json_response(
            {"ok": False, "error": "CRE_REPORT_CHAT_ID не задан"}, status=503
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "empty text"}, status=400)
    bot = request.app["bot"]
    # Целевой чат может оказаться недоступным (бот не может писать первым:
    # "chat not found", если с этим id нет открытой переписки). Тогда фолбек —
    # owner_id из bot_state: туда watchdog уже успешно доставляет DM-ы.
    targets = [chat_id]
    try:
        owner = await repo.get_bot_state("owner_id")
        if owner and str(owner) != str(chat_id):
            targets.append(str(owner))
    except Exception:  # noqa: BLE001
        pass
    last_err = ""
    for target in targets:
        try:
            await bot.send_message(int(target), text[:4000])
            return web.json_response({"ok": True, "sent_to": target})
        except Exception as e:  # noqa: BLE001
            last_err = f"{target}: {type(e).__name__}: {str(e)[:120]}"
            logger.warning("cre/notify: не доставлено в %s", last_err)
    return web.json_response({"ok": False, "error": last_err}, status=502)


def build_app(bot: Any) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.add_routes(
        [
            web.get("/", health),
            web.get("/health", health),
            web.post("/cre/notify", api_cre_notify),
            web.get("/app", serve_app),
            web.get("/chat", serve_chat),
            web.post("/api/data", api_data),
            web.get("/api/data", api_data),
            web.post("/api/sections", api_sections),
            web.post("/api/project", api_create_project),
            web.post("/api/section", api_create_section),
            web.post("/api/bind", api_bind),
            web.post("/api/unbind", api_unbind),
            web.get("/api/default-prompt", api_default_prompt),
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
