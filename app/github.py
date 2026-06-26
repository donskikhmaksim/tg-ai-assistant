"""GitHub API helpers — invite collaborators for self-hosted onboarding."""
from __future__ import annotations

import httpx


async def add_collaborator(token: str, repo: str, username: str) -> bool:
    """Invite `username` as a collaborator on `repo` (owner/name).

    Returns True on success (201 created or 204 already exists).
    Raises httpx.HTTPStatusError on API errors.
    """
    url = f"https://api.github.com/repos/{repo}/collaborators/{username}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(url, headers=headers, json={"permission": "pull"})
        if r.status_code in (201, 204):
            return True
        r.raise_for_status()
        return False
