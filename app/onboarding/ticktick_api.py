"""Per-user TickTick Open API client (direct, no MCP).

Multi-tenant Большой Брат holds each user's own OAuth tokens (in the vault) and
creates tasks in THAT user's TickTick account directly via the official Open
API — no per-user ticktick-mcp deploy. One instance of this client is bound to
one user's access/refresh token.

Only the surface the pipeline needs: projects, columns, create task, complete
task. Refreshes the access token on 401 and persists the new tokens back to the
vault via the injected `on_refresh` callback.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

BASE = "https://api.ticktick.com/open/v1"
TOKEN_URL = "https://ticktick.com/oauth/token"
TIMEOUT = 20


class TickTickAPI:
    def __init__(
        self,
        access_token: str,
        refresh_token: Optional[str] = None,
        client_id: str = "",
        client_secret: str = "",
        on_refresh: Optional[Callable[[str, Optional[str]], Awaitable[None]]] = None,
    ):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self._on_refresh = on_refresh

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"}

    async def _refresh(self) -> bool:
        if not (self.refresh_token and self.client_id and self.client_secret):
            return False
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.post(TOKEN_URL, data={
                    "grant_type": "refresh_token", "refresh_token": self.refresh_token,
                }, headers={"Authorization": f"Basic {auth}",
                            "Content-Type": "application/x-www-form-urlencoded"})
                r.raise_for_status()
                tok = r.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("TickTick refresh failed: %s", e)
            return False
        self.access_token = tok.get("access_token", self.access_token)
        if tok.get("refresh_token"):
            self.refresh_token = tok["refresh_token"]
        if self._on_refresh:
            try:
                await self._on_refresh(self.access_token, self.refresh_token)
            except Exception:  # noqa: BLE001
                logger.exception("on_refresh callback failed (continuing)")
        return True

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        url = f"{BASE}{path}"
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            resp = await c.request(method, url, headers=self._headers(), json=json)
            if resp.status_code == 401 and await self._refresh():
                resp = await c.request(method, url, headers=self._headers(), json=json)
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.text:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"error": "non-JSON response"}

    # --- surface used by the pipeline ---------------------------------------
    async def get_projects(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/project")
        return data if isinstance(data, list) else []

    async def get_columns(self, project_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/project/{project_id}/data")
        cols = (data or {}).get("columns", []) or []
        return sorted(cols, key=lambda x: x.get("sortOrder", 0))

    async def create_task(
        self, title: str, project_id: str, content: Optional[str] = None,
        due_date: Optional[str] = None, is_all_day: bool = False,
        column_id: Optional[str] = None,
    ) -> Optional[str]:
        body: dict[str, Any] = {"title": title, "projectId": project_id}
        if content:
            body["content"] = content
        if due_date:
            body["dueDate"] = due_date
            body["isAllDay"] = is_all_day
        if column_id:
            body["columnId"] = column_id
        res = await self._request("POST", "/task", body)
        return (res or {}).get("id")

    async def complete_task(self, project_id: str, task_id: str) -> None:
        await self._request("POST", f"/project/{project_id}/task/{task_id}/complete")
