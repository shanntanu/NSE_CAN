import { cookies } from "next/headers";
import { ADMIN_COOKIE, checkAdmin } from "@/lib/admin-auth";
import { getSummary } from "@/lib/admin-data";
import { json, serverError } from "@/lib/http";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const jar = await cookies();
  const auth = checkAdmin(jar.get(ADMIN_COOKIE)?.value, req.headers.get("authorization"));
  if (auth === "disabled") return json({ error: "Admin disabled" }, 503);
  if (auth !== "ok") return json({ error: "Unauthorized" }, 401);
  try {
    return json(await getSummary());
  } catch {
    return serverError();
  }
}
