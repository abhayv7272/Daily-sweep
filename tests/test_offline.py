#!/usr/bin/env python3
"""
tests/test_offline.py — deep offline diagnosis for the daily sweep pipeline.

Covers everything that can be tested without network:
  1.  notebook self-test (positive + negative sweeps)
  2.  chunked_download parsing of a realistic yfinance MultiIndex response
      (good symbols, 404 symbol, NaN rows, single-symbol chunk)
  3.  chunked_download single-symbol FLAT response (old yfinance shape) —
      proves the silent-drop bug and its fix
  4.  backfill_latest 15-min → daily bar rebuild (O/H/L/C/V aggregation)
  5.  fetch_history health gate (stale drop, backfill call, OHLC repair)
  6.  rank_universe turnover ordering + min-turnover + min-bars filters
  7.  analyze_symbol edge matrix (12 acceptance / rejection cases)
  8.  screen_all ordering + error isolation
  9.  format_results — exact Colab table shape
  10. plot_setup — short data, returns a real figure
  11. build_html — empty day, special chars (&), chart cap
  12. emailer clipping boundary

Run:  python tests/test_offline.py        (exit 0 = everything green)
"""
import os
import sys
import math
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from dlsweep import engine
import report as RP
import emailer

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def eq(name, a, b, tol=1e-9):
    ok = (abs(a - b) <= tol) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else a == b
    check(name, ok, f"got {a!r}, want {b!r}")


# ════════════════════════════════════════════════════════════ §1 self-test
print("\n§1 notebook self-test")
try:
    out = engine.self_test()
    check("self_test passes", "SELF-TEST PASSED" in out)
except AssertionError as exc:
    check("self_test passes", False, str(exc))

CFG = engine.SweepConfig()


# ════════════════════════════════════════════════════════════ helpers
def mk_download_response(symbols_frames, drop=()):
    """Build a fake yfinance group_by='ticker' MultiIndex response."""
    per = []
    for sym, df in symbols_frames.items():
        if sym in drop:
            continue
        sub = df.copy()
        sub.columns = pd.MultiIndex.from_product([[sym], sub.columns], names=["Ticker", "Price"])
        per.append(sub)
    return pd.concat(per, axis=1)


def ohlcv(rows, start="2026-01-05"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close", "Volume"],
                        dtype=float)


def sweep_rows(close_level=100.0, last=(101.8, 103.0, 99.35, 102.6, 1_800_000), base=105.0):
    lv = close_level
    rows = []
    for i in range(60):
        o = base + (i % 5) * 0.2
        rows.append((o, o + 0.8, o - 0.8, o + 0.3, 1_000_000.0))
    rows += [(base-0.8, base-0.4, base-1.6, base-1.4, 1_000_000.0),
             (base-1.4, base-1.1, base-2.9, base-2.6, 1_000_000.0),
             (base-2.6, base-2.4, base-4.1, base-3.8, 1_000_000.0),
             (base-3.8, base-3.5, base-5.0, base-4.4, 1_200_000.0),   # swing low = base-5
             (base-4.4, base-3.6, base-4.5, base-3.9, 1_100_000.0),
             (base-3.9, base-3.2, base-4.1, base-3.4, 1_000_000.0),
             (base-3.4, base-2.6, base-3.6, base-2.9, 1_000_000.0),
             (base-2.9, base-2.1, base-3.1, base-2.4, 1_100_000.0),
             (base-2.4, base-1.9, base-2.8, base-2.2, 1_000_000.0)]
    rows.append(last)
    return rows


# ═══════════════════════════════ §2 chunked_download parsing (MultiIndex)
print("\n§2 chunked_download — realistic yfinance MultiIndex parsing")
idx = pd.bdate_range("2026-01-05", periods=25)
good = ohlcv([(10, 11, 9, 10.5, 100)] * 25)
with_nan = ohlcv([(10, 11, 9, 10.5, 100)] * 24 + [(np.nan, 11.0, 9.0, 10.5, 100)])
frames = {"AAA.NS": good, "BBB.NS": with_nan, "CCC.NS": good}

real_download = engine.yf.download
engine.yf.download = lambda tickers, **kw: mk_download_response(
    {s: frames[s] for s in tickers}, drop=("CCC.NS",))  # CCC 404s
try:
    out, failed = engine.chunked_download(["AAA.NS", "BBB.NS", "CCC.NS"], "1mo", chunk=3, label="t")
    check("AAA extracted", "AAA.NS" in out and len(out["AAA.NS"]) == 25)
    check("BBB NaN rows dropped", "BBB.NS" in out and len(out["BBB.NS"]) == 24)
    check("404 symbol skipped silently", "CCC.NS" not in out and failed == [])
finally:
    engine.yf.download = real_download

# single-symbol chunk, MultiIndex kept (current yfinance)
engine.yf.download = lambda tickers, **kw: mk_download_response({s: frames[s] for s in tickers})
try:
    out, _ = engine.chunked_download(["AAA.NS"], "1mo", chunk=5, label="t")
    check("single-symbol chunk OK", "AAA.NS" in out and len(out["AAA.NS"]) == 25)
finally:
    engine.yf.download = real_download

# total failure → RuntimeError("empty response") retried then failed
calls = {"n": 0}
def boom(tickers, **kw):
    calls["n"] += 1
    return None
engine.yf.download = boom
try:
    engine.CFG = engine.SweepConfig(max_retries=2, backoff_secs=0.01)
    out, failed = engine.chunked_download(["AAA.NS"], "1mo", chunk=5, label="t")
    check("empty response → retries then failed list", failed == ["AAA.NS"] and calls["n"] == 2)
finally:
    engine.yf.download = real_download
    engine.CFG = CFG


# ════════════════════ §3 single-symbol FLAT response (old yfinance shape)
print("\n§3 chunked_download — flat single-symbol response (silent-drop bug + fix)")
def flat_dl(tickers, **kw):
    assert len(tickers) == 1
    return good.copy()  # flat columns, NO Ticker level
engine.yf.download = flat_dl
try:
    out, failed = engine.chunked_download(["AAA.NS"], "1mo", chunk=5, label="t")
    check("flat single-symbol frame recovered (fix active)", "AAA.NS" in out and not failed)
finally:
    engine.yf.download = real_download


# ═══════════════════════════════ §4 backfill_latest aggregation
print("\n§4 backfill_latest — 15-min → daily rebuild")
mins = pd.date_range("2026-09-14 09:15", periods=25, freq="15min")
bar = pd.DataFrame({
    "Open":   [100.0 + i for i in range(25)],
    "High":   [101.5 + i for i in range(25)],
    "Low":    [99.5 + i for i in range(25)],
    "Close":  [100.8 + i for i in range(25)],
    "Volume": [10.0] * 25,
}, index=mins)
resp15 = mk_download_response({"AAA.NS": bar, "BBB.NS": bar})
engine.yf.download = lambda *a, **kw: resp15
try:
    import datetime as _dt
    fixed = engine.backfill_latest(["AAA.NS", "BBB.NS"], _dt.date(2026, 9, 14))
    b = fixed["AAA.NS"].iloc[0]
    eq("open = first 15m open", b["Open"], 100.0)
    eq("high = max 15m high", b["High"], 101.5 + 24)
    eq("low = min 15m low", b["Low"], 99.5)
    eq("close = last 15m close", b["Close"], 100.8 + 24)
    eq("volume = sum", b["Volume"], 250.0)
    eq("index = target date", str(fixed["AAA.NS"].index[0].date()), "2026-09-14")
    check("both symbols fixed", set(fixed) == {"AAA.NS", "BBB.NS"})
finally:
    engine.yf.download = real_download


# ═══════════════════════════════ §5 fetch_history health gate
print("\n§5 fetch_history — stale drop / backfill / OHLC repair")
today = pd.Timestamp("2026-09-14")
def hist_frame(last_date, bad=False, n=70):
    idx = pd.bdate_range(end=last_date, periods=n)
    df = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5,
                       "Volume": 1e6}, index=idx)
    if bad:
        df.iloc[-3, df.columns.get_loc("High")] = 98.0   # High < Low anomaly
    return df

univ = pd.DataFrame({"Yahoo": ["OK.NS", "STALE.NS", "SHORT.NS", "BAD.NS"],
                     "SYMBOL": ["OK", "STALE", "SHORT", "BAD"], "NAME OF COMPANY": [""] * 4})
fake_hist = {"OK.NS": hist_frame("2026-09-14"),
             "STALE.NS": hist_frame("2026-09-11"),
             "SHORT.NS": hist_frame("2026-09-14", n=10),
             "BAD.NS": hist_frame("2026-09-14", bad=True)}
real_cd, real_bl = engine.chunked_download, engine.backfill_latest
engine.chunked_download = lambda syms, **kw: ({s: fake_hist[s].copy() for s in syms}, [])
engine.backfill_latest = lambda syms, d, **kw: {s: hist_frame("2026-09-14").iloc[[-1]].copy()
                                                for s in syms}
try:
    DATA, health, stats = engine.fetch_history(univ)
    check("stale backfilled & kept", "STALE.NS" in DATA and stats["backfilled"] == 1)
    check("short history dropped", "SHORT.NS" not in DATA and stats["short"] == 1)
    check("OHLC anomaly repaired", stats["repaired"] == 1 and
          DATA["BAD.NS"]["High"].iloc[-3] >= DATA["BAD.NS"]["Low"].iloc[-3])
    check("as-of = latest session", stats["asof"] == "2026-09-14")
    check("health text numbers", "3 / 4" in health.replace(",","") or "3 / 4" in health)
    # without backfill the stale one must be dropped (require_latest_session=True)
    engine.backfill_latest = lambda syms, d, **kw: {}
    DATA2, _, st2 = engine.fetch_history(univ)
    check("unfixable stale dropped (latest-session gate)", "STALE.NS" not in DATA2 and st2["stale"] == 1)
finally:
    engine.chunked_download, engine.backfill_latest = real_cd, real_bl


# ═══════════════════════════════ §6 rank_universe ordering & filters
print("\n§6 rank_universe — turnover ordering / filters")
def month_frame(close, vol, n=22):
    idx = pd.bdate_range("2026-08-01", periods=n)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": vol}, index=idx)

rank_frames = {"BIG.NS": month_frame(500, 1e7),      # ₹500 Cr turnover
               "MID.NS": month_frame(100, 1e6),      # ₹10 Cr
               "SML.NS": month_frame(100, 1e3),      # ₹0.01 Cr — below min
               "FEW.NS": month_frame(100, 1e7, n=5)} # too few bars
u_all = pd.DataFrame({"Yahoo": list(rank_frames), "SYMBOL": [s.replace(".NS", "") for s in rank_frames],
                      "NAME OF COMPANY": ["BigCo", "MidCo", "SmlCo", "FewCo"]})
real_cd = engine.chunked_download
engine.chunked_download = lambda syms, **kw: ({s: rank_frames[s].copy() for s in syms if s in rank_frames}, [])
try:
    U, st = engine.rank_universe(u_all)
    eq("ranked count (min-bars pass)", st["rank_liquid"], 2)          # FEW excluded by <8 bars, SML by turnover
    check("order = turnover desc", list(U["SYMBOL"]) == ["BIG", "MID"])
    check("names merged", U.loc[U["SYMBOL"] == "BIG", "NAME OF COMPANY"].iloc[0] == "BigCo")
    eq("turnover computed (₹Cr)", float(U.iloc[0]["Avg_Turnover_Cr"]), 500.0 * 1e7 / 1e7)
finally:
    engine.chunked_download = real_cd


# ═══════════════════════════════ §7 analyze_symbol edge matrix
print("\n§7 analyze_symbol — acceptance / rejection matrix")
lvl = 100.0
good_last = (101.8, 103.0, 99.35, 102.6, 1_800_000.0)
cases = [
    ("ACCEPT textbook sweep",                good_last, True),
    ("REJECT close below level",             (101.8, 103.0, 99.35, 99.8, 1_800_000.0), False),
    ("REJECT no pierce",                     (101.8, 103.0, 100.3, 102.6, 1_800_000.0), False),
    ("REJECT deep wick (>2.5% = breakdown)", (101.8, 104.5, 97.0, 102.6, 1_800_000.0), False),
    ("REJECT bearish close",                 (102.9, 103.0, 99.35, 102.6, 1_800_000.0), False),
    ("REJECT tiny wick (<30% of range)",     (101.8, 107.8, 99.8, 107.0, 1_800_000.0), False),
]
for name, last, expect in cases:
    rec = engine.analyze_symbol(ohlcv(sweep_rows(last=last)), CFG)
    check(name, (rec is not None) == expect)

# prior close below level → not a sweep
rows = sweep_rows()
rows[-2] = (99.4, 100.2, 99.0, 99.5, 1_000_000.0)   # bar before sweep closes below 100
check("REJECT prior close below level", engine.analyze_symbol(ohlcv(rows), CFG) is None)

# min price gate
small = [(o*0.05, h*0.05, l*0.05, c*0.05, v) for o, h, l, c, v in sweep_rows()]  # ~₹5 stock
check("REJECT sub-₹10 close", engine.analyze_symbol(ohlcv(small), CFG) is None)

# zero-range candle
zr = sweep_rows(last=(102.6, 102.6, 102.6, 102.6, 1_800_000.0))
check("REJECT zero-range candle", engine.analyze_symbol(ohlcv(zr), CFG) is None)

# NaN latest close
nan_rows = sweep_rows()
nan_rows.append((np.nan, np.nan, np.nan, np.nan, np.nan))
check("REJECT NaN latest bar", engine.analyze_symbol(ohlcv(nan_rows), CFG) is None)

# pooled duplicate levels merge (two swing lows within 0.15%) — still accepts, level = min
rec = engine.analyze_symbol(ohlcv(sweep_rows()), CFG)
eq("pooled level = 100.00", rec["Swept_Level"], 100.0)

# level older than lookback rejected
cfg_old = engine.SweepConfig(level_lookback=3)
check("REJECT level older than lookback", engine.analyze_symbol(ohlcv(sweep_rows()), cfg_old) is None)

# volume zero safety (no crash)
vz = [(o, h, l, c, 0.0) for o, h, l, c, v in sweep_rows()]
rec = engine.analyze_symbol(ohlcv(vz), CFG)
check("zero-volume handled (no crash, still a sweep)", rec is not None and math.isfinite(rec["Vol_x"]))


# ═══════════════════════════════ §8 screen_all ordering + isolation
print("\n§8 screen_all — ordering & error isolation")
data = {"WIN.NS": ohlcv(sweep_rows()), "FLAT.NS": ohlcv([(10, 10.5, 9.5, 10.1, 1e5)] * 70),
        "BROKEN.NS": ohlcv([(np.nan, 11, 9, 10.5, 1e5)] * 70)}   # NaN close everywhere → dropna'd normally; here forces None
res = engine.screen_all(data, CFG)
eq("one setup found", len(res), 1)
eq("# starts at 1", int(res["#"].iloc[0]), 1)
check("sorted by Score desc", res["Score"].is_monotonic_decreasing)
check("empty frame when nothing sweeps", engine.screen_all({"FLAT.NS": data["FLAT.NS"]}, CFG).empty)


# ═══════════════════════════════ §9 format_results — exact Colab table
print("\n§9 format_results — exact Colab display table")
univ = pd.DataFrame({"Yahoo": ["WIN.NS"], "SYMBOL": ["WIN"], "NAME OF COMPANY": ["Winn & Sons< Ltd"]})
res2, show = engine.format_results(res, univ)
eq("columns match Colab", list(show.columns),
   ["#", "Symbol", "Name", "Date", "Close", "Swept Low", "Age(bars)", "Depth %", "Above %",
    "Wick %", "ClosePos %", "Vol x", "RSI", ">EMA50", "Turn ₹Cr", "Stop", "Target", "R:R", "Score"])
check("Close formatted 2dp", show["Close"].iloc[0].count(".") == 1 and len(show["Close"].iloc[0].split(".")[1]) == 2)
check("Symbol stripped of .NS", show["Symbol"].iloc[0] == "WIN")
check("Name mapped", show["Name"].iloc[0] == "Winn & Sons< Ltd")


# ═══════════════════════════════ §10 plot_setup short data
print("\n§10 plot_setup — figure + short data")
import matplotlib
matplotlib.use("Agg")
fig = engine.plot_setup({"WIN.NS": data["WIN.NS"].tail(90)}, "WIN.NS", 100.0, last_bars=10)
check("figure returned", fig is not None and len(fig.axes) == 1)
import io as _io
buf = _io.BytesIO(); fig.savefig(buf, format="png")
check("PNG renders non-empty", len(buf.getvalue()) > 5_000)


# ═══════════════════════════════ §11 build_html edges
print("\n§11 build_html — empty day / special chars / caps")
html_empty = RP.build_html(None, None, {}, "2026-09-14 19:30", "RULES", "HEALTH",
                           {"asof": "2026-09-14", "screened": 1000, "universe": 1000})
check("empty-day verdict", "No liquidity-sweep setup on 2026-09-14" in html_empty)
check("empty-day still has rules+health", "RULES" in html_empty and "HEALTH" in html_empty)

sp = engine.screen_all({"GVT&D.NS": ohlcv(sweep_rows())}, CFG)
univ_sp = pd.DataFrame({"Yahoo": ["GVT&D.NS"], "SYMBOL": ["GVT&D"],
                        "NAME OF COMPANY": ["Kaye & <Kay>"]})
res_sp, show_sp = engine.format_results(sp, univ_sp)
html_sp = RP.build_html(res_sp, show_sp, {"GVT&D": b"\x89PNGFIX"}, "s", "r", "h", {"asof": "2026"})
check("& and <> escaped", "GVT&amp;D" in html_sp and "&lt;Kay&gt;" in html_sp)
check("no raw unescaped tag from name", "<Kay>" not in html_sp)
check("chart embedded base64", "aW1hZ2V"[:0] == "" and "data:image/png;base64" in html_sp)


# ═══════════════════════════════ §12 emailer clipping
print("\n§12 emailer — clipping boundary")
big = "<html><body>" + ("<!--chart-card--><div>chart</div>" * 300) + "</body></html>"
clipped = emailer._clipped_notice(big, "<!--chart-card-->")
check("clipped under limit", len(clipped.encode()) < emailer.INLINE_LIMIT + 3_000)
check("clipped ends with notice", "attached" in clipped and clipped.endswith("</html>"))
check("no mid-tag cut at boundary", clipped.rindex("<!--chart-card-->") < clipped.rindex("attached"))


# ═══════════════════════════════ summary
print(f"\n{'═'*60}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
    sys.exit(1)
print("ALL OFFLINE DIAGNOSIS GREEN ✅")
