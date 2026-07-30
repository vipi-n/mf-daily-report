#!/usr/bin/env python3
"""Send a short email summary for the latest generated mutual-fund report."""

from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any


REPORT_URL = os.getenv("REPORT_URL", "https://vipi-n.github.io/mf-daily-report/")


def pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:+.2f}%"


def latest_report_json(reports_dir: Path) -> Path:
    files = sorted(reports_dir.glob("mf_daily_change_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit("No generated JSON report found.")
    return files[0]


def fund_name(row: dict[str, Any]) -> str:
    name = str(row.get("fund", {}).get("name") or "")
    return name.replace(" - DIRECT PLAN", "").strip()


def build_email(payload: dict[str, Any]) -> tuple[str, str, str]:
    rows = list(payload.get("funds") or [])
    generated = str(payload.get("generated_at") or "")
    dates = sorted({str(row.get("analysis_date") or "") for row in rows if row.get("analysis_date")})
    label = dates[-1] if dates else dt.date.today().strftime("%d-%m-%Y")
    avg = sum(float(row.get("estimated_change_pct") or 0) for row in rows) / len(rows) if rows else 0.0
    gainers = [row for row in rows if float(row.get("estimated_change_pct") or 0) > 0]
    losers = [row for row in rows if float(row.get("estimated_change_pct") or 0) < 0]
    flags = [row for row in rows if row.get("watchlist_notes")]
    top = sorted(rows, key=lambda row: float(row.get("estimated_change_pct") or 0), reverse=True)[:3]
    bottom = sorted(rows, key=lambda row: float(row.get("estimated_change_pct") or 0))[:3]

    subject = f"Mutual fund daily report {label}: {pct(avg)}"

    lines = [
        f"Mutual Fund Daily Change - {label}",
        "",
        f"Overall equal-weight change: {pct(avg)}",
        f"Gainers / Losers: {len(gainers)} / {len(losers)}",
        f"Watchlist flags: {len(flags)}",
        "",
        "Top movers:",
        *[f"- {fund_name(row)}: {pct(row.get('estimated_change_pct'))}" for row in top],
        "",
        "Weakest movers:",
        *[f"- {fund_name(row)}: {pct(row.get('estimated_change_pct'))}" for row in bottom],
    ]
    if flags:
        lines.extend(
            [
                "",
                "Watchlist:",
                *[
                    f"- {fund_name(row)}: {' | '.join(str(note) for note in row.get('watchlist_notes', [])[:2])}"
                    for row in flags[:5]
                ],
            ]
        )
    lines.extend(["", f"Full report: {REPORT_URL}", f"Generated: {generated}"])

    html_items = "".join(
        f"<li><strong>{fund_name(row)}</strong>: {pct(row.get('estimated_change_pct'))}</li>" for row in top
    )
    html_flags = ""
    if flags:
        html_flags = "<h3>Watchlist</h3><ul>" + "".join(
            f"<li><strong>{fund_name(row)}</strong>: {' | '.join(str(note) for note in row.get('watchlist_notes', [])[:2])}</li>"
            for row in flags[:5]
        ) + "</ul>"
    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #1f2933;">
        <h2>Mutual Fund Daily Change - {label}</h2>
        <p><strong>Overall equal-weight change:</strong> {pct(avg)}</p>
        <p><strong>Gainers / Losers:</strong> {len(gainers)} / {len(losers)}<br>
        <strong>Watchlist flags:</strong> {len(flags)}</p>
        <h3>Top movers</h3>
        <ul>{html_items}</ul>
        {html_flags}
        <p><a href="{REPORT_URL}">Open full report</a></p>
        <p style="color:#66758a;font-size:12px;">Generated: {generated}</p>
      </body>
    </html>
    """
    return subject, "\n".join(lines), html


def main() -> int:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER") or os.getenv("EMAIL_ADDRESS") or os.getenv("EMAIL_FROM")
    smtp_password = os.getenv("SMTP_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD") or os.getenv("GMAIL")
    email_from = os.getenv("EMAIL_FROM") or smtp_user
    email_to = os.getenv("EMAIL_TO") or os.getenv("EMAIL_ADDRESS") or smtp_user

    if not smtp_user or not smtp_password or not email_from or not email_to:
        print("Email not sent: missing SMTP/email environment variables.")
        return 0

    payload = json.loads(latest_report_json(Path("reports")).read_text(encoding="utf-8"))
    subject, text_body, html_body = build_email(payload)

    if os.getenv("EMAIL_DRY_RUN") == "1":
        print(subject)
        print(text_body)
        return 0

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email_from
    message["To"] = email_to
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError:
        print(
            "Email not sent: Gmail rejected the login. Check EMAIL_ADDRESS and "
            "GMAIL repository secrets. GMAIL must be a Gmail app password, not "
            "your normal Google password."
        )
        return 0
    except smtplib.SMTPException as exc:
        print(f"Email not sent: SMTP error: {exc}")
        return 0
    print(f"Email sent to {email_to}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
