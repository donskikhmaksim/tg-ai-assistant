"""Конфигурация из переменных окружения (см. .env.example, §10 ТЗ)."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Telegram
    bot_token: str = Field(alias="BOT_TOKEN")

    # MongoDB
    mongo_url: str = Field(alias="MONGO_URL")
    mongo_db: str = Field(default="tg_ai_assistant", alias="MONGO_DB")

    # Claude
    anthropic_api_key: str = Field(alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-opus-4-8", alias="ANTHROPIC_MODEL")

    # Qwen / Ollama
    qwen_base_url: str = Field(alias="QWEN_BASE_URL")
    qwen_model: str = Field(default="qwen2.5:32b-instruct", alias="QWEN_MODEL")
    qwen_api_key: str = Field(default="ollama", alias="QWEN_API_KEY")

    # ticktick-mcp (Railway)
    ticktick_mcp_url: str = Field(alias="TICKTICK_MCP_URL")
    ticktick_mcp_transport: str = Field(default="sse", alias="TICKTICK_MCP_TRANSPORT")
    ticktick_mcp_auth_token: str | None = Field(
        default=None, alias="TICKTICK_MCP_AUTH_TOKEN"
    )

    # Пайплайн
    batch_interval_min: int = Field(default=30, alias="BATCH_INTERVAL_MIN")
    conv_gap_hours: int = Field(default=6, alias="CONV_GAP_HOURS")
    max_lookback_hours: int = Field(default=48, alias="MAX_LOOKBACK_HOURS")
    raw_ttl_days: int = Field(default=30, alias="RAW_TTL_DAYS")
    default_project: str = Field(default="Inbox", alias="DEFAULT_PROJECT")

    # Прочее
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
