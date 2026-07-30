# Mutual Fund Report Details

## Purpose

This project generates a static interactive HTML report for the funds listed in `holdings_public.json`.

The GitHub Pages workflow runs:

```bash
python3 mf_daily_change.py --input holdings_public.json --no-open
```

## Input File

`holdings_public.json` is the source of truth for the public report.

Required fields:

- `name`: fund name
- `isin`: fund ISIN
- `instrument_type`: category text used for filtering arbitrage funds

Arbitrage funds are excluded unless `--include-arbitrage` is used.

## Data Sources

- Current-day holdings and stock moves: Groww pages where available
- Older-date fund movement: AMFI official historical NAV download
- Indian benchmark values: NSE daily index archive first, with Finology fallback for current-day pages
- MFData: optional holdings fallback

The AMFI historical NAV request uses only a date range. Fund ISINs are filtered locally after the data is downloaded.

## Report Method

- Public output is percentage-only because `holdings_public.json` does not contain invested values.
- Overall change is an equal-weight average across visible funds.
- Past-date reports use official NAV-to-NAV movement where available.
- Current-day reports are holdings-based estimates until official NAV is available.

## Useful Commands

```bash
python3 mf_daily_change.py
python3 mf_daily_change.py --date 29-07-2026
python3 mf_daily_change.py --days 2
python3 mf_daily_change.py --list-funds
python3 mf_daily_change.py --refresh-holdings
python3 mf_daily_change.py --source groww
python3 mf_daily_change.py --source mfdata
```

## Generated Files

- `reports/mf_daily_change_DD-MM-YYYY.csv`
- `reports/mf_daily_change_DD-MM-YYYY.json`
- `reports/mf_daily_change_DD-MM-YYYY.html`
- `.mf_change_cache/`

These are generated artifacts and do not need to be committed.

## Configuration Files

- `groww_fund_urls.json`: maps fund ISINs to Groww mutual-fund pages
- `benchmark_overrides.json`: maps index funds to benchmark/index sources
- `investment_watchlist.json`: controls watchlist thresholds and manual PE comparison inputs
- `fund_overrides.json`: optional MFData mapping overrides
- `stock_symbol_overrides.json`: retained for compatibility; not used by the current Groww-only stock pricing mode

## Limitations

- Current-day output is an estimate until official NAV is published.
- Holdings shown by public sites usually reflect disclosed portfolios, not live intraday portfolios.
- Cash, derivatives, expenses, overseas market timing, and portfolio changes can move official NAV away from a holdings estimate.
- Benchmark availability depends on whether the index appears in NSE's daily index archive or configured fallback sources.
