import { NextResponse } from "next/server";
import { createHash, timingSafeEqual } from "node:crypto";
import { normalizeMobile } from "./mobile";

const WINDOW_MS = 60_000;
const MAX_REQUESTS = 10;
const hits = new Map<string, number[]>();

export function json(body: unknown, status = 200) {
  return NextResponse.json(body, { status, headers: { "Cache-Control": "no-store" } });
}

export const badRequest = () => json({ error: "Invalid request" }, 400);
export const serverError = () => json({ error: "Something went wrong" }, 500);
export const tooManyRequests = () => json({ error: "Too many requests, please try again shortly" }, 429);

export function clientIp(req: Request): string {
  const fwd = req.headers.get("x-forwarded-for");
  return (fwd ? fwd.split(",")[0].trim() : req.headers.get("x-real-ip")) || "unknown";
}

export function rateLimited(req: Request): boolean {
  const now = Date.now();
  const ip = clientIp(req);
  const recent = (hits.get(ip) ?? []).filter((t) => now - t < WINDOW_MS);
  recent.push(now);
  hits.set(ip, recent);
  if (hits.size > 5000) {
    for (const [key, times] of hits) {
      if (now - times[times.length - 1] >= WINDOW_MS) hits.delete(key);
    }
  }
  return recent.length > MAX_REQUESTS;
}

export async function readJson(req: Request): Promise<Record<string, unknown> | null> {
  try {
    const body = await req.json();
    return body && typeof body === "object" && !Array.isArray(body) ? (body as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

export function mobileFrom(input: unknown): string | null {
  return normalizeMobile(input, process.env.NEXT_PUBLIC_DEFAULT_COUNTRY_CODE || "+91");
}

export function tokenMatches(given: string | null | undefined, expected: string): boolean {
  if (!given) return false;
  const a = createHash("sha256").update(given).digest();
  const b = createHash("sha256").update(expected).digest();
  return timingSafeEqual(a, b);
}
