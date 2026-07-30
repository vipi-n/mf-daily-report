# Mutual Fund Daily Change Estimator

This script estimates the end-of-day percentage move for the non-arbitrage mutual funds in `holdings_public.xlsx`.

It reads your fund list from the public workbook, excludes funds whose name or instrument type contains `Arbitrage`, and writes fund-level movement to `reports/`. For today's run it fetches the latest displayed fund holdings from Groww and prices stock 1D changes where available. For older dates it uses AMFI's official historical NAV download, filtered locally by your fund ISINs. Because `holdings_public.xlsx` does not contain quantities or invested values, the public report shows percentage changes only. The combined overall change is an equal-weight average across the listed funds, not a money-weighted portfolio return. MFData can still be used as a holdings fallback; Indian benchmarks use NSE's daily index archive first and then Finology for current-day fallback.

Run:

```bash
python3 mf_daily_change.py --xlsx holdings_public.xlsx
```

Local runs:

- Public percentage-only report:

```bash
python3 mf_daily_change.py --xlsx holdings_public.xlsx
```

- Specific date:

```bash
python3 mf_daily_change.py --xlsx holdings_public.xlsx --date 29-07-2026
```

- Last 2 calendar days:

```bash
python3 mf_daily_change.py --xlsx holdings_public.xlsx --days 2
```

- Private local report with rupee impact, only if you keep `holdings.xlsx` locally:

```bash
python3 mf_daily_change.py --xlsx holdings.xlsx
```

Useful options:

```bash
python3 mf_daily_change.py --refresh-holdings
python3 mf_daily_change.py --include-arbitrage
python3 mf_daily_change.py --list-funds
python3 mf_daily_change.py --source groww
python3 mf_daily_change.py --source mfdata
python3 mf_daily_change.py --xlsx holdings_public.xlsx --date 28-07-2026
python3 mf_daily_change.py --no-open
python3 mf_daily_change.py --min-weight 0.1
python3 mf_daily_change.py --max-holdings 30
```

Outputs:

- `reports/mf_daily_change_DD-MM-YYYY.csv`: summary table
- `reports/mf_daily_change_DD-MM-YYYY.json`: detailed contributors and missing holdings
- `reports/mf_daily_change_DD-MM-YYYY.html`: interactive report page for one date
- `.mf_change_cache/`: cached API responses and price lookups

Date-wise runs:

- `--date DD-MM-YYYY` estimates one specific date. `YYYY-MM-DD` also works.
- Current-day runs use Groww stock pages for holding moves, with NSE/Finology benchmark moves where configured.
- Older date runs use AMFI's official historical NAV file for actual fund-day movement. The AMFI request contains only the date range; fund ISINs are filtered locally after download.
- `--days N` uses recent calendar dates. If a date is not a trading day, AMFI NAV logic uses the latest available NAV on or before that date.

Three override files are created on first run:

- `groww_fund_urls.json`: maps your fund ISINs to Groww mutual-fund pages.
- `benchmark_overrides.json`: maps index funds to benchmark/index daily moves before holdings math is used.
- `investment_watchlist.json`: controls informational watchlist flags, including down-day thresholds and optional manual PE comparison inputs.
- `fund_overrides.json`: use this if a mutual fund cannot be matched to an MFData family.
- `stock_symbol_overrides.json`: no longer used by the current Groww-only stock pricing mode.

Investment watchlist notes:

- The script flags funds whose estimated day move is below the thresholds in `investment_watchlist.json`.
- It flags benchmark lag when a fund underperforms its configured tracking benchmark by more than `underperform_warn_pct`; a larger lag uses `underperform_bad_pct`.
- Benchmark mappings live under `tracking_benchmarks` in `investment_watchlist.json`, so you can change the index used for any fund.
- Indian benchmark comparisons use Finology's NSE index table where the index is listed; Nasdaq 100 uses Google Finance because it is not on the NSE index table.
- It also flags a PE discount only when you fill both `fund_pe` and `tracking_index_pe` for a fund in `investment_watchlist.json`.
- These notes are informational checks, not buy/sell recommendations.

Important limitations:

- `holdings_public.xlsx` contains only fund name, ISIN, and instrument type, so the public report cannot calculate rupee impact or money-weighted return.
- Current-day output is an approximation until the official NAV is published.
- Older-date output is based on official AMFI NAV movement, but benchmark availability depends on whether the index appears in NSE's daily index archive for that date.
- Groww holdings still reflect disclosed portfolios, not live daily portfolios.
- Cash, derivatives, expenses, overseas market timing, and missed ticker matches can move the official NAV away from the estimate.
- AMFI portfolio disclosure is the official source, but each AMC publishes files in different layouts; this script uses Groww first because it normalizes those disclosures into a consistent page payload.

Index fund handling:

- `UTI NIFTY 50 INDEX FUND` uses the configured `Nifty 50` benchmark.
- `UTI NIFTY200 MOMENTUM 30 INDEX FUND` uses the configured `Nifty200 Momentum 30` benchmark.

GitHub Pages automation:

- `.github/workflows/daily-report-pages.yml` runs the script on push, manual workflow dispatch, and Monday-Friday at 18:45 IST.
- The workflow uses `holdings_public.xlsx`, which keeps only fund name, ISIN, and instrument type.
- The public report publishes percentage changes only. Its combined overall change is an equal-weight average across the listed funds, not a money-weighted portfolio return.
- The workflow publishes the newest generated report as the GitHub Pages home page.
- In GitHub, open the repository settings, go to `Pages`, and set `Build and deployment` -> `Source` to `GitHub Actions`.

