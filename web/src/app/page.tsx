import Subscribe from "./subscribe";
import { STOCKS } from "@/lib/stocks";

export default function Home() {
  return (
    <main className="wrap">
      <h1>Nifty 50 Alerts</h1>
      <p className="lead">
        Pick the stocks you follow and get a short AI summary of every NSE announcement and corporate action, the
        moment it is listed.
      </p>
      <Subscribe stocks={STOCKS} defaultCountryCode={process.env.NEXT_PUBLIC_DEFAULT_COUNTRY_CODE || "+91"} />
      <p className="muted">Telegram alerts are live. WhatsApp alerts will be added later.</p>
    </main>
  );
}
