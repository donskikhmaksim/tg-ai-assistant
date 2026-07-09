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
- **Your own `ticktick-mcp` instance** — this is what determines whose TickTick
  account tasks land in, so it **must be yours**:
  1. Deploy <https://github.com/donskikhmaksim/ticktick-mcp> (its README +
     `ONBOARDING` cover the `/setup` OAuth flow and generating `MCP_SECRET`).
  2. Your `TICKTICK_MCP_URL` is
     `https://<your-app>.up.railway.app/mcp/<your MCP_SECRET>`.
  - ⚠️ **Never reuse a `ticktick-mcp` URL someone shared with you** — your tasks
    would be created in *their* account.
- **Optional: Ollama** for cheap Tier-1 triage. If you don't provide it (or it's
  unreachable), triage **fails open to Claude** — everything still works, it just
  costs more. Host it anywhere reachable and set `QWEN_BASE_URL`.

---

## 1. Railway (recommended)

### a. Create the service

Use the **Deploy on Railway** button in the [README](README.md) (once you've
published a template), or create the service manually from your fork. Either way
you get a project containing the bot service.

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
`MONGO_URL`) plus your timezone. **TickTick is connected from inside the bot**,
not via env: after the service is up, DM it `/connect https://<your
ticktick-mcp>.up.railway.app/mcp/<secret>`. (You can still preset it with the
`TICKTICK_MCP_URL` env var if you prefer.)

Optional: `QWEN_BASE_URL` (if you host Ollama for the cheap Tier-1 gate — without
it everything just goes to Claude, which works), `EMBED_MODEL` (retrieval
memory), `ONBOARDING_REPO_URL` / `ONBOARDING_RAILWAY_TEMPLATE_URL` (the `/start`
message shown to non-owners). Railway injects `PORT` automatically.

> CLI alternative: the full `railway` CLI runbook is in [CLAUDE.md](CLAUDE.md).

### d. Connect Telegram Business

The bot must be attached to your Telegram account to read your DMs:

Telegram **Settings → Telegram Business → Chatbots** → your bot → grant
read/reply/manage → scope **"all chats"**. Also add the same bot to any group you
want monitored.

> The `business_connection` event only fires **while the service is running**. If
> the owner-id line never shows in the logs, re-add the bot in Telegram after the
> service is up.

### e. Verify

- Railway logs should show: `Mongo connected` → `Batch scheduler started` →
  (after you connect Business) `Business connection … for owner …`.
- Message your bot `/start` — you should get the welcome + reply menu.
- Send yourself a DM containing a task ("напомни завтра купить молоко") and
  confirm it appears in your TickTick within a few minutes.

### f. Automatic updates (stay current with the maintainer)

Your instance runs your data on your infra, but its **code** can track the
maintainer's automatically — so you get new features and fixes without doing
anything:

1. **Deploy from your fork**, not from a manual upload. In the Railway service →
   **Settings → Source**, connect it to *your* fork's `main` and turn on **Deploy
   on push**.
2. In your fork on GitHub, open the **Actions** tab and click **"I understand my
   workflows, go ahead and enable them"** (GitHub disables Actions on new forks
   by default).

That's it. The bundled [`Sync from upstream`](.github/workflows/sync-upstream.yml)
workflow fast-forwards your fork from the maintainer every 30 minutes; each sync
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

Then connect Telegram Business exactly as in [step 1d](#d-connect-telegram-business)
and verify as in [step 1e](#e-verify).

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
