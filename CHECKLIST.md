# What is pending

Status as of 6 Oct 2026. Built and unit-tested means it passes automated tests here; it does not mean it has run against
your real accounts.

## A. Set up the accounts and servers (you)
- [ ] Neon Postgres database created; connection string put in `worker/.env` and in the Vercel environment variables
- [ ] Schemas applied: `python -m nse_alerts.cli init-db` creates the small Neon schema and the server database
      (`worker/data/local.db`); run it again after every update
- [ ] Server backups copied off the server (or disk snapshots on): `worker/data/backups/` is the only copy of the history
- [ ] Neon free-plan limits checked (storage and compute hours); `db-sizes` run after a week
- [ ] OpenRouter key in `worker/.env`; model chosen in `worker/config.yaml`; checked with `summarise-test`
- [ ] A dedicated Telegram account and number for sending; `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from my.telegram.org
- [ ] EC2 instance (Mumbai): code copied, virtual environment, `pip install -r requirements.txt`, `.env` filled in
- [ ] `telegram-login` run once on the server
- [ ] `poll-once` run on the server (NSE reachable from AWS; it worked in the earlier test, confirm again)
- [ ] systemd service installed and started (`worker/nse-alerts.service`); logs checked
- [ ] Web app deployed on Vercel with root directory `web`; env vars set (`DATABASE_URL`, `ADMIN_TOKEN`,
      `REQUIRE_VERIFICATION`, `NEXT_PUBLIC_DEFAULT_COUNTRY_CODE`)

## B. Past data for the Nifty 50 (not done: nothing is loaded in your database yet)
- [ ] `signals-backfill` for all 50 stocks (prices and 5 years of announcements; free, about 10 minutes)
- [ ] Trial first: `signals-backfill --symbols TCS,INFY` then `signals-score --pdf --limit 200`; read `signals-stats`
- [ ] `signals-score --pdf` for everything (about 40,000 model calls; slower with `--pdf`)
- [ ] `signals-returns` to fill the 15 and 30 day outcomes
- [ ] Spot-check 10 to 20 scored items by hand (topic and sentiment make sense?)
- [ ] `signals-preview` on a few real stocks; decide tolerance (points or percent), minimum cases, cooldown

## C. First real run (nothing below has run against real accounts yet)
- [ ] `add-subscriber` with your own number, then a real alert reaches your Telegram within 60 seconds
- [ ] The alert shows bullets (under 100 words), price and volume, and the past-signals block
- [ ] The 30-minute follow-up arrives (only for alerts that come in market hours)
- [ ] Website flow end to end: pick stocks, enter number, code arrives on Telegram, verify, confirmation
- [ ] STOP reply and the website stop flow both unsubscribe
- [ ] Admin page loads and shows events, deliveries and the worker heartbeat
- [ ] One full market day: check SLA breaches in the admin page and how late NSE lists items
- [ ] One busy day (results season): check no announcement is missed when more than 20 arrive between polls
- [ ] Telegram sender account stays healthy (no flood or spam limit) with real recipients

## D. Built but switched off
- [ ] WhatsApp: verified Business account, message template approved (two variables), webhook set to
      `/api/whatsapp/webhook`, token and phone number ID in `.env`, then `channels.whatsapp.enabled: true`
- [ ] BSE polling: needs a proxy (BSE blocks AWS); then `polling.bse.enabled: true`

## E. Not built yet
- [ ] News headlines as a source (the requirements list news, only NSE announcements and corporate actions are built)
- [ ] Daily digest and quiet hours
- [ ] Chat commands (for example /upcoming TCS)
- [ ] A public web page for browsing past signals (the data is stored, no page yet)
- [ ] Rate limiting that survives Vercel restarts (the current limit is in memory, best effort)

## F. Decisions and risks for you
- [ ] Legal review: SEBI research-analyst rules for past-return statistics and for a public alert service
- [ ] Privacy policy, terms and consent wording on the website (DPDP Act); disclaimer text approved
- [ ] NSE terms of use: scraping is against them; plan the move to a licensed feed
- [ ] Residential proxy provider chosen and budgeted, in case NSE blocks the server later
- [ ] Yahoo Finance (prices, fundamentals, volume) is unofficial; choose a paid source if it becomes unreliable
- [ ] Monitoring: set `admin.notify_phone` for feed outages; add an alarm if the worker stops
- [ ] Review the BRD once more and confirm it matches what was built
