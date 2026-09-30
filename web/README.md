# Nifty 50 Alerts - web

Next.js (App Router) front end and API for the NSE alerts service. It shares a Postgres database with the Python
worker, which polls NSE, summarises, sends alerts and verification codes over Telegram, and will add WhatsApp later.
The schema lives in `../db/schema.postgres.sql` and is the single source of truth.

## Run locally

```bash
cd web
npm install
cp .env.example .env.local   # fill in DATABASE_URL at least
npm run dev                  # http://localhost:3000
```

Apply the schema once to your database:

```bash
psql "$DATABASE_URL" -f ../db/schema.postgres.sql
# or use the worker: python -m nse_alerts.cli init-db
```

Other scripts: `npm run build`, `npm test` (vitest), `npx tsc --noEmit`. The build needs no database.

## Environment variables

| Name | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres connection string (Neon: keep `sslmode=require`). |
| `ADMIN_TOKEN` | Enables `/admin` and `/api/admin/summary`. If unset the admin returns 503. |
| `NEXT_PUBLIC_DEFAULT_COUNTRY_CODE` | Default country code for numbers typed without one. Default `+91`. |
| `REQUIRE_VERIFICATION` | `false` activates subscriptions without the Telegram code step. Default `true`. |
| `WHATSAPP_VERIFY_TOKEN` | Token for the Meta webhook verification handshake. |
| `WHATSAPP_APP_SECRET` | When set, `X-Hub-Signature-256` on webhook POSTs is verified. |

## Deploy on Vercel

1. Push the repo and import it in Vercel, setting the root directory to `web`.
2. Create a Neon Postgres database (Vercel Storage or neon.tech) and run the schema file once (see above).
3. Set the environment variables above in the project settings and deploy.
4. Point the Meta webhook (when WhatsApp is enabled) at `https://<your-domain>/api/whatsapp/webhook`.

## Endpoints

- `GET /api/stocks`
- `POST /api/subscribe`, `POST /api/verify`, `POST /api/stop`, `GET /api/status?mobile=`
- `GET|POST /api/whatsapp/webhook`
- `POST /api/admin/login`, `GET /api/admin/summary` (cookie or `Authorization: Bearer <ADMIN_TOKEN>`)

Rate limiting is in memory per server instance (10 POSTs per minute per IP), so on Vercel it is best effort.
