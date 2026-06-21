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
    batch_interval_min: int = 30
    conv_gap_hours: int = 6
    max_lookback_hours: int = 48
    raw_ttl_days: int = 30
    default_project: str = "Inbox"

    @property
    def raw_ttl_seconds(self) -> int:
        return self.raw_ttl_days * 24 * 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
