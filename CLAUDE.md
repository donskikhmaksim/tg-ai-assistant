# CLAUDE.md — tg-ai-assistant

Context for any Claude Code session continuing this project. Read this first.

## What this is

Telegram AI assistant that reads the owner's conversations — personal DMs
(incoming **and own outgoing**, via Telegram Business) and groups — and on a
batched schedule extracts tasks/agreements/promises into TickTick.

Pipeline: capture every update → Mongo → every 30 min build a "conversation
window" per dirty chat → Qwen triage (local, Ollama) → Claude extraction
(`claude-opus-4-8`, structured output) → dedup → TickTick via MCP. Long-term
memory (`chat_summary` + open tasks) is separate from the window and survives
the 30-day raw TTL. Full spec: the original ТЗ (self-contained).

## Layout

- `app/config.py` — env settings (pydantic-settings). See `.env.example`.
- `app/db.py` — Motor connection + index bootstrap (raw TTL, unique dedupHash).
- `app/repositories.py` — all Mongo access (raw_messages, tasks, chat_state,
  chat_summary, chat_project_map, bot_state).
- `app/telegram/handlers_messages.py` — business_connection + business_message +
  group message capture → raw_messages (atomic, before any processing).
- `app/telegram/handlers_ui.py` — Phase-1 bind UX (/start menu, /bind, inline
  project picker).
- `app/llm/qwen.py` — Tier-1 triage (OpenAI SDK → Ollama), fails OPEN.
- `app/llm/claude.py` — Tier-2 extraction (`output_config.format` structured
  output + prompt caching + adaptive thinking).
- `app/pipeline/windows.py` — conversation-window builder (gap 6h, cap 48h).
- `app/pipeline/batch.py` — the §7 orchestrator.
- `app/ticktick/mcp_client.py` — Streamable-HTTP MCP client to the Railway
  `ticktick-mcp` server (parses formatted string output of get_projects /
  create_task / complete_task).
- `tests/` — pure-logic tests (windows, deadline formatting).

## Status (as of handoff)

Done & verified:
- Full implementation written, committed, pushed to `claude/untitled-session-v9hk5t`.
- Compiles, imports cleanly in a venv, 8 unit tests pass.
- Anthropic SDK accepts `output_config`/`thinking`; aiogram dispatcher requests
  `business_*` updates; Railway CLI command syntax verified.
- Telegram side: BotFather "Secretary Mode" (the renamed Business Mode) enabled,
  Group Privacy off, bot connected via Telegram Business (Manage Messages 5/5).

NOT done yet:
- Never run against live infra. No deploy yet.
- **TickTick MCP output parser is unverified against the live server** — first
  thing to check (tool names + exact string format vs `mcp_client.py` parser).
- Railway deploy pending (see runbook below).
- Qwen connectivity from cloud unresolved (see gotchas).

## Next steps

1. Verify TickTick MCP live: call `get_projects` / `create_task`, confirm the
   parser in `app/ticktick/mcp_client.py` matches real output.
2. Deploy to Railway (CLI runbook below).
3. Stand up MongoDB (Railway plugin) + set env vars.
4. Resolve Qwen reachability (tunnel) or rely on fail-open to Claude for now.
5. End-to-end test: send a DM task → confirm it lands in TickTick within 30 min.

## Railway CLI runbook (verified, CLI 5.20.0)

```bash
railway login
railway link                       # project that hosts ticktick-mcp
railway add -d mongo
railway add --service tg-ai-assistant \
  --repo donskikhmaksim/tg-ai-assistant \
  --branch claude/untitled-session-v9hk5t
railway variable set -s tg-ai-assistant \
  ANTHROPIC_MODEL=claude-opus-4-8 MONGO_DB=tg_ai_assistant \
  QWEN_MODEL=qwen2.5:32b-instruct QWEN_API_KEY=ollama \
  'MONGO_URL=${{MongoDB.MONGO_URL}}'
# secrets via stdin:
printf '%s' '<BOT_TOKEN>'         | railway variable set -s tg-ai-assistant BOT_TOKEN --stdin
printf '%s' '<ANTHROPIC_API_KEY>' | railway variable set -s tg-ai-assistant ANTHROPIC_API_KEY --stdin
printf '%s' '<TICKTICK_MCP_URL>'  | railway variable set -s tg-ai-assistant TICKTICK_MCP_URL --stdin
railway logs -s tg-ai-assistant
```

Expected logs: `Mongo connected` → `Batch scheduler started` → (after the bot is
connected in Telegram) `Business connection … for owner …`.

## Gotchas

- **Qwen runs locally (Mac mini), backend on Railway can't reach localhost.**
  Either expose Ollama via Tailscale/Cloudflare/ngrok and set `QWEN_BASE_URL`,
  or leave it unset — `qwen.has_task()` fails OPEN, so everything goes to Claude
  (works, just costs more).
- **`business_connection` only fires while the backend is running** — if the
  owner-id log never appears, re-add the bot in Telegram → Telegram Business →
  Chatbots after the service is up.
- BotFather's "Business Mode" is now labelled **Secretary Mode**.
- Updates are saved before processing — the DB is the only history; Telegram
  never resends.

## Run locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill values
python -m app.main
pytest                 # pure-logic tests
```

## Conventions

- Model: always `claude-opus-4-8`. Structured output via `output_config.format`
  (not the deprecated `output_format`). Adaptive thinking + prompt caching on the
  stable system prompt.
- Keep work on branch `claude/untitled-session-v9hk5t` unless told otherwise.
