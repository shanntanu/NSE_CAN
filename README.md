<<<<<<< HEAD
# NSE Alerts

Sends short AI summaries of NSE corporate actions and announcements for the Nifty 50 to users on Telegram
(active) and WhatsApp (built, off), within 60 seconds of NSE listing them.

```
NSE website ──poll every 5s──▶  worker (Python, always on, AWS EC2)  ──▶ Telegram (MTProto, Telethon)
                                   │  stores events, corporate actions,      WhatsApp Cloud API (off)
                                   │  summaries and delivery results
                                   ▼
                           Postgres (Neon)  ◀──────  web app (Next.js on Vercel): choose stocks,
                                                      enter mobile, verify code, admin view
```

| Folder | What it is |
| --- | --- |
| `worker/` | The always-on Python service: poller, context cache, summaries, delivery, tracking |
| `web/` | Next.js site for Vercel: stock picker, mobile number, code check, admin page, WhatsApp webhook |
| `db/schema.postgres.sql` | The one shared schema (the worker applies it with `init-db`) |
| `data/nifty50.csv` | The tracked stocks (refresh with `python -m nse_alerts.cli sync-universe`) |
| `nse_feasibility_test.py` | Standalone script that checks NSE/BSE answer from a server |

## How an alert flows

1. The poller reads NSE's announcements feed every 5 seconds in market hours (60 s after market, 300 s on weekends),
   and the corporate actions list every 6th poll. All values are in `worker/config.yaml`.
2. Nifty 50 items are stored in `events`; corporate actions also go in `corporate_actions`.
3. For each new alert with subscribers, the worker downloads the attachment, reads the filing text, takes the stock's
   cached fundamentals and technicals (refreshed every 10 minutes in market hours), and asks OpenRouter for a summary
   of 3 to 4 bullets, under 100 words. The current price and volume (from Yahoo Finance, about a minute behind) are added under it. If the model is late or fails, a short template summary is sent instead of missing the deadline.
4. The text goes out first, then the attachment. Each send is recorded in `deliveries` with the time since NSE listed it
   (`exchdisstime`) and since our poller saw it, and whether it met the 60-second target.
5. 30 minutes later (or at the close, if sooner) the same people get a follow-up of about 30 words in 2 bullets: did the
   price move more or less than the Nifty 50 (positive, negative or no clear impact), what volume suggests, and the
   current price and volume. Alerts that arrive outside market hours get no follow-up. See the `follow_up` and `quotes`
   settings; results are stored in the `follow_ups` table (run the schema again to add it).

## Set up

### 1. Database (Neon)
Create a Postgres database at neon.tech and copy the connection string. Apply the schema once:

```bash
psql "$DATABASE_URL" -f db/schema.postgres.sql      # or: cd worker && python -m nse_alerts.cli init-db
```

### 2. Web app (Vercel)
Import the repo in Vercel with **Root Directory = `web`**. Set `DATABASE_URL`, `ADMIN_TOKEN` and the other variables
listed in `web/.env.example`. See `web/README.md`.

### 3. Worker (AWS EC2, Mumbai region, t3.micro is enough)
```bash
git clone <your repo> && cd NSE_CAN/worker
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env        # DATABASE_URL, OPENROUTER_API_KEY, TELEGRAM_API_ID, TELEGRAM_API_HASH
python -m nse_alerts.cli init-db
python -m nse_alerts.cli telegram-login  # once: phone number, code, 2FA password of the SENDER account
python -m nse_alerts.cli poll-once       # check NSE answers from this server
python -m nse_alerts.cli summarise-test  # check the OpenRouter key and model
sudo cp nse-alerts.service /etc/systemd/system/ && sudo systemctl enable --now nse-alerts
journalctl -u nse-alerts -f
```

Get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` at https://my.telegram.org (API development tools). Use a dedicated
Telegram account for sending, and run only one worker with that session.

### Try it without the website
```bash
python -m nse_alerts.cli add-subscriber --mobile +919876543210 --symbols TCS,INFY,RELIANCE
```

## Configuration
Everything tunable is in `worker/config.yaml` and is re-read when the file changes (no restart needed):
polling seconds for market hours, after market and weekends, the model and fallback model, summary length, deadlines,
the 60-second target and where its clock starts, channel switches, attachment limits, alert filters.
Any value can be overridden with an environment variable, for example
`NSE_ALERTS__summary__model=<any OpenRouter model id>` or `NSE_ALERTS__polling__nse__market_hours_interval_sec=3`.
Secrets (OpenRouter key, Telegram and WhatsApp credentials) live only in `worker/.env`.

## Where the data lives

Two databases, so the free Neon database stays small:

| Database | Holds | Why there |
| --- | --- | --- |
| **Shared (Neon)**, `DATABASE_URL`, `db/schema.postgres.sql` | subscribers and their stocks, the last 30 days of alerts and deliveries, corporate actions, a heartbeat | the website needs these |
| **Server (SQLite file)**, `LOCAL_DATABASE_URL`, `db/schema.local.sql` | 5 years of scored history, daily prices, cached company data, raw source JSON, alerts older than 30 days, live poll status | heavy, and only the worker reads it |

The server database defaults to `worker/data/local.db`; set `LOCAL_DATABASE_URL` to use a Postgres on the server instead.
`init-db` creates both. Every 6 hours the worker moves finished alerts older than `retention.shared_days` (30) from
Neon to the server database, backs up the server file daily to `worker/data/backups/` (last 7 kept), and warns if Neon
passes `retention.warn_shared_mb`. Live status is written to the server every poll but to Neon at most every 30 seconds.

```bash
python -m nse_alerts.cli db-sizes        # rows and sizes in both databases
python -m nse_alerts.cli archive-now     # move old alerts now
python -m nse_alerts.cli backup-local    # copy the server database file now
```

Copy `worker/data/backups/` off the server too (or snapshot the disk): the history exists nowhere else.
Neon's free plan also limits compute time, and the worker keeps asking Neon for new sign-ups every
`channels.telegram.verification_interval_sec` seconds, so check Neon's current free limits and raise that interval if needed.

## Past signals: what similar news did before

Every alert can carry a short section such as "Past signals (TCS, dividend news, sentiment 62, 8 cases in 5 yrs: if
₹10,000 was invested after each, 15 days avg +₹210 and 6 of 8 gained, 30 days ...)", plus a second message listing the
cases. It works like this:

- **Topic and sentiment:** each NSE announcement gets one of 18 fixed topics (dividend, financial results, buyback,
  order win, regulatory ...) and a sentiment from 1 to 100 (50 is neutral), judged by your OpenRouter model from the
  exchange text. When that text is too generic to judge ("X has informed the Exchange about ..."), the model's
  confidence is low and the item is left out of matching. Topics and the prompt are in
  `worker/nse_alerts/signals/taxonomy.py`.
- **Matching:** same topic, sentiment within ±10 points (`signals.sentiment_tolerance`), within the last 5 years, same
  stock. If the stock has fewer than 5 cases, it widens to all Nifty 50 stocks and says so.
- **Returns:** buy at the first price a quick reader could get (before the open: that day's open; during the session:
  that day's close; after the close: next open), sell at the close 15 and 30 calendar days later. Prices are daily
  and adjusted for splits and dividends. It also shows what the Nifty 50 did over the same days, counts same-stock cases
  closer than 15 days once, and ignores costs and taxes.
- **Speed:** the history is built ahead of time; at alert time it is one scoring call plus a database query, and the
  section is dropped rather than delaying the alert.

Build the history once (on the worker, with the virtual environment active):

```bash
python -m nse_alerts.cli init-db                  # adds the new tables
python -m nse_alerts.cli signals-backfill         # prices + 5 years of NSE announcements, free, about 10 minutes
python -m nse_alerts.cli signals-score --pdf      # topic + sentiment for every item (uses OpenRouter)
python -m nse_alerts.cli signals-returns          # 15 and 30 day outcomes
python -m nse_alerts.cli signals-stats            # topics, score spread, how many were too generic
python -m nse_alerts.cli signals-preview --symbol TCS --subject "Outcome of Board Meeting" --detail "Interim dividend of Rs 10 declared"
```

Try `signals-backfill --symbols TCS,INFY` and `signals-score --limit 200` first to see the quality and cost. On five
stocks the history held about 3,900 items, so all 50 stocks is roughly 40,000 model calls. A cheap fast model is
enough. `--pdf` reads each filing PDF when the exchange text is generic (about a third of items) and is much slower,
but the text alone rarely says what happened. New alerts are added to the history automatically and their 15 and 30 day
outcomes are filled in by a background job every 6 hours. Everything is in the `signals` section of `config.yaml`.

Limits to keep in mind: it uses today's Nifty 50 members only (stocks that dropped out are missing, which flatters
the results), 5 years gives few cases per stock and topic, and sentiment is a model's judgement. This is historical
statistics, not a recommendation; check with a lawyer how SEBI's research analyst rules apply before showing it to
the public.

## Switching on later
- **WhatsApp:** set `channels.whatsapp.enabled: true`, add `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID`, create and
  approve a message template (two body variables: title and summary), point Meta's webhook at
  `https://<your-site>/api/whatsapp/webhook`.
- **BSE:** set `polling.bse.enabled: true` once a proxy is available (`BSE_PROXY_URL`); BSE currently blocks AWS addresses.
- **NSE proxy:** if NSE starts blocking the server, set `NSE_PROXY_URL` to an India-based residential proxy.

## Tests
```bash
cd worker && python -m pytest          # 30 tests, no network or accounts needed
cd web && npm test
```

## Not verified yet
These parts were built and unit-tested but could not be run here: sending through a real Telegram account, the
WhatsApp channel, the web app against a real Postgres, and BSE. Run `poll-once`, `summarise-test` and a message to
yourself with `add-subscriber` before relying on it.
=======
# NSE_CAN
>>>>>>> f529941c510e2176028da4608778ad470d3c2877
