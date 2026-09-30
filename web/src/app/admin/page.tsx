import { cookies } from "next/headers";
import { ADMIN_COOKIE, checkAdmin } from "@/lib/admin-auth";
import { getSummary, type Summary } from "@/lib/admin-data";

export const dynamic = "force-dynamic";
export const metadata = { title: "Admin - Nifty 50 Alerts", robots: { index: false, follow: false } };

const time = (v: unknown) => (v ? new Date(v as string).toISOString().replace("T", " ").slice(0, 19) : "-");
const secs = (ms: unknown) => (ms === null || ms === undefined ? "-" : (Number(ms) / 1000).toFixed(1) + "s");

function Login({ error, disabled }: { error: boolean; disabled: boolean }) {
  return (
    <main className="wrap">
      <h1>Admin</h1>
      {disabled ? (
        <p className="lead">Admin is disabled. Set ADMIN_TOKEN to enable it.</p>
      ) : (
        <form className="card" method="post" action="/api/admin/login">
          <label htmlFor="token">Admin token</label>
          <input id="token" name="token" type="password" autoComplete="off" required />
          {error && <p className="error">Invalid token.</p>}
          <p><button className="primary" type="submit">Sign in</button></p>
        </form>
      )}
    </main>
  );
}

function Dashboard({ s }: { s: Summary }) {
  const age = s.heartbeat?.ageSeconds ?? null;
  const stale = age === null || age > 60;
  return (
    <main className="wrap wide">
      <h1>Admin</h1>
      <p className="lead">
        Worker heartbeat:{" "}
        <span className={stale ? "bad" : "good"}>
          {age === null ? "never" : `${Math.round(age)}s ago`}
        </span>
      </p>

      <div className="grid">
        <section className="card">
          <h2>Subscribers</h2>
          <table>
            <tbody>
              {s.subscribers.map((r) => (
                <tr key={r.status}><td>{r.status}</td><td>{r.count}</td></tr>
              ))}
              {s.subscribers.length === 0 && <tr><td className="muted">None yet</td></tr>}
            </tbody>
          </table>
        </section>
        <section className="card">
          <h2>Top stocks by subscriptions</h2>
          <table>
            <tbody>
              {s.topStocks.map((r) => (
                <tr key={r.symbol}><td>{r.symbol}</td><td>{r.count}</td></tr>
              ))}
              {s.topStocks.length === 0 && <tr><td className="muted">None yet</td></tr>}
            </tbody>
          </table>
        </section>
        <section className="card">
          <h2>SLA, last 24h</h2>
          <p>
            Sent: {s.sla24h.sent} &middot; Within SLA:{" "}
            {s.sla24h.withinPct === null ? "-" : `${s.sla24h.withinPct}% (${s.sla24h.withinSla})`}
          </p>
          <p className="muted">Breaches: {s.sla24h.breaches.length}</p>
        </section>
        <section className="card">
          <h2>Delivery status</h2>
          <table>
            <tbody>
              {s.deliveryStatus.map((r) => (
                <tr key={r.channel + r.status}><td>{r.channel}</td><td>{r.status}</td><td>{r.count}</td></tr>
              ))}
              {s.deliveryStatus.length === 0 && <tr><td className="muted">None yet</td></tr>}
            </tbody>
          </table>
        </section>
      </div>

      {s.sla24h.breaches.length > 0 && (
        <section className="card scroll">
          <h2>SLA breaches, last 24h</h2>
          <table>
            <thead><tr><th>Sent</th><th>Channel</th><th>Symbol</th><th>Subject</th><th>Since listed</th><th>Since detected</th></tr></thead>
            <tbody>
              {s.sla24h.breaches.map((r) => (
                <tr key={String(r.id)}>
                  <td>{time(r.sent_at)}</td><td>{String(r.channel)}</td><td>{String(r.symbol)}</td>
                  <td>{String(r.subject ?? "")}</td><td>{secs(r.since_listed_ms)}</td><td>{secs(r.since_detected_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="card scroll">
        <h2>Latest events</h2>
        <table>
          <thead><tr><th>Detected</th><th>Symbol</th><th>Category</th><th>Subject</th><th>Status</th><th>Summary</th></tr></thead>
          <tbody>
            {s.events.map((r) => (
              <tr key={String(r.id)}>
                <td>{time(r.detected_at)}</td><td>{String(r.symbol)}</td><td>{String(r.category)}</td>
                <td>{String(r.subject ?? "")}</td><td>{String(r.status)}</td><td>{String(r.summary ?? "")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card scroll">
        <h2>Latest deliveries</h2>
        <table>
          <thead><tr><th>Queued</th><th>Channel</th><th>Status</th><th>Since listed</th><th>Since detected</th><th>Within SLA</th></tr></thead>
          <tbody>
            {s.deliveries.map((r) => (
              <tr key={String(r.id)}>
                <td>{time(r.queued_at)}</td><td>{String(r.channel)}</td><td>{String(r.status)}</td>
                <td>{secs(r.since_listed_ms)}</td><td>{secs(r.since_detected_ms)}</td>
                <td>{r.within_sla === null ? "-" : r.within_sla ? "yes" : <span className="bad">no</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card scroll">
        <h2>Corporate actions</h2>
        <table>
          <thead><tr><th>Detected</th><th>Symbol</th><th>Subject</th><th>Ex date</th><th>Record date</th></tr></thead>
          <tbody>
            {s.corporateActions.map((r) => (
              <tr key={String(r.id)}>
                <td>{time(r.detected_at)}</td><td>{String(r.symbol)}</td><td>{String(r.subject)}</td>
                <td>{String(r.ex_date)}</td><td>{String(r.record_date)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}

export default async function AdminPage({ searchParams }: { searchParams: Promise<{ error?: string }> }) {
  const jar = await cookies();
  const auth = checkAdmin(jar.get(ADMIN_COOKIE)?.value);
  if (auth === "disabled") return <Login error={false} disabled />;
  if (auth !== "ok") return <Login error={Boolean((await searchParams).error)} disabled={false} />;

  try {
    return <Dashboard s={await getSummary()} />;
  } catch {
    return (
      <main className="wrap">
        <h1>Admin</h1>
        <p className="error">Could not load data. Check DATABASE_URL and that the schema is applied.</p>
      </main>
    );
  }
}
