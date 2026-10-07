-- LOCAL schema: heavy data kept on the worker server (SQLite file by default, or any Postgres you run there).
-- Nothing here is needed by the web app. Applied by `python -m nse_alerts.cli init-db`.

-- Cached fundamentals and technicals per stock, refreshed by the worker.
create table if not exists context_cache (
  symbol     text primary key,
  data       text not null,                             -- JSON
  updated_at timestamptz not null default now()
);

-- Adjusted daily prices (splits and dividends applied) for the tracked stocks and the Nifty 50 index (symbol NIFTY50).
create table if not exists daily_prices (
  symbol     text not null,
  trade_date text not null,                             -- YYYY-MM-DD
  open       double precision,
  close      double precision,
  primary key (symbol, trade_date)
);

-- Every scored news item, past and live: its topic, sentiment 1-100 and what the stock did afterwards.
create table if not exists event_signals (
  id              bigserial primary key,
  source          text not null default 'nse',
  source_uid      text not null,
  symbol          text not null,
  category        text not null,                        -- announcement | corporate_action | news
  subject         text,
  text_used       text,                                 -- the text the score was based on
  attachment_url  text,                                 -- the filing, read when the exchange text is too generic
  listed_at       timestamptz not null,
  topic           text,
  sentiment       integer,                              -- 1 to 100, 50 is neutral
  confidence      double precision,                     -- 0 to 1; low means the text was too generic to judge
  rationale       text,
  scored_model    text,
  prompt_version  text,
  scored_at       timestamptz,
  entry_date      text,
  entry_basis     text,                                 -- open | close
  entry_price     double precision,
  ret_15d_pct     double precision,
  ret_30d_pct     double precision,
  nifty_15d_pct   double precision,
  nifty_30d_pct   double precision,
  returns_status  text not null default 'pending',      -- pending | done | no_prices
  unique (source, source_uid)
);
create index if not exists idx_signals_topic on event_signals(topic, sentiment);
create index if not exists idx_signals_symbol on event_signals(symbol, listed_at);
create index if not exists idx_signals_unscored on event_signals(scored_at);

-- Raw source JSON for each alert, kept here so the shared database stays small.
create table if not exists event_raw (
  event_id bigint primary key,
  raw      text not null
);

-- Status written every few seconds (poll health, context refresh). The shared copy is written much less often.
create table if not exists system_status (
  key        text primary key,
  value      text,
  updated_at timestamptz not null default now()
);

-- Old rows moved out of the shared database after retention.shared_days. Same columns, no foreign keys.
create table if not exists events_archive (
  id               bigint primary key,
  source           text,
  source_uid       text,
  category         text,
  symbol           text,
  company          text,
  subject          text,
  detail           text,
  attachment_url   text,
  attachment_size  text,
  listed_at        timestamptz,
  company_time     timestamptz,
  detected_at      timestamptz,
  is_nifty50       boolean,
  raw              text,
  summary          text,
  summary_model    text,
  summary_ms       integer,
  summary_fallback boolean,
  status           text,
  processed_at     timestamptz,
  created_at       timestamptz
);
create index if not exists idx_events_archive_symbol on events_archive(symbol, listed_at);

create table if not exists deliveries_archive (
  id                  bigint primary key,
  event_id            bigint,
  subscriber_id       bigint,
  channel             text,
  status              text,
  provider_message_id text,
  attachment_status   text,
  error               text,
  attempts            integer,
  queued_at           timestamptz,
  sent_at             timestamptz,
  delivered_at        timestamptz,
  read_at             timestamptz,
  since_listed_ms     bigint,
  since_detected_ms   bigint,
  within_sla          boolean
);
create index if not exists idx_deliveries_archive_event on deliveries_archive(event_id);

create table if not exists follow_ups_archive (
  event_id           bigint primary key,
  symbol             text,
  due_at             timestamptz,
  status             text,
  attempts           integer,
  price_at_alert     double precision,
  volume_at_alert    bigint,
  index_at_alert     double precision,
  price_now          double precision,
  volume_now         bigint,
  index_now          double precision,
  change_pct         double precision,
  index_change_pct   double precision,
  verdict            text,
  summary            text,
  summary_model      text,
  follow_up_event_id bigint,
  created_at         timestamptz,
  processed_at       timestamptz
);
