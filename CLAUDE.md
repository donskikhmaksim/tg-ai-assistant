# CLAUDE.md — tg-ai-assistant

Context for any Claude Code session continuing this project. Read this first.

## What this is

Telegram AI assistant that reads the owner's conversations — personal DMs
(incoming **and own outgoing**, via Telegram Business) and groups — and on a
batched schedule extracts tasks/agreements/promises into TickTick.

Pipeline: capture every update → Mongo → on a debounce build a "conversation
window" per dirty chat → Qwen triage (optional, Ollama) → Claude extraction
(`claude-opus-4-8`, structured output) → dedup → TickTick via MCP. Long-term
memory (`chat_summary` + open tasks) is separate from the window and survives
the 90-day raw TTL. Full spec: the original ТЗ (self-contained); the detailed
feature catalog is `docs/FEATURES.md`.

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
- `app/web/server.py` + `app/web/static/app.html` — the owner-only Mini App
  (settings cabinet); `app/web/static/onboarding.html` + `app/onboarding/
  ai_help.py` — the pre-auth onboarding screen + "Ask AI" Q&A helper.
- `app/policy/` — manifest-policy admin (Phase 1: storage + Mini App UI only,
  see its Status entry below). `catalog.json` is the static tool catalog
  (class + recommended tier + has_manifest per tool); `catalog.py` resolves
  the effective tier; `GET/POST /api/policy` (owner-auth, `app/web/server.py`)
  + `repositories.get_policy/save_policy` are the store.
- `tests/` — pure-logic tests (windows, deadline formatting).

## Status

Live and verified in production (Railway, auto-deploy from `main`):
- Deployed and processing the owner's real chats end-to-end: DM/group capture →
  debounced batch → extraction → TickTick. MongoDB via the Railway plugin.
- TickTick MCP live-verified and hardened: parser matches the real server
  output; identity guards armed (task title on complete, `automation_key` on
  direct create); silent-failure bugs from the MCP audit fixed.
- Semantic dedup shipped (three-band cosine + gray-zone LLM judge; uncertainty
  always creates, never drops) with candidates from all routed destinations.
- Topic routing shipped: one chat's tasks can fan out into several projects via
  a per-chat route map; unlabelled tasks fall back to the chat binding.
- Mini App shipped: per-chat/global settings cabinet (prompts, model/effort,
  control, routes, daily summary, project/section picker with inline create),
  Telegram-style transcript page with task deep links.
- «Контроль» attribution, extraction watchdog (DMs the owner on chain
  breakage), opt-in end-of-day group summaries, voice transcription hook.
- Strictly SINGLE-TENANT: one owner per instance (`bot_state.owner_id`), one
  global TickTick connector via `resolve_ticktick()` (env `TICKTICK_MCP_URL` or
  the `/connect`-set `bot_state["ticktick_mcp_url"]` override). The multi-tenant
  serving machinery (per-user vault, connection→owner registry, per-chat owner
  routing) was removed in `feat/single-tenant-and-autoupdate`.
- Onboarding/distribution KEPT (that IS the model): invite-gated `/setup`
  connector installers + `scripts/setup.sh` self-deploy of the bot (now forks
  upstream into the deployer's GitHub and connects the fork); fork auto-sync
  workflow (every ~5 min) for deployers.
- Manifest-policy admin — **Phase 1 only**: per-tool tri-state confirmation
  policy (`hard_manifest` | `soft_guard` | `off`, keyed `"<server>.<tool>"`)
  with a class-based (destructive/external/mutating/read) fallback, stored in
  this bot's own Mongo and edited from the Mini App's "🛡 Манифест-политика"
  screen (`app/policy/`, `GET/POST /api/policy`). This phase is STORAGE + UI
  ONLY — nothing enforces the resolved tier yet, on this or any other server.
  A static catalog (`app/policy/catalog.json`) seeds ~70 known ticktick-mcp
  tools with a class + recommended tier ahead of the full #54 audit landing.
  Also ships a machine-readable `GET /policy` (bearer `POLICY_PULL_TOKEN`,
  ETag/304) for a LATER phase where each MCP server (ticktick-mcp first) pulls
  and enforces this policy in its own repo — not built here.
- Mini App onboarding screen (`/onboarding`, `app/web/static/onboarding.html`):
  a friendlier self-host walkthrough (deploy links/CLI one-liner, a client-side
  "check my deploy" health probe) plus an "Ask AI" Q&A box — the ONE Mini App
  route not gated by owner auth (`POST /api/onboarding/ask`), mitigated with a
  kill switch (`ONBOARDING_AI_HELP_ENABLED`), a message-length cap, an
  in-memory per-IP rate limit keyed on the real peer address (`request.remote`
  — NOT the client-supplied `X-Onboarding-Session` header, after an
  adversarial review found that header trivially bypassable), and a hard
  aggregate cap across all callers (`ONBOARDING_AI_GLOBAL_HOURLY_CAP`) as a
  botnet backstop (see `app/web/server.py` and `app/onboarding/ai_help.py`).
  v1 is system-prompt-only (condensed onboarding docs baked in) — no codebase
  RAG, by design; a documented deferred enhancement.
- Tier-2 can run via the `claude -p` shim (`CLAUDE_CLI_*`) instead of the API.

Known loose ends:
- Qwen/embeddings depend on an external Ollama endpoint (`QWEN_BASE_URL`); when
  unreachable, triage fails open to Claude and semantic dedup degrades to the
  exact-title hash — by design, but costlier.
- `app/onboarding/crypto.py` (+ its test) is now unused at runtime after the
  vault removal — kept as a helper; `TOKEN_ENC_KEY` is no longer required.
- Docs (`README.md`, `DEPLOY.md`, `.env.example`) refreshed 2026-07-22 to match
  `app/config.py`; keep them in sync when adding settings.

## Next steps

No fixed roadmap — pick up from the task list / Maksim's requests. When adding
env settings, update `.env.example` (kept 1:1 with `app/config.py`) and, if
deployer-facing, `DEPLOY.md`.

## Railway CLI runbook (verified, CLI 5.20.0)

```bash
railway login
railway link                       # your Railway project
railway add -d mongo
railway add --service tg-ai-assistant \
  --repo <your-org>/<your-fork> \
  --branch main
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

- **Ollama (Qwen triage) is external and optional.** A backend on Railway can't
  reach a `localhost` Ollama, so either host Ollama somewhere reachable (expose it
  via Tailscale/Cloudflare/ngrok, etc.) and set `QWEN_BASE_URL`, or leave it unset
  — `qwen.has_task()` fails OPEN, so everything goes to Claude (works, costs more).
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
- Work on a feature branch off `main`; PR into `main`.

## Onboarding / distribution

This is a **public repo**; every user deploys their OWN fully-isolated instance
(own bot, own MongoDB, own Anthropic key, own `ticktick-mcp`). The primary path
is **one-click Railway**, with a docker-compose fallback — see `DEPLOY.md`. There
is no GitHub-collaborator-invite flow anymore; `ONBOARDING_REPO_URL` /
`ONBOARDING_RAILWAY_TEMPLATE_URL` only populate the `/start` message a non-owner
sees. Keep docs deployer-facing and free of any owner-specific values.
