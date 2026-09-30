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
   of about 150 words. If the model is late or fails, a short template summary is sent instead of missing the deadline.
4. The text goes out first, then the attachment. Each send is recorded in `deliveries` with the time since NSE listed it
   (`exchdisstime`) and since our poller saw it, and whether it met the 60-second target.

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
