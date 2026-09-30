#!/usr/bin/env python3
"""
NSE / BSE feasibility test. Run it on the EC2 instance (and again through a proxy).

It answers three questions:
  1. Does NSE answer requests from this server's IP address?
  2. How fast does it answer, and does it keep answering at a 15-second polling rhythm?
  3. Does the BSE announcements feed work as a backup?

It is a low-volume test: a few requests per source, spaced out. It stores nothing.

Usage:
  pip install requests
  python3 nse_feasibility_test.py                 # direct from this server
  PROXY_URL="http://user:pass@host:port" python3 nse_feasibility_test.py   # through a proxy
  python3 nse_feasibility_test.py --polls 8 --interval 15

Note: these are the same unofficial web endpoints the exchange websites use.
They are not a supported API and can change or be blocked at any time.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

NSE_HOME = "https://www.nseindia.com/"
NSE_ANN = "https://www.nseindia.com/api/corporate-announcements?index=equities"
NSE_ACTIONS = "https://www.nseindia.com/api/corporates-corporateActions?index=equities"

BSE_HOME = "https://www.bseindia.com/"
BSE_ANN = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    "?pageno=1&strCat=-1&strPrevDate={d}&strScrip=&strSearch=P&strToDate={d}&strType=C"
)

IST = timezone(timedelta(hours=5, minutes=30))


def make_session(referer, proxy):
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
            "Origin": referer.rstrip("/"),
        }
    )
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def classify(resp):
    """Turn a response into a plain verdict."""
    body = resp.text[:300].lower()
    if resp.status_code == 200:
        return "OK"
    if resp.status_code in (401, 403) or "access denied" in body:
        return "BLOCKED (bot protection or IP denied)"
    if resp.status_code == 429:
        return "RATE LIMITED"
    return f"ERROR {resp.status_code}"


def timed_get(session, url, timeout=10):
    t0 = time.time()
    try:
        r = session.get(url, timeout=timeout)
        return r, time.time() - t0, None
    except requests.RequestException as e:
        return None, time.time() - t0, e


def egress_ip(proxy):
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        return requests.get("https://api.ipify.org", timeout=8, proxies=proxies).text
    except requests.RequestException as e:
        return f"unknown ({e.__class__.__name__})"


def record_count(resp):
    try:
        data = resp.json()
    except ValueError:
        return None, "response was not JSON"
    if isinstance(data, list):
        return len(data), data[0] if data else None
    if isinstance(data, dict):
        for key in ("Table", "data", "Data"):
            if isinstance(data.get(key), list):
                rows = data[key]
                return len(rows), rows[0] if rows else None
    return None, "unrecognised JSON shape"


TIME_KEYS = ("an_dt", "sort_date", "exchdisstime", "NEWS_DT", "DissemDT", "DT_TM")
TIME_FORMATS = (
    "%d-%b-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%d-%b-%Y",
)


def freshness(record):
    """Show the newest record's own timestamp and how old it is (IST).

    A fast 200 response can still be a cached copy. If the newest record keeps
    lagging far behind the clock in market hours, the feed is too stale for a
    60-second alert rule.
    """
    for key in TIME_KEYS:
        raw = record.get(key)
        if not raw:
            continue
        for fmt in TIME_FORMATS:
            try:
                ts = datetime.strptime(str(raw)[:26], fmt).replace(tzinfo=IST)
            except ValueError:
                continue
            age = (datetime.now(IST) - ts).total_seconds()
            return f", newest item time: {raw} ({age / 60:.1f} min ago)"
        return f", newest item time: {raw} (format not parsed)"
    return ", newest item time: not found in record"


def test_source(name, home, urls, polls, interval, proxy):
    print(f"\n=== {name} ===")
    session = make_session(home, proxy)

    r, dt, err = timed_get(session, home)
    if err:
        print(f"warm-up (home page): FAILED - {err.__class__.__name__}: {err}")
        return {"name": name, "ok": 0, "total": 0, "latencies": []}
    print(f"warm-up (home page): HTTP {r.status_code} in {dt:.2f}s -> {classify(r)}; cookies: {len(session.cookies)}")

    ok = total = 0
    latencies = []
    for i in range(1, polls + 1):
        for label, url in urls:
            total += 1
            r, dt, err = timed_get(session, url)
            if err:
                print(f"poll {i} [{label}]: FAILED - {err.__class__.__name__}")
                continue
            verdict = classify(r)
            extra = ""
            if r.status_code == 200:
                n, first = record_count(r)
                extra = f", records: {n}"
                if isinstance(first, dict):
                    sym = first.get("symbol") or first.get("SLONGNAME") or first.get("SCRIP_CD") or ""
                    extra += f", newest: {sym}"
                    extra += freshness(first)
            print(f"poll {i} [{label}]: HTTP {r.status_code} in {dt:.2f}s -> {verdict}{extra}")
            if verdict == "OK":
                ok += 1
                latencies.append(dt)
        if i < polls:
            time.sleep(interval)
    return {"name": name, "ok": ok, "total": total, "latencies": latencies}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--polls", type=int, default=4, help="polls per source (default 4)")
    ap.add_argument("--interval", type=int, default=15, help="seconds between polls (default 15)")
    args = ap.parse_args()

    proxy = os.environ.get("PROXY_URL")
    print(f"Time (IST): {datetime.now(IST):%Y-%m-%d %H:%M:%S}")
    print(f"Outgoing IP: {egress_ip(proxy)}  (proxy: {'yes' if proxy else 'no'})")

    today = datetime.now(IST).strftime("%Y%m%d")
    results = [
        test_source(
            "NSE", NSE_HOME,
            [("announcements", NSE_ANN), ("corporate actions", NSE_ACTIONS)],
            args.polls, args.interval, proxy,
        ),
        test_source(
            "BSE (backup)", BSE_HOME,
            [("announcements", BSE_ANN.format(d=today))],
            args.polls, args.interval, proxy,
        ),
    ]

    print("\n=== Summary ===")
    for r in results:
        if r["total"] == 0:
            print(f"{r['name']}: could not connect")
            continue
        lat = r["latencies"]
        avg = f"{sum(lat) / len(lat):.2f}s avg, {max(lat):.2f}s max" if lat else "n/a"
        status = "USABLE" if r["ok"] == r["total"] else ("PARTIAL" if r["ok"] else "NOT USABLE")
        print(f"{r['name']}: {r['ok']}/{r['total']} requests OK ({avg}) -> {status}")
    print("\nRun this during market hours (9:15-15:30 IST) and again outside them;")
    print("a pass from one address or time is no guarantee for the next.")


if __name__ == "__main__":
    sys.exit(main())
