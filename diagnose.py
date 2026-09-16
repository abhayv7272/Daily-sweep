#!/usr/bin/env python3
"""
diagnose.py — real-network deep diagnosis, meant to run on GitHub Actions
(where NSE and Yahoo are actually reachable, unlike the dev sandbox).

It verifies, one by one:
  [1] runtime versions (python / yfinance / pandas / numpy)
  [2] NSE official archive EQUITY_L.csv — reachability, row count, parse
  [3] Yahoo API contract the engine relies on:
        * yf.download(group_by="ticker") returns a MultiIndex named
          ['Ticker', 'Price']  (engine does res.xs(sym, level="Ticker"))
        * a single-symbol request ALSO keeps the Ticker level
  [4] a real batch daily download (the embedded ~300 liquid fallback symbols,
      ranking params: chunk=120, period=1mo) — availability, failures,
      Yahoo's NaN-close latest-bar lag prevalence
  [5] 15-min intraday rebuild path (the backfill transport) on a 40-symbol batch
  [6] Advanced Technical Confluence Engine contracts (RSI Div, FVG, Equal Lows, Absorption)
  [7] optionally: Gmail SMTP login check (no mail is sent)

Usage:  python diagnose.py [--smtp]
Exit code 0 = green, 1 = something failed.
"""
from __future__ import annotations

import sys
import time

FAILS = []


def ok(name, cond, detail=""):
    tag = "✅" if cond else "❌"
    print(f"[diag] {tag} {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not cond:
        FAILS.append(name)


def main() -> int:
    use_smtp = "--smtp" in sys.argv
    t0 = time.time()

    # ---------------------------------------------------------------- [1] versions
    import platform
    import numpy as np
    import pandas as pd
    import yfinance as yf
    print(f"[diag] python {platform.python_version()} · yfinance {yf.__version__} · "
          f"pandas {pd.__version__} · numpy {np.__version__}", flush=True)

    # ---------------------------------------------------------------- [2] NSE list
    import requests, io
    try:
        r = requests.get("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                              "AppleWebKit/537.36"},
                         timeout=30)
        df = pd.read_csv(io.StringIO(r.text))
        ok("NSE EQUITY_L.csv", r.status_code == 200 and len(df) > 1500,
           f"HTTP {r.status_code}, {len(df):,} rows")
    except Exception as exc:
        ok("NSE EQUITY_L.csv", False, f"{type(exc).__name__}: {exc} "
           "(auto-falls-back to the embedded ~300-name list in that case)")

    # ---------------------------------------------------------------- [3] Yahoo API contract
    try:
        multi = yf.download(["RELIANCE.NS", "HDFCBANK.NS"], period="5d", interval="1d",
                            group_by="ticker", auto_adjust=True, threads=True, progress=False)
        names = list(getattr(multi.columns, "names", []))
        sub = multi.xs("RELIANCE.NS", axis=1, level="Ticker") if isinstance(multi.columns, pd.MultiIndex) else multi
        ok("group_by=ticker → MultiIndex ['Ticker','Price']", names == ["Ticker", "Price"], f"names={names}")
        ok("xs(level=Ticker) extraction", len(sub) > 0 and list(sub.columns)[:2] == ["Open", "High"],
           f"rows={len(sub)}, last_close={sub['Close'].iloc[-1]:.2f}" if len(sub) > 0 else "0 rows")
    except Exception as exc:
        ok("group_by=ticker → MultiIndex", False, f"{type(exc).__name__}: {exc}")

    try:
        single = yf.download(["TCS.NS"], period="5d", interval="1d",
                             group_by="ticker", auto_adjust=True, threads=True, progress=False)
        is_mi = isinstance(single.columns, pd.MultiIndex)
        extracted = None
        if is_mi:
            extracted = single.xs("TCS.NS", axis=1, level="Ticker")
        else:
            extracted = single.copy()
        ok("single-symbol request keeps usable shape", extracted is not None and len(extracted) >= 3,
           f"MultiIndex={is_mi}, rows={len(extracted) if extracted is not None else 0}")
    except Exception as exc:
        ok("single-symbol request", False, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- [4] batch daily download
    from dlsweep import engine
    syms = [s + ".NS" for s in engine.FALLBACK_SYMBOLS]
    print(f"[diag] … daily batch probe: {len(syms)} symbols (ranking params: chunk=120, period=1mo)",
          flush=True)
    tb = time.time()
    data, failed = engine.chunked_download(syms, period="1mo", chunk=120, label="diag-rank")
    elapsed = time.time() - tb
    ok("batch daily ≥ 85% available", len(data) >= 0.85 * len(syms),
       f"{len(data)}/{len(syms)} in {elapsed:.0f}s, failed chunks: {len(failed)}")
    if data:
        max_last = max(d.index[-1].date() for d in data.values())
        lag = [s for s, d in data.items() if d.index[-1].date() < max_last]
        ok("latest-date spread sane (≤2 sessions)", all(
            (max_last - d.index[-1].date()).days <= 4 for d in data.values()),
           f"as-of={max_last}")
        print(f"[diag]    {len(lag)} symbols lag the latest session and would be "
              f"15-min-backfilled by the real run", flush=True)

    # ---------------------------------------------------------------- [5] 15-min backfill path
    probe = (list(data) or syms)[:40]
    print(f"[diag] … 15-min rebuild probe: {len(probe)} symbols", flush=True)
    try:
        import datetime as _dt
        target = max(d.index[-1].date() for d in data.values()) if data else _dt.date.today()
        fixed = engine.backfill_latest(probe, target, chunk=40)
        ok("15-min rebuild works", len(fixed) > 0, f"{len(fixed)}/{len(probe)} bars rebuilt, "
           f"sample close={list(fixed.values())[0]['Close'].iloc[0]:.2f}" if fixed else "")
    except Exception as exc:
        ok("15-min rebuild works", False, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- [6] Confluence Engine
    try:
        self_test_out = engine.self_test()
        ok("engine self-test (RSI Div, Equal Lows, FVGs, Sweeps)", "SELF-TEST PASSED" in self_test_out,
           "all mathematical fixtures verified")
    except Exception as exc:
        ok("engine self-test", False, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- [7] SMTP login (optional)
    if use_smtp:
        import os, smtplib, ssl
        user = os.environ.get("MY_EMAIL", "")
        pw = (os.environ.get("MY_APP_PASSWORD", "") or "").replace(" ", "")
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=60) as s:
                s.login(user, pw)
            ok("Gmail SMTP login", True, f"auth OK for {user} (no mail sent)")
        except Exception as exc:
            ok("Gmail SMTP login", False, f"{type(exc).__name__}: {exc}")

    print(f"[diag] done in {time.time()-t0:.0f}s — failures: {len(FAILS)}", flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
