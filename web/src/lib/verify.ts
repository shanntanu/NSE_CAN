import { createHash, randomInt, timingSafeEqual } from "node:crypto";

export const CODE_TTL_MINUTES = 10;
export const RESEND_COOLDOWN_SECONDS = 60;
export const MAX_ATTEMPTS = 5;

export type CodeState = {
  verify_code: string | null;
  verify_purpose: string | null;
  verify_expires_at: Date | null;
  verify_attempts: number;
};

export type CodeCheck = "ok" | "wrong" | "expired" | "locked" | "none";

export function generateCode(): string {
  return String(randomInt(0, 1_000_000)).padStart(6, "0");
}

export function safeEqual(a: string, b: string): boolean {
  const ha = createHash("sha256").update(a).digest();
  const hb = createHash("sha256").update(b).digest();
  return timingSafeEqual(ha, hb);
}

export function checkCode(state: CodeState, code: string, now: Date = new Date()): CodeCheck {
  if (!state.verify_code || !state.verify_purpose || !state.verify_expires_at) return "none";
  if (state.verify_expires_at.getTime() <= now.getTime()) return "expired";
  if (state.verify_attempts >= MAX_ATTEMPTS) return "locked";
  return safeEqual(state.verify_code, code) ? "ok" : "wrong";
}
