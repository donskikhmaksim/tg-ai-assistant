"""MCP client to the Railway-hosted `ticktick-mcp` server.

Transport: Streamable HTTP at the full URL (incl. /mcp/<secret> path).
The backend is the MCP *client*; Claude never touches MCP directly.

Tool surface (confirmed against the ticktick-mcp source). The server merged the
singular create/complete tools into array-based ones, so we call those:
  - get_projects() -> formatted text
  - create_project(name) -> formatted text (echoes the new project + its id)
  - list_project_columns(project_id) -> formatted text   (sections / "разделы")
  - create_project_column(project_id, name) -> formatted text (new column + id)
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


def _any_id(text: str) -> str | None:
    """First id in either shape the server emits: an `ID: <id>` line or a
    parenthesised `(id: <id>)`. create_project / create_project_column echo the
    created object as a formatted block whose id may be parenthesised (lowercase)
    rather than an `ID:` line, so we tolerate both to recover the new id."""
    if (mid := _first_id(text)) is not None:
        return mid
    m = _PAREN_ID_RE.search(text)
    return m.group(1).strip() if m else None


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
# search_tasks line: "- [Project] <title> · due <d>, P-High #tag  (id:<id> proj:<pid>)".
# Capture the title so we can match it EXACTLY (a substring match would link a
# task to a longer near-duplicate's id). The optional " · <meta>" block between
# title and "(id:" (due/priority/tags) and the "↳ " subtask marker must be
# stripped, else any task WITH metadata never exact-matches its own title.
_SEARCH_TITLE_RE = re.compile(
    r"^(?:↳\s*)?[-*]\s*(?:↳\s*)?(?:\[[^\]]*\]\s*)?(.+?)"
    r"(?:\s+·\s+(?:due |P-|#)[^()]*)?\s*\(id:",
    re.I,
)
# get_project_tasks / search_tasks task lines: "- [Project] <title>  (id:<id> ...)"
# The optional "[...]" label and trailing "proj:<pid>" are tolerated.
_TASK_LINE_RE = re.compile(
    r"^[-*]\s*(?:\[[^\]]*\]\s*)?(.+?)\s*\(id:\s*(\S+?)(?:\s+proj:[^)]*)?\)\s*$", re.I
)


def _parse_task_lines(text: str) -> list[dict[str, str]]:
    """Parse `- <title>  (id:<id> ...)` task lines into [{title, id}].

    Tolerant of an optional `[Project]` label prefix and a trailing `proj:<pid>`.
    Non-matching lines (headers, blanks) are ignored, so it degrades to [] on an
    unrecognised format instead of raising."""
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        m = _TASK_LINE_RE.match(line.strip())
        if m:
            out.append({"title": m.group(1).strip(), "id": m.group(2).strip()})
    return out


# get_project_tasks emits rich `Task N:` blocks, NOT bullets: a `Title:` line,
# optional `Due Date:`/`Priority:`/`Status:`, a multi-line `Content:` section,
# then a closing `(id: <id> | project: <pid>)` line. Parse the full card so the
# semantic dedup / curation can weigh content + due, not just the title.
_BLOCK_SPLIT_RE = re.compile(r"(?m)^Task\s+\d+:\s*$")
_BLK_TITLE_RE = re.compile(r"(?m)^Title:\s*(.+?)\s*$")
_BLK_DUE_RE = re.compile(r"(?m)^Due Date:\s*(.+?)\s*$")
_BLK_PRIO_RE = re.compile(r"(?m)^Priority:\s*(.+?)\s*$")
_BLK_STATUS_RE = re.compile(r"(?m)^Status:\s*(.+?)\s*$")
_BLK_ID_RE = re.compile(r"\(id:\s*(\S+?)\s*\|\s*project:\s*([^)]+?)\)", re.I)
_BLK_CONTENT_RE = re.compile(r"(?ms)^Content:\s*\n(.*?)(?=\n\(id:|\Z)")


def _parse_project_cards(text: str) -> list[dict[str, str]]:
    """Parse get_project_tasks' `Task N:` blocks into rich cards:
    [{id, title, due, priority, status, content}] (missing fields omitted).

    Falls back to the bullet parser (search_tasks shape) when no blocks are
    present, so it copes with either output. Returns [] on anything unrecognised.
    """
    blocks = _BLOCK_SPLIT_RE.split(text)
    cards: list[dict[str, str]] = []
    for blk in blocks:
        m_id = _BLK_ID_RE.search(blk)
        m_title = _BLK_TITLE_RE.search(blk)
        if not (m_id and m_title):
            continue
        card = {"id": m_id.group(1).strip(), "title": m_title.group(1).strip()}
        if (m := _BLK_DUE_RE.search(blk)) and m.group(1).strip().lower() != "none":
            card["due"] = m.group(1).strip()
        if (m := _BLK_PRIO_RE.search(blk)) and m.group(1).strip().lower() != "none":
            card["priority"] = m.group(1).strip()
        if (m := _BLK_STATUS_RE.search(blk)):
            card["status"] = m.group(1).strip()
        if (m := _BLK_CONTENT_RE.search(blk)):
            content = m.group(1).strip()
            if content:
                card["content"] = content
        cards.append(card)
    return cards or _parse_task_lines(text)


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

    async def create_project(self, name: str) -> str | None:
        """Create a new TickTick project and return its id (best-effort).

        The server echoes the created project as a formatted block; we recover
        the id from either the `ID:`/`(id: …)` line or, failing that, by looking
        it up by name in get_projects. Returns None if it can't be resolved."""
        text = await self.call("create_project", {"name": name})
        pid = _any_id(text)
        if pid:
            return pid
        for p in await self.get_projects():
            if p["name"] == name:
                return p["id"]
        return None

    async def create_project_column(self, project_id: str, name: str) -> str | None:
        """Create a section (kanban column) inside a project and return its id.

        Recovers the id from the tool's echoed block, falling back to a
        list_project_columns lookup by name. Returns None if unresolved."""
        text = await self.call(
            "create_project_column", {"project_id": project_id, "name": name}
        )
        cid = _any_id(text)
        if cid:
            return cid
        for c in await self.get_sections(project_id):
            if c["name"] == name:
                return c["id"]
        return None

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
        tags: list[str] | None = None,
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
        if tags:
            # `tags` is a list of tag names; the server creates missing ones. Like
            # column_id this is a v2 field the server accepts transparently.
            obj["tags"] = tags
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
        tags: list[str] | None = None,
    ) -> str | None:
        """Create a single task via the array-based `create_tasks` tool (the
        singular `create_task` was merged into it).

        `create_tasks` now echoes the created id inline as `(id:<id>)` on the
        result line, so we read it straight from the output — no title search.
        The `find_task_id` fallback stays only as defence for an older server
        build that doesn't emit the id yet; on the current server it's never hit.
        Returns the id, or None if it couldn't be resolved."""
        task = self._task_obj(title, project_id, content, due_date, section_id, is_all_day, tags)
        text = await self.call(
            "create_tasks", {"summary": summary or title, "tasks": [task]}
        )
        m = _PAREN_ID_RE.search(text)
        if m:
            return m.group(1).strip()
        return _first_id(text) or await self.find_task_id(title)

    async def find_task_id(self, title: str) -> str | None:
        """Look up a task id by its EXACT title via search_tasks. Best-effort:
        returns None if no task with this exact title is found (e.g. the v2 cache
        hasn't settled yet). Exact match — NOT substring: a substring match would
        wrongly link a task to a longer near-duplicate's id, so two different docs
        could share one ticktickTaskId."""
        try:
            raw = await self.call("search_tasks", {"search_term": title})
        except Exception:  # noqa: BLE001
            logger.exception("search_tasks failed for %r", title)
            return None
        needle = title.strip().lower()
        for line in raw.splitlines():
            m_id = _SEARCH_ID_RE.search(line)
            if not m_id:
                continue
            m_title = _SEARCH_TITLE_RE.match(line.strip())
            if m_title and m_title.group(1).strip().lower() == needle:
                return m_id.group(1)
        return None

    async def get_project_tasks(
        self, project_id: str, limit: int = 200
    ) -> list[dict[str, str]]:
        """Open/active tasks in a project as rich cards [{id, title, due,
        priority, status, content}] (best-effort; fields beyond id/title omitted
        when absent).

        Used by the semantic dedup to compare a new task against what's already
        in the bound project. Capped at `limit` so a huge project can't blow up
        the batch. Returns [] on any error or unrecognised output — the caller
        then simply compares against the chat's local open tasks only."""
        try:
            raw = await self.call("get_project_tasks", {"project_id": project_id})
        except Exception:  # noqa: BLE001 — dedup is best-effort
            logger.exception("get_project_tasks failed for project %s", project_id)
            return []
        return _parse_project_cards(raw)[:limit]

    async def add_task_comment(
        self, project_id: str, task_id: str, content: str, task_title: str = ""
    ) -> str:
        """Append a comment to an existing task (enrich, append-only — no
        overwrite risk). Used when a new task is a semantic duplicate of one
        already in TickTick.

        The server tool signature is (task_title, text, project_id, task_id) —
        `task_title` is display-only (confirmation dialog) but REQUIRED, and the
        body param is `text`, not `content`. We used to send {content,…} and
        every call failed schema validation silently."""
        return await self.call(
            "add_task_comment",
            {
                "task_title": task_title or "(задача)",
                "text": content,
                "project_id": project_id,
                "task_id": task_id,
            },
        )

    async def create_tasks(self, tasks: list[dict[str, Any]], summary: str) -> str:
        """Create MANY tasks in ONE call. Each item is a task dict from
        `_task_obj` (title/project_id/content/due_date/column_id/is_all_day).
        Returns the server's summary text."""
        return await self.call("create_tasks", {"summary": summary, "tasks": tasks})

    async def complete_task(
        self, project_id: str, task_id: str, title: str = ""
    ) -> str:
        """Complete a single task via the array-based `complete_tasks` tool.

        Pass `title` to arm the server's identity guard: it cross-checks the id
        against the live task's title and REFUSES to complete a different task if
        a stale id points elsewhere. Omitting it keeps the id-only behaviour."""
        task: dict[str, Any] = {"task_id": task_id, "project_id": project_id}
        if title:
            task["title"] = title
        return await self.call(
            "complete_tasks",
            {"summary": "Завершение задачи", "tasks": [task]},
        )


_client: TickTickMCP | None = None


def get_ticktick() -> TickTickMCP:
    global _client
    if _client is None:
        _client = TickTickMCP()
    return _client
