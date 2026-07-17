# Titan AI Trader

Clean-architecture market analysis and signal-alerting system for NIFTY
and BANKNIFTY. **Paper trading only.** `shared/constants.py` hardcodes
`TRADING_MODE = "PAPER"` and no live-broker adapter exists anywhere in
this codebase — nothing in this project can place a real order. Its
output is analysis and Telegram alerts, not trade execution.

## What it does

On a schedule (09:00 / 09:08 / 09:15 IST, every 30 minutes, and at
market close), the service:
1. Pulls live NIFTY/BANKNIFTY spot, option chain, PCR, Max Pain, OI
   change, India VIX, FII/DII cash + F&O participant activity, news
   sentiment, and macro signals (global indices, USDINR, crude, bond
   yield).
2. Combines all of it into an overall market sentiment score and a
   BUY/SELL/WAIT decision, with reasoning, confidence, risk level, and
   generated strategy setups (entry/SL/targets/position size).
3. Formats the result and posts it to a Telegram channel.

Every decision payload carries an explicit disclaimer: this is a
rule-based signal generator, not investment advice, and not a
statistically calibrated probability model.

## Quick start (local)

```bash
git clone <your-fork-url>
cd titan_ai_trader
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in BOT_TOKEN and CHANNEL_ID at minimum
python main.py
```

`main.py` validates required environment variables, initializes SQLite,
builds every agent, runs a one-shot Telegram connection test, and then
runs the scheduler forever until you press Ctrl+C (or it receives
SIGTERM).

## Run the tests

```bash
pip install -e ".[dev]"
pytest
```

## Environment variables

See `.env.example` for the full, commented list. Summary:

| Variable | Required? | Purpose |
|---|---|---|
| `BOT_TOKEN` | **Yes** | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `CHANNEL_ID` | **Yes** | Channel/chat to post to (bot must be an admin — see below) |
| `NEWS_API_KEY` | No | Optional NewsAPI.org key; falls back to Google News RSS if unset |
| `NSE_API_KEY` | No | Reserved; NSE endpoints used here are public and need no key |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` / `CLAUDE_API_KEY` | No | Reserved for future AI integration; unused by current code |
| `TZ` | No | Container/log timestamp convenience; scheduler uses a fixed IST offset regardless |
| `PORT` | No (Render sets it) | If set, `main.py` starts a `/health` endpoint on it |
| `TITAN_SYMBOLS` | No | Comma-separated indices to analyze (default `NIFTY,BANKNIFTY`) |
| `TITAN_DB_PATH`, `TITAN_STARTING_CASH` | No | Phase 1 SQLite persistence |
| `TITAN_MARKET_DATA_TIMEOUT`, `TITAN_MARKET_DATA_MAX_RETRIES` | No | NSE/HTTP client tuning |
| `TITAN_CAPITAL`, `TITAN_RISK_PER_TRADE_PCT`, `TITAN_NIFTY_LOT_SIZE`, `TITAN_BANKNIFTY_LOT_SIZE` | No | `strategy_agent.py` risk sizing |
| `LOG_LEVEL`, `LOG_DIR` | No | Logging verbosity and file location |

`main.py` prints a line for every one of these at startup (present/
missing/using-default), so a misconfiguration is always visible in the
first few lines of the log, never silent.

## Telegram setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram, run
   `/newbot`, and copy the token it gives you into `BOT_TOKEN`.
2. Create a channel (or use an existing one), add your bot to it, and
   **promote it to admin** (Channel → Administrators → Add Admin →
   your bot). This step is required — Telegram does not allow a
   non-admin bot to post to a channel, and `main.py`'s startup test
   will report `is_channel_admin: false` until this is done.
3. Set `CHANNEL_ID` to the channel's `@username` (public channels) or
   its numeric chat id (private channels — forward a message from the
   channel to [@userinfobot](https://t.me/userinfobot) to find it;
   private channel ids look like `-100xxxxxxxxxx`).
4. Start the app. Look for these two lines in the log:
   ```
   titan.telegram | Running Telegram startup connection test...
   titan.telegram | Telegram startup test PASSED: bot @yourbot is an admin of CHANNEL_ID.
   ```
   If it instead says `FAILED`, see Troubleshooting below.

`telegram_formatter.py`'s `TelegramSender` is never invoked
automatically by any agent — only `main.py`'s scheduled callback calls
`.send(...)`, and only when `BOT_TOKEN`/`CHANNEL_ID` are both set.

### Bot commands

The bot also runs an interactive command listener (`telegram_bot.py`)
alongside the scheduler, so it responds to direct messages:

| Command | Response |
|---|---|
| `/start` | Confirms the bot is running |
| `/help` | Lists available commands |
| `/status` | Live report: trading mode, tracked symbols, Telegram delivery status, and the next scheduled analysis run |

Message the bot directly (not the channel) to use these — a bot can
reply in a private chat with anyone even without being an admin there;
admin rights are only required to *post* to `CHANNEL_ID`.

This listens via Telegram's `getUpdates` long-polling (stdlib `urllib`,
same as every other HTTP call in this project) rather than
`python-telegram-bot` or `aiogram`. That's intentional, not a
missing feature: this project has been zero-third-party-dependency at
runtime since Phase 1 specifically to keep Render deployment simple and
every network call independently testable offline — a bot framework's
own dependency tree would reintroduce exactly the kind of packaging
risk that caused this project's earlier Render deployment failure (see
Troubleshooting below). Three commands don't need a framework.

## Deploying on Render

This repo includes `render.yaml`, so Render can deploy it via
**Blueprint** with no manual configuration beyond secrets.

### GitHub upload

```bash
git init                      # if not already a git repo
git add .
git commit -m "Titan AI Trader"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

`.gitignore` already excludes `.env`, `*.db`, `logs/`, and
`__pycache__/` — real secrets and local runtime state never get pushed.

### Render Blueprint deploy

1. In the Render dashboard: **New → Blueprint**, connect the GitHub
   repo you just pushed, and select it.
2. Render reads `render.yaml` and provisions a Web Service named
   `titan-ai-trader` with a 1GB persistent disk mounted at `/var/data`
   (for the SQLite DB and rotating logs to survive restarts).
3. Render prompts for the `sync: false` secrets declared in
   `render.yaml`: `BOT_TOKEN`, `CHANNEL_ID`, and optionally
   `NEWS_API_KEY` / `NSE_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY`
   / `CLAUDE_API_KEY`. Fill in at least `BOT_TOKEN` and `CHANNEL_ID`.
4. Deploy. Render runs `buildCommand` (`pip install -e .` then
   `pip install -r requirements.txt`), then `startCommand`
   (`python main.py`), and health-checks `GET /health` (which
   `main.py` serves on the `$PORT` Render injects automatically).
5. Watch the Logs tab for the same startup sequence described above
   (env validation → SQLite init → agent graph ready → Telegram test →
   health server → scheduler running).

If you'd rather deploy as a Background Worker (no HTTP health check,
slightly cheaper on some plans), change `type: web` to `type: worker`
and remove the `healthCheckPath` line in `render.yaml` — `main.py`
already handles this: it only starts the health server when `$PORT` is
present, so no code change is needed either way.

### Manual (non-Blueprint) Render setup

If you prefer configuring the service by hand instead of via
`render.yaml`:
- **Build Command:** `pip install -e . && pip install -r requirements.txt`
- **Start Command:** `python main.py`
- **Health Check Path:** `/health`
- Add the environment variables listed in `.env.example`.

## Troubleshooting / common errors

**Startup ABORTED: missing required environment variable(s): BOT_TOKEN, CHANNEL_ID**
You haven't set one or both. On Render: Dashboard → your service →
Environment. Locally: fill them into `.env`.

**Telegram startup test FAILED: could not authenticate with BOT_TOKEN**
The token is wrong, revoked, or was copy-pasted with extra whitespace.
Get a fresh one from @BotFather and confirm no leading/trailing spaces.

**Telegram startup test FAILED: ... bot @x connected, but is NOT an admin of CHANNEL_ID**
The token is valid but the bot isn't a channel admin yet — see
"Telegram setup" step 2 above. This is the single most common setup
mistake.

**Health check failing on Render / service marked unhealthy**
Confirm you deployed as `type: web` (not `worker`) if you want Render's
HTTP health check — a Background Worker deployment has no health check
and this is expected/fine for that type.

**SQLite database resets on every deploy**
You're on the free plan or removed the `disk:` block in `render.yaml`
— Render's default filesystem is ephemeral. Add a persistent disk (paid
plans) or accept that history resets on redeploy (fine for pure
analysis/alerting use, since Phase 1's trade ledger isn't currently
driven by the scheduler).

**Bot doesn't reply to /start, /help, or /status**
Message the bot directly in a private chat, not the channel (Telegram
channels don't relay member messages back to bots the way group chats
do). Confirm `BOT_TOKEN` is valid first — if the startup Telegram test
failed, the command bot's `getUpdates` polling will also be failing for
the same reason, and this is logged in `titan_telegram.log`.

**"No module named titan_ai_trader" when running `python main.py` locally**
Run `pip install -e .` first (or just run from the repo root — `main.py`
also falls back to adding `src/` onto `sys.path` automatically if the
package isn't installed).

**NewsAPI / NSE / global-markets calls failing intermittently**
All of these have automatic retry with exponential backoff built in
(see `infrastructure/market_data/retry.py`) and degrade gracefully —
e.g. missing news degrades the sentiment score's confidence rather than
crashing the whole cycle. Persistent failures are logged at ERROR level
in `titan_errors.log`.

## Project layout

```
main.py                          process entry point (validate -> build -> run)
render.yaml, Procfile, runtime.txt   Render / PaaS deployment config
requirements.txt                 dependencies (stdlib-only at runtime; pytest for dev)
.env.example                     full environment variable reference

src/titan_ai_trader/
  bootstrap.py                   composition root: env validation + full agent graph
  domain/                        entities, value objects, enums, exceptions, pure calculators
  application/
    interfaces/                  ports (NSE/news/FII-DII/cache abstractions)
    services/
      market_data_service.py     unified market-data facade (caching + retry)
      market_agent.py            live NIFTY/BANKNIFTY analysis (spot/PCR/MaxPain/OI)
      fii_dii_agent.py           FII/DII cash + F&O participant OI + build-up detection
      news_agent.py              NewsAPI + Google RSS fallback + economic calendar
      market_sentiment_agent.py  combines every signal into an overall 0-100 score
      strategy_agent.py          Option Buying/Selling, Intraday, Swing, Scalping setups
      ai_decision_engine.py      the brain: BUY/SELL/WAIT with full reasoning
      telegram_formatter.py      MarkdownV2 message rendering + TelegramSender
      telegram_bot.py            interactive /start /help /status command bot
      scheduler.py                async, IST-aware, holiday-aware trigger scheduler
  infrastructure/
    persistence/                 SQLite (Phase 1 paper trading ledger)
    market_data/                 NSE adapters, cache, retry, HTTP client
    logging/logger.py            console + daily-rotating + error + Telegram log setup
    config/settings.py           env-driven Settings

tests/
  test_phase1_engine.py          end-to-end Phase 1 lifecycle test
  unit/domain/                   entity validation + calculator tests
  unit/infrastructure/           cache + retry tests
  unit/application/              agent orchestration tests (with fakes)
```

## Explicitly out of scope

Smart Money Concepts, Supply & Demand zones, technical Support &
Resistance (beyond the lightweight OI-based estimate used for Telegram
display), candlestick pattern detection, and any form of live order
execution or broker connectivity. No broker adapter exists in this
codebase, and none of the scheduled logic ever calls one.
