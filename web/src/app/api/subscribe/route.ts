import { json, badRequest, serverError, tooManyRequests, rateLimited, readJson, mobileFrom } from "@/lib/http";
import { query, transaction } from "@/lib/db";
import { parseSymbols } from "@/lib/stocks";
import { CODE_TTL_MINUTES, RESEND_COOLDOWN_SECONDS, generateCode } from "@/lib/verify";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  if (rateLimited(req)) return tooManyRequests();
  const body = await readJson(req);
  if (!body || body.consent !== true) return badRequest();
  const mobile = mobileFrom(body.mobile);
  const symbols = parseSymbols(body.symbols);
  if (!mobile || !symbols) return badRequest();

  try {
    if (process.env.REQUIRE_VERIFICATION === "false") {
      await transaction(async (db) => {
        const { rows } = await db.query<{ id: string }>(
          `insert into subscribers (mobile, status, consent_at)
           values ($1, 'active', now())
           on conflict (mobile) do update
             set status = 'active', consent_at = now(), pending_symbols = null, verify_code = null,
                 verify_purpose = null, verify_expires_at = null, verify_attempts = 0, updated_at = now()
           returning id`,
          [mobile],
        );
        const id = rows[0].id;
        await db.query("delete from subscriptions where subscriber_id = $1", [id]);
        await db.query(
          "insert into subscriptions (subscriber_id, symbol) select $1, unnest($2::text[])",
          [id, symbols],
        );
      });
      return json({ status: "active" });
    }

    const rows = await query(
      `insert into subscribers
         (mobile, status, pending_symbols, verify_code, verify_purpose, verify_expires_at,
          verify_attempts, verify_sent_at, consent_at)
       values ($1, 'pending', $2, $3, 'start', now() + make_interval(mins => $4), 0, null, now())
       on conflict (mobile) do update
         set status = case when subscribers.status = 'active' then 'active' else 'pending' end,
             pending_symbols = excluded.pending_symbols,
             verify_code = excluded.verify_code,
             verify_purpose = 'start',
             verify_expires_at = excluded.verify_expires_at,
             verify_attempts = 0,
             verify_sent_at = null,
             telegram_status = case when subscribers.telegram_status = 'resolved' then 'resolved' else 'unknown' end,
             consent_at = now(),
             updated_at = now()
         where subscribers.verify_code is null
            or subscribers.verify_expires_at is null
            or subscribers.verify_expires_at < now() + make_interval(mins => $4) - make_interval(secs => $5)
       returning id`,
      [mobile, symbols.join(","), generateCode(), CODE_TTL_MINUTES, RESEND_COOLDOWN_SECONDS],
    );
    if (rows.length === 0) return tooManyRequests();
    return json({ status: "pending" });
  } catch {
    return serverError();
  }
}
