#!/usr/bin/env python3
"""Send a short email summary for the latest generated mutual-fund report."""

from __future__ import annotations

import datetime as dt
import html as html_lib
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


def esc(value: Any) -> str:
    return html_lib.escape(str(value or ""), quote=True)


def build_email(payload: dict[str, Any]) -> tuple[str, str, str]:
    rows = list(payload.get("funds") or [])
    generated = str(payload.get("generated_at") or "")
    dates = sorted({str(row.get("analysis_date") or "") for row in rows if row.get("analysis_date")})
    label = dates[-1] if dates else dt.date.today().strftime("%d-%m-%Y")
    avg = sum(float(row.get("estimated_change_pct") or 0) for row in rows) / len(rows) if rows else 0.0
    gainers = [row for row in rows if float(row.get("estimated_change_pct") or 0) > 0]
    losers = [row for row in rows if float(row.get("estimated_change_pct") or 0) < 0]
    top = sorted(rows, key=lambda row: float(row.get("estimated_change_pct") or 0), reverse=True)[:3]
    bottom = sorted(rows, key=lambda row: float(row.get("estimated_change_pct") or 0))[:3]
    missing_avg = sum(float(row.get("missing_weight_pct") or 0) for row in rows) / len(rows) if rows else 0.0

    subject = f"{len(rows)} funds worth a look - FundScope"

    lines = [
        f"FundScope - {label}",
        "",
        f"Overall equal-weight change: {pct(avg)}",
        f"Gainers / Losers: {len(gainers)} / {len(losers)}",
        f"Average missing weight: {pct(missing_avg)}",
        "",
        "Top movers:",
        *[f"- {fund_name(row)}: {pct(row.get('estimated_change_pct'))}" for row in top],
        "",
        "Weakest movers:",
        *[f"- {fund_name(row)}: {pct(row.get('estimated_change_pct'))}" for row in bottom],
    ]
    lines.extend(["", f"Full report: {REPORT_URL}", f"Generated: {generated}"])

    def fund_rows(title: str, items: list[dict[str, Any]]) -> str:
        if not items:
            return ""
        body = "".join(
            f"""
            <tr>
              <td style="padding:18px 0;border-top:1px solid #d7d2c6">
                <div style="color:#ee6c3b;font:700 11px Arial,sans-serif;text-transform:uppercase;letter-spacing:.08em">{esc(row.get("benchmark_name") or "Mutual fund")}</div>
                <div style="color:#183128;font:600 20px Georgia,serif;margin:5px 0">{esc(fund_name(row))}</div>
                <div style="color:#607169;font:12px Arial,sans-serif">
                  Estimate <strong style="color:{'#1d7d58' if float(row.get('estimated_change_pct') or 0) >= 0 else '#c4473a'}">{pct(row.get("estimated_change_pct"))}</strong>
                  · Benchmark {pct(row.get("benchmark_change_pct")) or "n/a"}
                  · Priced {pct(row.get("priced_weight_pct"))}
                </div>
              </td>
            </tr>
            """
            for row in items
        )
        return f"""
          <tr>
            <td style="padding:22px 0 8px;color:#ee6c3b;font:700 10px Arial,sans-serif;letter-spacing:.12em">
              {esc(title)}
            </td>
          </tr>
          {body}
        """

    cards = fund_rows("TOP MOVERS", top) + fund_rows("WEAKEST MOVERS", bottom)
    html = f"""
    <!doctype html>
    <html>
      <body style="margin:0;background:#f5f2ea;padding:28px 12px">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
          <tr><td align="center">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#fffdfa">
              <tr>
                <td style="background:#183128;color:#fff;padding:34px 38px">
                  <div style="color:#f18a62;font:700 11px Arial,sans-serif;letter-spacing:.12em">FUNDSCOPE · DAILY EDITION</div>
                  <h1 style="font:500 38px Georgia,serif;line-height:1.05;margin:13px 0 8px">Fund moves,<br><i>right on time.</i></h1>
                  <p style="color:#c4d0cb;font:14px Arial,sans-serif;line-height:1.5;margin:0">Daily estimated changes for your selected mutual funds.</p>
                </td>
              </tr>
              <tr>
                <td style="padding:25px 38px 8px">
                  <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                    <tr>
                      <td style="width:33%;color:#183128;font:500 28px Georgia,serif">{pct(avg)}<div style="color:#607169;font:10px Arial,sans-serif">AVG MOVE</div></td>
                      <td style="width:33%;color:#183128;font:500 28px Georgia,serif">{len(gainers)} / {len(losers)}<div style="color:#607169;font:10px Arial,sans-serif">GAINERS / LOSERS</div></td>
                      <td style="width:33%;color:#183128;font:500 28px Georgia,serif">{len(rows)}<div style="color:#607169;font:10px Arial,sans-serif">FUNDS</div></td>
                    </tr>
                  </table>
                </td>
              </tr>
              <tr><td style="padding:13px 38px 8px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0">{cards}</table></td></tr>
              <tr>
                <td style="padding:24px 38px 38px">
                  <a href="{REPORT_URL}" style="display:inline-block;background:#ee6c3b;color:white;text-decoration:none;font:700 13px Arial,sans-serif;padding:14px 20px;border-radius:2px">Open full report -></a>
                  <p style="color:#7c8984;font:10px Arial,sans-serif;line-height:1.5;margin:20px 0 0">Generated {esc(generated)}. This estimate is based on available holding and market data and may differ from official NAV movement.</p>
                </td>
              </tr>
            </table>
          </td></tr>
        </table>
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
            server.login(smtp_user, smtp_password.replace(" ", ""))
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
