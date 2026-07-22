# Deploying your own tg-ai-assistant

This guide is for **deploying your own private instance**. Every deploy is fully
isolated — your own bot, your own database, your own keys, your own TickTick. The
upstream author has **zero access** to your messages or tasks (see
[Privacy & isolation](#privacy--isolation) at the end).

Two paths:

1. [Railway (recommended, one-click)](#1-railway-recommended)
2. [Own VPS via docker-compose (advanced)](#2-own-vps-via-docker-compose-advanced)

---

## Prerequisites (both paths)

Gather these first — the bot won't start usefully without them:

- **Your own Telegram bot.** In [@BotFather](https://t.me/BotFather): `/newbot`
  → copy the `BOT_TOKEN`. Then **Bot Settings**:
  - **Business Mode** (a.k.a. "Secretary Mode") → **Enable**.
  - **Group Privacy** → **Turn off** (so it can read group messages).
- **Your own Anthropic API key** (`ANTHROPIC_API_KEY`) — usage is billed to it.
  (Alternative: a `claude -p` HTTP shim + `CLAUDE_CLI_URL`/`CLAUDE_CLI_TOKEN` to
  run extraction on a Claude Code subscription instead of the API — optional,
  see `.env.example`.)
- **Your own `ticktick-mcp` URL** (`TICKTICK_MCP_URL`) — the single TickTick
  connector for this instance. Set it in env now, or connect it from inside the
  bot later with `/connect` (see below). No vault key is needed.
- **Your own `ticktick-mcp` instance** — this is what determines whose TickTick
  account tasks land in, so it **must be yours**:
  1. Deploy <https://github.com/donskikhmaksim/ticktick-mcp> — see the
     [companion server](#the-companion-ticktick-mcp-server) section below for
     what to configure there.
  2. Your connector URL is
     `https://<your-app>.up.railway.app/mcp/<your MCP_SECRET>`.
  - ⚠️ **Never reuse a `ticktick-mcp` URL someone shared with you** — your tasks
    would be created in *their* account.
- **Optional: Ollama** for cheap Tier-1 triage, retrieval-memory embeddings, and
  semantic dedup. If you don't provide it, triage is skipped (**fails open to
  Claude** — everything still works, it just costs more) and
  embeddings-dependent features fall back to simpler behavior. Host it anywhere
  reachable and set `QWEN_BASE_URL` (embeddings share the same endpoint via
  `EMBED_MODEL`).

### The companion ticktick-mcp server

The bot writes to TickTick through a separate MCP server you also deploy:
<https://github.com/donskikhmaksim/ticktick-mcp>. Its README is the source of
truth; the short version:

- **`MCP_SECRET`** — generate your own (`openssl rand -hex 24`). It becomes the
  URL path (`/mcp/<secret>`) that *is* the credential, and it also gates the
  self-service **`/setup/<secret>`** browser page where you log in to *your own*
  TickTick (OAuth) — no local CLI needed.
- **`USER_TIMEZONE`** — your IANA zone for due-date handling (defaults to UTC).
- **Optional `CLAUDE_CLI_URL`/`CLAUDE_CLI_TOKEN`** — enables the server's
  destination-suggester (it asks a Claude shim which project/section fits a
  task) for interactive use. Not required for this bot.
- **`DIRECT_DELETE_CAP`** (default 1) — safety semantics: at most this many
  tasks can be deleted per direct call, and only with an exact title match
  (id↔title identity guard); anything bigger must go through the
  plan → approve → execute manifest flow. The same guard family requires the
  bot to pass the connection secret (`automation_key`) on direct creates and
  the task title on completes — the bot does this automatically.

---

## 1. Railway (recommended)

### a. Create the service

Three equivalent ways to get the service:

- **One-command installer** (easiest): [`scripts/setup.sh`](scripts/setup.sh)
  drives the Railway CLI end-to-end — creates the project, adds MongoDB, **forks
  this repo into your GitHub account and connects the fork** as the source (so
  Railway auto-redeploys — see step g), generates a domain, and sets the core
  env vars from `--bot-token` / `--anthropic-key` / `--timezone` arguments.
- The **Deploy on Railway** template button in the [README](README.md), if a
  template has been published.
- Manually: create a Railway service from your fork of this repo.

Either way you end up with a project containing the bot service.

### b. Add MongoDB

In the Railway project: **New → Database → MongoDB** (the Mongo plugin). Then
wire the bot to it by setting `MONGO_URL` to reference the plugin variable:

```
MONGO_URL=${{MongoDB.MONGO_URL}}
```

(Railway resolves `${{MongoDB.MONGO_URL}}` to the plugin's connection string.)

### c. Set the env vars

In the bot service → **Variables**, set the values from
[`.env.example`](.env.example). At minimum:

| Variable            | Value                                                        |
| ------------------- | ------------------------------------------------------------ |
| `BOT_TOKEN`         | your BotFather token                                         |
| `ANTHROPIC_API_KEY` | your Anthropic key                                           |
| `MONGO_URL`         | `${{MongoDB.MONGO_URL}}`                                     |
| `DEFAULT_TIMEZONE`  | your IANA zone, e.g. `Europe/Moscow` (default `UTC`)         |
| `WEBAPP_URL`        | your service's public https origin (for the Mini App button) |

That's the whole minimum — three secrets (`BOT_TOKEN`, `ANTHROPIC_API_KEY`,
`MONGO_URL`) plus your timezone. **TickTick** is the single global connector for
the instance: set `TICKTICK_MCP_URL` here in env, **or** connect it from inside
the bot — after the service is up, DM it
`/connect https://<your ticktick-mcp>.up.railway.app/mcp/<secret>`. `/connect`
stores the URL as a runtime override in `bot_state` (taking priority over the
env) and deletes the message from the chat. No vault, no `TOKEN_ENC_KEY`.

Everything else is optional and documented inline in
[`.env.example`](.env.example). Highlights:

- `QWEN_BASE_URL` — an Ollama/OpenAI-compatible endpoint for the cheap Tier-1
  gate (unset → everything goes straight to Claude, which works) and for
  `EMBED_MODEL` embeddings (retrieval memory + semantic dedup).
- `CLAUDE_CLI_URL` / `CLAUDE_CLI_TOKEN` — run extraction through a `claude -p`
  shim (Claude Code subscription) instead of the paid API.
- `CONTROL_MODE` / `CONTROL_MARKER` / `CONTROL_TAG` — how DM tasks whose action
  is on the *other* person become tracked «Контроль» items.
- `DEDUP_*` — the three-band semantic near-duplicate detection (on by default;
  needs embeddings to actually engage).
- `HEALTHCHECK_*` — the watchdog that probes Qwen → Claude → TickTick and DMs
  you when the chain breaks (on by default).
- `DAILY_SUMMARY` / `SUMMARY_HOUR` — opt-in end-of-day recap posted into groups.
- `DEFAULT_PROJECT[_ID]` / `DEFAULT_SECTION[_ID]` — where tasks from unbound
  chats land.
- `TRANSCRIBE_URL` — a Whisper endpoint for voice/audio messages.
- `ONBOARDING_*` / `NOTES_BASE_URL` — the `/start` text for non-owners and the
  invite-gated `/setup` connector-onboarding buttons.

Railway injects `PORT` automatically.

> CLI alternative: the full `railway` CLI runbook is in [CLAUDE.md](CLAUDE.md).

### d. Connect Telegram Business

The bot must be attached to your Telegram account to read your DMs:

Telegram **Settings → Telegram Business → Chatbots** → your bot → grant
read/reply/manage → scope **"all chats"**. Also add the same bot to any group you
want monitored.

> The `business_connection` event only fires **while the service is running**. If
> the owner-id line never shows in the logs, re-add the bot in Telegram after the
> service is up.

### e. Connect your TickTick

DM the bot:

```
/connect https://<your-ticktick-mcp>.up.railway.app/mcp/<MCP_SECRET>
```

The bot verifies the connector answers, stores the URL encrypted, and deletes
your message (the URL is a secret). Then `/bind` in a group (or the Mini App)
binds chats to TickTick projects; unbound chats fall back to
`DEFAULT_PROJECT`/`DEFAULT_PROJECT_ID`.

### f. Verify

- Railway logs should show: `Mongo connected` → `Batch scheduler started` →
  (after you connect Business) `Business connection … for owner …`.
- Message your bot `/start` — you should get the welcome + reply menu.
- Send yourself a DM containing a task ("напомни завтра купить молоко") and
  confirm it appears in your TickTick — the pipeline is debounced, so allow
  ~10 minutes (`QUIET_MINUTES=8` of chat silence + a scheduler tick).
- The built-in watchdog will DM you if the extraction chain breaks later.

### g. Automatic updates (stay current with the maintainer)

Your instance runs your data on your infra, but its **code** can track the
maintainer's automatically — so you get new features and fixes without doing
anything. The one-command installer (`scripts/setup.sh`) sets this up for you:
it forks this repo into your GitHub account with `gh`, enables Actions on the
fork, and connects the **fork** (not the upstream) as the Railway source — the
critical detail, since Railway only redeploys on a push into the repo it is
connected to. If you deployed some other way, do it manually:

1. **Deploy from your fork**, not from a manual upload. In the Railway service →
   **Settings → Source**, connect it to *your* fork's `main` and turn on **Deploy
   on push**.
2. In your fork on GitHub, open the **Actions** tab and click **"I understand my
   workflows, go ahead and enable them"** (GitHub disables Actions on new forks
   by default).

That's it. The bundled [`Sync from upstream`](.github/workflows/sync-upstream.yml)
workflow fast-forwards your fork from the maintainer every ~5 minutes; each sync
pushes to your `main`, which triggers a Railway redeploy. You can also trigger it
on demand: **Actions → Sync from upstream → Run workflow**.

- It only ever fast-forwards **code** — never your env vars, database, or tasks.
- If you make **local commits** to your fork, it stops auto-syncing (it won't
  overwrite your changes); merge upstream manually to resume.

---

## 2. Own VPS via docker-compose (advanced)

The included [`docker-compose.yml`](docker-compose.yml) runs **two** services: the
bot and a local MongoDB. Everything else the bot talks to — Ollama
(`QWEN_BASE_URL`) and your `ticktick-mcp` (`TICKTICK_MCP_URL`) — is **external**
and not started by compose; point the bot at those via your `.env`.

```bash
cp .env.example .env
# edit .env: BOT_TOKEN, ANTHROPIC_API_KEY, TICKTICK_MCP_URL, DEFAULT_TIMEZONE, …
# (leave MONGO_URL as-is — compose overrides it to reach the bundled mongo)
docker compose up -d
docker compose logs -f bot
```

Then connect Telegram Business exactly as in [step 1d](#d-connect-telegram-business),
connect TickTick as in [step 1e](#e-connect-your-ticktick), and verify as in
[step 1f](#f-verify).

Notes:
- MongoDB data persists in the `mongo_data` volume.
- Port `8080` is exposed for the Mini App / health check; set `WEBAPP_URL` to the
  public https origin you serve it behind (a reverse proxy with TLS).
- If you don't run Ollama, leave `QWEN_BASE_URL` unset — triage fails open to
  Claude.

---

## Privacy & isolation

Everything a deploy touches is **yours**:

- **Your Telegram bot** reads only the chats you connect it to.
- **Your MongoDB** holds all captured messages, tasks, and summaries — on your
  infrastructure (Railway plugin or your VPS volume).
- **Your Anthropic key** makes the Claude calls; usage is billed to you.
- **Your `ticktick-mcp`** holds your TickTick tokens, so tasks land only in your
  account.

**The upstream author has no access** to your bot, database, keys, or TickTick —
there is no shared backend, no telemetry, and no collaborator/invite step.

One thing to be aware of for your own judgment: message content **is** sent to
Anthropic's Claude API (and to your Ollama endpoint, if configured) for
extraction — including messages from other people in monitored groups. That's the
normal cost of the feature; it goes to *your* API account, not to the author.
