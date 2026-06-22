"""Environment-backed configuration (see .env.example)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str = ""

    # Mongo
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "tg_ai_assistant"

    # Claude (Tier 2)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    anthropic_effort: str = "medium"  # low | medium | high | max

    # Qwen via Ollama (Tier 1)
    qwen_base_url: str = "http://localhost:11434/v1"
    qwen_model: str = "qwen2.5:32b-instruct"
    qwen_api_key: str = "ollama"

    # TickTick MCP (Railway, Streamable HTTP — full URL incl. secret path)
    ticktick_mcp_url: str = ""

    # Pipeline tuning
    batch_interval_min: int = 30  # scheduler tick; with debounce keep it small (2–3)
    # Debounce: process a dirty chat only once it's been quiet for quiet_minutes
    # (a settled thought, not mid-conversation), with a max-wait safety so a
    # never-quiet chat is still processed within max_dirty_minutes.
    quiet_minutes: int = 8
    max_dirty_minutes: int = 45
    conv_gap_hours: int = 6
    max_lookback_hours: int = 48
    raw_ttl_days: int = 30
    default_project: str = "Inbox"
    # TickTick id for the fallback project of unbound chats. The built-in Inbox
    # (id like "inbox<uid>") is NOT returned by get_projects, so it can only be
    # targeted by id. Takes priority over default_project (name) when set.
    default_project_id: str = ""

    # Web / Mini App (Phase 2)
    # Public https origin of this service, e.g. https://tg-ai-assistant-production.up.railway.app
    # Railway injects PORT; the aiohttp server binds it. WEBAPP_URL drives the
    # Telegram menu button and is the origin the WebApp calls back to.
    webapp_url: str = ""
    port: int = 8080

    @property
    def raw_ttl_seconds(self) -> int:
        return self.raw_ttl_days * 24 * 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
