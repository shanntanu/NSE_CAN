import { json, badRequest, serverError, tooManyRequests, rateLimited, readJson, mobileFrom } from "@/lib/http";
import { query } from "@/lib/db";
import { CODE_TTL_MINUTES, generateCode } from "@/lib/verify";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  if (rateLimited(req)) return tooManyRequests();
  const body = await readJson(req);
  const mobile = mobileFrom(body?.mobile);
  if (!mobile) return badRequest();

  try {
    await query(
      `update subscribers
       set verify_code = $2, verify_purpose = 'stop', verify_expires_at = now() + make_interval(mins => $3),
           verify_attempts = 0, verify_sent_at = null, updated_at = now()
       where mobile = $1 and status = 'active'
         and (verify_code is null or verify_expires_at is null
              or verify_expires_at < now() + make_interval(mins => $3) - interval '60 seconds')`,
      [mobile, generateCode(), CODE_TTL_MINUTES],
    );
    return json({ ok: true });
  } catch {
    return serverError();
  }
}
