import { query } from "./db";

export async function getSummary() {
  const [subs, stocks, heartbeat, events, deliveries, sla, breaches, channels, actions] = await Promise.all([
    query<{ status: string; n: string }>(
      "select status, count(*) as n from subscribers group by status order by status",
    ),
    query<{ symbol: string; n: string }>(
      "select symbol, count(*) as n from subscriptions group by symbol order by n desc, symbol limit 10",
    ),
    query<{ value: string | null; updated_at: Date; age_s: string }>(
      `select value, updated_at, extract(epoch from (now() - updated_at)) as age_s
       from system_status where key = 'worker_heartbeat'`,
    ),
    query(
      `select id, listed_at, detected_at, symbol, category, subject, status,
              left(coalesce(summary, ''), 120) as summary
       from events order by detected_at desc limit 25`,
    ),
    query(
      `select id, queued_at, sent_at, channel, status, since_listed_ms, since_detected_ms, within_sla
       from deliveries order by queued_at desc limit 50`,
    ),
    query<{ sent: string; within: string }>(
      `select count(*) as sent, count(*) filter (where within_sla) as within
       from deliveries where sent_at > now() - interval '24 hours'`,
    ),
    query(
      `select d.id, d.sent_at, d.channel, d.since_listed_ms, d.since_detected_ms, e.subject, e.symbol
       from deliveries d join events e on e.id = d.event_id
       where d.sent_at > now() - interval '24 hours' and d.within_sla = false
       order by d.sent_at desc limit 50`,
    ),
    query<{ channel: string; status: string; n: string }>(
      "select channel, status, count(*) as n from deliveries group by channel, status order by channel, status",
    ),
    query(
      `select id, symbol, company, subject, ex_date, record_date, detected_at
       from corporate_actions order by detected_at desc limit 10`,
    ),
  ]);

  const sent = Number(sla[0]?.sent ?? 0);
  const within = Number(sla[0]?.within ?? 0);
  const hb = heartbeat[0];

  return {
    subscribers: subs.map((r) => ({ status: r.status, count: Number(r.n) })),
    topStocks: stocks.map((r) => ({ symbol: r.symbol, count: Number(r.n) })),
    heartbeat: hb ? { value: hb.value, updatedAt: hb.updated_at, ageSeconds: Number(hb.age_s) } : null,
    events,
    deliveries,
    sla24h: {
      sent,
      withinSla: within,
      withinPct: sent ? Math.round((within / sent) * 1000) / 10 : null,
      breaches,
    },
    deliveryStatus: channels.map((r) => ({ channel: r.channel, status: r.status, count: Number(r.n) })),
    corporateActions: actions,
  };
}

export type Summary = Awaited<ReturnType<typeof getSummary>>;
