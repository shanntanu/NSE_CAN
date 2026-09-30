-- Shared schema for the NSE alerts worker (Python) and the web app (Next.js on Vercel).
-- Single source of truth. The worker applies this file with `python -m nse_alerts.cli init-db`
-- (for SQLite in tests it is converted automatically). All timestamps are UTC (timestamptz).
-- Keep the SQL portable: plain types, no extensions.

create table if not exists subscribers (
  id                   bigserial primary key,
  mobile               text not null unique,            -- E.164, e.g. +919876543210
  status               text not null default 'pending', -- pending | active | stopped
  pending_symbols      text,                            -- comma list applied when the code is verified
  telegram_status      text not null default 'unknown', -- unknown | resolved | not_found
  telegram_user_id     bigint,
  telegram_access_hash bigint,
  whatsapp_opt_in      boolean not null default true,
  verify_code          text,                            -- 6 digits, cleared once used
  verify_purpose       text,                            -- start | stop
  verify_expires_at    timestamptz,
  verify_attempts      integer not null default 0,
  verify_sent_at       timestamptz,                     -- set by the worker when the code went out
  consent_at           timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

create table if not exists subscriptions (
  subscriber_id bigint not null references subscribers(id) on delete cascade,
  symbol        text not null,
  primary key (subscriber_id, symbol)
);
create index if not exists idx_subscriptions_symbol on subscriptions(symbol);

-- Every announcement, news item and corporate action that becomes an alert.
create table if not exists events (
  id               bigserial primary key,
  source           text not null,                       -- nse | bse
  source_uid       text not null,                       -- NSE seq_id, or a hash for corporate actions
  category         text not null,                       -- announcement | corporate_action | news
  symbol           text not null,
  company          text,
  subject          text,                                -- short title, e.g. "Board Meeting Intimation"
  detail           text,                                -- longer text from the source
  attachment_url   text,
  attachment_size  text,
  listed_at        timestamptz,                         -- when NSE disseminated it (exchdisstime)
  company_time     timestamptz,                         -- when the company submitted it (an_dt)
  detected_at      timestamptz not null,                -- when our poller first saw it
  is_nifty50       boolean not null default true,
  raw              text,                                -- source JSON
  summary          text,
  summary_model    text,
  summary_ms       integer,
  summary_fallback boolean not null default false,
  status           text not null default 'new',         -- new | processing | done | skipped | failed
  processed_at     timestamptz,
  created_at       timestamptz not null default now(),
  unique (source, source_uid)
);
create index if not exists idx_events_symbol_listed on events(symbol, listed_at);
create index if not exists idx_events_status on events(status);

-- Structured corporate actions (dividend, split, bonus ...). One alert event is also created per new row.
create table if not exists corporate_actions (
  id          bigserial primary key,
  symbol      text not null,
  company     text,
  isin        text,
  series      text,
  subject     text not null,                            -- e.g. "Dividend - Rs 2.35 Per Share"
  ex_date     text not null default '-',
  record_date text not null default '-',
  bc_start    text,
  bc_end      text,
  nd_start    text,
  nd_end      text,
  face_value  text,
  is_nifty50  boolean not null default true,
  event_id    bigint references events(id),
  raw         text,
  detected_at timestamptz not null,
  unique (symbol, subject, ex_date, record_date)
);
create index if not exists idx_corporate_actions_symbol on corporate_actions(symbol);

create table if not exists deliveries (
  id                  bigserial primary key,
  event_id            bigint not null references events(id) on delete cascade,
  subscriber_id       bigint not null references subscribers(id) on delete cascade,
  channel             text not null,                    -- telegram | whatsapp
  status              text not null default 'queued',   -- queued | sent | delivered | read | failed
  provider_message_id text,                             -- Telegram message id or WhatsApp wamid
  attachment_status   text,                             -- none | sent | failed | link_only
  error               text,
  attempts            integer not null default 0,
  queued_at           timestamptz not null default now(),
  sent_at             timestamptz,
  delivered_at        timestamptz,
  read_at             timestamptz,
  since_listed_ms     bigint,                           -- sent_at minus events.listed_at
  since_detected_ms   bigint,                           -- sent_at minus events.detected_at
  within_sla          boolean,
  unique (event_id, subscriber_id, channel)
);
create index if not exists idx_deliveries_status on deliveries(status);
create index if not exists idx_deliveries_provider on deliveries(provider_message_id);

-- Cached fundamentals and technicals per stock, refreshed by the worker.
create table if not exists context_cache (
  symbol     text primary key,
  data       text not null,                             -- JSON
  updated_at timestamptz not null default now()
);

-- Small key/value table: worker heartbeat, last poll results, feed health.
create table if not exists system_status (
  key        text primary key,
  value      text,
  updated_at timestamptz not null default now()
);
