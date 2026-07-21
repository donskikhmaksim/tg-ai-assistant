"""MCP client to the Railway-hosted `ticktick-mcp` server.

Transport: Streamable HTTP at the full URL (incl. /mcp/<secret> path).
The backend is the MCP *client*; Claude never touches MCP directly.

Tool surface (confirmed against the ticktick-mcp source). The server merged the
singular create/complete tools into array-based ones, so we call those:
  - get_projects() -> formatted text
  - list_project_columns(project_id) -> formatted text   (sections / "разделы")
  - create_tasks(summary, tasks=[{title, project_id, content, due_date, column_id, ...}])
  - complete_tasks(summary, tasks=[{task_id, project_id}])
  - update_tasks(summary, tasks=[...])

Sections are TickTick "columns": list them with `list_project_columns` and
file a task into one by passing `column_id` to create_task. (column_id is a
v2 field absent from the Open API, but the server accepts it transparently.)
If a project has no columns we simply skip the section step.

These tools return human-readable strings, not JSON, so we parse the
`Name:`/`ID:` lines out of the output.
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..config import get_settings

logger = logging.getLogger(__name__)


def _text(result: Any) -> str:
    """Concatenate text content blocks from an MCP tool result."""
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


_ID_RE = re.compile(r"^ID:\s*(\S+)\s*$", re.MULTILINE)


def _first_id(text: str) -> str | None:
    m = _ID_RE.search(text)
    return m.group(1) if m else None


# Tolerant key/name parsing for both projects and columns.
_NAME_RE = re.compile(r"^(?:Name|Title|Column|Section|Раздел)\s*:\s*(.+)$", re.I)
_KV_ID_RE = re.compile(r"^ID\s*:\s*(\S+)\s*$", re.I)
# get_projects blocks end the id on its own line as "(id: <id>)" (parenthesised,
# lowercase) — NOT "ID: <id>". Match that too, else the whole project list parses
# to empty (create_task still works, but the Mini App project picker goes blank).
_PAREN_ID_RE = re.compile(r"\(id:\s*([^)]+?)\)", re.I)
# list_project_columns format: "- <name>  (id: <id>)".
_BULLET_ID_RE = re.compile(r"^[-*]\s*(.+?)\s*\(id:\s*([^)]+?)\)\s*$", re.I)
# search_tasks format: "- [Project] <title>  (id:<id> proj:<pid>)" (no space
# after id:, and "proj:" right after) — used to recover a freshly-created id.
_SEARCH_ID_RE = re.compile(r"\(id:\s*(\S+?)\s+proj:", re.I)


def _parse_pairs(text: str) -> list[dict[str, str]]:
    """Parse a list of {name, id} from the server's formatted output.

    Handles three shapes: the `- <name>  (id: <id>)` lines from
    list_project_columns, the `Name:`/`ID:` blocks from get_projects, and a
    JSON array fallback — so it copes whatever the exact format is.
    """
    pairs: list[dict[str, str]] = []

    # Format A: "- <name>  (id: <id>)" per line (list_project_columns).
    for line in text.splitlines():
        m = _BULLET_ID_RE.match(line.strip())
        if m:
            pairs.append({"name": m.group(1).strip(), "id": m.group(2).strip()})
    if pairs:
        return pairs

    # Format B: "Name: ..." blocks whose id is either "ID: <id>" or a
    # parenthesised "(id: <id>)" line (get_projects). Other block lines
    # (Color/View Mode/Kind) are ignored until the id shows up.
    name: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        m_name = _NAME_RE.match(stripped)
        if m_name:
            name = m_name.group(1).strip()
            continue
        m_id = _KV_ID_RE.match(stripped) or _PAREN_ID_RE.search(stripped)
        if m_id and name is not None:
            pairs.append({"name": name, "id": m_id.group(1).strip()})
            name = None
    if pairs:
        return pairs

    # Format C: JSON array [{"id": ..., "name"/"title": ...}, ...].
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return pairs
    items = data if isinstance(data, list) else data.get("columns") or data.get("sections") if isinstance(data, dict) else None
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            ident = it.get("id") or it.get("columnId") or it.get("sectionId")
            label = it.get("name") or it.get("title") or it.get("label")
            if ident and label:
                pairs.append({"name": str(label), "id": str(ident)})
    return pairs


def _parse_projects(text: str) -> list[dict[str, str]]:
    """Parse `get_projects` output: blocks of `Name: ...` / `ID: ...`."""
    return _parse_pairs(text)


class TickTickMCP:
    """Thin async wrapper. Opens a fresh session per call — connection volume
    is low (a handful of bind/batch operations) and per-call sessions avoid
    holding a long-lived SSE stream across the 30-min idle between batches."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or get_settings().ticktick_mcp_url

    @asynccontextmanager
    async def _session(self):
        if not self.url:
            raise RuntimeError("TICKTICK_MCP_URL is not configured")
        async with streamablehttp_client(self.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def call(self, name: str, args: dict[str, Any]) -> str:
        async with self._session() as session:
            result = await session.call_tool(name, args)
            return _text(result)

    async def get_projects(self) -> list[dict[str, str]]:
        return _parse_projects(await self.call("get_projects", {}))

    async def get_sections(self, project_id: str) -> list[dict[str, str]]:
        """List a project's sections (TickTick columns). Empty if it has none."""
        try:
            return _parse_pairs(
                await self.call("list_project_columns", {"project_id": project_id})
            )
        except Exception:  # noqa: BLE001 — sections are best-effort
            logger.exception("list_project_columns failed for project %s", project_id)
            return []

    @staticmethod
    def _task_obj(
        title: str,
        project_id: str,
        content: str | None = None,
        due_date: str | None = None,
        section_id: str | None = None,
        is_all_day: bool = False,
    ) -> dict[str, Any]:
        obj: dict[str, Any] = {"title": title, "project_id": project_id}
        if content:
            obj["content"] = content
        if due_date:
            obj["due_date"] = due_date
        if is_all_day:
            obj["is_all_day"] = True
        if section_id:
            obj["column_id"] = section_id
        return obj

    async def create_task(
        self,
        title: str,
        project_id: str,
        content: str | None = None,
        due_date: str | None = None,
        section_id: str | None = None,
        is_all_day: bool = False,
        summary: str | None = None,
    ) -> str | None:
        """Create a single task via the array-based `create_tasks` tool (the
        singular `create_task` was merged into it). `create_tasks` does NOT echo
        the new id, so we recover it by searching for the exact title — otherwise
        the caller can't link it (breaks status-sync and makes re-pushes create
        duplicates). Returns the id, or None if it couldn't be found."""
        task = self._task_obj(title, project_id, content, due_date, section_id, is_all_day)
        text = await self.call(
            "create_tasks", {"summary": summary or title, "tasks": [task]}
        )
        return _first_id(text) or await self.find_task_id(title)

    async def find_task_id(self, title: str) -> str | None:
        """Look up a task id by its exact title via search_tasks. Best-effort:
        returns None if not found (e.g. the v2 cache hasn't settled yet)."""
        try:
            raw = await self.call("search_tasks", {"search_term": title})
        except Exception:  # noqa: BLE001
            logger.exception("search_tasks failed for %r", title)
            return None
        needle = title.strip().lower()
        for line in raw.splitlines():
            if needle and needle in line.lower():
                m = _SEARCH_ID_RE.search(line)
                if m:
                    return m.group(1)
        return None

    async def create_tasks(self, tasks: list[dict[str, Any]], summary: str) -> str:
        """Create MANY tasks in ONE call. Each item is a task dict from
        `_task_obj` (title/project_id/content/due_date/column_id/is_all_day).
        Returns the server's summary text."""
        return await self.call("create_tasks", {"summary": summary, "tasks": tasks})

    async def complete_task(self, project_id: str, task_id: str) -> str:
        """Complete a single task via the array-based `complete_tasks` tool."""
        return await self.call(
            "complete_tasks",
            {"summary": "Завершение задачи",
             "tasks": [{"task_id": task_id, "project_id": project_id}]},
        )


_client: TickTickMCP | None = None


def get_ticktick() -> TickTickMCP:
    global _client
    if _client is None:
        _client = TickTickMCP()
    return _client
