"use client";

import { useEffect, useMemo, useState } from "react";
import { normalizeMobile } from "@/lib/mobile";
import type { Stock } from "@/lib/stocks";

type Mode = "start" | "stop";
type Step = "form" | "code" | "done";

async function post(url: string, body: unknown) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  } catch {
    return { ok: false, status: 0, data: {} };
  }
}

export default function Subscribe({ stocks, defaultCountryCode }: { stocks: Stock[]; defaultCountryCode: string }) {
  const [mode, setMode] = useState<Mode>("start");
  const [step, setStep] = useState<Step>("form");
  const [selected, setSelected] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [mobile, setMobile] = useState("");
  const [consent, setConsent] = useState(false);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [notOnTelegram, setNotOnTelegram] = useState(false);
  const [confirmed, setConfirmed] = useState<string[]>([]);

  const normalized = normalizeMobile(mobile, defaultCountryCode);
  const isStart = mode === "start";

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return stocks;
    return stocks.filter((s) => [s.symbol, s.company, s.industry].some((v) => v.toLowerCase().includes(q)));
  }, [stocks, search]);

  useEffect(() => {
    if (step !== "code" || !normalized) return;
    let stop = false;
    const tick = async () => {
      try {
        const res = await fetch(`/api/status?mobile=${encodeURIComponent(normalized)}`, { cache: "no-store" });
        const data = await res.json();
        if (!stop) setNotOnTelegram(data.telegram_status === "not_found");
      } catch {
        // keep polling
      }
    };
    tick();
    const timer = setInterval(tick, 3000);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, [step, normalized]);

  function toggle(symbol: string) {
    setSelected((cur) => (cur.includes(symbol) ? cur.filter((s) => s !== symbol) : [...cur, symbol]));
  }

  function reset(next: Mode) {
    setMode(next);
    setStep("form");
    setCode("");
    setError("");
    setNotOnTelegram(false);
  }

  async function submit() {
    setError("");
    if (!normalized) return setError("Enter a valid mobile number.");
    if (isStart && selected.length === 0) return setError("Select at least one stock.");
    if (isStart && !consent) return setError("Please tick the consent box to continue.");

    setBusy(true);
    const res = isStart
      ? await post("/api/subscribe", { mobile: normalized, symbols: selected, consent: true })
      : await post("/api/stop", { mobile: normalized });
    setBusy(false);

    if (res.status === 429) return setError("Please wait a minute before requesting another code.");
    if (!res.ok) return setError("Something went wrong. Please check your details and try again.");

    if (isStart && res.data.status === "active") {
      setConfirmed(selected);
      return setStep("done");
    }
    setNotOnTelegram(false);
    setStep("code");
  }

  async function verify() {
    setError("");
    if (!normalized || !/^\d{6}$/.test(code.trim())) return setError("Enter the 6-digit code.");
    setBusy(true);
    const res = await post("/api/verify", { mobile: normalized, code: code.trim() });
    setBusy(false);
    if (res.status === 429) return setError("Too many attempts. Please wait a minute.");
    if (!res.ok) return setError("That code is invalid or has expired.");
    setConfirmed(Array.isArray(res.data.symbols) ? res.data.symbols : []);
    setStep("done");
  }

  if (step === "done") {
    return (
      <section className="card">
        {isStart ? (
          <>
            <h2 className="success">Alerts are on</h2>
            <p>You will receive Telegram alerts on {normalized} for:</p>
            <div className="chips">
              {confirmed.map((s) => (
                <span className="chip" key={s}>{s}</span>
              ))}
            </div>
          </>
        ) : (
          <>
            <h2 className="success">Alerts stopped</h2>
            <p>You will no longer receive alerts on {normalized}.</p>
          </>
        )}
        <p><button className="link" onClick={() => reset("start")}>Back to start</button></p>
      </section>
    );
  }

  if (step === "code") {
    return (
      <section className="card">
        <h2>Enter your code</h2>
        <p>We sent a 6-digit code to your Telegram (the number must be on Telegram).</p>
        {notOnTelegram && (
          <p className="error">
            This number is not on Telegram, so we cannot send the code. Check the number or join Telegram first.
          </p>
        )}
        <div className="row">
          <input
            className="code"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
            aria-label="Verification code"
          />
          <button className="primary" onClick={verify} disabled={busy}>
            {isStart ? "Confirm" : "Confirm stop"}
          </button>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        <p><button className="link" onClick={() => reset(mode)}>Start over</button></p>
      </section>
    );
  }

  if (!isStart) {
    return (
      <section className="card">
        <h2>Stop alerts</h2>
        <p className="muted">Enter your mobile number. We will send a code to confirm.</p>
        <input
          type="tel"
          inputMode="tel"
          autoComplete="tel"
          placeholder={`${defaultCountryCode} 98765 43210`}
          value={mobile}
          onChange={(e) => setMobile(e.target.value)}
          aria-label="Mobile number"
        />
        {error && <p className="error" role="alert">{error}</p>}
        <div className="row" style={{ marginTop: 12 }}>
          <button className="primary" onClick={submit} disabled={busy}>Send code</button>
          <button className="link" onClick={() => reset("start")}>Cancel</button>
        </div>
      </section>
    );
  }

  return (
    <>
      <section className="card">
        <h2>1. Pick stocks</h2>
        <div className="row">
          <input
            className="grow"
            type="search"
            placeholder="Search symbol, company or industry"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search stocks"
          />
          <button onClick={() => setSelected(stocks.map((s) => s.symbol))}>Select all</button>
          <button onClick={() => setSelected([])}>Clear</button>
        </div>
        <p className="muted" style={{ margin: "8px 0 0" }}>{selected.length} of {stocks.length} selected</p>
        <div className="list">
          {visible.map((s) => {
            const on = selected.includes(s.symbol);
            return (
              <label key={s.symbol} className={`item${on ? " on" : ""}`}>
                <input type="checkbox" checked={on} onChange={() => toggle(s.symbol)} />
                <span>
                  <span className="sym">{s.symbol}</span> {s.company}
                  <br />
                  <span className="ind">{s.industry}</span>
                </span>
              </label>
            );
          })}
          {visible.length === 0 && <div className="item muted">No stocks match.</div>}
        </div>
      </section>

      <section className="card">
        <h2>2. Your mobile number</h2>
        <input
          type="tel"
          inputMode="tel"
          autoComplete="tel"
          placeholder={`${defaultCountryCode} 98765 43210`}
          value={mobile}
          onChange={(e) => setMobile(e.target.value)}
          aria-label="Mobile number"
        />
        {mobile && (
          <p className="muted" style={{ margin: "6px 0 0" }}>
            {normalized ? `Will use ${normalized}` : "That does not look like a valid number."}
          </p>
        )}
        <label className="check">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          <span>
            I agree to receive alerts on Telegram/WhatsApp on this number. Informational only, not investment advice.
          </span>
        </label>
        {error && <p className="error" role="alert">{error}</p>}
        <button className="primary" onClick={submit} disabled={busy}>Start alerts</button>
      </section>

      <p className="muted">
        Already subscribed? <button className="link" onClick={() => reset("stop")}>Stop alerts</button>
      </p>
    </>
  );
}
