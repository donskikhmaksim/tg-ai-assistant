"""MCP client to the Railway-hosted `ticktick-mcp` server.

Transport: Streamable HTTP at the full URL (incl. /mcp/<secret> path).
The backend is the MCP *client*; Claude never touches MCP directly.

Tool surface (confirmed against the ticktick-mcp source):
  - get_projects() -> formatted text
  - create_task(title, project_id, content=, due_date=, ...) -> formatted text
  - update_task(task_id, project_id, ...) -> formatted text
  - complete_task(project_id, task_id) -> formatted text

These tools return human-readable strings, not JSON, so we parse the
`Name:`/`ID:` lines out of the output.
"""
from __future__ import annotations

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


def _parse_projects(text: str) -> list[dict[str, str]]:
    """Parse `get_projects` output: blocks of `Name: ...` / `ID: ...`."""
    projects: list[dict[str, str]] = []
    name: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            name = line[len("Name:"):].strip()
        elif line.startswith("ID:") and name is not None:
            projects.append({"name": name, "id": line[len("ID:"):].strip()})
            name = None
    return projects


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

    async def create_task(
        self,
        title: str,
        project_id: str,
        content: str | None = None,
        due_date: str | None = None,
    ) -> str | None:
        """Create a task; returns the new TickTick task id (or None if unparsable)."""
        args: dict[str, Any] = {"title": title, "project_id": project_id}
        if content:
            args["content"] = content
        if due_date:
            args["due_date"] = due_date
        return _first_id(await self.call("create_task", args))

    async def complete_task(self, project_id: str, task_id: str) -> str:
        return await self.call("complete_task", {"project_id": project_id, "task_id": task_id})


_client: TickTickMCP | None = None


def get_ticktick() -> TickTickMCP:
    global _client
    if _client is None:
        _client = TickTickMCP()
    return _client
