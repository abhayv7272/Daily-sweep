"""
report.py — turns a finished daily screener run into ONE self-contained,
visually stunning, executive-grade HTML report.

Design highlights:
  * Executive dark dashboard palette (#0b1220, #111827, #1e293b, #1f2a3a)
  * Rich confluence analytics: RSI Divergence, Bullish FVG status, Relative Equal Lows & % distance,
    Major Structural Low distance, Volume Absorption, Candlestick signatures, and Confluence Grade (A+/A/B+)
  * Dedicated "Why Trend Will Go UP 🚀" Bullish Catalyst checklist for every swept stock
  * Multi-panel high-res charts with FVG shading and RSI Divergence annotations
  * 100% self-contained inline CSS (flawless rendering in Gmail, Apple Mail, Outlook, mobile, and web)
"""

from __future__ import annotations

import base64
import html
import io
from typing import Dict, List, Optional

import pandas as pd

# ---------------------------------------------------------------------------- palette
BG = "#070c16"
CARD = "#0f172a"
CARD_INNER = "#1e293b"
LINE = "#1e293b"
LINE_SUBTLE = "#334155"
TXT = "#f8fafc"
TXT_SEC = "#cbd5e1"
MUTE = "#94a3b8"
ACCENT = "#38bdf8"
CYAN = "#06b6d4"
GREEN = "#10b981"
GREEN_GLOW = "#059669"
AMBER = "#f59e0b"
RED = "#f43f5e"
PURPLE = "#a78bfa"
VIOLET = "#8b5cf6"

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',"
        "Arial,sans-serif")
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

CHART_CARD_MARKER = "<!--chart-card-->"

LEFT_COLS = ("Symbol", "Name", "Date", "Equal Lows", "RSI Div", "FVG Status", "Grade")


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _score_bg(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return CARD_INNER
    if s >= 80:
        return "#065f46"
    if s >= 70:
        return "#047857"
    if s >= 55:
        return "#92400e"
    return "#334155"


def _grade_pill(grade: str) -> str:
    g = str(grade).upper()
    if "A+" in g:
        return (f"<span style='display:inline-block;padding:3px 10px;border-radius:6px;"
                f"background:rgba(16,185,129,0.18);border:1px solid {GREEN};color:{GREEN};"
                f"font:700 11px {FONT};letter-spacing:.5px'>🚀 A+ ULTRA</span>")
    if "A " in g or g.endswith("A") or "STRONG" in g:
        return (f"<span style='display:inline-block;padding:3px 10px;border-radius:6px;"
                f"background:rgba(6,182,212,0.18);border:1px solid {CYAN};color:{CYAN};"
                f"font:700 11px {FONT};letter-spacing:.5px'>⭐ A STRONG</span>")
    if "B+" in g:
        return (f"<span style='display:inline-block;padding:3px 10px;border-radius:6px;"
                f"background:rgba(167,139,250,0.18);border:1px solid {PURPLE};color:{PURPLE};"
                f"font:700 11px {FONT};letter-spacing:.5px'>✅ B+ SOLID</span>")
    return (f"<span style='display:inline-block;padding:3px 10px;border-radius:6px;"
            f"background:rgba(148,163,184,0.15);border:1px solid {MUTE};color:{MUTE};"
            f"font:600 11px {FONT}'>B STANDARD</span>")


def _chip(text: str, colour: str, bg_alpha: str = "0.12") -> str:
    return (f"<span style='display:inline-block;padding:3px 10px;border-radius:6px;"
            f"background:rgba(255,255,255,{bg_alpha});border:1px solid {colour};color:{colour};"
            f"font:600 11px {FONT};margin:0 4px 4px 0;white-space:nowrap'>{_esc(text)}</span>")


def _catalyst_badge(text: str) -> str:
    # Stylized catalyst pill with icon
    color = GREEN
    bg = "rgba(16,185,129,0.12)"
    border = "#10b981"
    if "RSI" in text:
        color = "#38bdf8"
        bg = "rgba(56,189,248,0.12)"
        border = "#0284c7"
    elif "Equal Lows" in text or "Bottom" in text:
        color = "#a78bfa"
        bg = "rgba(167,139,250,0.12)"
        border = "#7c3aed"
    elif "FVG" in text:
        color = "#06b6d4"
        bg = "rgba(6,182,212,0.12)"
        border = "#0891b2"
    elif "Volume" in text or "Absorption" in text:
        color = "#f59e0b"
        bg = "rgba(245,158,11,0.12)"
        border = "#d97706"

    return (f"<div style='display:inline-block;margin:0 6px 6px 0;padding:5px 12px;"
            f"background:{bg};border:1px solid {border};border-radius:7px;"
            f"font:600 11.5px {FONT};color:{color}'>{_esc(text)}</div>")


def _kpi(label: str, value: str, colour: str = TXT, sub: str = "") -> str:
    sub_html = (f"<div style='font:400 11px {FONT};color:{MUTE};margin-top:3px'>"
                f"{_esc(sub)}</div>") if sub else ""
    return (
        f"<td style='padding:5px;width:20%'>"
        f"<div style='background:{CARD};border:1px solid {LINE};border-radius:10px;"
        f"padding:13px 15px;box-shadow:0 4px 12px rgba(0,0,0,0.2)'>"
        f"<div style='font:600 10px {FONT};color:{MUTE};letter-spacing:.8px;"
        f"text-transform:uppercase'>{_esc(label)}</div>"
        f"<div style='font:700 24px {FONT};color:{colour};margin-top:4px;"
        f"line-height:1.1'>{_esc(value)}</div>{sub_html}</div></td>")


def _matrix_table(res: pd.DataFrame) -> str:
    """Render the Trend Reversal Quick Matrix."""
    rows = []
    for i, (_, r) in enumerate(res.iterrows()):
        stripe = "#0d1527" if i % 2 else CARD
        sym = _esc(r.get("Symbol", ""))
        name = _esc(r.get("Name", ""))
        close = f"₹{float(r.get('Close', 0)):,.2f}"
        swept = f"₹{float(r.get('Swept_Level', 0)):,.2f}"
        above_swp = f"+{float(r.get('Above_Level_', 0)):.2f}%"

        # Equal lows
        is_eql = bool(r.get("Is_Equal_Lows", False))
        eql_txt = f"<b style='color:{PURPLE}'>Yes ({r.get('Equal_Lows_Count', 2)} touches)</b>" if is_eql else "<span style='color:#64748b'>No</span>"
        above_eql = f"+{float(r.get('Above_Equal_Lows_', r.get('Above_Level_', 0))):.2f}%" if is_eql else "—"

        # RSI Div
        rsi_div = str(r.get("RSI_Div", "None"))
        if "Bullish Div" in rsi_div:
            rsi_badge = f"<span style='color:{GREEN};font-weight:700'>⚡ Bullish Div</span>"
        elif "Hidden" in rsi_div:
            rsi_badge = f"<span style='color:{CYAN};font-weight:700'>🚀 Hidden Bull</span>"
        else:
            rsi_badge = "<span style='color:#64748b'>None</span>"

        # FVG
        in_fvg = bool(r.get("In_Bullish_FVG", False))
        if in_fvg:
            fvg_badge = f"<span style='color:{CYAN};font-weight:700'>📦 In FVG</span>"
        elif float(r.get("FVG_Dist_Pct", 999)) <= 3.0:
            fvg_badge = f"<span style='color:{TXT_SEC}'>+{float(r.get('FVG_Dist_Pct', 0)):.1f}% above</span>"
        else:
            fvg_badge = "<span style='color:#64748b'>—</span>"

        vol_x = f"{float(r.get('Vol_x', 1.0)):.1f}×"
        rr = f"{float(r.get('RR', 0)):.1f}:1"
        score = f"{float(r.get('Score', 0)):.1f}"
        grade = str(r.get("Confluence_Grade", "B"))

        rows.append(
            f"<tr style='background:{stripe}'>"
            f"<td style='padding:9px 10px;text-align:center;font:700 12px {MONO};color:{MUTE};border-bottom:1px solid {LINE}'>{i+1}</td>"
            f"<td style='padding:9px 10px;font:700 13px {FONT};color:{ACCENT};border-bottom:1px solid {LINE}'>{sym}"
            f"<div style='font:400 11px {FONT};color:{MUTE};max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{name}</div></td>"
            f"<td style='padding:9px 10px;text-align:center;border-bottom:1px solid {LINE}'>{_grade_pill(grade)}</td>"
            f"<td style='padding:9px 10px;text-align:right;font:600 12px {MONO};color:{TXT};border-bottom:1px solid {LINE}'>{close}</td>"
            f"<td style='padding:9px 10px;text-align:right;font:500 12px {MONO};color:{AMBER};border-bottom:1px solid {LINE}'>{swept}"
            f"<div style='font:400 10.5px {FONT};color:{GREEN}'>{above_swp}</div></td>"
            f"<td style='padding:9px 10px;text-align:center;font:500 11.5px {FONT};border-bottom:1px solid {LINE}'>{eql_txt}"
            f"<div style='font:400 10.5px {MONO};color:{MUTE}'>{above_eql}</div></td>"
            f"<td style='padding:9px 10px;text-align:center;font:500 11.5px {FONT};border-bottom:1px solid {LINE}'>{rsi_badge}</td>"
            f"<td style='padding:9px 10px;text-align:center;font:500 11.5px {FONT};border-bottom:1px solid {LINE}'>{fvg_badge}</td>"
            f"<td style='padding:9px 10px;text-align:right;font:600 12px {MONO};color:{TXT};border-bottom:1px solid {LINE}'>{vol_x}</td>"
            f"<td style='padding:9px 10px;text-align:right;font:700 12px {MONO};color:{PURPLE};border-bottom:1px solid {LINE}'>{rr}</td>"
            f"<td style='padding:9px 10px;text-align:right;font:700 12.5px {MONO};color:#fff;background:{_score_bg(score)};border-bottom:1px solid {LINE}'>{score}</td>"
            f"</tr>"
        )

    headers = [
        ("#", "center"), ("Symbol", "left"), ("Grade", "center"), ("Close", "right"),
        ("Swept Low", "right"), ("Equal Lows", "center"), ("RSI Divergence", "center"),
        ("FVG Status", "center"), ("Vol", "right"), ("R:R", "right"), ("Score", "right")
    ]
    head_html = "".join(
        f"<th style='padding:10px 10px;text-align:{align};font:700 10px {FONT};color:{MUTE};"
        f"letter-spacing:.7px;text-transform:uppercase;border-bottom:2px solid {LINE};white-space:nowrap'>{title}</th>"
        for title, align in headers
    )

    return (
        f"<div style='overflow-x:auto;border:1px solid {LINE};border-radius:10px;margin-bottom:24px'>"
        f"<table cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;background:{CARD}'>"
        f"<thead><tr style='background:#0a101d'>{head_html}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _table_from_show(show: pd.DataFrame, max_rows: int) -> str:
    """Render the master Colab display table."""
    cols = list(show.columns)
    body = show.head(max_rows)

    head = "".join(
        f"<th style=\"padding:9px 10px;text-align:{('left' if c in LEFT_COLS else 'right')};"
        f"font:600 10px {FONT};color:{MUTE};letter-spacing:.6px;text-transform:uppercase;"
        f"border-bottom:2px solid {LINE};white-space:nowrap\">{_esc(c)}</th>"
        for c in cols)

    rows = []
    for i, (_, r) in enumerate(body.iterrows()):
        stripe = "#0d1527" if i % 2 else CARD
        tds = []
        for c in cols:
            align = "left" if c in LEFT_COLS else "right"
            style = (f"padding:8px 10px;text-align:{align};font:400 12px {MONO};"
                     f"color:{TXT};border-bottom:1px solid {LINE};white-space:nowrap")
            if c == "Symbol":
                style = (f"padding:8px 10px;text-align:left;font:700 12.5px {FONT};"
                         f"color:{ACCENT};border-bottom:1px solid {LINE};white-space:nowrap")
            if c == "Name":
                style += ";max-width:180px;overflow:hidden;text-overflow:ellipsis"
            if c == ">EMA50":
                ok = str(r.get(c)) == "True"
                style = (f"padding:8px 10px;text-align:right;font:700 11.5px {FONT};"
                         f"color:{GREEN if ok else MUTE};border-bottom:1px solid {LINE}")
            if c == "Grade":
                style = (f"padding:8px 10px;text-align:center;font:700 11.5px {FONT};"
                         f"color:{CYAN};border-bottom:1px solid {LINE}")
            if c == "Score":
                style = (f"padding:8px 10px;text-align:right;font:700 12.5px {FONT};"
                         f"color:#fff;background:{_score_bg(r.get(c))};"
                         f"border-bottom:1px solid {LINE}")
            tds.append(f"<td style='{style}'>{_esc(r.get(c))}</td>")
        rows.append(f"<tr style='background:{stripe}'>{''.join(tds)}</tr>")

    more = ""
    if len(show) > max_rows:
        more = (f"<div style='font:400 11px {FONT};color:{MUTE};padding:8px 2px'>"
                f"… {len(show) - max_rows} more row(s) — see attached CSV.</div>")

    return (
        f"<div style='overflow-x:auto;border:1px solid {LINE};border-radius:10px'>"
        f"<table cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;"
        f"background:{CARD}'><thead><tr>{head}</tr></thead><tbody>"
        f"{''.join(rows)}</tbody></table></div>{more}")


def _section(title: str, subtitle: str = "") -> str:
    sub = (f"<div style='font:400 12.5px {FONT};color:{MUTE};margin-top:4px'>"
           f"{_esc(subtitle)}</div>") if subtitle else ""
    return (f"<div style='margin:32px 0 14px'>"
            f"<div style='font:700 18px {FONT};color:{TXT};letter-spacing:-.3px'>{_esc(title)}</div>"
            f"{sub}</div>")


def chart_pngs(data, res: pd.DataFrame, last_bars: int = 45,
               max_charts: int = 25) -> Dict[str, bytes]:
    """Render high-res multi-panel charts for setups."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from dlsweep import engine

    out: Dict[str, bytes] = {}
    for _, r in res.head(max_charts).iterrows():
        sym = r["Yahoo"]
        try:
            fig = engine.plot_setup(data, sym, r["Swept_Level"], last_bars=last_bars)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                        facecolor=fig.get_facecolor(), edgecolor="none")
            plt.close(fig)
            out[str(r["Symbol"])] = buf.getvalue()
        except Exception as exc:
            print(f"  ! chart skipped {sym}: {type(exc).__name__}: {exc}", flush=True)
    return out


def build_html(res: Optional[pd.DataFrame], show: Optional[pd.DataFrame],
               charts: Dict[str, bytes], stamp: str, rules: str, health: str,
               stats: Dict[str, object], table_rows: int = 60) -> str:
    """Assemble the full standalone executive HTML report."""
    n_hits = 0 if res is None else len(res)
    top = res.iloc[0] if n_hits else None
    asof = str(stats.get("asof", ""))

    head_colour = GREEN if n_hits else AMBER
    verdict = (f"{n_hits} High-Probability Swing-Buy Setup{'s' if n_hits != 1 else ''} Confirmed on {asof}"
               if n_hits else f"No Liquidity-Sweep Setup on {asof}")

    # Count grade A+/A setups
    grade_a_cnt = 0
    avg_rr = 0.0
    if n_hits:
        grade_a_cnt = sum(1 for _, r in res.iterrows() if "A" in str(r.get("Confluence_Grade", "")))
        avg_rr = float(res["RR"].mean())

    parts: List[str] = [
        f"<div style='background:{BG};padding:24px 0;font-family:{FONT}'>"
        f"<div style='max-width:1100px;margin:0 auto;padding:0 18px'>",

        # Title Card
        f"<div style='background:linear-gradient(135deg, #0b1329 0%, #172554 100%);"
        f"border:1px solid #1e3a8a;border-left:5px solid {head_colour};"
        f"border-radius:14px;padding:24px 28px;box-shadow:0 8px 24px rgba(0,0,0,0.35)'>"
        f"<div style='font:700 11px {FONT};color:{ACCENT};letter-spacing:2px;"
        f"text-transform:uppercase'>NSE INDIA · DAILY LIQUIDITY SWEEP & CONFLUENCE SCANNER</div>"
        f"<div style='font:800 28px {FONT};color:{TXT};margin:10px 0 8px;letter-spacing:-.5px'>{_esc(verdict)}</div>"
        f"<div style='font:400 13px {FONT};color:{TXT_SEC};line-height:1.5'>Scanned at {_esc(stamp)} IST"
        f"<b style='color:{AMBER}'>{_esc(str(stats.get('queue_delay_note', '')))}</b> · "
        f"Top {stats.get('universe_size', 1000):,} liquid NSE stocks scanned on the latest confirmed daily candle for "
        f"<b style='color:{TXT}'>Liquidity Sweeps, RSI Divergence, Equal Lows & Bullish FVGs</b>.</div></div>",
    ]

    # KPIs
    kpis = [
        _kpi("Total Setups", str(n_hits), GREEN if n_hits else MUTE, "passed hard filters"),
        _kpi("Grade A+ / A", f"{grade_a_cnt}", CYAN if grade_a_cnt else MUTE, "high confluence"),
        _kpi("Avg R : R", f"{avg_rr:.1f} : 1" if n_hits else "—", PURPLE, "asymmetry ratio"),
        _kpi("Top Pick", f"{top['Symbol']}" if top is not None else "—", ACCENT,
             f"Score {float(top['Score']):.1f}" if top is not None else ""),
        _kpi("Market Date", asof, TXT, "latest session"),
    ]
    parts.append("<table cellspacing='0' cellpadding='0' style='width:100%;margin:16px 0 0'>"
                 f"<tr>{''.join(kpis)}</tr></table>")

    # Data Health strip
    parts.append(
        f"<div style='background:{CARD};border:1px solid {LINE};border-radius:10px;"
        f"padding:11px 16px;margin-top:14px;font:400 12px {FONT};color:{MUTE}'>"
        f"<b style='color:{TXT}'>Data Health</b> · NSE Archive: {_esc(stats.get('list_source', '—'))} · "
        f"{int(stats.get('rank_fetched', 0)):,} ranked · {int(stats.get('hist_fetched', 0)):,} usable history · "
        f"{int(stats.get('backfilled', 0))} rebuilt 15-min candles · "
        f"{int(stats.get('repaired', 0))} OHLC repairs · "
        f"Screened in {float(stats.get('screen_secs', 0)):.1f}s"
        f"</div>")

    # Trend Reversal Quick Matrix
    if n_hits:
        parts.append(_section(
            "⚡ Trend Reversal Confluence Matrix",
            "At-a-glance scanner view of all swept stocks, their confluence ratings, and key upward catalysts."
        ))
        parts.append(_matrix_table(res))

        # Master Table
        parts.append(_section(
            "📋 Master Results Table",
            "Sorted by Setup Score (Wick 25 · Close 15 · Depth 15 · Volume 15 · RSI 15 · EMA Trend 15)."
        ))
        parts.append(_table_from_show(show, table_rows))
    else:
        parts.append(
            f"<div style='background:{CARD};border:1px solid {LINE};border-left:4px solid {AMBER};"
            f"border-radius:10px;padding:20px 24px;margin-top:24px;font:400 14px {FONT};"
            f"color:{TXT};line-height:1.6'>"
            f"<b>No stock printed a confirmed liquidity sweep on {asof}.</b><br>"
            f"<span style='color:{MUTE}'>This is normal and disciplined — false breakouts/sweeps happen often, "
            f"but high-quality liquidity sweeps with proper wicks and positive closes appear 0–5 times a day. "
            f"The scanner is ready for tomorrow's market close at 20:30 IST.</span></div>")

    # Individual Setup Spotlight Cards with Charts
    if charts and n_hits:
        parts.append(_section(
            "📊 Setup Spotlights & Marked Technical Charts",
            "Comprehensive technical breakdown: Swept Levels, Equal Lows, Bullish FVGs, RSI Divergences & Trade Plans."
        ))

        for sym, png in charts.items():
            row_match = res[res["Symbol"].astype(str) == sym]
            if row_match.empty:
                continue
            r = row_match.iloc[0]

            # Catalysts HTML
            catalysts_html = "".join(_catalyst_badge(cat) for cat in r.get("Bullish_Catalysts", []))
            if not catalysts_html:
                catalysts_html = "<span style='color:#64748b;font:400 12px " + FONT + "'>Standard liquidity reclaim</span>"

            # Trade Plan chips
            plan_chips = [
                _chip(f"Entry ≤ ₹{float(r['Close']):,.2f}", ACCENT, "0.18"),
                _chip(f"SL ₹{float(r['Stop_Loss']):,.2f}", RED, "0.18"),
                _chip(f"Target 1 ₹{float(r['Target_Ref']):,.2f}", GREEN, "0.18"),
                _chip(f"Target 2 (2R) ₹{float(r.get('Target_2R', r['Target_Ref'])):,.2f}", PURPLE, "0.18"),
                _chip(f"R:R {float(r['RR']):.1f}:1", VIOLET, "0.18"),
                _chip(f"> EMA50: {r['E50']}", GREEN if r['E50'] else MUTE, "0.15"),
                _chip(f"Score {float(r['Score']):.1f}", TXT, "0.15"),
            ]

            # Metric Grid Boxes
            m_swept = f"₹{float(r['Swept_Level']):,.2f}"
            m_swept_sub = f"Born {int(r['Level_Age_Bars'])}b ago · Touched {int(r['Touch_Ago_Bars'])}b ago"
            m_above_swp = f"+{float(r['Above_Level_']):.2f}%"
            m_above_maj = f"+{float(r.get('Above_Major_Low_', 0)):.1f}% above 60d base"

            m_eql = _esc(r.get("Equal_Lows_Desc", "Single Low"))
            m_fvg = _esc(r.get("FVG_Status", "No FVG"))
            m_rsi = f"RSI(14): {float(r['RSI14']):.0f} · {_esc(r.get('RSI_Div', 'None'))}"
            m_vol = f"{float(r['Vol_x']):.1f}× 20d Avg · {_esc(r.get('Vol_Div', 'Normal'))}"
            m_candle = _esc(r.get("Candle_Pattern", "Rejection"))

            grid_html = (
                f"<table cellspacing='0' cellpadding='0' style='width:100%;margin:12px 0 16px'>"
                f"<tr>"
                f"<td style='width:33.3%;padding:4px'>"
                f"<div style='background:{CARD_INNER};border:1px solid {LINE_SUBTLE};border-radius:8px;padding:9px 12px'>"
                f"<div style='font:600 10px {FONT};color:{MUTE};text-transform:uppercase'>Swept Swing Low</div>"
                f"<div style='font:700 15px {MONO};color:{AMBER};margin-top:2px'>{m_swept}</div>"
                f"<div style='font:400 10.5px {FONT};color:{MUTE};margin-top:2px'>{m_swept_sub}</div></div></td>"
                f"<td style='width:33.3%;padding:4px'>"
                f"<div style='background:{CARD_INNER};border:1px solid {LINE_SUBTLE};border-radius:8px;padding:9px 12px'>"
                f"<div style='font:600 10px {FONT};color:{MUTE};text-transform:uppercase'>Distance Above Lows</div>"
                f"<div style='font:700 15px {MONO};color:{GREEN};margin-top:2px'>{m_above_swp}</div>"
                f"<div style='font:400 10.5px {FONT};color:{MUTE};margin-top:2px'>{m_above_maj}</div></div></td>"
                f"<td style='width:33.3%;padding:4px'>"
                f"<div style='background:{CARD_INNER};border:1px solid {LINE_SUBTLE};border-radius:8px;padding:9px 12px'>"
                f"<div style='font:600 10px {FONT};color:{MUTE};text-transform:uppercase'>Equal Lows Structure</div>"
                f"<div style='font:700 13px {FONT};color:{PURPLE};margin-top:2px'>{m_eql}</div>"
                f"<div style='font:400 10.5px {FONT};color:{MUTE};margin-top:2px'>Liquidity pool status</div></div></td>"
                f"</tr><tr>"
                f"<td style='width:33.3%;padding:4px'>"
                f"<div style='background:{CARD_INNER};border:1px solid {LINE_SUBTLE};border-radius:8px;padding:9px 12px'>"
                f"<div style='font:600 10px {FONT};color:{MUTE};text-transform:uppercase'>Bullish FVG Status</div>"
                f"<div style='font:700 13px {FONT};color:{CYAN};margin-top:2px'>{m_fvg}</div>"
                f"<div style='font:400 10.5px {FONT};color:{MUTE};margin-top:2px'>Smart Money Imbalance</div></div></td>"
                f"<td style='width:33.3%;padding:4px'>"
                f"<div style='background:{CARD_INNER};border:1px solid {LINE_SUBTLE};border-radius:8px;padding:9px 12px'>"
                f"<div style='font:600 10px {FONT};color:{MUTE};text-transform:uppercase'>RSI & Momentum</div>"
                f"<div style='font:700 13px {FONT};color:{TXT};margin-top:2px'>{m_rsi}</div>"
                f"<div style='font:400 10.5px {FONT};color:{MUTE};margin-top:2px'>{_esc(r.get('RSI_Div_Detail', ''))}</div></div></td>"
                f"<td style='width:33.3%;padding:4px'>"
                f"<div style='background:{CARD_INNER};border:1px solid {LINE_SUBTLE};border-radius:8px;padding:9px 12px'>"
                f"<div style='font:600 10px {FONT};color:{MUTE};text-transform:uppercase'>Volume & Pattern</div>"
                f"<div style='font:700 13px {FONT};color:{TXT};margin-top:2px'>{m_vol}</div>"
                f"<div style='font:400 10.5px {FONT};color:{MUTE};margin-top:2px'>Candle: {m_candle}</div></div></td>"
                f"</tr></table>"
            )

            b64 = base64.b64encode(png).decode()
            parts.append(
                f"{CHART_CARD_MARKER}"
                f"<div style='background:{CARD};border:1px solid {LINE};border-radius:12px;"
                f"padding:20px;margin-bottom:20px;box-shadow:0 6px 18px rgba(0,0,0,0.25)'>"
                # Card Header
                f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px'>"
                f"<div>"
                f"<span style='font:800 20px {FONT};color:{ACCENT}'>{_esc(sym)}</span>"
                f"<span style='font:400 13px {FONT};color:{MUTE}'> · {_esc(r['Name'])} · Rank #{int(r['#'])}</span>"
                f"</div>"
                f"<div>{_grade_pill(r.get('Confluence_Grade', 'B'))}</div>"
                f"</div>"
                # Why Trend Will Go UP
                f"<div style='background:#091122;border:1px solid #1e293b;border-left:3px solid {GREEN};border-radius:8px;padding:12px 14px;margin-bottom:10px'>"
                f"<div style='font:700 11px {FONT};color:{GREEN};letter-spacing:.8px;text-transform:uppercase;margin-bottom:6px'>Why Trend is Poised to Move UP 🚀</div>"
                f"{catalysts_html}</div>"
                # Key Metrics Grid
                f"{grid_html}"
                # Trade Plan Strip
                f"<div style='margin-bottom:14px'>{''.join(plan_chips)}</div>"
                # High-Res Chart
                f"<img src='data:image/png;base64,{b64}' "
                f"style='width:100%;max-width:1060px;border-radius:8px;display:block;border:1px solid {LINE}'/></div>"
            )

    # Educational Playbook
    parts.append(_section("📖 Confluence Playbook & Smart Money Concepts",
                          "How to combine Liquidity Sweeps, Equal Lows, FVGs, and RSI Divergence for maximum edge."))
    playbook = [
        ("Liquidity Sweep", "Smart money drives price below obvious swing lows to trigger retail sell stops, absorbing supply before rallying."),
        ("Equal Lows (Double Bottom)", "Double/triple bottoms hold massive stop-loss pools. A sweep of equal lows followed by a reclaim is a prime high-probability buy."),
        ("Bullish FVG (Imbalance)", "Fair Value Gaps represent institutional buying inefficiency. Dipping into and bouncing from an FVG confirms strong support."),
        ("RSI Divergence ⚡", "Price making lower lows while RSI prints higher lows indicates downward exhaustion and impending violent upward trend reversal."),
        ("Volume Absorption 🔥", "Heavy volume with a long lower wick (>=50% of range) proves institutions are aggressively scooping shares on dips."),
        ("Invalidation Rule 🛡️", "If the NEXT-DAY daily candle closes below the sweep candle's low, immediately exit. Respect the invalidation without emotion."),
    ]
    playbook_cells = "".join(
        f"<td style='width:33.3%;padding:6px;vertical-align:top'>"
        f"<div style='background:{CARD};border:1px solid {LINE};border-radius:9px;padding:12px 14px;min-height:90px'>"
        f"<div style='font:700 12px {FONT};color:{ACCENT}'>{_esc(k)}</div>"
        f"<div style='font:400 11.5px {FONT};color:{MUTE};margin-top:4px;line-height:1.45'>{_esc(v)}</div>"
        f"</div></td>"
        for k, v in playbook)
    parts.append(f"<table cellspacing='0' cellpadding='0' style='width:100%'><tr>{playbook_cells[:len(playbook_cells)//2]}</tr>"
                 f"<tr>{playbook_cells[len(playbook_cells)//2:]}</tr></table>")

    # Screener Rules & Data Health
    parts.append(_section("⚙️ Screening Rules Applied"))
    parts.append(
        f"<pre style='background:{CARD};border:1px solid {LINE};border-radius:10px;"
        f"padding:15px 18px;font:400 11.5px {MONO};color:{MUTE};white-space:pre-wrap;"
        f"overflow-x:auto;margin:0'>{_esc(rules)}</pre>")

    if health:
        parts.append(_section("🩺 Data Health & Diagnostics"))
        parts.append(
            f"<pre style='background:{CARD};border:1px solid {LINE};border-radius:10px;"
            f"padding:15px 18px;font:400 11.5px {MONO};color:{MUTE};white-space:pre-wrap;"
            f"overflow-x:auto;margin:0'>{_esc(health)}</pre>")

    # Footer
    parts.append(
        f"<div style='margin:34px 0 10px;padding:18px 22px;background:{CARD};"
        f"border:1px solid {LINE};border-radius:10px;font:400 12px {FONT};color:{MUTE};line-height:1.55'>"
        f"<b style='color:{AMBER}'>⚠ Educational & Screening Tool Only.</b> Not investment advice or financial recommendation. "
        f"This tool detects Smart Money liquidity sweep and confluence setups on NSE daily candles. "
        f"Always verify charts independently and enforce strict risk management on every position.<br><br>"
        f"Automated by <b style='color:{TXT}'>Daily-sweep</b> on GitHub Actions · "
        f"Scheduled every trading day at 20:30 IST."
        f"</div></div></div>")

    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>NSE Daily Sweep & Confluence Report · {_esc(stamp)}</title></head>"
            f"<body style='margin:0;background:{BG}'>" + "".join(parts) + "</body></html>")
