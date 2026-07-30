# Mutual Fund Daily Change

Live report: https://vipi-n.github.io/mf-daily-report/

This project builds a daily percentage-change report for the mutual funds listed in `holdings_public.json`.

It runs automatically on GitHub Pages every weekday after market close. You can also run it manually from GitHub Actions or locally.

## Daily Email

The workflow can email a short daily summary after each run.

Add these repository secrets in GitHub:

- `GMAIL`: Gmail app password
- `EMAIL_ADDRESS`: Gmail address used to send and receive the report

If Gmail rejects the login, the site still publishes; fix the secrets and rerun the workflow to get email again.

## Update Holdings

Edit `holdings_public.json` when you want to add, remove, or rename a fund.

Each fund should look like this:

```json
{
  "name": "UTI NIFTY 50 INDEX FUND - DIRECT PLAN",
  "isin": "INF789F01XA0",
  "instrument_type": "Others - Index Funds/ETFs"
}
```

On GitHub:

1. Open `holdings_public.json`.
2. Click the pencil edit button.
3. Add, remove, or edit fund entries.
4. Click **Commit changes**.

After you commit, the report rebuilds automatically.

## Run Manually

On GitHub:

1. Open the **Actions** tab.
2. Click **Build and publish mutual fund report**.
3. Click **Run workflow**.

Locally:

```bash
python3 mf_daily_change.py
```

For a specific date:

```bash
python3 mf_daily_change.py --date 29-07-2026
```

For the last 2 calendar days:

```bash
python3 mf_daily_change.py --days 2
```

## What The Report Shows

- Overall equal-weight percentage change across all listed funds
- Per-fund estimated percentage change
- Benchmark comparison where available
- Watchlist flags for large moves or underperformance
- Expandable details for contributors and missing data
- Cards/table view toggle and dark mode

The public report does not use invested amounts, so it does not show rupee gain/loss or a money-weighted portfolio return.

## More Details

Technical details, data sources, limitations, and configuration notes are in `SKILL.md`.
