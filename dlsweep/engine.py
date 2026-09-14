"""
dlsweep/engine.py — the NSE DAILY Liquidity-Sweep Screener engine.

Every function below is copied VERBATIM from NSE_Liquidity_Sweep_Screener_FIXED.ipynb
(cell numbers noted above each block) so an unattended GitHub Actions run produces
EXACTLY the result you see when you press "Run all" in Colab. Only two mechanical
adaptations exist, and neither can change a number:

  1. The one-line typo in the data-health gate of the original GitHub upload
     (`if CFG.require_latest_s sessession:`) is repaired to
     `if CFG.require_latest_session:` — as committed that line is a SyntaxError,
     and the repaired text is what the working Colab copy of the notebook has.
  2. Notebook cell top-level statements are wrapped in functions
     (rank_universe / fetch_history / self_test / format_results), and
     plot_setup() takes the DATA dict as an argument and returns the figure
     instead of calling plt.show() — same drawing code, headless backend.

Notebook map:
  cell 1   imports
  cell 3   SweepConfig (dataclass, all knobs)
  cell 5   NSE universe fetch (+embedded fallback list)
  cell 8   chunked_download() + turnover ranking
  cell 9   backfill_latest() + full-history download + data-health gate
  cell 11  sweep engine + scoring
  cell 12  self-test (synthetic candles)
  cell 14  results-table formatter
  cell 15  setup_report text + plot_setup chart
"""

# ============================================================
# cell 1 — imports (pip installs are handled by requirements.txt in Actions)
# ============================================================
import random
import time
import warnings

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ============================================================
# cell 3 — CONFIGURATION (verbatim)
# ============================================================
from dataclasses import dataclass


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
    # ---- Scoring ----
    volume_lookback     : int   = 20
    rsi_period          : int   = 14


CFG = SweepConfig()


def describe_config(cfg=None):
    """The exact banner cell 3 prints in the notebook."""
    cfg = cfg or CFG
    lines = [
        "Active rules on the LATEST daily candle",
        "-" * 62,
        f"  Swing low           : 5-bar fractal, up to {cfg.level_lookback} bars old (old or new)",
        f"  Sweep pierce        : low >= {cfg.min_pierce_pct:.2%} below level, no deeper than {cfg.max_sweep_depth:.1%}",
        f"  Reclaim             : close >= {cfg.close_above_buffer:.2%} ABOVE level",
        f"  Proper wick         : lower wick >= {cfg.min_wick_ratio:.0%} of range AND >= {cfg.min_wick_pct_price:.2%} of price",
        f"  Bullish close       : {cfg.require_bullish_close}   |   Prior close above level: {cfg.require_prior_above}",
        f"  Universe            : top {cfg.universe_size} by turnover (>= ₹{cfg.min_avg_turnover_cr} Cr/day), price >= ₹{cfg.min_price:.0f}",
    ]
    return "\n".join(lines)


# ============================================================
# cell 5 — UNIVERSE (verbatim)
# ============================================================
import io

import requests

NSE_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

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
    try:
        r = requests.get(NSE_LIST_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.strip() for c in df.columns]
        df = df[df["SYMBOL"].notna() & (df["SYMBOL"].astype(str).str.len() > 0)]
        df = df[["SYMBOL", "NAME OF COMPANY"]].copy()
        df["SYMBOL"] = df["SYMBOL"].astype(str).str.upper().str.strip()
        df = df.drop_duplicates("SYMBOL").reset_index(drop=True)
        src = "NSE official archive"
        print(f"  ✓ fetched full NSE list ({len(df):,} symbols) from {src}")
    except Exception as exc:
        print(f"  ⚠ NSE list fetch failed ({exc.__class__.__name__}: {exc})")
        print(f"  → falling back to embedded list of {len(FALLBACK_SYMBOLS)} liquid names")
        df = pd.DataFrame({"SYMBOL": FALLBACK_SYMBOLS, "NAME OF COMPANY": ""})
        src = "embedded fallback"
    df["Yahoo"] = df["SYMBOL"].str.upper() + ".NS"
    df["source"] = src
    return df


# ============================================================
# cell 8 — chunked downloader (verbatim) + turnover ranking
# ============================================================
def chunked_download(symbols, period, chunk=None, label="download"):
    """Chunked yfinance download with per-chunk retry + exponential backoff.

    Returns (dict {yahoo_symbol: OHLCV DataFrame}, list_of_failed_symbols).
    A single bad chunk never aborts the rest of the run.
    """
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
                continue  # symbol missing from response (delisted/renamed/404)
            except TypeError:
                # [fix] older yfinance returned a FLAT frame for a 1-symbol request —
                # xs() then raised TypeError and the symbol was silently dropped.
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
    """cell 8, second half (verbatim): rank all of NSE by avg daily turnover.

    Returns (UNIVERSE, stats_dict).
    """
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
# cell 9 — latest-session backfill (verbatim) + history + health gate
# ============================================================
import datetime as _dt


def backfill_latest(symbols, target_date, chunk=40):
    """Rebuild missing latest-session daily candles from 15-min data (Close = final trade).

    Yahoo frequently leaves Close=NaN on the newest NSE daily bar; the 15-min feed has the
    full session. Daily O/H/L and the 15-min aggregates agree to tick precision.
    """
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
                ok = intr is not None and len(intr) > 0
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
                i = intr.xs(sym, axis=1, level="Ticker").dropna(subset=["Close"])
            except (KeyError, ValueError):
                continue
            except TypeError:
                # [fix] same 1-symbol flat-frame recovery as chunked_download
                if len(part) != 1 or isinstance(intr.columns, pd.MultiIndex):
                    continue
                i = intr.dropna(subset=["Close"])
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
    """cell 9, download + health gate (verbatim). Returns (DATA, health_text, stats)."""
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
        if CFG.require_latest_session:                       # (typo repaired here — see module docstring)
            if df.index[-1].date() < max_last:
                stale_list.append(sym); continue   # missing latest session (Yahoo data lag)
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
# cell 11 — SWEEP ENGINE (verbatim)
# ============================================================
def find_swing_lows(low, k):
    """Indices i where low[i] is the UNIQUE minimum of low[i-k .. i+k] (fractal swing low)."""
    n = len(low)
    out = []
    for i in range(k, n - k):
        w = low[i - k:i + k + 1]
        if low[i] <= w.min() and np.count_nonzero(w == low[i]) == 1:
            out.append((i, float(low[i])))
    return out


def merge_pools(swings, dedup_pct):
    """Merge nearly-equal swing levels into liquidity pools: [level, first_bar, last_bar]."""
    pools = []
    for i, L in swings:
        for p in pools:
            if abs(L - p[0]) / p[0] <= dedup_pct:
                p[0] = min(p[0], L)
                p[2] = max(p[2], i)
                break
        else:
            pools.append([L, i, i])
    return pools


def rsi_wilder(close, period=14):
    """Wilder RSI series (aligned to close[1:])."""
    delta = np.diff(close)
    gain = pd.Series(np.clip(delta, 0, None)).ewm(alpha=1 / period, adjust=False).mean()
    loss = pd.Series(np.clip(-delta, 0, None)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = (100 - 100 / (1 + rs)).fillna(100.0)
    return out


def analyze_symbol(df, cfg):
    """Return a setup record if the latest candle of df completed a valid swing-low sweep, else None."""
    n = len(df)
    if n < max(cfg.min_bars, 2 * cfg.fractal_k + 4):
        return None
    o = df["Open"].to_numpy(dtype=float)
    h = df["High"].to_numpy(dtype=float)
    l = df["Low"].to_numpy(dtype=float)
    c = df["Close"].to_numpy(dtype=float)
    v = df["Volume"].to_numpy(dtype=float)
    T = n - 1
    if not (np.isfinite(o[T]) and np.isfinite(c[T]) and h[T] > 0):
        return None
    if c[T] < cfg.min_price:
        return None
    rng = h[T] - l[T]
    if rng <= 0:
        return None
    lw = min(o[T], c[T]) - l[T]

    pools = merge_pools(find_swing_lows(l, cfg.fractal_k), cfg.level_dedup_pct)
    best = None
    for level, first_i, last_i in pools:
        age = T - first_i
        if age <= 0 or age > cfg.level_lookback:
            continue
        if l[T] >= level * (1 - cfg.min_pierce_pct):          # no genuine pierce
            continue
        if c[T] <= level * (1 + cfg.close_above_buffer):       # no reclaim above
            continue
        depth = (level - l[T]) / level
        if depth > cfg.max_sweep_depth:                        # too deep = breakdown
            continue
        if lw / rng < cfg.min_wick_ratio:                      # no proper wick
            continue
        if lw < cfg.min_wick_pct_price * level:
            continue
        if cfg.require_bullish_close and c[T] <= o[T]:
            continue
        if cfg.require_prior_above and c[T - 1] <= level:      # was already below → not a sweep
            continue
        key = (depth, -age)
        if best is None or key < (best["depth"], -best["age"]):
            best = dict(level=level, age=age, touch_ago=T - last_i, depth=depth)
    if best is None:
        return None

    vol_base = float(v[T - cfg.volume_lookback:T].mean())
    vol_x = float(v[T]) / vol_base if vol_base > 0 else 1.0
    rsi = float(rsi_wilder(c, cfg.rsi_period).iloc[-1])
    e20 = float(pd.Series(c).ewm(span=20, adjust=False).mean().iloc[-1])
    e50 = float(pd.Series(c).ewm(span=50, adjust=False).mean().iloc[-1])
    close_pos = (c[T] - l[T]) / rng
    wick_ratio = lw / rng
    turnover_cr = float((c * v)[T - cfg.volume_lookback:T].mean() / 1e7)
    stop = float(l[T])
    target_ref = max(float(np.max(h[T - 20:T])), float(c[T]) * 1.01)
    rr = max(0.0, (target_ref - c[T]) / max(c[T] - stop, 1e-9))

    # ---- score components (each 0..1) ----
    s_wick = min(wick_ratio / 0.75, 1.0)
    s_close = max(0.0, min(1.0, (close_pos - 0.5) / 0.45))
    s_depth = max(0.0, 1.0 - best["depth"] / cfg.max_sweep_depth)
    if vol_x <= 1.0:
        s_vol = 0.5 * vol_x
    elif vol_x <= 3.0:
        s_vol = 0.5 + 0.5 * (vol_x - 1.0) / 2.0
    else:
        s_vol = max(0.0, 1.0 - (vol_x - 3.0) / 5.0)
    if 25.0 <= rsi <= 65.0:
        s_rsi = 1.0
    elif rsi < 25.0:
        s_rsi = max(0.0, 0.6 * rsi / 25.0)
    else:
        s_rsi = max(0.0, 1.0 - (rsi - 65.0) / 30.0)
    s_ema = {2: 1.0, 1: 0.55, 0: 0.2}[(c[T] > e50) + (c[T] > e20)]

    score = 25 * s_wick + 15 * s_close + 15 * s_depth + 15 * s_vol + 15 * s_rsi + 15 * s_ema

    return dict(
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
        Vol_x=vol_x, RSI14=rsi,
        E20=bool(c[T] > e20), E50=bool(c[T] > e50),
        Avg_Turn_Cr=turnover_cr,
        Stop_Loss=stop, Target_Ref=target_ref, RR=rr,
        Score=float(score),
    )


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
# cell 12 — ENGINE SELF-TEST (verbatim, as a function)
# ============================================================
def _synthetic_df(rows):
    idx = pd.bdate_range("2026-01-05", periods=len(rows))
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close", "Volume"])


def self_test(cfg=None):
    """cell 12, verbatim. Raises AssertionError if the engine logic drifts. Returns the print text."""
    cfg = cfg or CFG
    rows = []
    for i in range(60):                                   # calm drift around 105
        o = 105.0 + (i % 5) * 0.2
        rows.append((o, o + 0.8, o - 0.8, o + 0.3, 1_000_000))
    seq = [(104.2, 104.6, 103.4, 103.6, 1_000_000),       # decline into the swing low
           (103.6, 103.9, 102.1, 102.4, 1_000_000),
           (102.4, 102.6, 100.9, 101.2, 1_000_000),
           (101.2, 101.5, 100.0, 100.6, 1_200_000),       # <-- swing low 100.00 (fractal)
           (100.6, 101.4, 100.5, 101.1, 1_100_000),
           (101.1, 101.8, 100.9, 101.6, 1_000_000),
           (101.6, 102.4, 101.4, 102.1, 1_000_000),
           (102.1, 102.9, 101.9, 102.6, 1_100_000),
           (102.6, 103.1, 102.2, 102.8, 1_000_000)]       # bars 60..68
    rows += seq
    # bar 69: THE SWEEP — wick to 99.35 below level 100, close 102.6 back above
    rows.append((101.8, 103.0, 99.35, 102.6, 1_800_000))

    rec = analyze_symbol(_synthetic_df(rows), SweepConfig())
    assert rec is not None, "positive test FAILED — sweep not detected"
    assert abs(rec["Swept_Level"] - 100.0) < 1e-9, "wrong level picked"

    rows_bad = rows[:-1] + [(101.8, 103.0, 99.35, 99.8, 1_800_000)]   # close BELOW level
    assert analyze_symbol(_synthetic_df(rows_bad), SweepConfig()) is None, \
        "negative test FAILED — close below level must not count"

    rows_flat = rows[:-1] + [(101.8, 103.0, 100.3, 102.6, 1_800_000)]  # no pierce at all
    assert analyze_symbol(_synthetic_df(rows_flat), SweepConfig()) is None, \
        "negative test 2 FAILED — no pierce must not count"

    out = "\n".join([
        "✅ ENGINE SELF-TEST PASSED",
        f"   positive  : level={rec['Swept_Level']:.2f} age={rec['Level_Age_Bars']}bars "
        f"depth={rec['Sweep_Depth_']:.2f}% wick={rec['LowerWick_']:.0f}% score={rec['Score']:.1f}",
        "   negative  : close-below-level rejected ✓ | no-pierce rejected ✓",
    ])
    return out


# ============================================================
# cell 14 — results table formatter (verbatim, as a function)
# ============================================================
def format_results(RESULTS, universe):
    """cell 14 else-branch, verbatim. Returns (res, show) — `show` is the exact table Colab displays."""
    UNIVERSE = universe
    res = RESULTS.copy()
    res["Name"] = res["Yahoo"].map(UNIVERSE.set_index("Yahoo")["NAME OF COMPANY"].to_dict()).fillna("")
    res["Symbol"] = res["Yahoo"].str.replace(".NS", "", regex=False)
    show = res.copy()
    fmt = {"Close": "{:.2f}", "Swept_Level": "{:.2f}", "Sweep_Depth_": "{:.2f}",
           "Above_Level_": "{:.2f}", "LowerWick_": "{:.1f}", "ClosePos_": "{:.1f}",
           "Vol_x": "{:.1f}", "RSI14": "{:.0f}", "Avg_Turn_Cr": "{:.1f}",
           "Stop_Loss": "{:.2f}", "Target_Ref": "{:.2f}", "RR": "{:.2f}", "Score": "{:.1f}"}
    for col, f in fmt.items():
        show[col] = show[col].map(f.format)
    cols = ["#", "Symbol", "Name", "Date", "Close", "Swept_Level", "Level_Age_Bars",
            "Sweep_Depth_", "Above_Level_", "LowerWick_", "ClosePos_", "Vol_x",
            "RSI14", "E50", "Avg_Turn_Cr", "Stop_Loss", "Target_Ref", "RR", "Score"]
    show = show[cols]
    show.columns = ["#", "Symbol", "Name", "Date", "Close", "Swept Low", "Age(bars)",
                    "Depth %", "Above %", "Wick %", "ClosePos %", "Vol x",
                    "RSI", ">EMA50", "Turn ₹Cr", "Stop", "Target", "R:R", "Score"]
    return res, show


# ============================================================
# cell 15 — top-picks detail text + chart (verbatim, headless-adapted)
# ============================================================
def setup_report_lines(r):
    """cell 15 setup_report(), verbatim — returned as a list of lines instead of printed."""
    return [
        "=" * 74,
        f"  {r.Symbol}  ({r.Name})   ·   close {r.Close:.2f} on {r.Date}",
        "=" * 74,
        f"  Swept swing low : {r.Swept_Level:.2f}   (born {r.Level_Age_Bars} bars ago, "
        f"last touched {r.Touch_Ago_Bars} bars ago)",
        f"  Wick pierce     : low {r.Sweep_Depth_:.2f}% below level  →  close {r.Above_Level_:.2f}% ABOVE level",
        f"  Wick quality    : lower wick {r.LowerWick_:.0f}% of range · close at {r.ClosePos_:.0f}% of range",
        f"  Context         : volume {r.Vol_x:.1f}x 20d avg · RSI(14) {r.RSI14:.0f} · "
        f"above EMA20: {r.E20} · above EMA50: {r.E50}",
        f"  Trade plan      : entry ≤ {r.Close:.2f} (or next open) | SL below {r.Stop_Loss:.2f} | "
        f"ref target {r.Target_Ref:.2f} (R:R {r.RR:.1f})",
    ]


def plot_setup(data, sym, level, last_bars=45):
    """cell 15 plot_setup(), verbatim drawing — takes the data dict, returns the figure."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    df = data[sym].tail(last_bars)
    x = np.arange(len(df))
    fig, ax = plt.subplots(figsize=(11, 5.8))
    for i, (oo, hh, ll, cc) in enumerate(zip(df["Open"], df["High"], df["Low"], df["Close"])):
        col = "#1a7f37" if cc >= oo else "#c62828"
        ax.vlines(i, ll, hh, color=col, lw=0.9)
        ax.bar(i, max(abs(cc - oo), 0.01), bottom=min(oo, cc), width=0.62,
               color=col, edgecolor=col, lw=0.5)
    ax.axhline(level, color="#e65100", ls="--", lw=1.4, label=f"swept swing low = {level:.2f}")
    ax.axhline(df["Low"].iloc[-1], color="#6a1b9a", ls=":", lw=1.1,
               label=f"sweep low / SL zone = {df['Low'].iloc[-1]:.2f}")
    ax.annotate("SWEEP ✓", xy=(len(df) - 1, df["High"].iloc[-1]),
                xytext=(len(df) - 7, df["High"].iloc[-1]),
                fontsize=10, weight="bold", color="#e65100")
    step = max(len(df) // 8, 1)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([d.strftime("%d %b") for d in df.index[::step]], rotation=25, fontsize=8)
    ax.set_title(f"{sym}  ·  daily liquidity sweep of swing low  ·  {df.index[-1].date()}",
                 fontsize=12, weight="bold")
    ax.grid(alpha=0.25)
    ax.legend(loc="upper left", fontsize=9)
    plt.tight_layout()
    return fig
