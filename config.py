"""
config.py — knobs for the NSE DAILY Liquidity-Sweep Screener automation.

`CONFIG` mirrors the notebook's SweepConfig defaults VERBATIM (cell 3 of
NSE_Liquidity_Sweep_Screener_FIXED.ipynb) so the unattended run reproduces the
Colab result exactly. Change a value here and the 19:30 IST job picks it up —
no engine edits needed. Leave it untouched and it matches the notebook 1:1.

`REPORT` only affects presentation (email/report), never the screening logic.
"""

CONFIG = {
    # ---- Universe ----
    "universe_size": 1000,          # NSE top-N by avg daily turnover
    "min_avg_turnover_cr": 0.25,    # min ₹ Cr avg daily turnover to stay in universe
    "rank_bars_needed": 8,          # min bars to be eligible for turnover ranking
    # ---- Data ----
    "history_period": "1y",         # ~250 daily bars of history
    "min_bars": 60,                 # drop tickers with thinner history
    "require_latest_session": True, # screen ONLY candles of the market's latest session
    "backfill_latest_close": True,  # rebuild a missing latest-session candle from 15-min data
    "max_staleness_days": 4,        # (used only if require_latest_session=False)
    "download_chunk": 80,           # symbols per Yahoo request
    "max_retries": 3,               # retries per chunk
    "backoff_secs": 4.0,            # base backoff (doubles per retry)
    # ---- Swing low (liquidity level) detection ----
    "fractal_k": 2,                 # 5-bar fractal: low = min of [i-2 .. i+2]
    "level_lookback": 120,          # level may be up to N bars old (old OR new)
    "level_dedup_pct": 0.0015,      # levels within 0.15% = same liquidity pool
    # ---- Sweep conditions (hard filters on latest candle) ----
    "min_pierce_pct": 0.0005,       # low must pierce >= 0.05% below level
    "close_above_buffer": 0.001,    # close must be >= 0.1% above level
    "max_sweep_depth": 0.025,       # wick deeper than 2.5% below level = breakdown, reject
    "min_wick_ratio": 0.30,         # lower wick >= 30% of candle range ("proper wick")
    "min_wick_pct_price": 0.0015,   # and >= 0.15% of price (ignore micro-noise)
    "require_bullish_close": True,  # close > open
    "require_prior_above": True,    # previous close above the level (true sweep)
    "min_price": 10.0,              # ignore sub-₹10 names
    # ---- Scoring ----
    "volume_lookback": 20,
    "rsi_period": 14,
}

REPORT = {
    "max_charts": 25,        # cap on sweep charts in the report (setups are usually 0–15)
    "chart_last_bars": 45,   # candles per chart — same as the notebook's plot_setup default
    "table_rows": 60,        # rows in the emailed setups table
}
