import { createHmac } from "node:crypto";
import { query } from "@/lib/db";
import { json, tokenMatches } from "@/lib/http";

export const dynamic = "force-dynamic";

type Status = { id?: string; status?: string; timestamp?: string; errors?: { title?: string; message?: string }[] };
type Message = { from?: string; type?: string; text?: { body?: string } };
type Payload = { entry?: { changes?: { value?: { statuses?: Status[]; messages?: Message[] } }[] }[] };

const KNOWN = new Set(["sent", "delivered", "read", "failed"]);

export function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const expected = process.env.WHATSAPP_VERIFY_TOKEN;
  if (expected && params.get("hub.mode") === "subscribe" && tokenMatches(params.get("hub.verify_token"), expected)) {
    return new Response(params.get("hub.challenge") ?? "", { status: 200, headers: { "Content-Type": "text/plain" } });
  }
  return new Response("Forbidden", { status: 403 });
}

function validSignature(raw: string, header: string | null): boolean {
  const secret = process.env.WHATSAPP_APP_SECRET;
  if (!secret) return true;
  if (!header?.startsWith("sha256=")) return false;
  const expected = "sha256=" + createHmac("sha256", secret).update(raw).digest("hex");
  return tokenMatches(header, expected);
}

async function applyStatus(s: Status) {
  if (!s.id || !s.status || !KNOWN.has(s.status)) return;
  const ts = Number(s.timestamp);
  const at = Number.isFinite(ts) && ts > 0 ? ts : Date.now() / 1000;
  const error = s.status === "failed" ? (s.errors?.[0]?.title ?? s.errors?.[0]?.message ?? "failed").slice(0, 500) : null;
  await query(
    `update deliveries set
       status = $1,
       delivered_at = case when $1 in ('delivered', 'read') then coalesce(delivered_at, to_timestamp($3::double precision)) else delivered_at end,
       read_at = case when $1 = 'read' then coalesce(read_at, to_timestamp($3::double precision)) else read_at end,
       error = case when $1 = 'failed' then $4 else error end
     where channel = 'whatsapp' and provider_message_id = $2
       and (case status when 'queued' then 0 when 'sent' then 1 when 'delivered' then 2 when 'read' then 3 else 1 end)
         < (case $1 when 'sent' then 1 when 'delivered' then 2 when 'read' then 3 else 2 end)`,
    [s.status, s.id, at, error],
  );
}

async function applyInbound(m: Message) {
  if (m.type !== "text" || !m.from || !/^\d{6,15}$/.test(m.from)) return;
  if ((m.text?.body ?? "").trim().toUpperCase() !== "STOP") return;
  const rows = await query<{ id: string }>(
    "update subscribers set status = 'stopped', updated_at = now() where mobile = $1 returning id",
    ["+" + m.from],
  );
  if (rows[0]) await query("delete from subscriptions where subscriber_id = $1", [rows[0].id]);
}

export async function POST(req: Request) {
  const raw = await req.text();
  if (!validSignature(raw, req.headers.get("x-hub-signature-256"))) {
    return new Response("Forbidden", { status: 403 });
  }
  try {
    const payload = JSON.parse(raw) as Payload;
    for (const entry of payload.entry ?? []) {
      for (const change of entry.changes ?? []) {
        for (const s of change.value?.statuses ?? []) await applyStatus(s);
        for (const m of change.value?.messages ?? []) await applyInbound(m);
      }
    }
  } catch {
    // always acknowledge so Meta does not retry endlessly
  }
  return json({ ok: true });
}
