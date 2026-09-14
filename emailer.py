"""
emailer.py — send the daily report over Gmail SMTP.

The HTML report is sent BOTH ways on purpose:
  * inline as the message body  -> you read it without opening anything
  * as a .html attachment       -> the pixel-perfect copy: opens in a browser,
                                   survives Gmail's message-size clipping, and
                                   carries every chart embedded.
Every sweep chart is ALSO attached as an individual .png, so the marked charts
are visible right inside the mail itself.

Credentials come from the environment (GitHub Actions secrets); nothing is
hard-coded. Secrets used (same names as the weekly-sweep repo, so one setup
works for both):
  MY_EMAIL         = your Gmail address (abhayv7272@gmail.com) — the sender
  MY_APP_PASSWORD  = the 16-character Gmail App Password for that account
  REPORT_TO        = optional; defaults to MY_EMAIL, so the report lands on
                     abhayv7272@gmail.com with nothing extra to configure.
"""

from __future__ import annotations

import datetime as dt
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, format_datetime
from typing import Optional, Sequence, Tuple

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# GitHub Actions runners use UTC as their OS timezone. If the email Date header
# is built from the runner's local time, Gmail can show a UTC timestamp while the
# report body says IST. India has no DST, so a fixed +05:30 timezone is exact all
# year round.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

# Gmail clips a message body past ~102 kB. Charts live in the attachment anyway,
# so keep the inline body under that and let the attachment carry the full thing.
INLINE_LIMIT = 95_000


def _format_email_date_ist(now: Optional[dt.datetime] = None) -> str:
    """Return an RFC 2822 Date header pinned to India time (+0530).

    The scheduled workflow runs on UTC-hosted GitHub runners, but the recipient
    expects the mail timestamp to match Indian Standard Time. This helper keeps
    the email header aligned with the report body's ``Generated ... IST`` stamp
    and with the 19:30 IST schedule.
    """
    current = now or dt.datetime.now(IST)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=IST)
    else:
        current = current.astimezone(IST)
    return format_datetime(current)


def send_report(subject: str, html_body: str, attachments: Sequence[Tuple[str, bytes, str]],
                to: Optional[str] = None, user: Optional[str] = None,
                password: Optional[str] = None, sender_name: str = "Daily Sweep",
                clip_marker: str = "<!--chart-card-->") -> None:
    """
    attachments: sequence of (filename, raw_bytes, mime_subtype) e.g.
                 ("report.html", b"...", "html")
    """
    # Secret names: MY_EMAIL / MY_APP_PASSWORD are what this repo uses; the
    # GMAIL_* names are accepted too so either convention works.
    user = user or os.environ.get("MY_EMAIL") or os.environ.get("GMAIL_USER") or ""
    password = (password
                or os.environ.get("MY_APP_PASSWORD")
                or os.environ.get("GMAIL_APP_PASSWORD")
                or "").replace(" ", "")
    # No separate recipient secret needed: default to sending it to yourself
    # (i.e. abhayv7272@gmail.com).
    to = to or os.environ.get("REPORT_TO") or user

    missing = [n for n, v in (("MY_EMAIL", user), ("MY_APP_PASSWORD", password)) if not v]
    if missing:
        raise RuntimeError(
            "Missing secret(s): " + ", ".join(missing) +
            ". Add them at Settings → Secrets and variables → Actions.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, user))
    msg["To"] = to
    msg["Date"] = _format_email_date_ist()

    inline = html_body
    if len(inline.encode("utf-8")) > INLINE_LIMIT:
        # Body too big for Gmail: send a pointer, keep the full report attached.
        inline = _clipped_notice(html_body, clip_marker)

    msg.set_content(
        "This report is HTML. Your client is showing the plain-text fallback — "
        "open the attached .html file for the charts and tables.")
    msg.add_alternative(inline, subtype="html")

    for name, blob, subtype in attachments:
        if subtype in ("html", "csv", "plain"):
            maintype, sub = "text", subtype
        elif subtype == "png":
            maintype, sub = "image", "png"
        else:
            maintype, sub = "application", subtype
        msg.add_attachment(blob, maintype=maintype, subtype=sub, filename=name)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=120) as s:
        s.login(user, password)
        s.send_message(msg)
    print(f"· email sent to {to} ({len(attachments)} attachment(s))", flush=True)


def _clipped_notice(full_html: str, marker: str) -> str:
    """Trim the body at a chart boundary so Gmail does not clip mid-report."""
    cut = full_html.rfind(marker, 0, INLINE_LIMIT)
    head = full_html[:cut] if cut > 1000 else full_html[:INLINE_LIMIT]
    return (head +
            "<div style=\"max-width:1080px;margin:0 auto;padding:18px;background:#111827;"
            "border:1px solid #1f2a3a;border-radius:10px;font:400 13px "
            "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#e5e7eb\">"
            "<b style='color:#38bdf8'>The remaining charts are in the attached "
            "report.html</b><br><span style='color:#94a3b8'>Gmail truncates long messages, "
            "so the rest of the gallery was moved to the attachment — open it for the "
            "complete, identical report. Every chart is also attached separately as a "
            ".png.</span></div></div></div></body></html>")
