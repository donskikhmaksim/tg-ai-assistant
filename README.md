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

1. Deploy <https://github.com/donskikhmaksim/ticktick-mcp> (its README +
   `ONBOARDING` walk you through the `/setup` OAuth flow and generating your
   `MCP_SECRET`).
2. Set `TICKTICK_MCP_URL=https://<your-app>.up.railway.app/mcp/<your MCP_SECRET>`.

> ⚠️ **Never point `TICKTICK_MCP_URL` at someone else's ticktick-mcp** — your
> extracted tasks would be created in *their* TickTick account. It must be yours.

Then follow **[DEPLOY.md](DEPLOY.md)** for the full step-by-step (Railway or VPS).

## How it works

```
Telegram
  ├─ business_message  (DM: in + your outgoing)  ─┐
  └─ message           (groups, privacy off)      ─┤→ one bot / one backend
                                                    ↓ save EVERY update immediately
                                        Mongo: raw_messages (TTL 30d, key chatId)
                                                    ↓ APScheduler, on a debounce
                     for each "dirty" chat → current CONVERSATION WINDOW
                                                    ↓
                     Tier 1 — Qwen (Ollama): any task in the window? yes/no
                                                    ↓ only "yes"
                     Tier 2 — Claude (opus-4-8): window + known tasks + memory
                                                → incremental JSON + updated summary
                                                    ↓ dedup
                     project: chat_project_map[chatId] ?? Inbox
                                                    ↓
                     TickTick via Railway MCP (create_task / complete_task)
```

Two memory mechanisms, kept separate (spec §7):

- **Conversation window** — only "which fresh raw messages to look at now"
  (gap 6h, cap 48h).
- **Long-term memory** — `chat_summary` + open tasks, independent of the raw
  TTL. Claude refreshes the summary every run, so a topic revisited weeks later
  (after the raw messages expired) is still understood.

## Stack

Python 3.12 · aiogram 3.x · MongoDB (Motor) · APScheduler · Anthropic SDK
(`claude-opus-4-8`, structured output + prompt caching) · OpenAI SDK pointed at
local Ollama (Qwen triage) · MCP client to the Railway `ticktick-mcp` server.

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

`TICKTICK_MCP_URL` is the full Streamable-HTTP URL of **your own**
`ticktick-mcp` deployment (see [step 1 above](#1-your-own-ticktick-mcp-required-first)),
**including the secret path**: `https://<app>.up.railway.app/mcp/<MCP_SECRET>`.
The TickTick OAuth tokens live in *that* instance, so it must be one you deployed
and control — never a URL someone shared with you. Tools used: `get_projects`,
`create_task`, `complete_task`.

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

No notifications — created tasks just appear in TickTick. The bot is a remote for
binding chats to projects.

- `/start` — welcome + reply menu (`🔗 Привязать проект`, `📋 Мои привязки`,
  `❌ Отвязать`).
- In a **group**: `/bind` → pick a project inline → binds that group.
- `/unbind`, `/bindings` also available.

Binding targets the chat where the command is issued. DM message *capture* works
without any binding; unbound chats still have their tasks extracted and stored
locally, and they sync to TickTick once a project is attached.

Prompt configuration, aliases, and per-chat settings live in the **Mini App**
(the Telegram menu button, enabled by setting `WEBAPP_URL`).

## Config

All via env (see `.env.example`): `BOT_TOKEN`, `MONGO_URL`/`MONGO_DB`,
`ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`/`ANTHROPIC_EFFORT`,
`QWEN_BASE_URL`/`QWEN_MODEL`, `TICKTICK_MCP_URL`, `DEFAULT_TIMEZONE`, and the
pipeline knobs `BATCH_INTERVAL_MIN=2` (+ `QUIET_MINUTES`/`MAX_DIRTY_MINUTES`
debounce), `CONV_GAP_HOURS=6`, `MAX_LOOKBACK_HOURS=48`, `RAW_TTL_DAYS=30`,
`DEFAULT_PROJECT=Inbox`. Optional retrieval memory: `EMBED_MODEL` (empty
disables), `RETRIEVE_TOP_K`, `RETRIEVE_MIN_SCORE`.

## Data model (MongoDB)

`raw_messages` (TTL 30d on `date`) · `tasks` (permanent, unique `dedupHash`) ·
`chat_project_map` · `chat_state` (processing cursor) · `chat_summary`
(permanent long-term memory) · `bot_state` (owner id / business connection).

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
