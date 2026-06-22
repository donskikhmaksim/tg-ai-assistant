"""Voice transcription via the Mac mini's Whisper service.

The service sits behind the same token-gated Caddy/Funnel as Qwen, so we reuse
`qwen_api_key` as the bearer. Fails SOFT: any error returns None and the caller
simply skips that message (better to miss one voice note than crash capture).
"""
from __future__ import annotations

import logging

import aiohttp

from .config import get_settings

logger = logging.getLogger(__name__)


async def transcribe_audio(data: bytes, filename: str = "audio.ogg") -> str | None:
    """POST audio bytes to the Whisper service; return the transcript or None."""
    s = get_settings()
    if not s.transcribe_url:
        return None
    headers = {"Authorization": f"Bearer {s.qwen_api_key}"}
    form = aiohttp.FormData()
    form.add_field("file", data, filename=filename, content_type="application/octet-stream")
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(s.transcribe_url, data=form, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("Transcribe failed: HTTP %s", resp.status)
                    return None
                payload = await resp.json()
                text = (payload.get("text") or "").strip()
                return text or None
    except Exception:  # noqa: BLE001 — fail soft, skip this message
        logger.exception("Transcription error")
        return None
