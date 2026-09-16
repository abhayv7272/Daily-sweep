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
  9.  format_results — display table shape & formatting
  10. plot_setup — multi-panel candlestick + RSI figure
  11. build_html — empty day, special chars (&), chart cap, matrix table
  12. emailer clipping boundary
  13. RSI series calculation & Wilder smoothing edge cases
  14. RSI Divergence detection (regular bullish, hidden bullish, flat)
  15. Fair Value Gap (Bullish FVG) detection & mitigation logic
  16. Relative Equal Lows (Double/Triple bottom) detection & % distance
  17. Volume Dynamics & Institutional Absorption classification
  18. Candlestick Reversal Pattern detection (Hammer, Dragonfly, Reclaim)
  19. Confluence Rating & Bullish Catalysts aggregation

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

# ---- FIXED-notebook behaviours (the version on main) ----
# tie-aware fractal: exact double bottom keeps its first touch
dbl = [(104.0, 104.3, 102.5, 102.8, 1e6), (102.8, 103.0, 100.0, 100.8, 1e6),
       (100.8, 101.9, 100.6, 101.6, 1e6), (101.6, 101.9, 100.0, 100.9, 1e6),
       (100.9, 102.2, 100.7, 102.0, 1e6), (102.0, 102.8, 101.8, 102.5, 1e6),
       (102.5, 103.0, 102.1, 102.7, 1e6), (102.0, 103.2, 99.60, 102.4, 1.6e6)]
pref = [(105.0 + (i % 5) * 0.2,) * 1 for i in range(60)]
pref = [(x[0], x[0] + 0.8, x[0] - 0.8, x[0] + 0.3, 1e6) for x in pref]
rec_d = engine.analyze_symbol(ohlcv(pref + dbl), CFG)
check("tie-aware: equal-low double bottom sweepable", rec_d is not None and
      abs(rec_d["Swept_Level"] - 100.0) < 1e-9)

# NaN low on the signal bar must never mint a setup
nanl = sweep_rows(last=(101.8, 103.0, float("nan"), 102.6, 1_800_000.0))
check("NaN-low signal bar rejected", engine.analyze_symbol(ohlcv(nanl), CFG) is None)

# flat-tape RSI is neutral 50 (not fake 100)
eq("flat-tape RSI = 50", float(engine.rsi_wilder(np.array([100.0] * 30)).iloc[-1]), 50.0)

# NaN volume is sanitised to 0 (Vol_x falls back to 1.0), never NaN
nv = [(o, h, l, c, float("nan")) for o, h, l, c, v in sweep_rows()]
rec_v = engine.analyze_symbol(ohlcv(nv), CFG)
check("NaN volume sanitised", rec_v is not None and math.isfinite(rec_v["Vol_x"])
      and rec_v["Vol_x"] == 1.0)


# ═══════════════════════════════ §8 screen_all ordering + isolation
print("\n§8 screen_all — ordering & error isolation")
data = {"WIN.NS": ohlcv(sweep_rows()), "FLAT.NS": ohlcv([(10, 10.5, 9.5, 10.1, 1e5)] * 70),
        "BROKEN.NS": ohlcv([(np.nan, 11, 9, 10.5, 1e5)] * 70)}
res = engine.screen_all(data, CFG)
eq("one setup found", len(res), 1)
eq("# starts at 1", int(res["#"].iloc[0]), 1)
check("sorted by Score desc", res["Score"].is_monotonic_decreasing)
check("empty frame when nothing sweeps", engine.screen_all({"FLAT.NS": data["FLAT.NS"]}, CFG).empty)


# ═══════════════════════════════ §9 format_results — table formatting
print("\n§9 format_results — table formatting")
univ = pd.DataFrame({"Yahoo": ["WIN.NS"], "SYMBOL": ["WIN"], "NAME OF COMPANY": ["Winn & Sons< Ltd"]})
res2, show = engine.format_results(res, univ)
check("Symbol stripped of .NS", show["Symbol"].iloc[0] == "WIN")
check("Name mapped", show["Name"].iloc[0] == "Winn & Sons< Ltd")
check("Close formatted 2dp", "." in show["Close"].iloc[0])
check("Score column present", "Score" in show.columns)
check("Swept Low column present", "Swept Low" in show.columns)


# ═══════════════════════════════ §10 plot_setup short data
print("\n§10 plot_setup — figure + multi-panel")
import matplotlib
matplotlib.use("Agg")
fig = engine.plot_setup({"WIN.NS": data["WIN.NS"].tail(90)}, "WIN.NS", 100.0, last_bars=10)
check("figure returned with 2 subpanels", fig is not None and len(fig.axes) == 2)
import io as _io
buf = _io.BytesIO(); fig.savefig(buf, format="png")
check("PNG renders non-empty", len(buf.getvalue()) > 5_000)


# ═══════════════════════════════ §11 build_html edges
print("\n§11 build_html — empty day / special chars / caps")
html_empty = RP.build_html(None, None, {}, "2026-09-14 19:30", "RULES", "HEALTH",
                           {"asof": "2026-09-14", "screened": 1000, "universe": 1000})
check("empty-day verdict", "No Liquidity-Sweep Setup on 2026-09-14" in html_empty or "No liquidity-sweep setup" in html_empty.lower())
check("empty-day still has rules+health", "RULES" in html_empty and "HEALTH" in html_empty)

sp = engine.screen_all({"GVT&D.NS": ohlcv(sweep_rows())}, CFG)
univ_sp = pd.DataFrame({"Yahoo": ["GVT&D.NS"], "SYMBOL": ["GVT&D"],
                        "NAME OF COMPANY": ["Kaye & <Kay>"]})
res_sp, show_sp = engine.format_results(sp, univ_sp)
html_sp = RP.build_html(res_sp, show_sp, {"GVT&D": b"\x89PNGFIX"}, "s", "r", "h", {"asof": "2026"})
check("& and <> escaped", "GVT&amp;D" in html_sp and "&lt;Kay&gt;" in html_sp)
check("no raw unescaped tag from name", "<Kay>" not in html_sp)
check("chart embedded base64", "data:image/png;base64" in html_sp)


# ═══════════════════════════════ §12 emailer clipping
print("\n§12 emailer — clipping boundary")
big = "<html><body>" + ("<!--chart-card--><div>chart</div>" * 300) + "</body></html>"
clipped = emailer._clipped_notice(big, "<!--chart-card-->")
check("clipped under limit", len(clipped.encode()) < emailer.INLINE_LIMIT + 3_000)
check("clipped ends with notice", "attached" in clipped and clipped.endswith("</html>"))
check("no mid-tag cut at boundary", clipped.rindex("<!--chart-card-->") < clipped.rindex("attached"))


# ═══════════════════════════════ §13 RSI series & Wilder smoothing
print("\n§13 compute_rsi_series — Wilder smoothing edge cases")
c_flat = np.array([100.0] * 50)
rsi_flat = engine.compute_rsi_series(c_flat, 14)
eq("flat series RSI = 50.0", float(rsi_flat[-1]), 50.0)
eq("length preserved", len(rsi_flat), 50)

# Monotonic uptrend -> RSI near 100
c_up = np.linspace(100, 200, 50)
rsi_up = engine.compute_rsi_series(c_up, 14)
check("uptrend RSI > 90", rsi_up[-1] > 90.0)

# Monotonic downtrend -> RSI near 0
c_dn = np.linspace(200, 100, 50)
rsi_dn = engine.compute_rsi_series(c_dn, 14)
check("downtrend RSI < 10", rsi_dn[-1] < 10.0)


# ═══════════════════════════════ §14 RSI Divergence Detection
print("\n§14 detect_rsi_divergence — regular & hidden bullish")
# Synthetic price series with regular bullish divergence
# Swing low 1 at bar 10: low 100.0, RSI 25
# Swing low 2 at bar 20: low 99.0 (Lower Low), RSI 38 (Higher Low)
swings_test = [(10, 100.0)]
c_test = np.array([105.0] * 21)
l_test = np.array([105.0] * 21)
l_test[10] = 100.0
l_test[20] = 99.0
rsi_test = np.array([50.0] * 21)
rsi_test[10] = 25.0
rsi_test[20] = 38.0

div_res = engine.detect_rsi_divergence(c_test, l_test, swings_test, rsi_test, lookback=25)
check("regular bullish div detected", div_res["type"] == "regular_bullish")
check("label contains Bullish Div", "Bullish Div" in div_res["label"])
check("prior swing matched", div_res["prior_low_idx"] == 10)

# Hidden bullish divergence (Higher Low in price + Lower Low in RSI)
l_test_h = l_test.copy()
l_test_h[20] = 101.5  # Higher Low
rsi_test_h = rsi_test.copy()
rsi_test_h[20] = 20.0  # Lower Low
div_hid = engine.detect_rsi_divergence(c_test, l_test_h, swings_test, rsi_test_h, lookback=25)
check("hidden bullish div detected", div_hid["type"] == "hidden_bullish")


# ═══════════════════════════════ §15 Bullish Fair Value Gap (FVG)
print("\n§15 find_bullish_fvgs & analyze_fvg_status")
# Candle 0: High = 100.0
# Candle 1: Big expansion
# Candle 2: Low = 102.5 -> Bullish FVG [100.0, 102.5]
# Candle 3: Low = 101.0, Close = 103.0 -> inside FVG!
fvg_df = pd.DataFrame([
    {"Open": 99.0, "High": 100.0, "Low": 98.5, "Close": 99.5},
    {"Open": 100.0, "High": 103.0, "Low": 100.0, "Close": 102.5},
    {"Open": 102.5, "High": 104.0, "Low": 102.5, "Close": 103.5},
    {"Open": 103.0, "High": 104.0, "Low": 101.0, "Close": 103.0},
])
fvgs = engine.find_bullish_fvgs(fvg_df, lookback=10)
eq("1 bullish FVG found", len(fvgs), 1)
eq("FVG top = 102.5", fvgs[0]["top"], 102.5)
eq("FVG bottom = 100.0", fvgs[0]["bottom"], 100.0)

fvg_stat = engine.analyze_fvg_status(fvg_df, fvgs)
check("candle is inside FVG", fvg_stat["in_fvg"] is True)
check("status text describes FVG", "Bullish FVG" in fvg_stat["fvg_status"])


# ═══════════════════════════════ §16 Relative Equal Lows (EQL)
print("\n§16 Relative Equal Lows (Double / Triple Bottom)")
# Merge pools with multiple touches
swings_eql = [(10, 100.0), (25, 100.10), (45, 105.0)]
pools_eql = engine.merge_pools(swings_eql, dedup_pct=0.002)
eq("2 pools formed", len(pools_eql), 2)
eq("first pool has 2 touches", pools_eql[0][3], 2)
eq("first pool level = min low (100.0)", pools_eql[0][0], 100.0)

# Touch counting function
l_touches = np.array([105.0] * 50)
l_touches[10] = 100.0
l_touches[20] = 100.05
l_touches[35] = 100.10
cnt, touch_idxs = engine.count_pool_touches(l_touches, 100.0, first_i=10, T=49, tol_pct=0.002)
eq("3 distinct touches counted", cnt, 3)


# ═══════════════════════════════ §17 Volume Dynamics
print("\n§17 analyze_volume_dynamics")
v_arr = np.array([1_000_000.0] * 25)
v_arr[-1] = 2_200_000.0  # 2.2x volume
c_arr = np.array([102.5] * 25)
l_arr = np.array([99.0] * 25)
h_arr = np.array([103.0] * 25)
o_arr = np.array([101.5] * 25)  # lower wick = 2.5 / 4.0 = 62.5% -> absorption!
vol_dyn = engine.analyze_volume_dynamics(v_arr, c_arr, l_arr, h_arr, o_arr, T=24, vol_lookback=20)
check("absorption identified", "Absorption" in vol_dyn["vol_div"])
eq("vol_x = 2.2", vol_dyn["vol_x"], 2.2)


# ═══════════════════════════════ §18 Candlestick Patterns
print("\n§18 analyze_candlestick_pattern")
# Hammer: Open 102.0, High 103.0, Low 99.0, Close 102.8 -> Lower wick = 3.0/4.0 = 75%, Close in top 20%
p_hammer = engine.analyze_candlestick_pattern(
    np.array([102.0]), np.array([103.0]), np.array([99.0]), np.array([102.8]), T=0)
check("Hammer pinbar detected", "Hammer" in p_hammer)

# Dragonfly Doji: Open 102.8, High 103.0, Low 99.0, Close 102.8
p_df = engine.analyze_candlestick_pattern(
    np.array([102.8]), np.array([103.0]), np.array([99.0]), np.array([102.8]), T=0)
check("Dragonfly doji detected", "Dragonfly" in p_df)


# ═══════════════════════════════ §19 Confluence & Grade
print("\n§19 compute_confluences_and_grade")
mock_rec = {
    "Score": 82.0, "RSI_Div_Type": "regular_bullish", "Is_Equal_Lows": True,
    "Equal_Lows_Count": 2, "Equal_Lows_Level": 100.0, "In_Bullish_FVG": True,
    "Vol_Div": "Bullish Absorption 🔥", "Vol_x": 1.8, "Candle_Pattern": "Hammer Pinbar 🔨",
    "E50": True, "E20": True, "E20_Regained": True, "RR": 2.6, "Sweep_Depth_": 0.5,
    "Target_Ref": 110.0
}
grade, grade_short, catalysts, count = engine.compute_confluences_and_grade(mock_rec)
eq("Grade is A+", grade_short, "A+")
check("multiple catalysts listed", len(catalysts) >= 5)
check("RSI div catalyst present", any("RSI" in c for c in catalysts))
check("Equal lows catalyst present", any("Equal Lows" in c for c in catalysts))
check("FVG catalyst present", any("FVG" in c for c in catalysts))


# ═══════════════════════════════ summary
print(f"\n{'═'*60}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
    sys.exit(1)
print("ALL OFFLINE DIAGNOSIS GREEN ✅")
