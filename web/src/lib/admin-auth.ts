import { tokenMatches } from "./http";

export const ADMIN_COOKIE = "admin_token";

export type AdminAuth = "disabled" | "denied" | "ok";

export function checkAdmin(cookieToken?: string | null, authHeader?: string | null): AdminAuth {
  const expected = process.env.ADMIN_TOKEN;
  if (!expected) return "disabled";
  const bearer = authHeader?.startsWith("Bearer ") ? authHeader.slice(7).trim() : null;
  return tokenMatches(cookieToken, expected) || tokenMatches(bearer, expected) ? "ok" : "denied";
}
