import { ADMIN_COOKIE, checkAdmin } from "@/lib/admin-auth";
import { json, rateLimited, tooManyRequests } from "@/lib/http";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

async function readToken(req: Request): Promise<{ token: string; form: boolean }> {
  const type = req.headers.get("content-type") ?? "";
  try {
    if (type.includes("application/json")) {
      const body = await req.json();
      return { token: typeof body?.token === "string" ? body.token : "", form: false };
    }
    const data = await req.formData();
    const token = data.get("token");
    return { token: typeof token === "string" ? token : "", form: true };
  } catch {
    return { token: "", form: false };
  }
}

export async function POST(req: Request) {
  if (rateLimited(req)) return tooManyRequests();
  const { token, form } = await readToken(req);
  const auth = checkAdmin(token, null);
  if (auth === "disabled") return json({ error: "Admin disabled" }, 503);

  if (auth !== "ok") {
    if (form) return NextResponse.redirect(new URL("/admin?error=1", req.url), 303);
    return json({ error: "Invalid token" }, 401);
  }

  const res = form ? NextResponse.redirect(new URL("/admin", req.url), 303) : json({ ok: true });
  res.cookies.set(ADMIN_COOKIE, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict",
    path: "/",
    maxAge: 60 * 60 * 12,
  });
  return res;
}
