# tg-ai-assistant

Telegram AI assistant that reads your conversations — personal DMs (incoming **and
your own outgoing**, via Telegram Business) and groups — batches them through a
local LLM (triage) and Claude (extraction), pulls out **tasks / agreements /
promises**, and creates them in **TickTick** under the right project.

Processing is **batched, not realtime**. A cheap local Qwen triage gates the
expensive Claude calls.

Full spec: see the task brief. This README covers running it.

## How it works

```
Telegram
  ├─ business_message  (DM: in + your outgoing)  ─┐
  └─ message           (groups, privacy off)      ─┤→ one bot / one backend
                                                    ↓ save EVERY update immediately
                                        Mongo: raw_messages (TTL 30d, key chatId)
                                                    ↓ APScheduler, every 30 min
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

`TICKTICK_MCP_URL` is the full Streamable-HTTP URL of your Railway
`ticktick-mcp` deployment, **including the secret path**:
`https://<app>.up.railway.app/mcp/<MCP_SECRET>`. The backend reuses the tokens
already configured there — no new TickTick OAuth. Tools used: `get_projects`,
`create_task`, `complete_task`.

### Qwen (Ollama, local)

```bash
ollama pull qwen2.5:32b-instruct   # 14B fallback if RAM is tight
```

Point `QWEN_BASE_URL` at the Ollama OpenAI-compatible endpoint (default
`http://localhost:11434/v1`).

## Run

```bash
python -m app.main
```

Starts the bot (long-polling, incl. `business_*` updates), the Mongo connection,
and the 30-minute batch scheduler in one process.

## Bot usage (Phase 1)

No notifications — created tasks just appear in TickTick. The bot is only a
remote for binding chats to projects.

- `/start` — welcome + reply menu (`🔗 Привязать проект`, `📋 Мои привязки`,
  `❌ Отвязать`).
- In a **group**: `/bind` → pick a project inline → binds that group.
- `/unbind`, `/bindings` also available.

Binding targets the chat where the command is issued. Per-counterparty DM
binding is intended for the **Phase-2 WebApp** (mini-app with dropdowns); DM
message *capture* already works without any binding. Unbound chats still have
their tasks extracted and stored locally — they sync to TickTick once a project
is attached.

## Config

All via env (see `.env.example`): `BOT_TOKEN`, `MONGO_URL`/`MONGO_DB`,
`ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`/`ANTHROPIC_EFFORT`,
`QWEN_BASE_URL`/`QWEN_MODEL`, `TICKTICK_MCP_URL`, and the pipeline knobs
`BATCH_INTERVAL_MIN=30`, `CONV_GAP_HOURS=6`, `MAX_LOOKBACK_HOURS=48`,
`RAW_TTL_DAYS=30`, `DEFAULT_PROJECT=Inbox`.

## Data model (MongoDB)

`raw_messages` (TTL 30d on `date`) · `tasks` (permanent, unique `dedupHash`) ·
`chat_project_map` · `chat_state` (processing cursor) · `chat_summary`
(permanent long-term memory) · `bot_state` (owner id / business connection).

## Tests

```bash
pip install pytest
pytest            # pure-logic tests: window construction, deadline formatting
```

## Deploy (Railway)

Builds from the `Dockerfile`. Set the env vars in the Railway dashboard. Runs
alongside the `ticktick-mcp` service. Qwen/Ollama runs locally (e.g. Mac mini);
expose it to the backend or run the backend where it can reach Ollama.

## Notes / caveats

- Updates are saved atomically **before** processing — Telegram never resends,
  so the DB is the only archive. Manual reruns work off stored raw messages
  within the TTL.
- Group messages from other people are sent to cloud Claude — fine for personal
  use, but worth keeping in mind.
- Idempotent: overlapping windows don't create duplicate tasks (dedup hash +
  unique index + incremental prompt).
