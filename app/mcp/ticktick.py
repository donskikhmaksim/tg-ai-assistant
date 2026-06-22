"""MCP-клиент к Railway-серверу `ticktick-mcp` (§4, §7, §12 ТЗ).

Бэкенд выступает MCP-клиентом и переиспользует токены, заведённые на сервере —
нового OAuth к TickTick не требуется. Транспорт: SSE или Streamable HTTP по URL.
Имена тулов подтверждены по схеме сервера: get_projects / create_task /
update_task / complete_task.

Соединение поднимается на каждую операцию (короткоживущая сессия) — это просто
и надёжно для пакетного воркера и бота.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger(__name__)


class TickTickMCP:
    def __init__(
        self,
        url: str,
        transport: str = "sse",
        auth_token: str | None = None,
    ) -> None:
        self._url = url
        self._transport = transport.lower()
        self._headers: dict[str, str] = {}
        if auth_token:
            self._headers["Authorization"] = f"Bearer {auth_token}"

    @asynccontextmanager
    async def _session(self):
        if self._transport in ("streamable-http", "streamable_http", "http"):
            async with streamablehttp_client(self._url, headers=self._headers) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:  # sse (по умолчанию)
            async with sse_client(self._url, headers=self._headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    # ── низкоуровневый вызов ──────────────────────────────────────────────────
    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        async with self._session() as session:
            result = await session.call_tool(name, arguments)
        return _extract_payload(result)

    # ── высокоуровневые операции ──────────────────────────────────────────────
    async def get_projects(self) -> list[dict[str, str]]:
        """Список проектов как [{'id': ..., 'name': ...}]."""
        payload = await self._call("get_projects", {})
        return _parse_projects(payload)

    async def create_task(
        self,
        title: str,
        project_id: str,
        content: str | None = None,
        due_date: str | None = None,
    ) -> str | None:
        """Создать задачу. Вернуть TickTick task id (best-effort) либо None."""
        args: dict[str, Any] = {"title": title, "project_id": project_id}
        if content:
            args["content"] = content
        if due_date:
            args["due_date"] = due_date
        payload = await self._call("create_task", args)
        return _parse_task_id(payload)

    async def complete_task(self, project_id: str, task_id: str) -> bool:
        await self._call(
            "complete_task", {"project_id": project_id, "task_id": task_id}
        )
        return True


# ── разбор ответов MCP ────────────────────────────────────────────────────────
def _extract_payload(result: Any) -> Any:
    """Достать полезную нагрузку из CallToolResult: structuredContent или текст."""
    # FastMCP-сервера кладут структуру в structuredContent (часто под "result")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    if structured is not None:
        return structured

    # иначе — конкатенированный текст блоков
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    text = "\n".join(parts).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text  # сырой текст — разберём эвристиками ниже


def _parse_projects(payload: Any) -> list[dict[str, str]]:
    projects: list[dict[str, str]] = []
    items = payload
    if isinstance(payload, dict):
        # бывает {"projects": [...]} или сам словарь — обернём
        items = payload.get("projects", payload.get("data", [payload]))
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                pid = it.get("id") or it.get("projectId") or it.get("project_id")
                name = it.get("name") or it.get("title") or "(без названия)"
                if pid:
                    projects.append({"id": str(pid), "name": str(name)})
    if projects:
        return projects

    # эвристика по тексту: строки вида "Name (ID: xxx)" или "ID: xxx ... Name: yyy"
    if isinstance(payload, str):
        import re

        for m in re.finditer(r"(.+?)\s*\(ID:\s*([^)]+)\)", payload):
            projects.append({"name": m.group(1).strip(), "id": m.group(2).strip()})
    return projects


def _parse_task_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        return (
            payload.get("id")
            or payload.get("taskId")
            or payload.get("task_id")
            or None
        )
    if isinstance(payload, str):
        import re

        m = re.search(r"ID:\s*([0-9a-fA-F]+)", payload)
        if m:
            return m.group(1)
    return None
