const E164 = /^\+[1-9]\d{7,14}$/;

export function normalizeMobile(input: unknown, defaultCountryCode = "+91"): string | null {
  if (typeof input !== "string" || input.length > 40) return null;
  const cc = defaultCountryCode.replace(/\D/g, "") || "91";
  const raw = input.trim().replace(/[\s\-().]/g, "");
  if (!/^\+?\d+$/.test(raw)) return null;

  let digits: string;
  if (raw.startsWith("+")) {
    digits = raw.slice(1);
  } else if (raw.startsWith("00")) {
    digits = raw.slice(2);
  } else if (raw.startsWith("0")) {
    digits = cc + raw.replace(/^0+/, "");
  } else if (raw.length > 10 && raw.startsWith(cc)) {
    digits = raw;
  } else {
    digits = cc + raw;
  }

  const e164 = "+" + digits;
  if (!E164.test(e164)) return null;
  if (e164.startsWith("+91") && !/^\+91[6-9]\d{9}$/.test(e164)) return null;
  return e164;
}
