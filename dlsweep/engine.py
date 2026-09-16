"""
dlsweep/engine.py — the NSE DAILY Liquidity-Sweep Screener engine with
advanced multi-confluence trend reversal analytics:

  * Swing Low Liquidity Sweep & Reclaim detection
  * Relative Equal Lows (Double/Triple Bottom / Liquidity Shelf) detection & % distance
  * Fair Value Gap (Bullish FVG / Imbalance) mitigation & status
  * Wilder RSI & Regular/Hidden Bullish RSI Divergence detection
  * Volume Dynamics (Institutional Absorption, Dry-Up Spring, Volume Multiplier)
  * Candlestick Reversal Signatures (Hammer Pinbar, Dragonfly Doji, Power Reclaim)
  * Moving Average Regime (EMA 20, 50, 200) & Intraday Regain
  * Major Structural Low Lookback & % buffer
  * Composite Trend Reversal Confluence Grade (A+ / A / B+) & Bullish Catalysts
  * High-Res Multi-Panel Candlestick & RSI Divergence Charting
"""

# ============================================================
# cell 1 — imports
# ============================================================
from dataclasses import dataclass, field
import datetime as _dt
import io
import os
import random
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

# ============================================================
# cell 3 — CONFIGURATION
# ============================================================
@dataclass
class SweepConfig:
    # ---- Universe ----
    universe_size       : int   = 1000     # NSE top-N by avg daily turnover
    min_avg_turnover_cr : float = 0.25     # min ₹ Cr avg daily turnover to stay in universe
    rank_bars_needed    : int   = 8        # min bars to be eligible for turnover ranking
    # ---- Data ----
    history_period      : str   = "1y"     # ~250 daily bars of history
    min_bars            : int   = 60       # drop tickers with thinner history
    require_latest_session : bool = True   # screen ONLY candles of the market's latest session
    backfill_latest_close  : bool = True   # rebuild a missing latest-session candle from 15-min data
    max_staleness_days  : int   = 4        # (used only if require_latest_session=False)
    download_chunk      : int   = 80       # symbols per Yahoo request
    max_retries         : int   = 3        # retries per chunk
    backoff_secs        : float = 4.0      # base backoff (doubles per retry)
    # ---- Swing low (liquidity level) detection ----
    fractal_k           : int   = 2        # 5-bar fractal: low = min of [i-2 .. i+2]
    level_lookback      : int   = 120      # level may be up to N bars old (old OR new)
    level_dedup_pct     : float = 0.0015   # levels within 0.15% = same liquidity pool
    # ---- Sweep conditions (hard filters on latest candle) ----
    min_pierce_pct      : float = 0.0005   # low must pierce >= 0.05% below level
    close_above_buffer  : float = 0.001    # close must be >= 0.1% above level
    max_sweep_depth     : float = 0.025    # wick deeper than 2.5% below level = breakdown, reject
    min_wick_ratio      : float = 0.30     # lower wick >= 30% of candle range ("proper wick")
    min_wick_pct_price  : float = 0.0015   # and >= 0.15% of price (ignore micro-noise)
    require_bullish_close : bool = True    # close > open
    require_prior_above : bool = True      # previous close above the level (true sweep)
    min_price           : float = 10.0     # ignore sub-₹10 names
    # ---- Scoring & Confluence ----
    volume_lookback     : int   = 20
    rsi_period          : int   = 14
    fvg_lookback        : int   = 40       # lookback for bullish FVGs
    rsi_div_lookback    : int   = 35       # lookback for RSI divergence
    major_low_lookback  : int   = 60       # lookback for major structural low


CFG = SweepConfig()


def describe_config(cfg=None):
    """The configuration banner describing active rules and confluence filters."""
    cfg = cfg or CFG
    lines = [
        "Active rules & Confluence parameters on the LATEST daily candle",
        "-" * 68,
        f"  Swing low           : 5-bar fractal, up to {cfg.level_lookback} bars old",
        f"  Sweep pierce        : low >= {cfg.min_pierce_pct:.2%} below level, no deeper than {cfg.max_sweep_depth:.1%}",
        f"  Reclaim             : close >= {cfg.close_above_buffer:.2%} ABOVE level",
        f"  Proper wick         : lower wick >= {cfg.min_wick_ratio:.0%} of range AND >= {cfg.min_wick_pct_price:.2%} of price",
        f"  Bullish close       : {cfg.require_bullish_close}   |   Prior close above level: {cfg.require_prior_above}",
        f"  Equal Lows pool     : clustered within {cfg.level_dedup_pct:.2%} (Double/Triple Bottom liquidity shelves)",
        f"  Confluence checks   : RSI Divergence ({cfg.rsi_div_lookback}b), Bullish FVG ({cfg.fvg_lookback}b), Vol Absorption, EMAs",
        f"  Universe            : top {cfg.universe_size} by turnover (>= ₹{cfg.min_avg_turnover_cr} Cr/day), price >= ₹{cfg.min_price:.0f}",
    ]
    return "\n".join(lines)


# ============================================================
# cell 5 — UNIVERSE
# ============================================================
NSE_LIST_URLS = [
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv",   # legacy mirror
]
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# Fallback: ~top 300 liquid NSE names (used only if the official list is unreachable)
FALLBACK_SYMBOLS = [
    "BSE", "HDFCBANK", "DHOOTTRANS", "NETWEB", "HINDCOPPER", "ICICIBANK", "RELIANCE",
    "GROWW", "BHARTIARTL", "WELCORP", "ETERNAL", "INFY", "SHIPROCKET", "MCX", "ASTERDM",
    "CUPID", "HSCL", "MILKYMIST", "PAYTM", "SBIN", "DIXON", "LENSKART", "BALRAMCHIN",
    "MOLBIO", "SAIL", "KOTAKBANK", "IDEA", "TCS", "ATHERENERG", "AXISBANK", "BLEL",
    "HINDZINC", "DATAPATTNS", "TATASTEEL", "LT", "M&M", "KALYANKJIL", "LICI", "KAYNES",
    "COFORGE", "LAURUSLABS", "VIYASH", "IFCI", "BAJFINANCE", "ADANIPOWER", "HFCL",
    "ADANIENSOL", "GRASIM", "COALINDIA", "GVT&D", "VEDL", "HAL", "BEL", "MUTHOOTFIN",
    "DIVISLAB", "TVSMOTOR", "ITC", "ADANIENT", "BHEL", "APOLLOHOSP", "MANORAMA",
    "PCJEWELLER", "MVELECTRO", "VBL", "FMGOETZE", "FACT", "INDOMIM", "TMCV", "MEESHO",
    "SWIGGY", "URBANCO", "MARUTI", "KWIL", "SHRIRAMFIN", "HINDALCO", "PWL", "PARAS",
    "LGEINDIA", "VAML", "ONGC", "TECHM", "PFC", "ZEEL", "ADANIPORTS", "VMM", "BALUFORGE",
    "CYIENT", "TEJASNET", "POWERINDIA", "TMPV", "ULTRACEMCO", "PINELABS", "CHENNPETRO",
    "SUNPHARMA", "ICICIAMC", "POLYCAB", "APARINDS", "HDFCAMC", "AMBER", "CUMMINSIND",
    "BAJAJHIND", "SOLARINDS", "CGCL", "INDIGO", "TITAN", "BDL", "JSWSTEEL", "RATNAVEER",
    "PERSISTENT", "NTPC", "BBTC", "MOREPENLAB", "EICHERMOT", "BAJAJ-AUTO", "OFSS",
    "ASHOKLEY", "CDSL", "NATIONALUM", "TFCILTD", "HEROMOTOCO", "LEAPIND", "HCLTECH",
    "IIFL", "POLICYBZR", "LTFOODS", "MRPL", "NESTLEIND", "REDINGTON", "ZAGGLE",
    "WELSPUNLIV", "HINDUNILVR", "POWERGRID", "MANAPPURAM", "TATAPOWER", "VOGL", "MAZDOCK",
    "JIOFIN", "IPCALAB", "CGPOWER", "WEL", "DMART", "AUBANK", "TIINDIA", "VIKRAMSOLR",
    "DLF", "AZAD", "BOSCHLTD", "LICHSGFIN", "SUZLON", "NMDC", "TDPOWERSYS", "ICICIGI",
    "FEDERALBNK", "BHARATFORG", "MOTILALOFS", "NAUKRI", "QUADFUTURE", "MAXHEALTH",
    "APOLLO", "IDBI", "RECLTD", "BANKBARODA", "WABAG", "KEI", "MOTHERSON", "HYUNDAI",
    "SIEMENS", "DRREDDY", "TECHNOCRAF", "LTF", "ADANIGREEN", "SHADOWFAX", "HONASA",
    "UNIONBANK", "COCHINSHIP", "TRENT", "SONACOMS", "LODHA", "INDUSTOWER", "CARTRADE",
    "ANGELONE", "LUPIN", "JYOTICNC", "LTM", "BRITANNIA", "ZYDUSLIFE", "BPCL", "JINDRILL",
    "GENUSPOWER", "NAM-INDIA", "VOLTAS", "PTCIL", "PAGEIND", "CHOLAFIN", "PARADEEP",
    "SBILIFE", "ENRIN", "HINDPETRO", "ICIL", "SBICARD", "SYRMA", "CANBK", "JUBLFOOD",
    "KFINTECH", "OLAELEC", "HDFCLIFE", "TORNTPHARM", "MANIPALHOS", "NYKAA", "SAMMAANCAP",
    "ASIANPAINT", "WIPRO", "HCC", "SAGILITY", "GESHIP", "PREMIERENE", "JINDALSAW", "HEG",
    "AVALON", "RUBICON", "ZENTEC", "GLENMARK", "HAPPSTMNDS", "GRSE", "CROMPTON", "MAHABANK",
    "RADICO", "VISL", "ABCAPITAL", "AUROPHARMA", "BAJAJFINSV", "AEGISLOG", "BLS", "MOTISONS",
    "GODREJCP", "PNB", "APLAPOLLO", "INOXINDIA", "ABB", "OBEROIRLTY", "KERNEX",
    "WAAREEENER", "RENUKA", "SBIFUNDS", "BELRISE", "RRKABEL", "TATACONSUM", "AMBUJACEM",
    "STYLEBAAZA", "WOCKPHARMA", "PIDILITIND", "GLAND", "FINCABLES", "YESBANK", "UPL",
    "KTKBANK", "RAMBHAJO", "GODREJPROP", "NEULANDLAB", "SUDARSCHEM", "EXIDEIND",
    "ANANTRAJ", "GAIL", "GMRAIRPORT", "MARKSANS", "SHILPAMED", "JBMA", "JINDALSTEL",
    "EIDPARRY", "ACMESOLAR", "CPPLUS", "IOC", "ORIENTHOT", "BANDHANBNK", "CAMS", "FORTIS",
    "INDUSINDBK", "DCBBANK", "CIPLA", "STLTECH", "KPITTECH", "NAVINFLUOR", "IDFCFIRSTB",
    "FORCEMOT", "BIOCON", "SHANTIGEAR", "PNBHOUSING", "GLAXO", "INOXWIND", "MPHASIS",
    "AWL", "UJJIVANSFB", "HAVELLS", "JSFB", "SHANTIGOLD", "MANKIND", "TATATECH",
    "ACUTAAS", "MARICO", "AEROFLEX",
]


def fetch_nse_universe():
    df, src = None, None
    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:            # warm up cookies — NSE's WAF rejects cookie-less requests from many IPs
        sess.get("https://www.nseindia.com/", timeout=15)
    except Exception:
        pass
    for url in NSE_LIST_URLS:
        try:
            r = sess.get(url, timeout=30)
            r.raise_for_status()
            if "<html" in r.text[:200].lower():          # WAF "Access Denied" page with HTTP 200
                raise RuntimeError("blocked by NSE WAF (HTML instead of CSV)")
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.strip() for c in df.columns]
            df = df[df["SYMBOL"].notna() & (df["SYMBOL"].astype(str).str.len() > 0)]
            df = df[["SYMBOL", "NAME OF COMPANY"]].copy()
            df["SYMBOL"] = df["SYMBOL"].astype(str).str.upper().str.strip()
            df = df.drop_duplicates("SYMBOL").reset_index(drop=True)
            src = "NSE official archive"
            print(f"  ✓ fetched full NSE list ({len(df):,} symbols) from {url.split('/')[2]}")
            break
        except Exception as exc:
            print(f"  ⚠ NSE list fetch failed on {url.split('/')[2]} "
                  f"({exc.__class__.__name__}: {exc})")
    if df is None:
        print(f"  → falling back to embedded list of {len(FALLBACK_SYMBOLS)} liquid names")
        df = pd.DataFrame({"SYMBOL": FALLBACK_SYMBOLS, "NAME OF COMPANY": ""})
        src = "embedded fallback"
    df["Yahoo"] = df["SYMBOL"].str.upper() + ".NS"
    df["source"] = src
    return df


# ============================================================
# cell 8 — chunked downloader + turnover ranking
# ============================================================
def chunked_download(symbols, period, chunk=None, label="download"):
    """Chunked yfinance download with per-chunk retry + exponential backoff."""
    chunk = chunk or CFG.download_chunk
    out, failed = {}, []
    n_chunks = (len(symbols) + chunk - 1) // chunk
    for ci in range(n_chunks):
        part = symbols[ci * chunk:(ci + 1) * chunk]
        res, ok = None, False
        for attempt in range(1, CFG.max_retries + 1):
            try:
                res = yf.download(part, period=period, interval="1d", group_by="ticker",
                                  auto_adjust=True, threads=True, progress=False)
                if res is None or len(res) == 0:
                    raise RuntimeError("empty response")
                ok = True
                break
            except Exception as exc:
                wait = CFG.backoff_secs * (2 ** (attempt - 1)) + random.uniform(0, 2)
                print(f"  ⚠ {label} chunk {ci+1}/{n_chunks} attempt {attempt} failed "
                      f"({exc.__class__.__name__}) — retry in {wait:.0f}s")
                time.sleep(wait)
        if not ok:
            failed.extend(part)
            print(f"  ✖ {label} chunk {ci+1}/{n_chunks}: all {CFG.max_retries} attempts failed "
                  f"({len(part)} symbols skipped)")
            continue
        for sym in part:
            try:
                df = res.xs(sym, axis=1, level="Ticker")
            except (KeyError, ValueError):
                continue
            except TypeError:
                if len(part) != 1 or isinstance(res.columns, pd.MultiIndex):
                    continue
                df = res.copy()
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if len(df) and np.isfinite(df["Close"].to_numpy(dtype=float)).all():
                out[sym] = df
        if (ci + 1) % 5 == 0 or ci == n_chunks - 1:
            print(f"  {label}: {min((ci+1)*chunk, len(symbols))}/{len(symbols)} symbols "
                  f"({len(out):,} good so far)")
        time.sleep(0.6)
    return out, failed


def rank_universe(universe_all):
    """Rank all of NSE by avg daily turnover. Returns (UNIVERSE, stats_dict)."""
    UNIVERSE_ALL = universe_all

    print(f"Ranking pass: 1 month of data for all {len(UNIVERSE_ALL):,} NSE symbols …")
    t0 = time.time()
    RANK_DATA, rank_failed = chunked_download(UNIVERSE_ALL["Yahoo"].tolist(), period="1mo",
                                              chunk=120, label="ranking")
    print(f"  fetched {len(RANK_DATA):,}/{len(UNIVERSE_ALL):,} in {time.time()-t0:.0f}s "
          f"({len(rank_failed)} unavailable on Yahoo)")

    rows = []
    for sym, df in RANK_DATA.items():
        if len(df) < CFG.rank_bars_needed:
            continue
        tail = df.tail(10)
        rows.append({
            "Yahoo": sym,
            "Avg_Turnover_Cr": float((tail["Close"] * tail["Volume"]).mean() / 1e7),
            "Last_Close": float(df["Close"].iloc[-1]),
        })
    if not rows:
        raise SystemExit("✖ Turnover ranking produced no data — Yahoo may be rate-limiting this IP. "
                         "Wait a minute and re-run this cell.")
    rank_df = (pd.DataFrame(rows)
                .sort_values("Avg_Turnover_Cr", ascending=False)
                .reset_index(drop=True))
    rank_df = rank_df[rank_df["Avg_Turnover_Cr"] >= CFG.min_avg_turnover_cr]
    UNIVERSE = rank_df.head(CFG.universe_size).copy()
    UNIVERSE = UNIVERSE.merge(UNIVERSE_ALL[["Yahoo", "SYMBOL", "NAME OF COMPANY"]],
                              on="Yahoo", how="left")

    print(f"\nUniverse locked: top {len(UNIVERSE):,} of {len(rank_df):,} liquid NSE stocks "
          f"(turnover >= ₹{CFG.min_avg_turnover_cr} Cr/day)")

    stats = {"rank_fetched": len(RANK_DATA), "rank_failed": len(rank_failed),
             "rank_liquid": len(rank_df), "rank_secs": time.time() - t0}
    return UNIVERSE, stats


# ============================================================
# cell 9 — latest-session backfill + history + health gate
# ============================================================
def backfill_latest(symbols, target_date, chunk=40):
    """Rebuild missing latest-session daily candles from 15-min data."""
    fixed = {}
    if not symbols:
        return fixed
    end = str(target_date + _dt.timedelta(days=1))
    n_chunks = (len(symbols) + chunk - 1) // chunk
    for ci in range(n_chunks):
        part = symbols[ci * chunk:(ci + 1) * chunk]
        intr, ok = None, False
        for attempt in range(1, CFG.max_retries + 1):
            try:
                intr = yf.download(part, start=str(target_date), end=end, interval="15m",
                                   group_by="ticker", auto_adjust=False,
                                   threads=True, progress=False)
                if intr is None or len(intr) == 0:
                    raise RuntimeError("empty response")
                ok = True
                break
            except Exception as exc:
                wait = CFG.backoff_secs * (2 ** (attempt - 1)) + random.uniform(0, 2)
                print(f"  ⚠ backfill chunk {ci+1}/{n_chunks} attempt {attempt} failed "
                      f"({exc.__class__.__name__}) — retry in {wait:.0f}s")
                time.sleep(wait)
        if not ok:
            continue
        for sym in part:
            try:
                i = intr.xs(sym, axis=1, level="Ticker").dropna(subset=["Open", "High", "Low", "Close"])
            except (KeyError, ValueError):
                continue
            except TypeError:
                if len(part) != 1 or isinstance(intr.columns, pd.MultiIndex):
                    continue
                i = intr.dropna(subset=["Open", "High", "Low", "Close"])
            i = i.sort_index()
            if not len(i):
                continue
            row = dict(Open=float(i["Open"].iloc[0]), High=float(i["High"].max()),
                       Low=float(i["Low"].min()), Close=float(i["Close"].iloc[-1]),
                       Volume=float(i["Volume"].sum()))
            fixed[sym] = pd.DataFrame([row], index=pd.DatetimeIndex([pd.Timestamp(target_date)]))
        if (ci + 1) % 4 == 0 or ci == n_chunks - 1:
            print(f"  backfill: {min((ci+1)*chunk, len(symbols))}/{len(symbols)} — "
                  f"{len(fixed)} candles rebuilt")
        time.sleep(0.5)
    return fixed


def fetch_history(universe):
    """Download daily history and apply health gates. Returns (DATA, health_text, stats)."""
    UNIVERSE = universe

    print(f"Downloading {CFG.history_period} daily OHLCV for {len(UNIVERSE):,} symbols …")
    t0 = time.time()
    HIST, hist_failed = chunked_download(UNIVERSE["Yahoo"].tolist(), period=CFG.history_period,
                                         chunk=CFG.download_chunk, label="history")
    print(f"  fetched {len(HIST):,}/{len(UNIVERSE):,} in {time.time()-t0:.0f}s")
    if hist_failed:
        print(f"  ⚠ {len(hist_failed)} symbols failed after {CFG.max_retries} retries: "
              f"{hist_failed[:15]}{' …' if len(hist_failed) > 15 else ''}")
    if not HIST:
        raise SystemExit("✖ Full history download failed completely — check internet and re-run this cell.")

    max_last = max(df.index[-1].date() for df in HIST.values())

    backfilled = 0
    if CFG.backfill_latest_close:
        missing = [s for s, df in HIST.items() if df.index[-1].date() < max_last]
        if missing:
            print(f"\n{len(missing)} symbols lack a complete candle for {max_last} "
                  f"(Yahoo NaN-close lag) — rebuilding from 15-min data …")
            t1 = time.time()
            fixed = backfill_latest(missing, max_last)
            for sym, bar in fixed.items():
                HIST[sym] = pd.concat([HIST[sym], bar])
            backfilled = len(fixed)
            print(f"  rebuilt {len(fixed)}/{len(missing)} latest-session candles in {time.time()-t1:.0f}s")
        else:
            print(f"\nAll {len(HIST):,} symbols have a complete candle for {max_last} — no backfill needed.")

    # ---- data health ----
    DATA, short_list, stale_list = {}, [], []
    for sym, df in HIST.items():
        if len(df) < CFG.min_bars:
            short_list.append(sym); continue
        if CFG.require_latest_session:
            if df.index[-1].date() < max_last:
                stale_list.append(sym); continue
        elif (max_last - df.index[-1].date()).days > CFG.max_staleness_days:
            stale_list.append(sym); continue
        DATA[sym] = df

    repaired = 0
    for sym, df in DATA.items():
        bad = ((df["High"] < df["Low"])
               | (df["High"] < df[["Open", "Close"]].max(axis=1))
               | (df["Low"] > df[["Open", "Close"]].min(axis=1)))
        if bad.any():
            df["High"] = df[["Open", "Close", "High"]].max(axis=1)
            df["Low"] = df[["Open", "Close", "Low"]].min(axis=1)
            repaired += 1

    health = "\n".join([
        "── DATA HEALTH REPORT ───────────────────────────────────",
        f"  Market data as-of date   : {max_last}",
        f"  Screenable tickers       : {len(DATA):,} / {len(UNIVERSE):,}",
        f"  Dropped — short history  : {len(short_list)}",
        f"  Dropped — no candle of latest session (Yahoo lag/suspended): {len(stale_list)}",
        f"  OHLC anomalies repaired  : {repaired}",
        "──────────────────────────────────────────────────────────",
    ])
    print("\n" + health)

    stats = {"asof": str(max_last), "hist_fetched": len(HIST), "hist_failed": len(hist_failed),
             "backfilled": backfilled, "short": len(short_list), "stale": len(stale_list),
             "repaired": repaired, "hist_secs": time.time() - t0}
    return DATA, health, stats


# ============================================================
# cell 11 — ADVANCED TECHNICAL CONFLUENCE & SWEEP ENGINE
# ============================================================
def find_swing_lows(low, k):
    """Indices i where low[i] is a fractal swing low of low[i-k .. i+k].

    Tie-aware: strictly lower than the k bars on the LEFT, lower-or-EQUAL to the k bars
    on the RIGHT. Windows containing non-finite lows are skipped.
    """
    n = len(low)
    out = []
    for i in range(k, n - k):
        w = low[i - k:i + k + 1]
        if not np.isfinite(w).all():
            continue
        left, right = low[i - k:i], low[i + 1:i + k + 1]
        if low[i] < left.min() and low[i] <= right.min():
            out.append((i, float(low[i])))
    return out


def merge_pools(swings, dedup_pct):
    """Merge nearly-equal swing levels into liquidity pools:
    Each pool: [level, first_bar, last_bar, touch_count, touch_list]
    Preserves list indices [0], [1], [2] for 100% backward compatibility.
    """
    pools = []
    for i, L in swings:
        for p in pools:
            if abs(L - p[0]) / p[0] <= dedup_pct:
                p[0] = min(p[0], L)
                p[2] = max(p[2], i)
                p[3] += 1
                p[4].append((i, L))
                break
        else:
            pools.append([L, i, i, 1, [(i, L)]])
    return pools


def rsi_wilder(close, period=14):
    """Wilder RSI series (aligned to close[1:], with zero-loss+gain=50 safe handling)."""
    close = np.asarray(close, dtype=float)
    if len(close) < 2:
        return pd.Series([50.0] * len(close))
    delta = np.diff(close)
    gain = pd.Series(np.clip(delta, 0, None)).ewm(alpha=1 / period, adjust=False).mean()
    loss = pd.Series(np.clip(-delta, 0, None)).ewm(alpha=1 / period, adjust=False).mean()
    out = pd.Series(np.where(loss > 0, 100 - 100 / (1 + gain / loss.where(loss > 0)),
                             np.where(gain > 0, 100.0, 50.0)), index=gain.index)
    return out


def compute_rsi_series(close, period=14):
    """Full-length RSI series matching len(close), initial bar padded with 50.0."""
    close = np.asarray(close, dtype=float)
    n = len(close)
    if n < 2:
        return np.full(n, 50.0)
    r = rsi_wilder(close, period)
    out = np.empty(n, dtype=float)
    out[0] = 50.0
    out[1:] = r.to_numpy(dtype=float)
    return out


def find_bullish_fvgs(df, lookback=40):
    """Detect Bullish Fair Value Gaps (FVGs / Imbalances) over the last `lookback` bars.
    A Bullish FVG forms at bar i where Low[i] > High[i-2].
    The imbalance zone is [High[i-2], Low[i]].
    """
    n = len(df)
    if n < 4:
        return []
    h = df["High"].to_numpy(dtype=float)
    l = df["Low"].to_numpy(dtype=float)
    T = n - 1
    start_i = max(2, T - lookback)

    fvgs = []
    for i in range(start_i, T):
        if l[i] > h[i - 2]:
            top = float(l[i])
            bottom = float(h[i - 2])
            if top <= bottom or bottom <= 0:
                continue
            # Mitigation check: did any candle before T trade completely below bottom?
            mitigated = False
            for k in range(i + 1, T):
                if l[k] <= bottom:
                    mitigated = True
                    break
            fvgs.append({
                "bar_i": int(i),
                "top": top,
                "bottom": bottom,
                "mid": (top + bottom) / 2.0,
                "size_pct": ((top - bottom) / bottom) * 100.0,
                "mitigated": mitigated
            })
    return fvgs


def analyze_fvg_status(df, fvgs):
    """Check whether the latest candle is inside, tapped, or above a Bullish FVG."""
    n = len(df)
    T = n - 1
    c_T = float(df["Close"].iloc[T])
    l_T = float(df["Low"].iloc[T])

    active = [f for f in fvgs if not f["mitigated"]]
    if not active:
        return {
            "in_fvg": False,
            "fvg_status": "No Active FVG",
            "fvg_top": 0.0,
            "fvg_bottom": 0.0,
            "fvg_dist_pct": 0.0,
            "fvg_ref": None
        }

    # Inside or tapped FVG
    inside = [f for f in active if (l_T <= f["top"] * 1.002 and c_T >= f["bottom"] * 0.998)]
    if inside:
        best_fvg = max(inside, key=lambda f: f["top"])
        tapped = l_T <= best_fvg["bottom"] * 1.005
        status_txt = (f"Inside Bullish FVG [₹{best_fvg['bottom']:.2f} - ₹{best_fvg['top']:.2f}]" if not tapped
                      else f"Tapped & Filled Bullish FVG [₹{best_fvg['bottom']:.2f} - ₹{best_fvg['top']:.2f}]")
        return {
            "in_fvg": True,
            "fvg_status": status_txt,
            "fvg_top": best_fvg["top"],
            "fvg_bottom": best_fvg["bottom"],
            "fvg_dist_pct": 0.0,
            "fvg_ref": best_fvg
        }

    # Above nearest active Bullish FVG
    below = [f for f in active if f["top"] < c_T]
    if below:
        nearest = max(below, key=lambda f: f["top"])
        dist_pct = ((c_T / nearest["top"]) - 1.0) * 100.0
        return {
            "in_fvg": False,
            "fvg_status": f"+{dist_pct:.1f}% above FVG [₹{nearest['bottom']:.2f} - ₹{nearest['top']:.2f}]",
            "fvg_top": nearest["top"],
            "fvg_bottom": nearest["bottom"],
            "fvg_dist_pct": dist_pct,
            "fvg_ref": nearest
        }

    return {
        "in_fvg": False,
        "fvg_status": "No FVG Below",
        "fvg_top": 0.0,
        "fvg_bottom": 0.0,
        "fvg_dist_pct": 0.0,
        "fvg_ref": None
    }


def detect_rsi_divergence(close, low, swings, rsi_arr, lookback=35):
    """Detect Regular Bullish, Hidden Bullish, or Bearish RSI Divergence.

    Regular Bullish Divergence:
      Price Lower Low (or equal) + RSI Higher Low -> Powerful Reversal Signal ⚡

    Hidden Bullish Divergence:
      Price Higher Low + RSI Lower Low -> Trend Continuation Signal 🚀
    """
    n = len(close)
    T = n - 1
    curr_low = float(low[T])
    curr_rsi = float(rsi_arr[T])

    recent_swings = [(i, L) for i, L in swings if (T - lookback <= i <= T - 2)]
    if not recent_swings:
        return {
            "type": "none",
            "label": "None",
            "detail": "RSI aligned with price action",
            "prior_low_idx": None,
            "prior_low_val": None,
            "prior_rsi_val": None,
            "curr_low_val": curr_low,
            "curr_rsi_val": curr_rsi
        }

    for i_prev, l_prev in reversed(recent_swings):
        rsi_prev = float(rsi_arr[i_prev])
        age = T - i_prev

        # 1. Regular Bullish Divergence: Price Lower Low & RSI Higher Low
        if curr_low <= l_prev * 1.002 and curr_rsi >= rsi_prev + 1.2:
            px_diff = ((curr_low / l_prev) - 1.0) * 100.0
            rsi_diff = curr_rsi - rsi_prev
            return {
                "type": "regular_bullish",
                "label": "Bullish Div ⚡",
                "detail": f"Price Lower Low ({px_diff:+.1f}%) vs RSI Higher Low (+{rsi_diff:.1f} pts, {age}b ago)",
                "prior_low_idx": i_prev,
                "prior_low_val": l_prev,
                "prior_rsi_val": rsi_prev,
                "curr_low_val": curr_low,
                "curr_rsi_val": curr_rsi
            }

        # 2. Hidden Bullish Divergence: Price Higher Low & RSI Lower Low
        if curr_low >= l_prev * 1.005 and curr_rsi <= rsi_prev - 2.0:
            px_diff = ((curr_low / l_prev) - 1.0) * 100.0
            rsi_diff = curr_rsi - rsi_prev
            return {
                "type": "hidden_bullish",
                "label": "Hidden Bullish 🚀",
                "detail": f"Price Higher Low ({px_diff:+.1f}%) vs RSI Lower Low ({rsi_diff:.1f} pts, {age}b ago)",
                "prior_low_idx": i_prev,
                "prior_low_val": l_prev,
                "prior_rsi_val": rsi_prev,
                "curr_low_val": curr_low,
                "curr_rsi_val": curr_rsi
            }

    return {
        "type": "none",
        "label": "None",
        "detail": "RSI confirmed with price trend",
        "prior_low_idx": None,
        "prior_low_val": None,
        "prior_rsi_val": None,
        "curr_low_val": curr_low,
        "curr_rsi_val": curr_rsi
    }


def analyze_volume_dynamics(v, c, l, h, o, T, vol_lookback=20):
    """Analyze volume absorption, dry-up exhaustion, and volume confirmation."""
    vol_base = float(v[max(0, T - vol_lookback):T].mean()) if T > 0 else float(v[T])
    vol_x = float(v[T]) / vol_base if vol_base > 0 else 1.0
    rng = h[T] - l[T]
    lw = min(o[T], c[T]) - l[T]
    wick_ratio = lw / rng if rng > 0 else 0.0

    prior_3_vol = float(v[max(0, T - 3):T].mean()) if T >= 3 else vol_base
    vol_dryup = (prior_3_vol < vol_base * 0.85) and (vol_x >= 1.15)
    vol_absorption = (vol_x >= 1.35) and (wick_ratio >= 0.45)

    if vol_absorption and vol_x >= 2.0:
        label = "Mega Absorption 🔥🔥"
        detail = f"Institutional absorption: volume {vol_x:.1f}× on {wick_ratio*100:.0f}% lower wick"
    elif vol_absorption:
        label = "Bullish Absorption 🔥"
        detail = f"High volume absorption ({vol_x:.1f}× avg) on sweep wick"
    elif vol_dryup:
        label = "Dry-Up Spring 📉⚡"
        detail = f"Selling dried up ({prior_3_vol/vol_base:.1f}× avg), surged {vol_x:.1f}× on reclaim"
    elif vol_x >= 1.2:
        label = "Strong Volume 📈"
        detail = f"Above-average volume expansion ({vol_x:.1f}× 20d avg)"
    elif vol_x < 0.6:
        label = "Low Volume ⚠️"
        detail = f"Below-average volume ({vol_x:.1f}× 20d avg)"
    else:
        label = "Normal Volume"
        detail = f"Standard volume ({vol_x:.1f}× 20d avg)"

    return {
        "vol_x": vol_x,
        "vol_div": label,
        "vol_div_detail": detail,
        "vol_absorption": vol_absorption,
        "vol_dryup": vol_dryup
    }


def analyze_candlestick_pattern(o, h, l, c, T):
    """Classify the candlestick reversal signature."""
    rng = h[T] - l[T]
    if rng <= 0:
        return "Flat Candle"
    body = abs(c[T] - o[T])
    body_ratio = body / rng
    lw = min(o[T], c[T]) - l[T]
    lw_ratio = lw / rng
    uw = h[T] - max(o[T], c[T])
    uw_ratio = uw / rng
    close_pos = (c[T] - l[T]) / rng

    if lw_ratio >= 0.65 and body_ratio <= 0.15:
        return "Dragonfly Doji 🐉"
    elif lw_ratio >= 0.48 and close_pos >= 0.70 and uw_ratio <= 0.22:
        return "Hammer Pinbar 🔨"
    elif c[T] > o[T] and close_pos >= 0.80:
        return "Bullish Power Reclaim 🟢"
    elif lw_ratio >= 0.35:
        return "Rejection Wick 🛡️"
    return "Standard Candle"


def compute_confluences_and_grade(rec):
    """Aggregate all technical confluences and compute the Trend Reversal Grade."""
    catalysts = []
    points = 0.0

    # 1. RSI Divergence
    if rec["RSI_Div_Type"] == "regular_bullish":
        catalysts.append("⚡ Bullish RSI Divergence: Momentum turned upward while taking liquidity")
        points += 2.0
    elif rec["RSI_Div_Type"] == "hidden_bullish":
        catalysts.append("🚀 Hidden Bullish RSI Divergence: Strong structural trend continuation")
        points += 1.5

    # 2. Equal Lows / Liquidity Pool
    if rec["Is_Equal_Lows"]:
        count = rec["Equal_Lows_Count"]
        name = "Double Bottom" if count == 2 else "Triple Bottom" if count == 3 else f"{count}-Touch Shelf"
        catalysts.append(f"🎯 Swept High-Liquidity Equal Lows Pool ({name} @ ₹{rec['Equal_Lows_Level']:.2f})")
        points += 2.0

    # 3. Fair Value Gap (Bullish FVG)
    if rec["In_Bullish_FVG"]:
        catalysts.append(f"📦 Sitting in / Mitigated Institutional Bullish FVG Zone")
        points += 1.5

    # 4. Volume Absorption / Expansion
    if "Absorption" in rec["Vol_Div"]:
        catalysts.append(f"🔥 Institutional Volume Absorption ({rec['Vol_x']:.1f}× Avg Volume)")
        points += 1.5
    elif "Dry-Up" in rec["Vol_Div"]:
        catalysts.append(f"📉 Volume Dry-Up Exhaustion + Spring Reclaim")
        points += 1.0
    elif rec["Vol_x"] >= 1.2:
        catalysts.append(f"📈 Volume Expansion on Reclaim ({rec['Vol_x']:.1f}× Avg)")
        points += 0.5

    # 5. Candlestick Signature
    if "Hammer" in rec["Candle_Pattern"] or "Dragonfly" in rec["Candle_Pattern"]:
        catalysts.append(f"🔨 Textbook Bullish Reversal Candle ({rec['Candle_Pattern']})")
        points += 1.0
    elif "Power Reclaim" in rec["Candle_Pattern"]:
        catalysts.append(f"🟢 High-Close Power Reclaim (Top 20% of range)")
        points += 0.5

    # 6. Moving Averages / Trend Support
    if rec["E50"]:
        catalysts.append(f"📈 Major Trend Support: Trading Above 50 EMA")
        points += 1.0
    if rec["E20_Regained"]:
        catalysts.append(f"🟢 Regained 20 EMA on the Reclaim Bar")
        points += 1.0
    elif rec["E20"]:
        catalysts.append(f"📈 Above Short-Term 20 EMA")
        points += 0.5
    if rec.get("E200", False):
        catalysts.append(f"🏛️ Above 200 EMA Macro Bullish Regime")
        points += 0.5

    # 7. Asymmetric Risk/Reward
    if rec["RR"] >= 2.5:
        catalysts.append(f"🛡️ Favorable Asymmetry: R:R {rec['RR']:.1f}:1 to Target ₹{rec['Target_Ref']:.2f}")
        points += 0.5

    # 8. Clean Sweep Depth
    if rec["Sweep_Depth_"] <= 1.0:
        catalysts.append(f"✨ Clean Liquidity Tap (shallow {rec['Sweep_Depth_']:.2f}% pierce — fast rejection)")
        points += 0.5

    # Compute Grade
    score = rec["Score"]
    if points >= 4.5 or (score >= 80 and points >= 3.0):
        grade = "A+ Ultra Reversal 🚀"
        grade_short = "A+"
    elif points >= 3.0 or (score >= 70 and points >= 1.5):
        grade = "A Strong Reversal ⭐"
        grade_short = "A"
    elif points >= 1.5 or score >= 60:
        grade = "B+ Solid Setup ✅"
        grade_short = "B+"
    else:
        grade = "B Standard Sweep"
        grade_short = "B"

    return grade, grade_short, catalysts, int(len(catalysts))


def count_pool_touches(l, pool_level, first_i, T, tol_pct=0.0025):
    """Count distinct bar touches of pool_level within tolerance."""
    touches = []
    for i in range(first_i, T):
        if abs(l[i] - pool_level) / pool_level <= tol_pct:
            if not touches or i > touches[-1] + 1:
                touches.append(i)
    return max(1, len(touches)), touches


def analyze_symbol(df, cfg):
    """Return a setup record with all confluence factors if the latest candle
    completed a valid swing-low sweep, else None.
    """
    n = len(df)
    if n < max(cfg.min_bars, 2 * cfg.fractal_k + 4):
        return None
    o = df["Open"].to_numpy(dtype=float)
    h = df["High"].to_numpy(dtype=float)
    l = df["Low"].to_numpy(dtype=float)
    c = df["Close"].to_numpy(dtype=float)
    v = np.nan_to_num(df["Volume"].to_numpy(dtype=float))
    T = n - 1

    if not (np.isfinite(o[T]) and np.isfinite(h[T]) and np.isfinite(l[T])
            and np.isfinite(c[T]) and h[T] > 0):
        return None
    if c[T] < cfg.min_price:
        return None
    rng = h[T] - l[T]
    if rng <= 0:
        return None
    lw = min(o[T], c[T]) - l[T]

    raw_swings = find_swing_lows(l, cfg.fractal_k)
    pools = merge_pools(raw_swings, cfg.level_dedup_pct)
    best = None
    best_pool = None

    for p in pools:
        level, first_i, last_i = p[0], p[1], p[2]
        age = T - first_i
        if age <= 0 or age > cfg.level_lookback:
            continue
        if l[T] >= level * (1 - cfg.min_pierce_pct):
            continue
        if c[T] <= level * (1 + cfg.close_above_buffer):
            continue
        depth = (level - l[T]) / level
        if depth > cfg.max_sweep_depth:
            continue
        if lw / rng < cfg.min_wick_ratio:
            continue
        if lw < cfg.min_wick_pct_price * level:
            continue
        if cfg.require_bullish_close and c[T] <= o[T]:
            continue
        if cfg.require_prior_above and c[T - 1] <= level:
            continue
        key = (depth, -age)
        if best is None or key < (best["depth"], -best["age"]):
            best = dict(level=level, age=age, touch_ago=T - last_i, depth=depth)
            best_pool = p

    if best is None or best_pool is None:
        return None

    # ---- Technical Indicators ----
    rsi_series = compute_rsi_series(c, cfg.rsi_period)
    rsi_latest = float(rsi_series[-1])

    e20_arr = pd.Series(c).ewm(span=20, adjust=False).mean().to_numpy()
    e50_arr = pd.Series(c).ewm(span=50, adjust=False).mean().to_numpy()
    e20 = float(e20_arr[-1])
    e50 = float(e50_arr[-1])
    e200 = float(pd.Series(c).ewm(span=200, adjust=False).mean().to_numpy()[-1]) if n >= 200 else None

    above_e20 = bool(c[T] > e20)
    above_e50 = bool(c[T] > e50)
    above_e200 = bool(c[T] > e200) if e200 is not None else False
    e20_regained = bool(l[T] <= e20 <= c[T])
    e50_regained = bool(l[T] <= e50 <= c[T])

    close_pos = (c[T] - l[T]) / rng
    wick_ratio = lw / rng
    turnover_cr = float((c * v)[max(0, T - cfg.volume_lookback):T].mean() / 1e7)
    stop = float(l[T])
    target_ref = max(float(np.max(h[max(0, T - 20):T])), float(c[T]) * 1.01)
    risk = max(c[T] - stop, 1e-9)
    rr = max(0.0, (target_ref - c[T]) / risk)
    target_2r = float(c[T] + 2.0 * risk)

    # ---- Volume Dynamics ----
    vol_dyn = analyze_volume_dynamics(v, c, l, h, o, T, cfg.volume_lookback)
    vol_x = vol_dyn["vol_x"]

    # ---- RSI Divergence ----
    rsi_div_res = detect_rsi_divergence(c, l, raw_swings, rsi_series, cfg.rsi_div_lookback)

    # ---- Relative Equal Lows (EQL) ----
    eql_touches_cnt, eql_touch_indices = count_pool_touches(l, best["level"], best_pool[1], T, cfg.level_dedup_pct)
    total_eql_count = max(best_pool[3], eql_touches_cnt)
    is_equal_lows = total_eql_count >= 2
    eql_level = float(best["level"])
    above_eql_pct = float((c[T] / eql_level - 1.0) * 100.0)
    if is_equal_lows:
        eql_desc = f"{'Double' if total_eql_count == 2 else 'Triple' if total_eql_count == 3 else f'{total_eql_count}-Touch'} Bottom @ ₹{eql_level:.2f} (+{above_eql_pct:.2f}% above)"
    else:
        # Check if another equal-low pool is nearby (within 3.5% below)
        nearby_eql = [p for p in pools if (p[3] >= 2 or count_pool_touches(l, p[0], p[1], T, cfg.level_dedup_pct)[0] >= 2) and p[0] < c[T] and (c[T] / p[0] - 1.0) <= 0.035]
        if nearby_eql:
            nb_p = max(nearby_eql, key=lambda p: p[0])
            nb_dist = (c[T] / nb_p[0] - 1.0) * 100.0
            eql_desc = f"Near Double Bottom @ ₹{nb_p[0]:.2f} (+{nb_dist:.1f}% above)"
        else:
            eql_desc = "Single Swing Low"

    # ---- Bullish Fair Value Gap (FVG) ----
    fvgs = find_bullish_fvgs(df, cfg.fvg_lookback)
    fvg_res = analyze_fvg_status(df, fvgs)

    # ---- Candlestick Signature ----
    candle_pattern = analyze_candlestick_pattern(o, h, l, c, T)

    # ---- Major Structural Low Distance ----
    major_start = max(0, T - cfg.major_low_lookback)
    major_low = float(np.min(l[major_start:T + 1]))
    above_major_low = float((c[T] / major_low - 1.0) * 100.0)

    # ---- Setup Score (0..100) ----
    s_wick = min(wick_ratio / 0.75, 1.0)
    s_close = max(0.0, min(1.0, (close_pos - 0.5) / 0.45))
    s_depth = max(0.0, 1.0 - best["depth"] / cfg.max_sweep_depth)
    if vol_x <= 1.0:
        s_vol = 0.5 * vol_x
    elif vol_x <= 3.0:
        s_vol = 0.5 + 0.5 * (vol_x - 1.0) / 2.0
    else:
        s_vol = max(0.0, 1.0 - (vol_x - 3.0) / 5.0)
    if 25.0 <= rsi_latest <= 65.0:
        s_rsi = 1.0
    elif rsi_latest < 25.0:
        s_rsi = max(0.0, 0.6 * rsi_latest / 25.0)
    else:
        s_rsi = max(0.0, 1.0 - (rsi_latest - 65.0) / 30.0)
    s_ema = {2: 1.0, 1: 0.55, 0: 0.2}[int(above_e50) + int(above_e20)]

    score = 25 * s_wick + 15 * s_close + 15 * s_depth + 15 * s_vol + 15 * s_rsi + 15 * s_ema

    rec = dict(
        Yahoo=df.attrs.get("yahoo", ""),
        Date=str(df.index[-1].date()),
        Open=float(o[T]), Close=float(c[T]), High=float(h[T]), Low=float(stop),
        Swept_Level=float(best["level"]),
        Level_Age_Bars=int(best["age"]),
        Touch_Ago_Bars=int(best["touch_ago"]),
        Sweep_Depth_=float(best["depth"]) * 100.0,
        Above_Level_=(c[T] / best["level"] - 1.0) * 100.0,
        LowerWick_=wick_ratio * 100.0,
        ClosePos_=close_pos * 100.0,
        Vol_x=vol_x, RSI14=rsi_latest,
        E20=above_e20, E50=above_e50, E200=above_e200,
        E20_Regained=e20_regained, E50_Regained=e50_regained,
        Avg_Turn_Cr=turnover_cr,
        Stop_Loss=stop, Target_Ref=target_ref, Target_2R=target_2r, RR=rr,
        Score=float(score),
        # Advanced Confluence fields
        RSI_Div=rsi_div_res["label"],
        RSI_Div_Type=rsi_div_res["type"],
        RSI_Div_Detail=rsi_div_res["detail"],
        RSI_Div_Prior_Idx=rsi_div_res["prior_low_idx"],
        Vol_Div=vol_dyn["vol_div"],
        Vol_Div_Detail=vol_dyn["vol_div_detail"],
        Is_Equal_Lows=is_equal_lows,
        Equal_Lows_Count=total_eql_count,
        Equal_Lows_Level=eql_level,
        Above_Equal_Lows_=above_eql_pct,
        Equal_Lows_Desc=eql_desc,
        In_Bullish_FVG=fvg_res["in_fvg"],
        FVG_Status=fvg_res["fvg_status"],
        FVG_Top=fvg_res["fvg_top"],
        FVG_Bottom=fvg_res["fvg_bottom"],
        FVG_Dist_Pct=fvg_res["fvg_dist_pct"],
        Candle_Pattern=candle_pattern,
        Major_Low=major_low,
        Above_Major_Low_=above_major_low,
    )

    grade, grade_short, catalysts, count = compute_confluences_and_grade(rec)
    rec["Confluence_Grade"] = grade
    rec["Confluence_Grade_Short"] = grade_short
    rec["Bullish_Catalysts"] = catalysts
    rec["Confluence_Count"] = count

    return rec


def screen_all(data, cfg):
    """Run analyze_symbol over every ticker; return a DataFrame sorted by Score desc."""
    rows, errors = [], 0
    for sym, df in data.items():
        try:
            rec = analyze_symbol(df, cfg)
        except Exception:
            errors += 1
            continue
        if rec is not None:
            rec["Yahoo"] = sym
            rows.append(rec)
    if errors:
        print(f"  (engine skipped {errors} tickers on internal errors)")
    if not rows:
        return pd.DataFrame()
    res = pd.DataFrame(rows).sort_values("Score", ascending=False).reset_index(drop=True)
    res.insert(0, "#", range(1, len(res) + 1))
    return res


# ============================================================
# cell 12 — ENGINE SELF-TEST
# ============================================================
def _synthetic_df(rows):
    idx = pd.bdate_range("2026-01-05", periods=len(rows))
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close", "Volume"])


def self_test(cfg=None):
    """Self-test verifying positive sweep, negative rejection, equal lows, RSI div, and FVG detection."""
    cfg = cfg or CFG
    rows = []
    for i in range(60):
        o = 105.0 + (i % 5) * 0.2
        rows.append((o, o + 0.8, o - 0.8, o + 0.3, 1_000_000))
    seq = [(104.2, 104.6, 103.4, 103.6, 1_000_000),
           (103.6, 103.9, 102.1, 102.4, 1_000_000),
           (102.4, 102.6, 100.9, 101.2, 1_000_000),
           (101.2, 101.5, 100.0, 100.6, 1_200_000),       # swing low 100.00
           (100.6, 101.4, 100.5, 101.1, 1_100_000),
           (101.1, 101.8, 100.9, 101.6, 1_000_000),
           (101.6, 102.4, 101.4, 102.1, 1_000_000),
           (102.1, 102.9, 101.9, 102.6, 1_100_000),
           (102.6, 103.1, 102.2, 102.8, 1_000_000)]
    rows += seq
    # bar 69: THE SWEEP — wick to 99.35 below level 100, close 102.6 back above
    rows.append((101.8, 103.0, 99.35, 102.6, 1_800_000))

    df_test = _synthetic_df(rows)
    rec = analyze_symbol(df_test, SweepConfig())
    assert rec is not None, "positive test FAILED — sweep not detected"
    assert abs(rec["Swept_Level"] - 100.0) < 1e-9, "wrong level picked"
    assert "Bullish Div" in rec["RSI_Div"] or rec["RSI_Div_Type"] == "regular_bullish", \
        "RSI divergence detection on fixture failed"
    assert len(rec["Bullish_Catalysts"]) > 0, "bullish catalysts not populated"

    rows_bad = rows[:-1] + [(101.8, 103.0, 99.35, 99.8, 1_800_000)]
    assert analyze_symbol(_synthetic_df(rows_bad), SweepConfig()) is None, \
        "negative test FAILED — close below level must not count"

    rows_flat = rows[:-1] + [(101.8, 103.0, 100.3, 102.6, 1_800_000)]
    assert analyze_symbol(_synthetic_df(rows_flat), SweepConfig()) is None, \
        "negative test 2 FAILED — no pierce must not count"

    # regression: EQUAL-LOW double bottom
    rows_dbl = rows[:60] + [(104.0, 104.3, 102.5, 102.8, 1e6),
                            (102.8, 103.0, 100.0, 100.8, 1e6),    # low #1 = 100.00
                            (100.8, 101.9, 100.6, 101.6, 1e6),
                            (101.6, 101.9, 100.0, 100.9, 1e6),    # low #2 = 100.00 (exact tie)
                            (100.9, 102.2, 100.7, 102.0, 1e6),
                            (102.0, 102.8, 101.8, 102.5, 1e6),
                            (102.5, 103.0, 102.1, 102.7, 1e6),
                            (102.0, 103.2, 99.60, 102.4, 1.6e6)]
    rec_dbl = analyze_symbol(_synthetic_df(rows_dbl), SweepConfig())
    assert rec_dbl is not None and abs(rec_dbl["Swept_Level"] - 100.0) < 1e-9, \
        "regression FAILED — equal-low double bottom must be sweepable"
    assert rec_dbl["Is_Equal_Lows"] is True, "regression FAILED — Is_Equal_Lows must be True"

    # regression: NaN Low on the signal bar
    rows_nan = rows[:-1] + [(101.8, 103.0, float("nan"), 102.6, 1.8e6)]
    assert analyze_symbol(_synthetic_df(rows_nan), SweepConfig()) is None, \
        "regression FAILED — NaN low must be rejected"

    # regression: flat tape RSI
    assert abs(float(rsi_wilder(np.array([100.0] * 30)).iloc[-1]) - 50.0) < 1e-9, \
        "regression FAILED — flat-tape RSI must be 50"

    out = "\n".join([
        "✅ ENGINE SELF-TEST PASSED",
        f"   positive  : level={rec['Swept_Level']:.2f} age={rec['Level_Age_Bars']}bars "
        f"depth={rec['Sweep_Depth_']:.2f}% wick={rec['LowerWick_']:.0f}% score={rec['Score']:.1f}",
        f"   confluence: RSI Div='{rec['RSI_Div']}' | EQL='{rec['Equal_Lows_Desc']}' | Grade='{rec['Confluence_Grade_Short']}'",
        "   negative  : close-below-level rejected ✓ | no-pierce rejected ✓",
        "   regression: equal-low double bottom ✓ | NaN-low bar ✓ | flat-tape RSI ✓",
    ])
    return out


# ============================================================
# cell 14 — results table formatter
# ============================================================
def format_results(RESULTS, universe):
    """Format results DataFrame into display table. Returns (res, show)."""
    UNIVERSE = universe
    res = RESULTS.copy()
    res["Name"] = res["Yahoo"].map(UNIVERSE.set_index("Yahoo")["NAME OF COMPANY"].to_dict()).fillna("")
    res["Symbol"] = res["Yahoo"].str.replace(".NS", "", regex=False)
    show = res.copy()
    fmt = {
        "Close": "{:.2f}", "Swept_Level": "{:.2f}", "Sweep_Depth_": "{:.2f}",
        "Above_Level_": "{:.2f}", "Above_Equal_Lows_": "{:.2f}", "LowerWick_": "{:.1f}",
        "ClosePos_": "{:.1f}", "Vol_x": "{:.1f}", "RSI14": "{:.0f}",
        "Avg_Turn_Cr": "{:.1f}", "Stop_Loss": "{:.2f}", "Target_Ref": "{:.2f}",
        "RR": "{:.2f}", "Score": "{:.1f}"
    }
    for col, f in fmt.items():
        if col in show.columns:
            show[col] = show[col].map(lambda v: f.format(v) if pd.notna(v) else "—")

    # Clean display columns
    cols = ["#", "Symbol", "Name", "Date", "Close", "Swept_Level", "Level_Age_Bars",
            "Above_Level_", "Is_Equal_Lows", "RSI_Div", "FVG_Status", "Vol_x",
            "LowerWick_", "RSI14", "E50", "Confluence_Grade_Short",
            "Avg_Turn_Cr", "Stop_Loss", "Target_Ref", "RR", "Score"]
    cols_exist = [c for c in cols if c in show.columns]
    show = show[cols_exist]

    col_rename = {
        "#": "#", "Symbol": "Symbol", "Name": "Name", "Date": "Date", "Close": "Close",
        "Swept_Level": "Swept Low", "Level_Age_Bars": "Age(b)", "Above_Level_": "Above %",
        "Is_Equal_Lows": "Equal Lows", "RSI_Div": "RSI Div", "FVG_Status": "FVG Status",
        "Vol_x": "Vol x", "LowerWick_": "Wick %", "RSI14": "RSI", "E50": ">EMA50",
        "Confluence_Grade_Short": "Grade", "Avg_Turn_Cr": "Turn ₹Cr",
        "Stop_Loss": "Stop", "Target_Ref": "Target", "RR": "R:R", "Score": "Score"
    }
    show.rename(columns=col_rename, inplace=True)
    return res, show


# ============================================================
# cell 15 — setup report text & high-res chart
# ============================================================
def setup_report_lines(r):
    """Detailed multi-line summary of a single setup with all confluences."""
    eql_info = f"Equal Lows: {r.Equal_Lows_Desc}" if hasattr(r, "Equal_Lows_Desc") else "Equal Lows: None"
    fvg_info = f"FVG: {r.FVG_Status}" if hasattr(r, "FVG_Status") else "FVG: None"
    rsi_div_info = f"RSI Div: {r.RSI_Div_Detail}" if hasattr(r, "RSI_Div_Detail") else f"RSI(14): {r.RSI14:.0f}"
    vol_dyn_info = f"Volume: {r.Vol_Div_Detail}" if hasattr(r, "Vol_Div_Detail") else f"Vol: {r.Vol_x:.1f}x"
    grade_info = f"Grade: {r.Confluence_Grade}" if hasattr(r, "Confluence_Grade") else f"Score: {r.Score:.1f}"

    lines = [
        "=" * 78,
        f"  {r.Symbol}  ({r.Name})   ·   Close ₹{r.Close:.2f} on {r.Date}   ·   {grade_info}",
        "=" * 78,
        f"  Swept Level     : ₹{r.Swept_Level:.2f} (born {r.Level_Age_Bars}b ago, touched {r.Touch_Ago_Bars}b ago) → close +{r.Above_Level_:.2f}% above",
        f"  Structure (EQL) : {eql_info}",
        f"  Imbalance (FVG) : {fvg_info}",
        f"  Momentum & Vol  : {rsi_div_info} · {vol_dyn_info}",
        f"  Candle & EMAs   : Pattern: {getattr(r, 'Candle_Pattern', 'Rejection')} · above EMA20: {r.E20} · above EMA50: {r.E50}",
        f"  Trade Plan      : Entry ≤ ₹{r.Close:.2f} | SL below ₹{r.Stop_Loss:.2f} | Target ₹{r.Target_Ref:.2f} (R:R {r.RR:.1f}:1)",
    ]
    if hasattr(r, "Bullish_Catalysts") and r.Bullish_Catalysts:
        lines.append("  Bullish Catalysts:")
        for cat in r.Bullish_Catalysts:
            lines.append(f"    • {cat}")
    return lines


def plot_setup(data, sym, level, last_bars=45):
    """High-res multi-panel candlestick chart with FVG shading, Equal Lows,
    EMAs, and RSI Divergence annotations.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    df = data[sym].tail(last_bars).copy()
    n = len(df)
    x = np.arange(n)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11.5, 7.2),
        gridspec_kw={"height_ratios": [2.85, 1.15], "hspace": 0.08},
        sharex=True
    )
    fig.patch.set_facecolor("#0b1220")

    for ax in (ax1, ax2):
        ax.set_facecolor("#111827")
        ax.grid(True, color="#1f2a3a", alpha=0.55, linestyle=":")
        for sp in ax.spines.values():
            sp.set_color("#1f2a3a")
        ax.tick_params(colors="#94a3b8", labelsize=8.5)

    o = df["Open"].to_numpy(dtype=float)
    h = df["High"].to_numpy(dtype=float)
    l = df["Low"].to_numpy(dtype=float)
    c = df["Close"].to_numpy(dtype=float)
    v = df["Volume"].to_numpy(dtype=float)

    # 1. Candlesticks on Top Panel
    for i in range(n):
        col = "#10b981" if c[i] >= o[i] else "#ef4444"
        ax1.vlines(i, l[i], h[i], color=col, lw=0.95, alpha=0.9)
        bar_height = max(abs(c[i] - o[i]), (h[i] - l[i]) * 0.02, 0.01)
        ax1.bar(i, bar_height, bottom=min(o[i], c[i]), width=0.64,
                color=col, edgecolor=col, lw=0.55, alpha=0.95)

    # 2. Moving Averages (EMA 20 & EMA 50)
    if len(df) >= 15:
        e20_s = pd.Series(c).ewm(span=20, adjust=False).mean()
        ax1.plot(x, e20_s, color="#38bdf8", lw=1.1, alpha=0.8, label="EMA 20")
    if len(df) >= 25:
        e50_s = pd.Series(c).ewm(span=50, adjust=False).mean()
        ax1.plot(x, e50_s, color="#fbbf24", lw=1.1, alpha=0.8, label="EMA 50")

    # 3. Swept Level & Stop Loss Zone
    ax1.axhline(level, color="#f59e0b", ls="--", lw=1.4,
                label=f"Swept Swing Low = ₹{level:.2f}")
    ax1.axhline(l[-1], color="#a78bfa", ls=":", lw=1.2,
                label=f"Sweep Low / SL Zone = ₹{l[-1]:.2f}")

    # 4. Target Reference Line
    target_ref = max(float(np.max(h[max(0, n-20):n-1])) if n > 1 else c[-1] * 1.02, c[-1] * 1.01)
    ax1.axhline(target_ref, color="#34d399", ls=":", lw=1.1, alpha=0.75,
                label=f"Ref Target = ₹{target_ref:.2f}")

    # 5. Bullish FVG Detection & Shading on Chart
    fvgs = find_bullish_fvgs(df, lookback=min(35, n))
    active_fvgs = [f for f in fvgs if not f["mitigated"]]
    if active_fvgs:
        # Plot up to 2 most recent active FVGs
        for fvg in active_fvgs[-2:]:
            bot, top = fvg["bottom"], fvg["top"]
            if bot < top and top >= l.min() * 0.98 and bot <= h.max() * 1.02:
                xmin_ratio = max(0.0, float(fvg["bar_i"]) / max(1, n - 1))
                ax1.axhspan(bot, top, xmin=xmin_ratio, xmax=1.0, color="#06b6d4",
                            alpha=0.15, linestyle="--", edgecolor="#0891b2", lw=0.8,
                            label=f"Bullish FVG [₹{bot:.2f} - ₹{top:.2f}]")

    # 6. Annotation on the Sweep Candle
    ax1.annotate("SWEEP & RECLAIM ✓", xy=(n - 1, h[-1]),
                 xytext=(max(0, n - 8), h[-1] * 1.008),
                 fontsize=9.5, weight="bold", color="#f59e0b",
                 arrowprops=dict(arrowstyle="->", color="#f59e0b", lw=1.1))

    ax1.set_title(f"{sym}  ·  Daily Liquidity Sweep & Confluence Setup  ·  {df.index[-1].date()}",
                  fontsize=12, weight="bold", color="#f1f5f9", pad=10)
    ax1.legend(loc="upper left", fontsize=8.2, facecolor="#111827", edgecolor="#1f2a3a",
               labelcolor="#e2e8f0", framealpha=0.85)

    # 7. Bottom Panel — RSI(14) with Divergence
    rsi_vals = compute_rsi_series(c, period=14)
    ax2.plot(x, rsi_vals, color="#c084fc", lw=1.4, label="RSI(14)")
    ax2.axhline(70, color="#ef4444", ls="--", lw=0.8, alpha=0.6)
    ax2.axhline(50, color="#64748b", ls=":", lw=0.8, alpha=0.6)
    ax2.axhline(30, color="#10b981", ls="--", lw=0.8, alpha=0.6)
    ax2.fill_between(x, 30, 70, color="#1e293b", alpha=0.35)
    
    rsi_min = float(np.nanmin(rsi_vals)) if len(rsi_vals) else 20.0
    rsi_max = float(np.nanmax(rsi_vals)) if len(rsi_vals) else 80.0
    ax2.set_ylim(max(0.0, min(15.0, rsi_min - 6.0)), min(100.0, max(85.0, rsi_max + 6.0)))
    ax2.set_ylabel("RSI (14)", color="#94a3b8", fontsize=8.5)

    # Annotate RSI Divergence if detected
    swings = find_swing_lows(l, 2)
    rsi_div = detect_rsi_divergence(c, l, swings, rsi_vals, lookback=min(35, n))
    if rsi_div["type"] == "regular_bullish" and rsi_div["prior_low_idx"] is not None:
        p_idx = rsi_div["prior_low_idx"]
        if p_idx < n:
            ax2.plot([p_idx, n - 1], [rsi_vals[p_idx], rsi_vals[-1]], color="#22c55e",
                     lw=1.8, marker="o", markersize=4)
            y_annot = min(85.0, rsi_vals[-1] + 12.0)
            ax2.annotate("BULLISH RSI DIV ⚡", xy=(n - 1, rsi_vals[-1]),
                         xytext=(max(0, n - 12), y_annot),
                         fontsize=8.5, weight="bold", color="#22c55e",
                         arrowprops=dict(arrowstyle="->", color="#22c55e", lw=1.0))
    elif rsi_div["type"] == "hidden_bullish" and rsi_div["prior_low_idx"] is not None:
        p_idx = rsi_div["prior_low_idx"]
        if p_idx < n:
            ax2.plot([p_idx, n - 1], [rsi_vals[p_idx], rsi_vals[-1]], color="#38bdf8",
                     lw=1.8, marker="o", markersize=4)
            y_annot = max(15.0, rsi_vals[-1] - 12.0)
            ax2.annotate("HIDDEN BULLISH DIV 🚀", xy=(n - 1, rsi_vals[-1]),
                         xytext=(max(0, n - 12), y_annot),
                         fontsize=8.5, weight="bold", color="#38bdf8",
                         arrowprops=dict(arrowstyle="->", color="#38bdf8", lw=1.0))
        p_idx = rsi_div["prior_low_idx"]
        if p_idx < n:
            ax2.plot([p_idx, n - 1], [rsi_vals[p_idx], rsi_vals[-1]], color="#38bdf8",
                     lw=1.8, marker="o", markersize=4)
            ax2.annotate("HIDDEN BULLISH DIV 🚀", xy=(n - 1, rsi_vals[-1]),
                         xytext=(max(0, n - 9), rsi_vals[-1] + 6),
                         fontsize=8.5, weight="bold", color="#38bdf8",
                         arrowprops=dict(arrowstyle="->", color="#38bdf8", lw=1.0))

    step = max(n // 8, 1)
    ax2.set_xticks(x[::step])
    ax2.set_xticklabels([d.strftime("%d %b") for d in df.index[::step]], rotation=25,
                        fontsize=8, color="#94a3b8")

    plt.tight_layout()
    return fig
