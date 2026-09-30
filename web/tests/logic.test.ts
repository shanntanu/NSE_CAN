import { describe, expect, it } from "vitest";
import { normalizeMobile } from "../src/lib/mobile";
import { checkCode, generateCode, type CodeState } from "../src/lib/verify";
import { parseSymbols } from "../src/lib/stocks";

describe("normalizeMobile", () => {
  it.each([
    ["9876543210", "+919876543210"],
    ["98765 43210", "+919876543210"],
    ["098765-43210", "+919876543210"],
    ["+91 98765 43210", "+919876543210"],
    ["919876543210", "+919876543210"],
    ["0091 9876543210", "+919876543210"],
    ["+1 (415) 555-2671", "+14155552671"],
  ])("%s -> %s", (input, expected) => {
    expect(normalizeMobile(input, "+91")).toBe(expected);
  });

  it.each(["", "12345", "abcdefghij", "5876543210", "+0123456789", "98765432101234567890"])("rejects %s", (input) => {
    expect(normalizeMobile(input, "+91")).toBeNull();
  });

  it("rejects non-strings", () => {
    expect(normalizeMobile(9876543210, "+91")).toBeNull();
  });

  it("uses the given default country code", () => {
    expect(normalizeMobile("4155552671", "+1")).toBe("+14155552671");
  });
});

describe("checkCode", () => {
  const now = new Date("2026-01-01T10:00:00Z");
  const base: CodeState = {
    verify_code: "123456",
    verify_purpose: "start",
    verify_expires_at: new Date("2026-01-01T10:05:00Z"),
    verify_attempts: 0,
  };

  it("accepts the right code", () => expect(checkCode(base, "123456", now)).toBe("ok"));
  it("rejects a wrong code", () => expect(checkCode(base, "654321", now)).toBe("wrong"));
  it("rejects an expired code", () => {
    expect(checkCode({ ...base, verify_expires_at: new Date("2026-01-01T09:59:00Z") }, "123456", now)).toBe("expired");
  });
  it("locks after 5 attempts", () => expect(checkCode({ ...base, verify_attempts: 5 }, "123456", now)).toBe("locked"));
  it("reports none when no code is pending", () => {
    expect(checkCode({ ...base, verify_code: null }, "123456", now)).toBe("none");
  });
});

describe("generateCode", () => {
  it("returns 6 digits", () => {
    for (let i = 0; i < 50; i++) expect(generateCode()).toMatch(/^\d{6}$/);
  });
});

describe("parseSymbols", () => {
  it("accepts Nifty 50 symbols and dedupes", () => {
    expect(parseSymbols(["ADANIENT", "ADANIENT"])).toEqual(["ADANIENT"]);
  });
  it("rejects unknown, empty and non-array input", () => {
    expect(parseSymbols(["NOTASTOCK"])).toBeNull();
    expect(parseSymbols([])).toBeNull();
    expect(parseSymbols("ADANIENT")).toBeNull();
  });
});
