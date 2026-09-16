"""
report.py — turns a finished daily screener run into ONE self-contained,
visually polished HTML report (results table + every sweep chart, marked,
embedded as base64 PNG).

Design constraints (same proven rules as the weekly report):
  * ONE file, no external CSS/JS/images -> renders identically in Gmail's web
    client, in Outlook, and offline from the attachment.
  * Inline styles only (Gmail strips <style> blocks in the message body).
  * The numbers are taken verbatim from the engine's own results table — the
    same frame the notebook display()s in Colab — so the report cannot
    disagree with the notebook.
"""

from __future__ import annotations

import base64
import html
import io
from typing import Dict, List, Optional

import pandas as pd

# ---------------------------------------------------------------------------- palette
BG = "#0b1220"
CARD = "#111827"
LINE = "#1f2a3a"
TXT = "#e5e7eb"
MUTE = "#94a3b8"
ACCENT = "#38bdf8"
GREEN = "#22c55e"
AMBER = "#f59e0b"
RED = "#ef4444"
PURPLE = "#a78bfa"

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',"
        "Arial,sans-serif")
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

# marker used by emailer.py to trim the inline body at a clean boundary
CHART_CARD_MARKER = "<!--chart-card-->"

LEFT_COLS = ("Symbol", "Name", "Date")
NUM_ALIGN = {c: "left" for c in LEFT_COLS}


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _score_bg(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return CARD
    if s >= 80:
        return "#14532d"
    if s >= 70:
        return "#166534"
    if s >= 55:
        return "#78350f"
    return "#334155"


def _kpi(label: str, value: str, colour: str = TXT, sub: str = "") -> str:
    sub_html = (f"<div style='font:400 11px {FONT};color:{MUTE};margin-top:3px'>"
                f"{_esc(sub)}</div>") if sub else ""
    return (
        f"<td style='padding:6px'>"
        f"<div style='background:{CARD};border:1px solid {LINE};border-radius:10px;"
        f"padding:13px 15px'>"
        f"<div style='font:600 10px {FONT};color:{MUTE};letter-spacing:.9px;"
        f"text-transform:uppercase'>{_esc(label)}</div>"
        f"<div style='font:700 25px {FONT};color:{colour};margin-top:5px;"
        f"line-height:1.1'>{_esc(value)}</div>{sub_html}</div></td>")


def _chip(text: str, colour: str) -> str:
    return (f"<span style='display:inline-block;padding:2px 9px;border-radius:999px;"
            f"border:1px solid {colour};color:{colour};font:600 10.5px {FONT};"
            f"margin:0 4px 3px 0;white-space:nowrap'>{_esc(text)}</span>")


def _table_from_show(show: pd.DataFrame, max_rows: int) -> str:
    """Render the notebook's exact display() table (already formatted strings)."""
    cols = list(show.columns)
    body = show.head(max_rows)

    head = "".join(
        f"<th style=\"padding:9px 10px;text-align:{('left' if c in LEFT_COLS else 'right')};"
        f"font:600 10px {FONT};color:{MUTE};letter-spacing:.6px;text-transform:uppercase;"
        f"border-bottom:2px solid {LINE};white-space:nowrap\">{_esc(c)}</th>"
        for c in cols)

    rows = []
    for i, (_, r) in enumerate(body.iterrows()):
        stripe = "#0e1626" if i % 2 else CARD
        tds = []
        for c in cols:
            align = "left" if c in LEFT_COLS else "right"
            style = (f"padding:8px 10px;text-align:{align};font:400 12px {MONO};"
                     f"color:{TXT};border-bottom:1px solid {LINE};white-space:nowrap")
            if c == "Symbol":
                style = (f"padding:8px 10px;text-align:left;font:700 12.5px {FONT};"
                         f"color:{ACCENT};border-bottom:1px solid {LINE};white-space:nowrap")
            if c == "Name":
                style += ";max-width:200px;overflow:hidden;text-overflow:ellipsis"
            if c == ">EMA50":
                ok = str(r.get(c)) == "True"
                style = (f"padding:8px 10px;text-align:right;font:700 11.5px {FONT};"
                         f"color:{GREEN if ok else MUTE};border-bottom:1px solid {LINE}")
            if c == "Score":
                style = (f"padding:8px 10px;text-align:right;font:700 12.5px {FONT};"
                         f"color:#fff;background:{_score_bg(r.get(c))};"
                         f"border-bottom:1px solid {LINE}")
            tds.append(f"<td style='{style}'>{_esc(r.get(c))}</td>")
        rows.append(f"<tr style='background:{stripe}'>{''.join(tds)}</tr>")

    more = ""
    if len(show) > max_rows:
        more = (f"<div style='font:400 11px {FONT};color:{MUTE};padding:8px 2px'>"
                f"… {len(show) - max_rows} more row(s) — see the attached CSV.</div>")

    return (
        f"<div style='overflow-x:auto;border:1px solid {LINE};border-radius:10px'>"
        f"<table cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;"
        f"background:{CARD}'><thead><tr>{head}</tr></thead><tbody>"
        f"{''.join(rows)}</tbody></table></div>{more}")


def _section(title: str, subtitle: str = "") -> str:
    sub = (f"<div style='font:400 12.5px {FONT};color:{MUTE};margin-top:3px'>"
           f"{_esc(subtitle)}</div>") if subtitle else ""
    return (f"<div style='margin:30px 0 12px'>"
            f"<div style='font:700 17px {FONT};color:{TXT}'>{_esc(title)}</div>"
            f"{sub}</div>")


def chart_pngs(data, res: pd.DataFrame, last_bars: int = 45,
               max_charts: int = 25) -> Dict[str, bytes]:
    """One candlestick PNG per setup, drawn by the notebook's own plot_setup() —
    same swept-swing-low dashed line, same sweep-low dotted line, same SWEEP ✓ marker."""
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
            fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
            plt.close(fig)
            out[str(r["Symbol"])] = buf.getvalue()
        except Exception as exc:  # one bad chart must not kill the report
            print(f"  ! chart skipped {sym}: {type(exc).__name__}: {exc}", flush=True)
    return out


def build_html(res: Optional[pd.DataFrame], show: Optional[pd.DataFrame],
               charts: Dict[str, bytes], stamp: str, rules: str, health: str,
               stats: Dict[str, object], table_rows: int = 60) -> str:
    """Assemble the full standalone report."""
    n_hits = 0 if res is None else len(res)
    top = res.iloc[0] if n_hits else None
    asof = str(stats.get("asof", ""))

    # ---------------------------------------------------------------- header
    head_colour = GREEN if n_hits else AMBER
    verdict = (f"{n_hits} swing-buy sweep setup{'s' if n_hits != 1 else ''} on {asof}"
               if n_hits else f"No liquidity-sweep setup on {asof}")

    parts: List[str] = [
        f"<div style='background:{BG};padding:22px 0'>"
        f"<div style='max-width:1080px;margin:0 auto;padding:0 18px'>",

        # title card
        f"<div style='background:linear-gradient(135deg,#0f172a,#1e293b);"
        f"border:1px solid {LINE};border-left:4px solid {head_colour};"
        f"border-radius:12px;padding:22px 24px'>"
        f"<div style='font:600 11px {FONT};color:{ACCENT};letter-spacing:1.6px;"
        f"text-transform:uppercase'>NSE · Daily Liquidity-Sweep Screener — Swing-Buy Setups</div>"
        f"<div style='font:700 26px {FONT};color:{TXT};margin:9px 0 6px'>{_esc(verdict)}</div>"
        f"<div style='font:400 13px {FONT};color:{MUTE}'>Generated {_esc(stamp)} IST"
        f"<b style='color:{AMBER}'>{_esc(str(stats.get('queue_delay_note', '')))}</b> · "
        f"Top {stats.get('universe_size', 1000):,} NSE stocks by turnover scanned on their "
        f"<b style='color:{TXT}'>latest daily candle</b> — sweep of a swing low, taken and "
        f"<b style='color:{TXT}'>reclaimed by the close</b></div></div>",
    ]

    # ---------------------------------------------------------------- KPIs
    kpis = [
        _kpi("Setups", str(n_hits), GREEN if n_hits else MUTE, "passed every hard filter"),
        _kpi("Screened", f"{int(stats.get('screened', 0)):,}", TXT,
             f"of {int(stats.get('universe', 0)):,}-stock universe"),
        _kpi("Candle date", asof, ACCENT, "latest NSE session"),
        _kpi("Top score",
             f"{float(top['Score']):.1f}" if top is not None else "—",
             TXT, str(top["Symbol"]) if top is not None else ""),
    ]
    parts.append("<table cellspacing='0' cellpadding='0' style='width:100%;margin:14px 0 0'>"
                 f"<tr>{''.join(kpis)}</tr></table>")

    # data-quality strip
    parts.append(
        f"<div style='background:{CARD};border:1px solid {LINE};border-radius:10px;"
        f"padding:11px 15px;margin-top:12px;font:400 12px {FONT};color:{MUTE}'>"
        f"<b style='color:{TXT}'>Data</b> · NSE list: {_esc(stats.get('list_source', '—'))} · "
        f"{int(stats.get('rank_fetched', 0)):,} ranked · {int(stats.get('hist_fetched', 0)):,} with usable history · "
        f"{int(stats.get('backfilled', 0))} latest candles rebuilt from 15-min data · "
        f"{int(stats.get('repaired', 0))} OHLC repairs · "
        f"fetch {float(stats.get('fetch_secs', 0)):.0f}s + screen {float(stats.get('screen_secs', 0)):.1f}s"
        f"</div>")

    # ---------------------------------------------------------------- setups table
    if n_hits:
        parts.append(_section(
            f"The setups — candle of {asof}",
            "Ranked by Setup Score (wick 25 · close 15 · depth 15 · volume 15 · RSI 15 · EMA trend 15). "
            "This is the exact table the notebook displays in Colab."))
        parts.append(_table_from_show(show, table_rows))
    else:
        parts.append(
            f"<div style='background:{CARD};border:1px solid {LINE};border-left:4px solid {AMBER};"
            f"border-radius:10px;padding:18px 20px;margin-top:22px;font:400 14px {FONT};"
            f"color:{TXT}'>No stock in the screened universe printed a completed "
            f"sweep-and-reclaim on the latest daily candle. That is a normal, honest answer — "
            f"this pattern is not supposed to appear every day. The run itself was healthy "
            f"(see the data-health block below); tomorrow's candle gets scanned at 19:30 IST.</div>")

    # ---------------------------------------------------------------- charts
    if charts:
        parts.append(_section(
            "Charts — every sweep, marked",
            "Daily candles. Dashed orange line = the swept swing low · dotted purple line = "
            "sweep low (stop-loss zone) · SWEEP ✓ = the sweep candle."))
        for sym, png in charts.items():
            row = res[res["Symbol"].astype(str) == sym].iloc[0] if n_hits else None
            meta, plan = "", ""
            if row is not None:
                ema_chip = _chip("> EMA50", GREEN) if bool(row["E50"]) else _chip("< EMA50", MUTE)
                meta = (
                    f"<div style='font:400 12px {MONO};color:{MUTE};margin:2px 0 8px'>"
                    f"close <b style='color:{TXT}'>₹{float(row['Close']):,.2f}</b> · "
                    f"swept <b style='color:{AMBER}'>₹{float(row['Swept_Level']):,.2f}</b> "
                    f"({int(row['Level_Age_Bars'])} bars old) · depth {float(row['Sweep_Depth_']):.2f}% · "
                    f"wick {float(row['LowerWick_']):.0f}% of range · close pos {float(row['ClosePos_']):.0f}% · "
                    f"vol {float(row['Vol_x']):.1f}× · RSI {float(row['RSI14']):.0f}</div>")
                plan = (
                    f"<div style='margin:0 0 10px'>"
                    f"{_chip('entry ≤ ₹' + format(float(row['Close']), ',.2f'), ACCENT)}"
                    f"{_chip('SL ₹' + format(float(row['Stop_Loss']), ',.2f'), RED)}"
                    f"{_chip('target ₹' + format(float(row['Target_Ref']), ',.2f'), GREEN)}"
                    f"{_chip('R:R ' + format(float(row['RR']), '.1f'), PURPLE)}"
                    f"{ema_chip}"
                    f"{_chip('score ' + format(float(row['Score']), '.1f'), TXT)}</div>")
            b64 = base64.b64encode(png).decode()
            parts.append(
                f"{CHART_CARD_MARKER}"
                f"<div style='background:{CARD};border:1px solid {LINE};border-radius:10px;"
                f"padding:15px;margin-bottom:14px'>"
                f"<div style='font:700 15px {FONT};color:{ACCENT}'>{_esc(sym)}"
                f"<span style='font:400 12px {FONT};color:{MUTE}'> · {_esc(row['Name']) if row is not None else ''}"
                f" · rank #{int(row['#']) if row is not None else '—'}</span></div>"
                f"{meta}{plan}"
                f"<img src='data:image/png;base64,{b64}' "
                f"style='width:100%;max-width:1020px;border-radius:6px;display:block'/></div>")

    # ---------------------------------------------------------------- reader's key
    parts.append(_section("How to read this",
                          "The playbook from the notebook, condensed."))
    legend = [
        ("Swept Low", "the pool of stops that got taken out — your narrative"),
        ("Age (bars)", "≤10 recent structure · >40 institutional shelf — both valid"),
        ("Depth %", "0.1–1% clean tap · near 2.5% aggressive hunt"),
        ("Wick %", "lower wick as % of range — ≥50% is textbook"),
        ("Vol x", "1.2–3× confirms · >5× can be distribution"),
        ("Best bar", "Score ≥ 70 + Vol ≥ 1.2× + above EMA50"),
        ("Invalidation", "next-day CLOSE below the swept level → exit, no questions"),
        ("Execution", "buy at sweep-candle close (aggressive) or next open (conservative)"),
    ]
    legend_rows = "".join(
        "<tr>" + "".join(
            f"<td style='width:25%;padding:5px;vertical-align:top'>"
            f"<div style='background:{CARD};border:1px solid {LINE};border-radius:8px;padding:10px 12px'>"
            f"<div style='font:700 11.5px {FONT};color:{ACCENT}'>{_esc(k)}</div>"
            f"<div style='font:400 11.5px {FONT};color:{MUTE};margin-top:3px'>{_esc(v)}</div>"
            f"</div></td>"
            for k, v in legend[i:i + 4]) + "</tr>"
        for i in range(0, len(legend), 4))
    parts.append("<table cellspacing='0' cellpadding='0' style='width:100%'>"
                 f"{legend_rows}</table>")

    # ---------------------------------------------------------------- rules + health
    parts.append(_section("The rules that were applied",
                          "Verbatim from the screener's configuration cell."))
    parts.append(
        f"<pre style='background:{CARD};border:1px solid {LINE};border-radius:10px;"
        f"padding:15px 17px;font:400 11.5px {MONO};color:{MUTE};white-space:pre-wrap;"
        f"overflow-x:auto;margin:0'>{_esc(rules)}</pre>")

    if health:
        parts.append(_section("Data health"))
        parts.append(
            f"<pre style='background:{CARD};border:1px solid {LINE};border-radius:10px;"
            f"padding:15px 17px;font:400 11.5px {MONO};color:{MUTE};white-space:pre-wrap;"
            f"overflow-x:auto;margin:0'>{_esc(health)}</pre>")

    # ---------------------------------------------------------------- footer
    parts.append(
        f"<div style='margin:30px 0 6px;padding:16px 18px;background:{CARD};"
        f"border:1px solid {LINE};border-radius:10px;font:400 12px {FONT};color:{MUTE}'>"
        f"<b style='color:{AMBER}'>⚠ Not investment advice.</b> Educational screening tool that "
        f"finds a liquidity-raid-and-reclaim pattern on the latest NSE daily candle. It has no "
        f"opinion on fundamentals, news or market regime — confirm on the chart and risk-manage "
        f"every entry. You own the risk.<br><br>"
        f"Generated automatically by <b style='color:{TXT}'>Daily-sweep</b> on GitHub Actions "
        f"from <b style='color:{TXT}'>NSE_Liquidity_Sweep_Screener_FIXED.ipynb</b> · "
        f"scheduled every day at 19:30 IST (after the NSE close, so the candle is confirmed)."
        f"</div></div></div>")

    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>NSE Daily Sweep · {_esc(stamp)}</title></head>"
            f"<body style='margin:0;background:{BG}'>" + "".join(parts) + "</body></html>")
