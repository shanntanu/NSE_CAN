import { json, serverError, mobileFrom } from "@/lib/http";
import { query } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const mobile = mobileFrom(new URL(req.url).searchParams.get("mobile"));
  if (!mobile) return json({ status: "none" });
  try {
    const rows = await query<{ status: string; telegram_status: string; code_sent: boolean }>(
      `select status, telegram_status, verify_sent_at is not null as code_sent
       from subscribers where mobile = $1`,
      [mobile],
    );
    if (rows.length === 0) return json({ status: "none" });
    return json(rows[0]);
  } catch {
    return serverError();
  }
}
