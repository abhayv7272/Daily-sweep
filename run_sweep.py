#!/usr/bin/env python3
"""
run_sweep.py — headless runner for the NSE DAILY Liquidity-Sweep Screener.

It executes EXACTLY the notebook's pipeline, in the notebook's cell order, with
config.py's CONFIG (= the notebook's SweepConfig defaults):

    [1/7] engine self-test          <- notebook cell 12 (proves the logic)
    [2/7] configuration banner      <- notebook cell 3
    [3/7] NSE universe + ranking    <- notebook cells 5 + 8
    [4/7] history + backfill        <- notebook cell 9 (incl. 15-min lag repair)
    [5/7] screen + results table    <- notebook cells 11 + 14 (the Colab table)
    [6/7] charts + HTML report      <- notebook cell 15 plot_setup, every setup
    [7/7] email the report          <- Gmail SMTP, inline + attachments

No screening logic lives in this file; it only orchestrates and reports, so the
emailed result cannot drift from the notebook's result.

Usage:
    python run_sweep.py                  # full run + email
    python run_sweep.py --no-email       # full run, write out/ only
    python run_sweep.py --limit 60       # smoke test on the first 60 NSE symbols
    python run_sweep.py --demo           # offline demo report from synthetic data
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
import traceback
import warnings

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "out")
sys.path.insert(0, ROOT)
os.makedirs(OUT_DIR, exist_ok=True)


def now_ist() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _queue_delay_note(now: dt.datetime) -> str:
    """Honesty note for the report header: GitHub's cron is a best-effort queue,
    not a timer — on busy days it fires hours late (14-15 Sept 2026: 4-5h).
    EXPECTED_SEND_IST (e.g. "19:30", set only for scheduled runs) is compared
    with the actual time; if the run is >25 min late we say so in the email, so
    a late email never again looks like a silent bug."""
    expected = os.environ.get("EXPECTED_SEND_IST", "").strip()
    if not expected:
        return ""
    try:
        hh, mm = (int(x) for x in expected.split(":")[:2])
    except ValueError:
        return ""
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    late_min = (now - target).total_seconds() / 60.0
    if late_min < 0:
        # GitHub's queue only ever runs LATE, never hours early — so a big
        # negative gap means the previous day's slot arrived after midnight.
        if late_min > -360:  # firing a few minutes/hours before the slot: on time
            return ""
        late_min += 1440
    if late_min <= 25:
        return ""
    return (f" · run queued {int(late_min // 60)}h {round(late_min % 60)}m late by GitHub "
            f"(planned ~{expected} IST)")


# -------------------------------------------------------------------------- demo market
def _demo_data():
    """Deterministic synthetic market (offline): 2 real sweeps, 1 rejected sweep, drift."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(42)
    idx = pd.bdate_range(end="2026-09-14", periods=90)

    def drift_df(base, vol, tail):
        rows = []
        px = base
        for _ in range(90 - len(tail)):
            o = px
            c = px * (1 + rng.normal(0, vol))
            h = max(o, c) * (1 + abs(rng.normal(0, vol / 2)))
            l = min(o, c) * (1 - abs(rng.normal(0, vol / 2)))
            rows.append((o, h, l, c, int(rng.integers(8, 15) * 100_000)))
            px = c
        rows += tail
        return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close", "Volume"])

    sweep_tail = [(104.2, 104.6, 103.4, 103.6, 1_000_000),   # decline into the swing low
                  (103.6, 103.9, 102.1, 102.4, 1_000_000),
                  (102.4, 102.6, 100.9, 101.2, 1_000_000),
                  (101.2, 101.5, 100.0, 100.6, 1_200_000),   # swing low 100.00
                  (100.6, 101.4, 100.5, 101.1, 1_100_000),
                  (101.1, 101.8, 100.9, 101.6, 1_000_000),
                  (101.6, 102.4, 101.4, 102.1, 1_000_000),
                  (102.1, 102.9, 101.9, 102.6, 1_100_000),
                  (102.6, 103.1, 102.2, 102.8, 1_000_000),
                  (101.8, 103.0, 99.35, 102.6, 1_800_000)]   # THE SWEEP of 100.00 (RSI Div + Absorption)

    # SWEEPDEMO2: Swept Equal Lows (Double Bottom) + Bullish FVG mitigation
    sweep_tail2 = [(205.0, 205.4, 203.2, 203.5, 900_000),
                   (203.5, 203.8, 200.0, 200.4, 1_100_000),  # Low #1 = 200.00 (Double bottom touch 1)
                   (200.4, 202.5, 200.3, 202.0, 1_000_000),
                   (202.0, 203.5, 201.8, 203.0, 1_000_000),
                   (203.0, 203.8, 202.0, 202.5, 1_000_000),
                   (202.5, 203.0, 200.05, 200.8, 1_100_000), # Low #2 = 200.05 (Double bottom touch 2)
                   (200.8, 203.0, 200.6, 202.4, 1_000_000),
                   (202.4, 203.5, 201.9, 202.8, 1_000_000),
                   (202.8, 203.2, 202.0, 202.6, 1_000_000),
                   (201.7, 204.1, 199.20, 203.4, 2_200_000)] # THE SWEEP of 200.00 Equal Lows with high volume!

    reject_tail = [(104.2, 104.6, 103.4, 103.6, 1_000_000),
                   (103.6, 103.9, 102.1, 102.4, 1_000_000),
                   (102.4, 102.6, 100.9, 101.2, 1_000_000),
                   (101.2, 101.5, 100.0, 100.6, 1_200_000),
                   (100.6, 101.4, 100.5, 101.1, 1_100_000),
                   (101.1, 101.8, 100.9, 101.6, 1_000_000),
                   (101.6, 102.4, 101.4, 102.1, 1_000_000),
                   (102.1, 102.9, 101.9, 102.6, 1_100_000),
                   (102.6, 103.1, 102.2, 102.8, 1_000_000),
                   (101.8, 103.0, 99.35, 99.8, 1_800_000)]   # pierce but close BELOW — rejected

    data = {
        "SWEEPDEMO1.NS": drift_df(105, 0.006, sweep_tail),
        "SWEEPDEMO2.NS": drift_df(210, 0.005, sweep_tail2),
        "REJECTED.NS": drift_df(105, 0.006, reject_tail),
        "FLATLINE.NS": drift_df(87, 0.004, []),
        "VOLATILE.NS": drift_df(153, 0.011, []),
    }
    universe = pd.DataFrame({
        "Yahoo": list(data),
        "SYMBOL": [s.replace(".NS", "") for s in data],
        "NAME OF COMPANY": ["Sweep Demo One Ltd (synthetic)", "Sweep Demo Two Ltd (synthetic)",
                            "Rejected Demo Ltd (synthetic)", "Flatline Demo Ltd (synthetic)",
                            "Volatile Demo Ltd (synthetic)"],
        "Avg_Turnover_Cr": [12.0, 9.0, 7.0, 5.0, 4.0],
    })
    return data, universe


# -------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-email", action="store_true", help="write out/ but do not send")
    ap.add_argument("--limit", type=int, default=0,
                    help="rank/screen only the first N symbols of the list (smoke test)")
    ap.add_argument("--skip-selftest", action="store_true")
    ap.add_argument("--demo", action="store_true",
                    help="build the report from deterministic synthetic data (offline QA)")
    args = ap.parse_args()

    import pandas as pd
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 240)

    from dlsweep import engine
    from config import CONFIG, REPORT

    cfg = engine.SweepConfig(**{k: v for k, v in CONFIG.items()
                                if k in engine.SweepConfig.__dataclass_fields__})
    engine.CFG = cfg  # module-level CFG is what the verbatim functions read

    import report as RP

    t_start = time.time()
    started = now_ist()
    stamp = started.strftime("%Y-%m-%d %H:%M")
    delay_note = _queue_delay_note(started)
    if delay_note:
        log(f"NOTE: {delay_note.strip(' ·')}")
    log("=" * 78)
    log("NSE DAILY LIQUIDITY-SWEEP SCREENER — Swing-Buy Setups (automated run)")
    log(f"started {now_ist():%Y-%m-%d %H:%M:%S} IST" + ("  [DEMO MODE]" if args.demo else ""))
    log("=" * 78)

    # ------------------------------------------------------------ [1/7] self-test
    if not args.skip_selftest:
        log("\n[1/7] engine self-test (synthetic candles — notebook cell 12)")
        try:
            log(engine.self_test(cfg))
        except AssertionError as exc:
            log(f"      ! SELF-TEST FAILED: {exc} — aborting, the engine is not trustworthy")
            return 2
    else:
        log("\n[1/7] self-test skipped")

    # ------------------------------------------------------------ [2/7] config banner
    rules = engine.describe_config(cfg)
    log("\n[2/7] configuration\n" + rules)

    charts = {}
    res = show = None
    health = ""
    stats = {"universe_size": cfg.universe_size}
    if delay_note:
        stats["queue_delay_note"] = delay_note

    if args.demo:
        # ---------------------------------------------- offline demo path
        log("\n[3/7] universe & ranking   — demo market (synthetic, offline)")
        DATA, UNIVERSE = _demo_data()
        health = ("── DATA HEALTH REPORT (demo) ────────────────────────────\n"
                  f"  Market data as-of date   : {max(df.index[-1].date() for df in DATA.values())}\n"
                  f"  Screenable tickers       : {len(DATA):,} / {len(DATA):,}\n"
                  "  Dropped — short history  : 0\n"
                  "  Dropped — no candle of latest session (Yahoo lag/suspended): 0\n"
                  "  OHLC anomalies repaired  : 0\n"
                  "──────────────────────────────────────────────────────────")
        stats.update({"asof": str(max(df.index[-1].date() for df in DATA.values())),
                      "screened": len(DATA), "universe": len(DATA),
                      "list_source": "synthetic demo", "rank_fetched": len(DATA),
                      "hist_fetched": len(DATA), "backfilled": 0, "repaired": 0,
                      "fetch_secs": 0.0})
    else:
        # ---------------------------------------------- real pipeline (verbatim cells)
        log("\n[3/7] NSE universe + turnover ranking (cells 5 + 8)")
        UNIVERSE_ALL = engine.fetch_nse_universe()
        list_source = UNIVERSE_ALL["source"].iloc[0]
        if args.limit:
            UNIVERSE_ALL = UNIVERSE_ALL.head(args.limit).copy()
            log(f"      --limit {args.limit}: using the first {len(UNIVERSE_ALL)} symbols only")
        UNIVERSE, rstats = engine.rank_universe(UNIVERSE_ALL)
        stats.update(rstats)
        stats["list_source"] = list_source

        log("\n[4/7] full history + latest-session backfill + health (cell 9)")
        DATA, health, hstats = engine.fetch_history(UNIVERSE)
        stats.update(hstats)

    stats["universe"] = len(UNIVERSE)
    stats["screened"] = len(DATA)

    # ------------------------------------------------------------ [5/7] screen
    log("\n[5/7] screening (cells 11 + 14)")
    t0 = time.time()
    RESULTS = engine.screen_all(DATA, cfg)
    stats["screen_secs"] = time.time() - t0
    log(f"Screened {len(DATA):,} symbols in {stats['screen_secs']:.1f}s")

    if RESULTS.empty:
        log("\n⚠ No liquidity-sweep setups on the latest daily candle.")
        log("\n  Try:  (a) run after today's 15:35 IST close (confirmed candle)")
        log("         (b) loosen rules in config.py, e.g.")
        log("              min_wick_ratio   = 0.25")
        log("              max_sweep_depth  = 0.03")
        log("              require_prior_above = False")
        log("              level_lookback   = 150")
        res = show = None
    else:
        res, show = engine.format_results(RESULTS, UNIVERSE)
        log(f"\n✅  {len(res)} SWING-BUY SWEEP SETUPS  —  candle of {res['Date'].iloc[0]}  (by score)\n")
        log(show.to_string(index=False))

    # ------------------------------------------------------------ [6/7] exports + charts + report
    log("\n[6/7] exports, charts & HTML report")
    day = stats.get("asof") or now_ist().strftime("%Y-%m-%d")
    report_path = os.path.join(OUT_DIR, f"daily_sweep_report_{day}.html")
    csv_path = os.path.join(OUT_DIR, f"daily_sweep_results_{day}.csv")

    if res is not None:
        res.to_csv(csv_path, index=False)
        log(f"      results csv      {os.path.basename(csv_path)}  ({os.path.getsize(csv_path):,} B)")

        # cell 15: detailed text report for the top picks (verbatim lines)
        log("")
        for _, r in res.head(3).iterrows():
            for line in engine.setup_report_lines(r):
                log(line)
            log("")

    stats["fetch_secs"] = (stats.get("rank_secs", 0.0) + stats.get("hist_secs", 0.0))

    if res is not None:
        charts = RP.chart_pngs(DATA, res, last_bars=REPORT["chart_last_bars"],
                               max_charts=REPORT["max_charts"])
        log(f"      charts rendered  : {len(charts)}")
        chart_dir = os.path.join(OUT_DIR, "charts")
        os.makedirs(chart_dir, exist_ok=True)
        for sym, png in charts.items():
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sym)
            with open(os.path.join(chart_dir, f"chart_{safe}.png"), "wb") as f:
                f.write(png)

    html_doc = RP.build_html(res, show, charts, stamp, rules, health, stats,
                             table_rows=REPORT["table_rows"])
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    log(f"      report           {os.path.basename(report_path)}  ({os.path.getsize(report_path):,} B)")

    # ------------------------------------------------------------ [7/7] email
    log("\n[7/7] email")
    if args.no_email:
        log("      --no-email: skipped")
    else:
        import emailer
        atts = [(os.path.basename(report_path), html_doc.encode("utf-8"), "html")]
        if res is not None and os.path.exists(csv_path) and os.path.getsize(csv_path) < 12_000_000:
            atts.append((os.path.basename(csv_path), open(csv_path, "rb").read(), "csv"))
        for sym, png in charts.items():
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sym)
            atts.append((f"chart_{safe}.png", png, "png"))

        n = 0 if res is None else len(res)
        subject = (f"NSE Daily Sweep · {n} setup{'s' if n != 1 else ''} · {day}") if n \
            else f"NSE Daily Sweep · no setups · {day}"
        emailer.send_report(subject, html_doc, atts,
                            sender_name="Daily Sweep",
                            clip_marker=RP.CHART_CARD_MARKER)

    log(f"\ndone in {time.time()-t_start:.0f}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
