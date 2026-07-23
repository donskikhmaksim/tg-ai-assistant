# tg-ai-assistant

Telegram AI assistant that reads your conversations — personal DMs (incoming **and
your own outgoing**, via Telegram Business) and groups — batches them through a
local LLM (triage) and Claude (extraction), pulls out **tasks / agreements /
promises**, and creates them in **TickTick** under the right project.

Processing is **batched, not realtime**. A cheap local Qwen triage gates the
expensive Claude calls.

This is an open, **self-hostable** project: you run your own private instance.

## Deploy your own (private)

Each deploy is **fully isolated**. You bring your own Telegram bot, your own
MongoDB, your own Anthropic key, and your own `ticktick-mcp` — so your messages
and tasks stay entirely on your infrastructure. **The original author has zero
access to anything you deploy.**

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template?...)

> Replace the link above with your published Railway template URL
> (`https://railway.app/new/template?...`). Railway one-click is the recommended
> path; a docker-compose path for your own VPS is in **[DEPLOY.md](DEPLOY.md)**.

### 1. Your own ticktick-mcp (required first)

The bot writes tasks through a `ticktick-mcp` server, and **that server holds the
TickTick OAuth tokens — whoever's account it is bound to is where tasks land.**
So you must deploy your **own** instance before anything else:

1. Deploy <https://github.com/donskikhmaksim/ticktick-mcp> (its README walks you
   through generating your `MCP_SECRET` and the browser `/setup/<MCP_SECRET>`
   OAuth flow that binds it to *your* TickTick account).
2. Once your bot is up, DM it
   `/connect https://<your-app>.up.railway.app/mcp/<your MCP_SECRET>`
   (or preset the same URL via the `TICKTICK_MCP_URL` env var).

> ⚠️ **Never connect someone else's ticktick-mcp URL** — your extracted tasks
> would be created in *their* TickTick account. It must be yours.

Then follow **[DEPLOY.md](DEPLOY.md)** for the full step-by-step (Railway or VPS).

## How it works

```
Telegram
  ├─ business_message  (DM: in + your outgoing)  ─┐
  └─ message           (groups, privacy off)      ─┤→ one bot / one backend
                                                    ↓ save EVERY update immediately
                                        Mongo: raw_messages (TTL 90d, key chatId)
                                                    ↓ APScheduler, on a debounce
                     for each "dirty" chat → current CONVERSATION WINDOW
                                                    ↓
                     Tier 1 — Qwen (Ollama, optional): any task here? yes/no
                                                    ↓ only "yes" (fails open)
                     Tier 2 — Claude (API or `claude -p` shim): window + known
                              tasks + memory + retrieval → incremental JSON
                                                    ↓ semantic dedup (3-band + judge)
                     project: topic route ?? chat binding ?? default project
                                                    ↓
                     your own ticktick-mcp (create / complete / enrich)
```

Two memory mechanisms, kept separate (spec §7):

- **Conversation window** — only "which fresh raw messages to look at now"
  (gap 6h, cap 48h).
- **Long-term memory** — `chat_summary` + open tasks + (optionally) an embedded
  archive of every processed message for retrieval, all independent of the raw
  TTL. Claude refreshes the summary every run, so a topic revisited months
  later (after the raw messages expired) is still understood.

Beyond the core pipeline (all optional / configurable):

- **Semantic dedup** — a new task is compared by embedding cosine against open
  tasks (the chat's and the destination projects'); a gray zone goes to a cheap
  LLM judge, and any uncertainty resolves to *create*, never drop.
- **Topic routing** — one chat's tasks can fan out into several TickTick
  projects by topic (a per-chat route map, e.g. «личное» → Inbox,
  «работа» → Work); unlabelled tasks fall back to the chat's binding.
- **«Контроль» tracking** — a DM task whose action is on the other person
  becomes a marked follow-up item (`CONTROL_MODE`/`CONTROL_MARKER`/`CONTROL_TAG`).
- **Watchdog** — probes Qwen → Claude → TickTick every 10 min and DMs you when
  the chain breaks (new error immediately, then at most once a day).
- **End-of-day group summary** — opt-in per chat: a daily recap of created and
  completed tasks posted into the group.
- **Voice messages** — transcribed via an optional Whisper endpoint
  (`TRANSCRIBE_URL`); without it they're skipped.
- **Mini App** — a Telegram WebApp (menu button, `WEBAPP_URL`) with per-chat and
  global settings: project/section binding, aliases, prompts and filter rules,
  model/effort, control toggles, topic routes, daily summary, plus a
  Telegram-style transcript page with deep links from each task to its source
  messages.

## Stack

Python 3.12 · aiogram 3.x · MongoDB (Motor) · APScheduler · Anthropic SDK
(`claude-opus-4-8`, structured output + prompt caching + adaptive thinking) —
or an HTTP `claude -p` shim · OpenAI SDK pointed at Ollama (Qwen triage +
embeddings, both optional) · MCP client to your `ticktick-mcp` server · aiohttp
web server (Mini App + transcript pages).

## Setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the values
```

### Telegram (@BotFather)

1. `/newbot` → `BOT_TOKEN`.
2. Bot Settings → **Business Mode → Enable**.
3. Bot Settings → **Group Privacy → Turn off**.
4. Connect your account: Settings → Telegram Business → Chatbots → your bot →
   grant read/reply/manage → scope "all chats".
5. Add the same bot to any group you want monitored.

### TickTick MCP

Connect it from inside the bot: DM
`/connect https://<app>.up.railway.app/mcp/<MCP_SECRET>` — the full
Streamable-HTTP URL of **your own** `ticktick-mcp` deployment (see
[step 1 above](#1-your-own-ticktick-mcp-required-first)), **including the secret
path**. `/connect` stores it as a runtime override in `bot_state` (owner-only;
it takes priority over the `TICKTICK_MCP_URL` env var, which you can set instead
if you prefer). This is the single global TickTick connector for the instance.
The TickTick OAuth tokens live in *that* `ticktick-mcp`, so it must be one you
deployed and control — never a URL someone shared with you.

### Qwen (Ollama, optional)

Triage is optional. Run Ollama **wherever you like** (a spare box, a VPS, your
laptop) and point `QWEN_BASE_URL` at its OpenAI-compatible endpoint (default
`http://localhost:11434/v1`):

```bash
ollama pull qwen2.5:32b-instruct   # 14B fallback if RAM is tight
```

If `QWEN_BASE_URL` is unset or unreachable, triage **fails open to Claude** —
everything still works, it just costs more (no cheap gate in front of Claude).

## Run

```bash
python -m app.main
```

Starts the bot (long-polling, incl. `business_*` updates), the Mongo connection,
and the debounced batch scheduler in one process.

## Bot usage

No per-task notifications — created tasks just appear in TickTick (the watchdog
DMs you only when something breaks, and the opt-in daily summary posts into
groups).

- `/start` — welcome + reply menu (`🔗 Привязать проект`, `📋 Мои привязки`,
  `❌ Отвязать`).
- `/connect <url>` — register your own `ticktick-mcp` connector (see above).
- In a **group**: `/bind` → pick a project inline → binds that group.
- `/unbind`, `/bindings` also available.
- `/invite` (owner) + `/setup` — optional invite-gated connector onboarding:
  hands a friend one-command installers for their own TickTick/Google MCP
  servers and their own copy of this bot, delivered via self-destruct notes
  (needs the `ONBOARDING_*` / `NOTES_BASE_URL` env vars).

Binding targets the chat where the command is issued. DM message *capture* works
without any binding; unbound chats still have their tasks extracted — they go to
`DEFAULT_PROJECT`/`DEFAULT_PROJECT_ID` (optionally into a
`DEFAULT_SECTION[_ID]` column), or stay local until TickTick is connected.

Prompt configuration, aliases, topic routes, and all per-chat settings live in
the **Mini App** (the Telegram menu button, enabled by setting `WEBAPP_URL`).

## Config

All via env, fully documented in [`.env.example`](.env.example) (it matches
`app/config.py` 1:1). Required core: `BOT_TOKEN`, `MONGO_URL`/`MONGO_DB`,
`ANTHROPIC_API_KEY` (or the `CLAUDE_CLI_*` shim), `DEFAULT_TIMEZONE`,
`WEBAPP_URL` (+ `TICKTICK_MCP_URL`, or set it later via `/connect`). Everything else is optional with sane
defaults: extraction (`ANTHROPIC_MODEL`/`ANTHROPIC_EFFORT`,
`EXTRACT_MODEL`/`EXTRACT_EFFORT`, `SYSTEM_PROMPT`), «Контроль» (`CONTROL_*`),
semantic dedup (`DEDUP_*`), Qwen triage (`QWEN_*`), retrieval memory
(`EMBED_MODEL`, `RETRIEVE_*`), voice (`TRANSCRIBE_URL`), watchdog
(`HEALTHCHECK_*`), group summaries (`DAILY_SUMMARY`/`SUMMARY_HOUR`), pipeline
knobs (`BATCH_INTERVAL_MIN`, `QUIET_MINUTES`/`MAX_DIRTY_MINUTES`,
`CONV_GAP_HOURS`, `MAX_LOOKBACK_HOURS`, `RAW_TTL_DAYS=90`),
default destination (`DEFAULT_PROJECT[_ID]`, `DEFAULT_SECTION[_ID]`), and
onboarding (`ONBOARDING_*`, `NOTES_BASE_URL`).

## Data model (MongoDB)

`raw_messages` (TTL 90d on `date`) · `tasks` (permanent, unique `dedupHash`) ·
`chat_project_map` · `chat_state` (processing cursor) ·
`chat_summary` (permanent long-term memory) · `chat_settings` (per-chat +
`__global__` overrides: prompts, routes, control, model…) · `bot_state`
(owner id / business connection / TickTick URL override) ·
`message_vectors` / `task_vectors`
(embeddings for retrieval + semantic dedup).

## Tests

```bash
pip install pytest
pytest            # pure-logic tests: window construction, deadline formatting
```

## Deploy

Full deployer guide: **[DEPLOY.md](DEPLOY.md)** (Railway one-click, or
docker-compose on your own VPS).

In short: it builds from the `Dockerfile`; set the env vars in the Railway
dashboard (or your `.env`). It talks to your own `ticktick-mcp`. Ollama, if you
use it, runs wherever you host it — expose it to the backend and set
`QWEN_BASE_URL`, or leave it unset and let triage fail open to Claude.

## Notes / caveats

- Updates are saved atomically **before** processing — Telegram never resends,
  so the DB is the only archive. Manual reruns work off stored raw messages
  within the TTL.
- Group messages from other people are sent to cloud Claude — fine for personal
  use, but worth keeping in mind.
- Idempotent: overlapping windows don't create duplicate tasks (dedup hash +
  unique index + incremental prompt).
