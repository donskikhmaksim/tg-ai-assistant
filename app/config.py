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

    # Claude via CLI shim (Claude Code subscription on a Mac mini behind a
    # token-gated Funnel). When claude_cli_url is set, tier-2 extraction runs
    # through the shim (subscription) instead of the paid Anthropic API, with
    # NO API fallback — if the shim is unreachable the chat stays dirty and is
    # retried later. Empty → use the Anthropic API (anthropic_api_key).
    claude_cli_url: str = ""
    claude_cli_token: str = ""
    claude_cli_model: str = "opus"  # Claude Code model alias (opus | sonnet | full id)
    claude_cli_timeout: int = 300

    # Extraction watchdog. Probes the chain (tier-1 Qwen → tier-2 Claude →
    # TickTick) every healthcheck_interval_min and DMs the owner in Russian when
    # something breaks. Policy per error: alert immediately on a NEW breakage,
    # then at most once/day while it persists (the daily repeat is held until
    # healthcheck_hour local, i.e. the morning). See pipeline/watchdog.py.
    healthcheck_enabled: bool = True
    healthcheck_interval_min: int = 10  # how often to probe (catches new errors)
    healthcheck_hour: int = 9  # morning gate for the once-a-day repeat (default_timezone)

    # Pipeline tuning
    batch_interval_min: int = 2  # scheduler tick; debounce (quiet_minutes) gates real work
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
    # Name of the section/column inside the default project that unbound
    # ("мои", from-Telegram) tasks land in, so they're easy to triage. Resolved
    # to a column id at runtime via list_project_columns; if the column doesn't
    # exist (or the project has no columns), tasks fall to the project root.
    # Empty → no column routing. Set it (e.g. "TG") only if your default project
    # actually has a column with that name.
    default_section: str = ""
    # Explicit column id for the default section. Set this to bypass the
    # name lookup entirely — required for the built-in Inbox, whose columns the
    # API does NOT list (so default_section by name can't be resolved there).
    # Takes priority over default_section when set.
    default_section_id: str = ""
    # Reference timezone for a deadline that has a clock time but no city/zone
    # named in the conversation. IANA name (e.g. "Europe/Moscow"). Deadlines
    # discussed without a timezone are interpreted here. Set it to YOUR home
    # zone; the default is UTC so a fresh deploy never silently offsets times.
    default_timezone: str = "UTC"

    # Onboarding (public self-host). The project is a PUBLIC repo and every
    # person deploys their OWN fully-isolated instance — no secrets or GitHub
    # access are involved. These just populate the /start message shown to a
    # non-owner who wants their own bot. Empty → generic "ask the owner" text.
    onboarding_repo_url: str = ""          # e.g. https://github.com/<owner>/tg-ai-assistant
    onboarding_railway_template_url: str = ""  # optional "Deploy on Railway" one-click URL

    # Connector onboarding (/setup): hand a person a ONE-command install for their
    # OWN TickTick + Google MCP servers, connected to THEIR Claude. The bot fills
    # the owner's SHARED secrets into that command (shared Google OAuth client +
    # relay — see the Google MCP hub) and delivers it via a self-destruct note so
    # the secrets never sit in Telegram history. Empty secrets → /setup is off.
    # NOTE: these are the OWNER's secrets; anyone who can run /setup receives them.
    notes_base_url: str = ""  # Self-Destroyed-Notes origin, e.g. https://self-destroyed-notes-production.up.railway.app
    onboarding_google_setup_url: str = "https://github.com/donskikhmaksim/sheets-mcp/raw/main/scripts/setup.sh"
    onboarding_google_client_id: str = ""
    onboarding_google_client_secret: str = ""
    onboarding_relay_url: str = "https://maksims-mac-mini.taild91c23.ts.net"
    onboarding_relay_secret: str = ""
    onboarding_ticktick_setup_url: str = "https://github.com/donskikhmaksim/ticktick-mcp/raw/main/scripts/setup.sh"
    onboarding_ticktick_client_id: str = ""
    onboarding_ticktick_client_secret: str = ""
    # Owner-only "add my own Google account" button. Full add-account URL on the
    # Google MCP dashboard, e.g.
    # https://<google-mcp>.up.railway.app/dashboard/<DASHBOARD_SECRET>/add
    # The bot GETs it, captures the Google consent URL from the redirect, and
    # delivers it as a self-destruct note (so the dashboard secret never appears
    # in Telegram). Empty → the button says it's not configured.
    google_dashboard_add_url: str = ""

    # Web / Mini App (Phase 2)
    # Public https origin of this service, e.g. https://tg-ai-assistant-production.up.railway.app
    # Railway injects PORT; the aiohttp server binds it. WEBAPP_URL drives the
    # Telegram menu button and is the origin the WebApp calls back to.
    webapp_url: str = ""
    port: int = 8080

    # Voice transcription (Whisper on the Mac mini, behind the same token-gated
    # Funnel as Qwen). Empty → voice/audio messages are skipped (text only).
    # Uses the same bearer token as Qwen (qwen_api_key).
    transcribe_url: str = ""

    # Retrieval memory (local embeddings on the mini via Ollama, vectors in
    # Mongo). Embeds processed messages into a permanent archive and injects the
    # most relevant past ones into Claude — deep recall without widening the
    # window. Empty embed_model → retrieval disabled. Shares qwen_base_url/key.
    embed_model: str = "bge-m3"
    retrieve_top_k: int = 6
    retrieve_min_score: float = 0.45

    @property
    def raw_ttl_seconds(self) -> int:
        return self.raw_ttl_days * 24 * 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
