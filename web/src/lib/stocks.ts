import data from "../../data/nifty50.json";

export type Stock = { symbol: string; company: string; industry: string; isin: string };

export const STOCKS: Stock[] = data;

const SYMBOLS = new Set(STOCKS.map((s) => s.symbol));

export function parseSymbols(input: unknown): string[] | null {
  if (!Array.isArray(input) || input.length === 0 || input.length > SYMBOLS.size) return null;
  const out = new Set<string>();
  for (const s of input) {
    if (typeof s !== "string" || !SYMBOLS.has(s)) return null;
    out.add(s);
  }
  return [...out];
}
