import { json, badRequest, serverError, tooManyRequests, rateLimited, readJson, mobileFrom } from "@/lib/http";
import { transaction } from "@/lib/db";
import { checkCode, type CodeState } from "@/lib/verify";

export const dynamic = "force-dynamic";

type Row = CodeState & { id: string; pending_symbols: string | null };

const invalid = () => json({ error: "Invalid or expired code" }, 400);

export async function POST(req: Request) {
  if (rateLimited(req)) return tooManyRequests();
  const body = await readJson(req);
  const mobile = mobileFrom(body?.mobile);
  const code = typeof body?.code === "string" ? body.code.trim() : "";
  if (!mobile || !/^\d{6}$/.test(code)) return badRequest();

  try {
    const result = await transaction(async (db) => {
      const { rows } = await db.query<Row>(
        `select id, pending_symbols, verify_code, verify_purpose, verify_expires_at, verify_attempts
         from subscribers where mobile = $1 for update`,
        [mobile],
      );
      const row = rows[0];
      if (!row) return null;

      const check = checkCode(row, code);
      if (check === "wrong") {
        await db.query("update subscribers set verify_attempts = verify_attempts + 1 where id = $1", [row.id]);
      }
      if (check !== "ok") return null;

      let status: string;
      if (row.verify_purpose === "stop") {
        await db.query("delete from subscriptions where subscriber_id = $1", [row.id]);
        status = "stopped";
      } else {
        const symbols = (row.pending_symbols ?? "").split(",").filter(Boolean);
        await db.query("delete from subscriptions where subscriber_id = $1", [row.id]);
        if (symbols.length > 0) {
          await db.query("insert into subscriptions (subscriber_id, symbol) select $1, unnest($2::text[])", [
            row.id,
            symbols,
          ]);
        }
        status = "active";
      }
      await db.query(
        `update subscribers
         set status = $2, verify_code = null, verify_purpose = null, pending_symbols = null,
             verify_expires_at = null, verify_attempts = 0, updated_at = now()
         where id = $1`,
        [row.id, status],
      );
      return { status, symbols: status === "active" ? (row.pending_symbols ?? "").split(",").filter(Boolean) : [] };
    });
    return result ? json(result) : invalid();
  } catch {
    return serverError();
  }
}
