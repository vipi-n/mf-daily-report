#!/usr/bin/env python3
"""
Estimate daily mutual-fund movement from latest disclosed holdings.

Data sources:
- holdings_public.json in this folder: your public mutual fund list.
- Groww: latest displayed mutual-fund holdings and current stock 1D changes.
- AMFI: official historical NAV movement for older dates.
- NSE daily index archive and Finology/Google Finance: selected index benchmark moves.
- https://mfdata.in: latest monthly mutual-fund portfolio holdings.

Current-day holdings-based output is an approximation. Older-date output uses
official NAV history when AMFI has published the date.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


MF_DATA_BASE = "https://mfdata.in/api/v1"
GROWW_BASE = "https://groww.in"
AMFI_NAV_HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
NSE_INDEX_ARCHIVE_BASE = "https://archives.nseindia.com/content/indices"
USER_AGENT = "mf-daily-change/1.0"
ARBITRAGE_RE = re.compile(r"\barbitrage\b", re.IGNORECASE)
DEFAULT_GROWW_FUND_URLS = {
    "INF179K01XQ0": "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth",
    "INF109KC1U50": "https://groww.in/mutual-funds/icici-prudential-nasdaq-100-index-fund-direct-growth",
    "INF247L01445": "https://groww.in/mutual-funds/motilal-oswal-most-focused-midcap-30-fund-direct-growth",
    "INF879O01027": "https://groww.in/mutual-funds/parag-parikh-long-term-value-fund-direct-growth",
    "INF966L01887": "https://groww.in/mutual-funds/quant-mid-cap-fund-direct-growth",
    "INF966L01689": "https://groww.in/mutual-funds/quant-small-cap-fund-direct-plan-growth",
    "INF789F1AUT5": "https://groww.in/mutual-funds/uti-nifty200-momentum-30-index-fund-direct-growth",
    "INF789F01XA0": "https://groww.in/mutual-funds/uti-nifty-fund-direct-growth",
}
DEFAULT_BENCHMARK_OVERRIDES = {
    "INF789F01XA0": {
        "name": "NIFTY 50",
        "source": "finology_index",
        "index_name": "Nifty 50",
        "url": "https://ticker.finology.in/market/index/nse",
    },
    "INF789F1AUT5": {
        "name": "NIFTY200 Momentum 30",
        "source": "finology_index",
        "index_name": "Nifty200 Momentum 30",
        "url": "https://ticker.finology.in/market/index/nse",
    },
}
DEFAULT_WATCHLIST_CONFIG = {
    "_comment": (
        "Informational flags only, not buy/sell advice. Add PE values manually if you want "
        "PE discount checks; live PE scraping is intentionally not assumed reliable."
    ),
    "rules": {
        "significant_down_pct": -1.0,
        "very_significant_down_pct": -2.0,
        "low_priced_weight_pct": 80.0,
        "pe_discount_pct": 10.0,
        "underperform_warn_pct": -0.75,
        "underperform_bad_pct": -1.5,
    },
    "tracking_benchmarks": {
        "INF179K01XQ0": {
            "name": "Nifty Midcap 150",
            "source": "finology_index",
            "index_name": "Nifty Midcap 150",
            "url": "https://ticker.finology.in/market/index/nse",
        },
        "INF247L01445": {
            "name": "Nifty Midcap 150",
            "source": "finology_index",
            "index_name": "Nifty Midcap 150",
            "url": "https://ticker.finology.in/market/index/nse",
        },
        "INF966L01887": {
            "name": "Nifty Midcap 150",
            "source": "finology_index",
            "index_name": "Nifty Midcap 150",
            "url": "https://ticker.finology.in/market/index/nse",
        },
        "INF966L01689": {
            "name": "Nifty Smallcap 250",
            "source": "finology_index",
            "index_name": "Nifty Smallcap 250",
            "url": "https://ticker.finology.in/market/index/nse",
        },
        "INF879O01027": {
            "name": "Nifty 500",
            "source": "finology_index",
            "index_name": "Nifty 500",
            "url": "https://ticker.finology.in/market/index/nse",
        },
        "INF109KC1U50": {
            "name": "NASDAQ 100",
            "source": "google_finance",
            "google_symbol": "NDX:INDEXNASDAQ",
            "url": "https://www.google.com/finance/quote/NDX:INDEXNASDAQ?hl=en",
        },
        "INF789F01XA0": {
            "name": "NIFTY 50",
            "source": "finology_index",
            "index_name": "Nifty 50",
            "url": "https://ticker.finology.in/market/index/nse",
        },
        "INF789F1AUT5": {
            "name": "NIFTY200 Momentum 30",
            "source": "finology_index",
            "index_name": "Nifty200 Momentum 30",
            "url": "https://ticker.finology.in/market/index/nse",
        },
    },
    "valuations": {
        "INF789F01XA0": {
            "tracking_index": "NIFTY 50",
            "fund_pe": None,
            "tracking_index_pe": None,
            "as_of": "",
            "source": "",
        },
        "INF789F1AUT5": {
            "tracking_index": "NIFTY200 Momentum 30",
            "fund_pe": None,
            "tracking_index_pe": None,
            "as_of": "",
            "source": "",
        },
    },
}


@dataclass
class FundRow:
    name: str
    isin: str
    instrument_type: str
    units: float | None
    nav: float | None
    present_value: float | None


@dataclass
class Holding:
    name: str
    weight_pct: float
    sector: str = ""
    symbol_hint: str = ""
    stock_url: str = ""


@dataclass
class PriceChange:
    symbol: str
    change_pct: float
    last_close: float
    previous_close: float
    price_date: str


@dataclass
class FundEstimate:
    fund: FundRow
    analysis_date: str
    family_id: int | None
    scheme_code: int | None
    data_source: str
    reference_url: str
    holdings_month: str
    official_day_change_pct: float | None
    estimated_change_pct: float
    equity_weight_pct: float
    priced_weight_pct: float
    missing_weight_pct: float
    holdings_count: int
    priced_count: int
    missing_count: int
    contributors: list[dict[str, Any]] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)
    invested_value: float | None = None
    estimated_value_change: float | None = None
    estimated_value_after_change: float | None = None
    benchmark_name: str = ""
    benchmark_symbol: str = ""
    benchmark_change_pct: float | None = None
    underperformance_pct: float | None = None
    watchlist_notes: list[str] = field(default_factory=list)


class Cache:
    def __init__(self, root: Path, refresh_holdings: bool = False):
        self.root = root
        self.refresh_holdings = refresh_holdings
        self.http = root / "http"
        self.prices = root / "prices"
        self.runtime: dict[str, Any] = {}
        self.root.mkdir(exist_ok=True)
        self.http.mkdir(exist_ok=True)
        self.prices.mkdir(exist_ok=True)

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]

    def read_json(self, path: Path) -> Any | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            text = clean_visible_text(" ".join(self._link_text))
            if text:
                self.links.append({"text": text, "href": absolute_groww_url(self._href)})
            self._href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        for part in re.split(r"[\n\r\t]+", data):
            text = clean_visible_text(part)
            if text:
                self.lines.append(text)
                if self._href is not None:
                    self._link_text.append(text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estimate each non-arbitrage mutual fund's daily % move from disclosed holdings."
    )
    parser.add_argument("--input", default="holdings_public.json", help="Path to holdings JSON file.")
    parser.add_argument("--reports-dir", default="reports", help="Directory for CSV/JSON reports.")
    parser.add_argument("--cache-dir", default=".mf_change_cache", help="Directory for cached API responses.")
    parser.add_argument("--include-arbitrage", action="store_true", help="Include arbitrage funds too.")
    parser.add_argument("--list-funds", action="store_true", help="List parsed JSON funds and exit without network calls.")
    parser.add_argument(
        "--source",
        choices=["groww", "mfdata", "groww-mfdata"],
        default="groww-mfdata",
        help="Data source preference. Default: Groww holdings/stock pages with MFData holdings fallback.",
    )
    parser.add_argument("--refresh-holdings", action="store_true", help="Refetch fund metadata and holdings.")
    parser.add_argument("--date", help="Estimate for a specific market date in DD-MM-YYYY format. YYYY-MM-DD also works.")
    parser.add_argument("--days", type=int, default=1, help="Number of recent trading days to run. Default: 1.")
    parser.add_argument("--no-open", action="store_true", help="Generate reports but do not open the interactive HTML page.")
    parser.add_argument(
        "--min-weight",
        type=float,
        default=0.05,
        help="Skip individual fund holdings below this portfolio weight percent. Default: 0.05.",
    )
    parser.add_argument(
        "--max-holdings",
        type=int,
        default=0,
        help="Limit priced holdings per fund after sorting by weight. 0 means all holdings.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=8,
        help="Number of top contributors/missing holdings to show per fund.",
    )
    return parser.parse_args()


def normalise(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [
        w
        for w in text.split()
        if w
        not in {
            "fund",
            "direct",
            "plan",
            "growth",
            "option",
            "regular",
            "idcw",
            "ltd",
            "limited",
            "the",
        }
    ]
    return " ".join(words)


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isnan(float(value)):
            return None
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "NA", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def clean_visible_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
    return text


def absolute_groww_url(href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return f"{GROWW_BASE}{href}"
    return f"{GROWW_BASE}/{href}"


def parse_date_arg(value: str) -> str:
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise SystemExit(f"Invalid --date {value!r}; use DD-MM-YYYY or YYYY-MM-DD.")


def today_iso() -> str:
    if ZoneInfo:
        return dt.datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    return dt.date.today().isoformat()


def display_date(value: str) -> str:
    try:
        return dt.date.fromisoformat(value).strftime("%d-%m-%Y")
    except ValueError:
        return value


def display_dates_in_text(value: str) -> str:
    return re.sub(r"\b(\d{4}-\d{2}-\d{2})\b", lambda m: display_date(m.group(1)), value)


def format_money(value: float | None) -> str:
    if value is None:
        return ""
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}Rs {value:,.0f}"


def watchlist_section(config: dict[str, Any], key: str) -> dict[str, Any]:
    section = config.get(key)
    return section if isinstance(section, dict) else {}


def fund_config(config: dict[str, Any], fund: FundRow, section: str) -> dict[str, Any]:
    data = watchlist_section(config, section)
    item = data.get(fund.isin) or data.get(fund.name)
    return item if isinstance(item, dict) else {}


def tracking_benchmark_config(est: FundEstimate, watchlist_config: dict[str, Any]) -> dict[str, Any]:
    if est.data_source.startswith("benchmark:") and est.contributors:
        contributor = est.contributors[0]
        return {
            "name": contributor.get("holding") or "Benchmark",
            "source": "estimate",
            "symbol": contributor.get("symbol") or "",
            "change_pct": contributor.get("stock_change_pct"),
        }
    config = fund_config(watchlist_config, est.fund, "tracking_benchmarks")
    if config:
        return config
    return {}


def add_benchmark_comparison(est: FundEstimate, watchlist_config: dict[str, Any], cache: Cache) -> None:
    config = tracking_benchmark_config(est, watchlist_config)
    if not config:
        return
    source = str(config.get("source") or "").lower()
    if source == "estimate":
        change_pct = as_float(config.get("change_pct"))
        if change_pct is None:
            return
        est.benchmark_name = str(config.get("name") or "Benchmark")
        est.benchmark_symbol = str(config.get("symbol") or "")
        est.benchmark_change_pct = change_pct
        est.underperformance_pct = est.estimated_change_pct - change_pct
        return
    price = get_benchmark_change(config, cache, est.analysis_date)
    if not price:
        return
    est.benchmark_name = str(config.get("name") or price.symbol)
    est.benchmark_symbol = price.symbol
    est.benchmark_change_pct = price.change_pct
    est.underperformance_pct = est.estimated_change_pct - price.change_pct


def finalize_estimate(est: FundEstimate, watchlist_config: dict[str, Any], cache: Cache) -> FundEstimate:
    value = est.fund.present_value
    if value is not None:
        est.invested_value = value
        est.estimated_value_change = value * est.estimated_change_pct / 100.0
        est.estimated_value_after_change = value + est.estimated_value_change
    add_benchmark_comparison(est, watchlist_config, cache)
    est.watchlist_notes = build_watchlist_notes(est, watchlist_config)
    return est


def build_watchlist_notes(est: FundEstimate, watchlist_config: dict[str, Any]) -> list[str]:
    rules = watchlist_section(watchlist_config, "rules")
    significant_down = as_float(rules.get("significant_down_pct")) or -1.0
    very_significant_down = as_float(rules.get("very_significant_down_pct")) or -2.0
    low_priced_weight = as_float(rules.get("low_priced_weight_pct")) or 80.0
    pe_discount_pct = as_float(rules.get("pe_discount_pct")) or 10.0
    underperform_warn = as_float(rules.get("underperform_warn_pct")) or -0.75
    underperform_bad = as_float(rules.get("underperform_bad_pct")) or -1.5
    notes: list[str] = []

    if est.underperformance_pct is not None and est.benchmark_change_pct is not None:
        lag = est.underperformance_pct
        benchmark = est.benchmark_name or "benchmark"
        if lag <= underperform_bad:
            notes.append(
                f"Underperformance warning: fund lagged {benchmark} by {abs(lag):.2f} pp "
                f"({est.estimated_change_pct:+.2f}% vs {est.benchmark_change_pct:+.2f}%)."
            )
        elif lag <= underperform_warn:
            notes.append(
                f"Lagging benchmark: {abs(lag):.2f} pp behind {benchmark} "
                f"({est.estimated_change_pct:+.2f}% vs {est.benchmark_change_pct:+.2f}%)."
            )

    if est.estimated_change_pct <= very_significant_down:
        notes.append(
            f"Large down day: estimated move {est.estimated_change_pct:+.2f}%. Review only if thesis and asset allocation still fit."
        )
    elif est.estimated_change_pct <= significant_down:
        notes.append(f"Down-day watch: estimated move {est.estimated_change_pct:+.2f}%.")

    if est.priced_weight_pct < low_priced_weight:
        notes.append(f"Lower confidence: only {est.priced_weight_pct:.1f}% of holdings were priced.")

    valuation = fund_config(watchlist_config, est.fund, "valuations")
    fund_pe = as_float(valuation.get("fund_pe") or valuation.get("current_pe"))
    index_pe = as_float(valuation.get("tracking_index_pe") or valuation.get("index_pe") or valuation.get("benchmark_pe"))
    if fund_pe is not None and index_pe is not None and index_pe > 0:
        discount = (1.0 - fund_pe / index_pe) * 100.0
        index_name = str(valuation.get("tracking_index") or "tracking index")
        as_of = str(valuation.get("as_of") or "").strip()
        suffix = f" as of {display_dates_in_text(as_of)}" if as_of else ""
        if discount >= pe_discount_pct:
            notes.append(
                f"PE discount: fund PE {fund_pe:.1f} vs {index_name} PE {index_pe:.1f}, {discount:.1f}% lower{suffix}."
            )

    manual_note = str(fund_config(watchlist_config, est.fund, "notes").get("note") or "").strip()
    if manual_note:
        notes.append(manual_note)
    return notes


def append_estimate(estimates: list[FundEstimate], estimate: FundEstimate, watchlist_config: dict[str, Any], cache: Cache) -> None:
    estimates.append(finalize_estimate(estimate, watchlist_config, cache))


def load_json_fund_rows(path: Path, include_arbitrage: bool) -> list[FundRow]:
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data.get("funds") if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise RuntimeError("JSON holdings must be a list, or an object with a 'funds' list.")

    funds: list[FundRow] = []
    for idx, item in enumerate(records, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"JSON holdings row {idx} must be an object.")
        name = str(item.get("name") or item.get("symbol") or "").strip()
        isin = str(item.get("isin") or item.get("ISIN") or "").strip()
        instrument = str(item.get("instrument_type") or item.get("instrumentType") or item.get("Instrument Type") or "").strip()
        if not name or not isin:
            raise RuntimeError(f"JSON holdings row {idx} must include name and isin.")
        if not include_arbitrage and ARBITRAGE_RE.search(name + " " + instrument):
            continue
        funds.append(
            FundRow(
                name=name,
                isin=isin,
                instrument_type=instrument,
                units=None,
                nav=None,
                present_value=None,
            )
        )
    return funds


def load_fund_rows(path: Path, include_arbitrage: bool) -> list[FundRow]:
    if path.suffix.lower() != ".json":
        raise RuntimeError(f"Unsupported holdings file type: {path.suffix}. Use .json.")
    return load_json_fund_rows(path, include_arbitrage)


def http_json(url: str, cache: Cache, cache_group: str, refresh: bool = False, retries: int = 2) -> Any:
    path = cache.http / cache_group / f"{cache.key(url)}.json"
    if not refresh:
        cached = cache.read_json(path)
        if cached is not None:
            return cached
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            cache.write_json(path, data)
            return data
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                time.sleep(4 + attempt * 4)
            elif exc.code >= 500 and attempt < retries:
                time.sleep(1 + attempt)
            else:
                break
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1 + attempt)
    if refresh and path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    raise RuntimeError(f"Failed GET {url}: {last_error}")


def http_text(url: str, cache: Cache, cache_group: str, refresh: bool = False, retries: int = 2) -> str:
    path = cache.http / cache_group / f"{cache.key(url)}.html"
    if not refresh and path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return text
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                time.sleep(4 + attempt * 4)
            elif exc.code >= 500 and attempt < retries:
                time.sleep(1 + attempt)
            else:
                break
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1 + attempt)
    if refresh and path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    raise RuntimeError(f"Failed GET {url}: {last_error}")


def http_text_live(url: str, cache: Cache, cache_group: str, retries: int = 2) -> str:
    path = cache.http / cache_group / f"{cache.key(url)}.txt"
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/plain,text/csv,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return text
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                time.sleep(4 + attempt * 4)
            elif exc.code >= 500 and attempt < retries:
                time.sleep(1 + attempt)
            else:
                break
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1 + attempt)
    raise RuntimeError(f"Failed live GET {url}: {last_error}")


def parse_visible_html(html: str) -> VisibleTextParser:
    parser = VisibleTextParser()
    parser.feed(html)
    return parser


def extract_next_json(html: str) -> dict[str, Any] | None:
    start = html.find('{"props":')
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html[start:])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def load_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_default_json(path: Path, data: Any) -> None:
    if not path.exists():
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def merge_defaults(data: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    for key, value in data.items():
        if (
            isinstance(value, dict)
            and isinstance(merged.get(key), dict)
        ):
            merged[key] = merge_defaults(value, merged[key])
        else:
            merged[key] = value
    return merged


def sync_default_json(path: Path, defaults: dict[str, Any]) -> None:
    current = load_overrides(path)
    merged = merge_defaults(current, defaults)
    if merged != current:
        path.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def resolve_fund(fund: FundRow, cache: Cache, fund_overrides: dict[str, Any]) -> tuple[int | None, int | None, str]:
    override = fund_overrides.get(fund.isin) or fund_overrides.get(fund.name)
    if override:
        if override.get("skip"):
            return None, None, "override-skip"
        return override.get("family_id"), override.get("scheme_code"), "override"

    candidates: list[dict[str, Any]] = []
    queries = [fund.isin, clean_fund_query(fund.name)]
    for query in [q for q in queries if q]:
        url = f"{MF_DATA_BASE}/search?{urllib.parse.urlencode({'q': query})}"
        try:
            data = http_json(url, cache, "mfdata_search", refresh=cache.refresh_holdings)
            candidates.extend(data.get("data", []) if isinstance(data, dict) else [])
        except Exception as exc:
            print(f"warning: MFData search failed for {fund.name}: {exc}", file=sys.stderr)

    if not candidates:
        return None, None, "not-found"

    best = max(candidates, key=lambda c: score_scheme_candidate(fund, c))
    scheme_code = first_int(best, "scheme_code", "schemeCode", "amfi_code")
    family_id = first_int(best, "family_id", "familyId")
    if family_id:
        return family_id, scheme_code, "search"

    if scheme_code:
        url = f"{MF_DATA_BASE}/schemes/{scheme_code}"
        try:
            detail = http_json(url, cache, "mfdata_scheme", refresh=cache.refresh_holdings).get("data", {})
            family_id = first_int(detail, "family_id", "familyId")
            if family_id:
                return family_id, scheme_code, "scheme-detail"
        except Exception as exc:
            print(f"warning: MFData scheme detail failed for {fund.name}: {exc}", file=sys.stderr)

    family_id, scheme_code_from_list = search_scheme_list_for_family(fund, cache)
    if family_id:
        return family_id, scheme_code or scheme_code_from_list, "scheme-list"

    family_id = search_family_id(fund, cache)
    return family_id, scheme_code, "family-search" if family_id else "no-family-id"


def clean_fund_query(name: str) -> str:
    text = re.sub(r"\b(direct|regular|plan|growth|idcw|option)\b", " ", name, flags=re.IGNORECASE)
    text = re.sub(r"[-_/]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def score_scheme_candidate(fund: FundRow, candidate: dict[str, Any]) -> float:
    cname = str(candidate.get("scheme_name") or candidate.get("schemeName") or candidate.get("name") or "")
    score = token_overlap(normalise(fund.name), normalise(cname))
    if fund.isin and fund.isin in json.dumps(candidate):
        score += 5
    if re.search(r"\bdirect\b", cname, re.IGNORECASE):
        score += 1
    if re.search(r"\bgrowth\b", cname, re.IGNORECASE):
        score += 0.5
    return score


def token_overlap(a: str, b: str) -> float:
    sa = set(a.split())
    sb = set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def first_int(data: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def search_family_id(fund: FundRow, cache: Cache) -> int | None:
    params = {
        "limit": 1000,
        "has_holdings": "true",
    }
    # The public docs primarily describe category/amc filters, but some builds
    # also accept search. If ignored, scoring over the returned page still works.
    params["search"] = clean_fund_query(fund.name)
    url = f"{MF_DATA_BASE}/families?{urllib.parse.urlencode(params)}"
    try:
        data = http_json(url, cache, "mfdata_families", refresh=cache.refresh_holdings)
    except Exception:
        return None
    families = data.get("data", []) if isinstance(data, dict) else []
    if not families:
        return None
    best = max(families, key=lambda c: token_overlap(normalise(fund.name), normalise(str(c.get("family_name", "")))))
    if token_overlap(normalise(fund.name), normalise(str(best.get("family_name", "")))) < 0.35:
        return None
    return first_int(best, "family_id", "familyId")


def search_scheme_list_for_family(fund: FundRow, cache: Cache) -> tuple[int | None, int | None]:
    params = {
        "search": clean_fund_query(fund.name),
        "has_holdings": "true",
        "limit": 25,
    }
    url = f"{MF_DATA_BASE}/schemes?{urllib.parse.urlencode(params)}"
    try:
        data = http_json(url, cache, "mfdata_schemes", refresh=cache.refresh_holdings)
    except Exception:
        return None, None
    schemes = data.get("data", []) if isinstance(data, dict) else []
    if not schemes:
        return None, None
    best = max(schemes, key=lambda c: score_scheme_candidate(fund, c))
    if score_scheme_candidate(fund, best) < 0.35:
        return None, None
    return first_int(best, "family_id", "familyId"), first_int(best, "scheme_code", "schemeCode", "amfi_code")


def get_holdings(family_id: int, cache: Cache) -> tuple[str, list[Holding]]:
    url = f"{MF_DATA_BASE}/families/{family_id}/holdings?holding_type=equity"
    data = http_json(url, cache, "mfdata_holdings", refresh=cache.refresh_holdings)
    payload = data.get("data", {}) if isinstance(data, dict) else {}
    month = str(payload.get("month") or payload.get("as_of") or payload.get("date") or "")
    raw_holdings = (
        payload.get("equity")
        or payload.get("equity_holdings")
        or payload.get("holdings")
        or payload.get("data")
        or []
    )
    holdings: list[Holding] = []
    for item in raw_holdings:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("stock_name") or item.get("security") or "").strip()
        weight = as_float(item.get("weight_pct") or item.get("weight") or item.get("percentage"))
        if not name or weight is None:
            continue
        symbol_hint = str(
            item.get("nse_symbol")
            or item.get("bse_symbol")
            or item.get("symbol")
            or item.get("ticker")
            or item.get("exchange_symbol")
            or ""
        ).strip()
        holdings.append(
            Holding(
                name=name,
                weight_pct=weight,
                sector=str(item.get("sector") or ""),
                symbol_hint=symbol_hint,
            )
        )
    holdings.sort(key=lambda h: h.weight_pct, reverse=True)
    return month, holdings


def resolve_groww_fund_url(fund: FundRow, groww_urls: dict[str, Any]) -> str | None:
    override = groww_urls.get(fund.isin) or groww_urls.get(fund.name)
    if isinstance(override, dict):
        if override.get("skip"):
            return None
        override = override.get("url")
    if isinstance(override, str) and override.strip():
        return absolute_groww_url(override.strip())
    default = DEFAULT_GROWW_FUND_URLS.get(fund.isin)
    if default:
        return default
    slug = groww_fund_slug(fund.name)
    return f"{GROWW_BASE}/mutual-funds/{slug}" if slug else None


def groww_fund_slug(name: str) -> str:
    text = clean_fund_query(name)
    text = re.sub(r"\bfund\s*$", "fund direct growth", text, flags=re.IGNORECASE)
    if not re.search(r"\bdirect\b", text, re.IGNORECASE):
        text = f"{text} direct growth"
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text


def get_groww_holdings(fund: FundRow, url: str, cache: Cache) -> tuple[str, float | None, list[Holding]]:
    html = http_text(url, cache, "groww_fund_pages", refresh=cache.refresh_holdings)
    next_json = extract_next_json(html)
    server_data = (
        ((next_json or {}).get("props") or {})
        .get("pageProps", {})
        .get("mfServerSideData")
    )
    if isinstance(server_data, dict):
        parsed = parse_groww_holdings_json(server_data)
        if parsed[2]:
            return parsed

    page = parse_visible_html(html)
    joined = " ".join(page.lines[:120])
    official_1d = parse_groww_fund_1d(joined)
    nav_date = parse_groww_nav_date(page.lines)

    holdings_start = None
    for idx, line in enumerate(page.lines):
        if re.match(r"^Holdings\s*\(\d+\)", line, flags=re.IGNORECASE):
            holdings_start = idx
            break
    if holdings_start is None:
        raise RuntimeError("Groww holdings section not found")

    rows = page.lines[holdings_start + 1 :]
    holdings = parse_groww_holdings_rows(rows, page.links)
    if not holdings:
        raise RuntimeError("Groww holdings table had no equity rows")
    label = f"Groww {nav_date}" if nav_date else "Groww latest"
    return label, official_1d, holdings


def parse_groww_holdings_json(data: dict[str, Any]) -> tuple[str, float | None, list[Holding]]:
    official_1d = as_float((data.get("simple_return") or {}).get("return1d"))
    nav_date = str(data.get("nav_date") or "")
    holdings: list[Holding] = []
    for item in data.get("holdings") or []:
        if not isinstance(item, dict):
            continue
        instrument = str(item.get("instrument_name") or item.get("nature_name") or "")
        if "equity" not in instrument.lower():
            continue
        name = str(item.get("company_name") or "").strip()
        weight = as_float(item.get("corpus_per"))
        if not name or weight is None:
            continue
        stock_search_id = str(item.get("stock_search_id") or "").strip()
        stock_url = f"{GROWW_BASE}/stocks/{stock_search_id}" if stock_search_id else ""
        holdings.append(
            Holding(
                name=name,
                weight_pct=weight,
                sector=str(item.get("sector_name") or ""),
                symbol_hint=extract_ticker_from_holding_name(name),
                stock_url=stock_url,
            )
        )
    holdings.sort(key=lambda h: h.weight_pct, reverse=True)
    portfolio_date = ""
    if data.get("holdings"):
        portfolio_date = str((data["holdings"][0] or {}).get("portfolio_date") or "")
        portfolio_date = portfolio_date[:10] if portfolio_date else ""
    label = "Groww"
    if portfolio_date:
        label = f"{label} portfolio {portfolio_date}"
    elif nav_date:
        label = f"{label} NAV {nav_date}"
    return label, official_1d, holdings


def parse_groww_fund_1d(text: str) -> float | None:
    match = re.search(r"([+-]?\d+(?:\.\d+)?)%\s*1D\b", text)
    return float(match.group(1)) if match else None


def parse_groww_nav_date(lines: list[str]) -> str:
    for idx, line in enumerate(lines):
        if line.startswith("NAV:"):
            return line
        if line == "NAV:" and idx + 1 < len(lines):
            return f"NAV: {lines[idx + 1]}"
    return ""


def parse_groww_holdings_rows(lines: list[str], links: list[dict[str, str]]) -> list[Holding]:
    cleaned = [
        line
        for line in lines
        if line
        not in {
            "Name",
            "Sector",
            "Instruments",
            "Assets",
            "---",
        }
    ]
    holdings: list[Holding] = []
    idx = 0
    stop_re = re.compile(
        r"^(See All|Minimum investments|Understand terms|Returns and rankings|Exit Load|Fund management|About\s)",
        re.IGNORECASE,
    )
    while idx + 3 < len(cleaned):
        if stop_re.search(cleaned[idx]):
            break
        name, sector, instrument, assets = cleaned[idx : idx + 4]
        if not re.search(r"^-?\d+(?:\.\d+)?%$", assets.replace(",", "")):
            idx += 1
            continue
        weight = as_float(assets.replace("%", ""))
        if weight is not None and "equity" in instrument.lower():
            holdings.append(
                Holding(
                    name=name,
                    weight_pct=weight,
                    sector=sector,
                    symbol_hint=extract_ticker_from_holding_name(name),
                    stock_url=find_groww_stock_url(name, links),
                )
            )
        idx += 4
    holdings.sort(key=lambda h: h.weight_pct, reverse=True)
    return holdings


def find_groww_stock_url(name: str, links: list[dict[str, str]]) -> str:
    target = normalise_stock_name(name)
    best_url = ""
    best_score = 0.0
    for link in links:
        href = link.get("href", "")
        if "/stocks/" not in href:
            continue
        score = token_overlap(target, normalise_stock_name(link.get("text", "")))
        if score > best_score:
            best_score = score
            best_url = href
    return best_url if best_score >= 0.45 else ""


def normalise_stock_name(name: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", name)
    text = re.sub(r"\b(Forgn|Eq|ADS|ADR|USA|US|Ltd|Limited|Company|Co)\b\.?", " ", text, flags=re.IGNORECASE)
    return normalise(text)


def extract_ticker_from_holding_name(name: str) -> str:
    matches = re.findall(r"\(([A-Z]{1,6})\)", name)
    return matches[-1] if matches else ""


def get_groww_price_change(holding: Holding, cache: Cache, trade_date: str) -> PriceChange | None:
    if not holding.stock_url:
        return None
    cache_path = cache.prices / trade_date / "groww" / f"{cache.key(holding.stock_url)}.json"
    cached = cache.read_json(cache_path)
    if cached is not None:
        return PriceChange(**cached)
    try:
        html = http_text(holding.stock_url, cache, "groww_stock_pages", refresh=True, retries=1)
    except Exception:
        return None
    next_json = extract_next_json(html)
    json_price = parse_groww_stock_json(next_json, trade_date)
    if json_price:
        cache.write_json(cache_path, json_price.__dict__)
        return json_price
    page = parse_visible_html(html)
    joined = " ".join(page.lines[:80])
    change_pct = parse_groww_stock_1d(joined)
    if change_pct is None:
        return None
    symbol = parse_groww_stock_symbol(page.lines) or holding.symbol_hint or holding.name
    price = PriceChange(
        symbol=f"Groww:{symbol}",
        change_pct=change_pct,
        last_close=0.0,
        previous_close=0.0,
        price_date=trade_date,
    )
    cache.write_json(cache_path, price.__dict__)
    return price


def parse_groww_stock_json(data: dict[str, Any] | None, trade_date: str) -> PriceChange | None:
    page_props = (((data or {}).get("props") or {}).get("pageProps") or {})
    stock_data = page_props.get("stockData") or {}
    header = stock_data.get("header") or {}
    live_prices = page_props.get("livePriceData") or {}
    if not isinstance(live_prices, dict):
        return None

    preferred_symbols = [
        str(header.get("nseScriptCode") or ""),
        str(header.get("bseScriptCode") or ""),
        str(header.get("nseTradingSymbol") or "").replace("-EQ", ""),
        str(header.get("bseTradingSymbol") or ""),
    ]
    candidates = [s for s in preferred_symbols if s]
    candidates.extend(str(k) for k in live_prices.keys())

    for symbol in unique(candidates):
        payload = live_prices.get(symbol)
        if not isinstance(payload, dict):
            continue
        change_pct = as_float(payload.get("dayChangePerc"))
        close = as_float(payload.get("close"))
        day_change = as_float(payload.get("dayChange"))
        if change_pct is None:
            continue
        previous_close = (close - day_change) if close is not None and day_change is not None else 0.0
        return PriceChange(
            symbol=f"Groww:{payload.get('symbol') or symbol}",
            change_pct=change_pct,
            last_close=close or 0.0,
            previous_close=previous_close,
            price_date=trade_date,
        )
    return None


def parse_groww_stock_1d(text: str) -> float | None:
    match = re.search(r"₹[\d,.]+\s*([+-]?)\s*[\d,.]+\s*\(([+-]?\d+(?:\.\d+)?)%\)\s*1D\b", text)
    if not match:
        match = re.search(r"\(([+-]?\d+(?:\.\d+)?)%\)\s*1D\b", text)
        return float(match.group(1)) if match else None
    sign, pct_text = match.groups()
    pct = abs(float(pct_text))
    return -pct if sign == "-" else pct


def parse_groww_stock_symbol(lines: list[str]) -> str:
    for line in lines[:40]:
        match = re.match(r"^([A-Z0-9&.-]+)\s*•", line)
        if match:
            return match.group(1)
    return ""


def resolve_benchmark_override(fund: FundRow, benchmark_overrides: dict[str, Any]) -> dict[str, Any] | None:
    override = benchmark_overrides.get(fund.isin) or benchmark_overrides.get(fund.name)
    if not isinstance(override, dict) or override.get("disabled"):
        return None
    return override


def get_benchmark_change(config: dict[str, Any], cache: Cache, trade_date: str) -> PriceChange | None:
    source = str(config.get("source") or "").lower()
    name = str(config.get("name") or "Benchmark")
    if source == "manual":
        change_pct = as_float(config.get("change_pct"))
        if change_pct is None:
            return None
        return PriceChange(
            symbol=f"Benchmark:{name}",
            change_pct=change_pct,
            last_close=0.0,
            previous_close=0.0,
            price_date=trade_date,
        )
    if source == "google_finance":
        google_symbol = str(config.get("google_symbol") or "").strip()
        url = str(config.get("url") or "").strip()
        price = get_google_finance_change(name, google_symbol, url, cache, trade_date) if url else None
        if price:
            return price
        return None
    if source == "finology_index":
        index_name = str(config.get("index_name") or name).strip()
        url = str(config.get("list_url") or config.get("url") or "https://ticker.finology.in/market/index/nse").strip()
        price = get_finology_index_change(name, index_name, url, cache, trade_date) if url else None
        if price:
            return price
        return None
    if source == "niftyindices_page":
        url = str(config.get("url") or "").strip()
        price = get_niftyindices_page_change(name, url, cache, trade_date) if url else None
        if price:
            return price
        return None
    if source in {"groww_page", "groww_index", "groww_etf"}:
        url = str(config.get("url") or "").strip()
        return get_groww_page_change(name, url, cache, trade_date) if url else None
    return None


def get_finology_index_change(
    name: str,
    index_name: str,
    url: str,
    cache: Cache,
    trade_date: str,
) -> PriceChange | None:
    price = get_nse_index_archive_change(name, index_name, cache, trade_date)
    if price:
        return price
    if trade_date != today_iso():
        return None
    cache_path = cache.prices / trade_date / "finology_index_benchmarks" / f"{cache.key(index_name)}.json"
    cached = cache.read_json(cache_path)
    if cached is not None:
        return PriceChange(**cached)
    try:
        html = http_text(url, cache, "finology_index_pages", refresh=True, retries=1)
    except Exception:
        return None
    price = parse_finology_index_price(name, index_name, html, trade_date)
    if price:
        cache.write_json(cache_path, price.__dict__)
    return price


def parse_finology_index_price(name: str, index_name: str, html: str, trade_date: str) -> PriceChange | None:
    row_re = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    cell_re = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
    tag_re = re.compile(r"<[^>]+>")
    target = normalise(index_name)
    for row_match in row_re.finditer(html):
        row = row_match.group(1)
        if target not in normalise(tag_re.sub(" ", row)):
            continue
        cells = [clean_visible_text(tag_re.sub(" ", c)) for c in cell_re.findall(row)]
        cells = [c for c in cells if c]
        if len(cells) < 5:
            continue
        row_name = cells[1]
        if normalise(row_name) != target:
            continue
        last_close = as_float(cells[2])
        change_amount = as_float(cells[3])
        change_pct = as_float(cells[4].replace("%", ""))
        if change_pct is None:
            continue
        if change_amount is not None and change_amount < 0 and change_pct > 0:
            change_pct = -change_pct
        previous_close = (last_close - change_amount) if last_close is not None and change_amount is not None else 0.0
        return PriceChange(
            symbol=f"Finology:{index_name}",
            change_pct=change_pct,
            last_close=last_close or 0.0,
            previous_close=previous_close or 0.0,
            price_date=trade_date,
        )
    return None


def nse_archive_date(value: str) -> str:
    return dt.date.fromisoformat(value).strftime("%d%m%Y")


def get_nse_index_archive_text(cache: Cache, trade_date: str) -> str:
    key = f"nse_index_archive:{trade_date}"
    cached = cache.runtime.get(key)
    if isinstance(cached, str):
        return cached
    url = f"{NSE_INDEX_ARCHIVE_BASE}/ind_close_all_{nse_archive_date(trade_date)}.csv"
    text = http_text_live(url, cache, "nse_index_archive", retries=1)
    cache.runtime[key] = text
    return text


def get_nse_index_archive_change(
    name: str,
    index_name: str,
    cache: Cache,
    trade_date: str,
) -> PriceChange | None:
    try:
        text = get_nse_index_archive_text(cache, trade_date)
    except Exception:
        return None
    target = normalise(index_name)
    for row in csv.DictReader(io.StringIO(text)):
        row_name = str(row.get("Index Name") or "").strip()
        if normalise(row_name) != target:
            continue
        change_pct = as_float(row.get("Change(%)"))
        close = as_float(row.get("Closing Index Value"))
        points_change = as_float(row.get("Points Change"))
        if change_pct is None:
            return None
        previous_close = (close - points_change) if close is not None and points_change is not None else 0.0
        return PriceChange(
            symbol=f"NSE:{row_name or name}",
            change_pct=change_pct,
            last_close=close or 0.0,
            previous_close=previous_close or 0.0,
            price_date=trade_date,
        )
    return None


def get_google_finance_change(
    name: str,
    google_symbol: str,
    url: str,
    cache: Cache,
    trade_date: str,
) -> PriceChange | None:
    if trade_date != today_iso():
        return None
    cache_path = cache.prices / trade_date / "google_finance_benchmarks" / f"{cache.key(url)}.json"
    cached = cache.read_json(cache_path)
    if cached is not None:
        return PriceChange(**cached)
    try:
        html = http_text(url, cache, "google_finance_pages", refresh=True, retries=1)
    except Exception:
        return None
    price = parse_google_finance_price(name, google_symbol, html, trade_date)
    if price:
        cache.write_json(cache_path, price.__dict__)
    return price


def parse_google_finance_price(name: str, google_symbol: str, html: str, trade_date: str) -> PriceChange | None:
    if google_symbol and ":" in google_symbol:
        symbol, exchange = google_symbol.split(":", 1)
        array_match = re.search(
            rf'\["[^"]+",\["{re.escape(symbol)}","{re.escape(exchange)}"\],"[^"]+",1,null,'
            r'\[([+-]?[\d,]+(?:\.\d+)?),([+-]?[\d,]+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)',
            html,
        )
        if array_match:
            last_close = as_float(array_match.group(1))
            change_amount = as_float(array_match.group(2))
            change_pct = as_float(array_match.group(3))
            return make_google_finance_price(name, google_symbol, change_pct, last_close, change_amount, trade_date)

    title = re.escape(name)
    match = re.search(
        rf'<div class="gO24Ff">{title}</div>.*?'
        r'<span jsname="Pdsbrc"[^>]*>\s*<span>([\d,]+(?:\.\d+)?)</span>.*?'
        r'<span jsname="vY9t3b"[^>]*>\s*<span class="ougHge">([+-]?\d+(?:\.\d+)?)%</span>.*?'
        r'<span jsname="xnruHf"[^>]*>\s*<span>([+-]?[\d,]+(?:\.\d+)?)</span>.*?\)\s*1D',
        html,
        flags=re.DOTALL,
    )
    if not match:
        match = re.search(
            rf'<div class="pKBk1e">{title}</div>.*?'
            r'<div class="YMlKec">([\d,]+(?:\.\d+)?)</div>.*?'
            r'aria-label="(Up|Down) by (\d+(?:\.\d+)?)%".*?'
            r'<span class="P2Luy Ez2Ioe">([+-]?[\d,]+(?:\.\d+)?)</span>',
            html,
            flags=re.DOTALL,
        )
        if match:
            last_close = as_float(match.group(1))
            change_pct = as_float(match.group(3))
            change_amount = as_float(match.group(4))
            if match.group(2) == "Down" and change_pct is not None:
                change_pct = -abs(change_pct)
            return make_google_finance_price(name, google_symbol, change_pct, last_close, change_amount, trade_date)
    if not match:
        return None
    last_close = as_float(match.group(1))
    change_pct = as_float(match.group(2))
    change_amount = as_float(match.group(3))
    return make_google_finance_price(name, google_symbol, change_pct, last_close, change_amount, trade_date)


def make_google_finance_price(
    name: str,
    google_symbol: str,
    change_pct: float | None,
    last_close: float | None,
    change_amount: float | None,
    trade_date: str,
) -> PriceChange | None:
    if change_pct is None:
        return None
    if change_amount is not None and change_amount < 0 and change_pct > 0:
        change_pct = -change_pct
    previous_close = (last_close - change_amount) if last_close is not None and change_amount is not None else 0.0
    return PriceChange(
        symbol=f"GoogleFinance:{google_symbol or name}",
        change_pct=change_pct,
        last_close=last_close or 0.0,
        previous_close=previous_close or 0.0,
        price_date=trade_date,
    )


def get_niftyindices_page_change(name: str, url: str, cache: Cache, trade_date: str) -> PriceChange | None:
    if trade_date != today_iso():
        return None
    cache_path = cache.prices / trade_date / "niftyindices_benchmarks" / f"{cache.key(url)}.json"
    cached = cache.read_json(cache_path)
    if cached is not None:
        return PriceChange(**cached)
    try:
        html = http_text(url, cache, "niftyindices_pages", refresh=True, retries=1)
    except Exception:
        return None
    page = parse_visible_html(html)
    price = parse_niftyindices_page_price(name, page.lines, trade_date)
    if price:
        cache.write_json(cache_path, price.__dict__)
    return price


def parse_niftyindices_page_price(name: str, lines: list[str], trade_date: str) -> PriceChange | None:
    for idx, line in enumerate(lines):
        combined_match = re.search(r"([+-]?\d+(?:,\d{2,3})*(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)%", line)
        pct_only_match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)%", line)
        if not combined_match and not pct_only_match:
            continue
        level = None
        change_amount = None
        if combined_match:
            change_amount = as_float(combined_match.group(1))
            change_pct = as_float(combined_match.group(2))
            level_candidates = lines[max(0, idx - 5) : idx]
        else:
            change_pct = as_float(pct_only_match.group(1))
            if idx > 0:
                change_amount = as_float(lines[idx - 1])
            level_candidates = lines[max(0, idx - 6) : max(0, idx - 1)]
        for prev in reversed(level_candidates):
            if re.fullmatch(r"\d+(?:,\d{2,3})*(?:\.\d+)?", prev):
                level = as_float(prev)
                break
        if change_pct is None:
            continue
        if change_amount is not None and change_amount < 0 and change_pct > 0:
            change_pct = -change_pct
        last_close = level or 0.0
        previous_close = last_close / (1.0 + change_pct / 100.0) if last_close and change_pct != -100 else 0.0
        return PriceChange(
            symbol=f"NiftyIndices:{name}",
            change_pct=change_pct,
            last_close=last_close,
            previous_close=previous_close,
            price_date=trade_date,
        )
    return None


def get_groww_page_change(name: str, url: str, cache: Cache, trade_date: str) -> PriceChange | None:
    cache_path = cache.prices / trade_date / "groww_benchmarks" / f"{cache.key(url)}.json"
    cached = cache.read_json(cache_path)
    if cached is not None:
        return PriceChange(**cached)
    try:
        html = http_text(url, cache, "groww_benchmark_pages", refresh=True, retries=1)
    except Exception:
        return None
    parsed_json = parse_groww_stock_json(extract_next_json(html), trade_date)
    if parsed_json:
        parsed_json.symbol = f"Benchmark:{name}"
        cache.write_json(cache_path, parsed_json.__dict__)
        return parsed_json
    page = parse_visible_html(html)
    change_pct = parse_groww_stock_1d(" ".join(page.lines[:100]))
    if change_pct is None:
        return None
    price = PriceChange(
        symbol=f"Benchmark:{name}",
        change_pct=change_pct,
        last_close=0.0,
        previous_close=0.0,
        price_date=trade_date,
    )
    cache.write_json(cache_path, price.__dict__)
    return price


def parse_nav_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            try:
                return dt.datetime.strptime(text[:10], fmt).date().isoformat()
            except ValueError:
                continue
    return None


def extract_nav_points(data: Any) -> list[tuple[str, float]]:
    points: list[tuple[str, float]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            nav_value = None
            date_value = None
            for key, item in value.items():
                key_norm = normalise(str(key))
                if key_norm in {"nav", "net asset value", "netassetvalue"}:
                    nav_value = item
                elif key_norm in {"date", "nav date", "navdate"}:
                    date_value = item
            date = parse_nav_date(date_value)
            nav = as_float(nav_value)
            if date and nav is not None:
                points.append((date, nav))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    unique_points = sorted(set(points), key=lambda x: x[0])
    return unique_points


def nav_change_from_points(points: list[tuple[str, float]], trade_date: str) -> PriceChange | None:
    usable = [(date, nav) for date, nav in points if date <= trade_date and nav > 0]
    if len(usable) < 2:
        return None
    price_date, nav = usable[-1]
    prev_date, prev_nav = usable[-2]
    if prev_nav == 0:
        return None
    return PriceChange(
        symbol="Official NAV",
        change_pct=(nav / prev_nav - 1.0) * 100.0,
        last_close=nav,
        previous_close=prev_nav,
        price_date=price_date,
    )


def amfi_display_date(value: str) -> str:
    return dt.date.fromisoformat(value).strftime("%d-%b-%Y")


def get_amfi_nav_history_text(cache: Cache, start_date: str, trade_date: str) -> str:
    key = f"amfi_nav_history:{start_date}:{trade_date}"
    cached = cache.runtime.get(key)
    if isinstance(cached, str):
        return cached
    params = {
        "tp": "1",
        "frmdt": amfi_display_date(start_date),
        "todt": amfi_display_date(trade_date),
    }
    url = f"{AMFI_NAV_HISTORY_URL}?{urllib.parse.urlencode(params)}"
    text = http_text_live(url, cache, "amfi_nav_history", retries=1)
    cache.runtime[key] = text
    return text


def amfi_nav_points_for_fund(text: str, fund: FundRow) -> list[tuple[str, float]]:
    if not fund.isin:
        return []
    target = fund.isin.strip().upper()
    points: list[tuple[str, float]] = []
    for row in csv.reader(io.StringIO(text), delimiter=";"):
        if len(row) < 8:
            continue
        if row[0].strip().lower() == "scheme code":
            continue
        isin_values = {row[2].strip().upper(), row[3].strip().upper()}
        if target not in isin_values:
            continue
        date = parse_nav_date(row[7])
        nav = as_float(row[4])
        if date and nav is not None:
            points.append((date, nav))
    return sorted(set(points), key=lambda x: x[0])


def get_nav_history_change(fund: FundRow, cache: Cache, trade_date: str) -> PriceChange | None:
    start_date = (dt.date.fromisoformat(trade_date) - dt.timedelta(days=14)).isoformat()
    try:
        text = get_amfi_nav_history_text(cache, start_date, trade_date)
    except Exception:
        return None
    price = nav_change_from_points(amfi_nav_points_for_fund(text, fund), trade_date)
    if price:
        price.symbol = "AMFI:NAV"
    return price


def estimate_nav_fund(fund: FundRow, cache: Cache, trade_date: str) -> FundEstimate | None:
    price = get_nav_history_change(fund, cache, trade_date)
    if not price:
        return None
    contributor = {
        "holding": "Official NAV change",
        "symbol": price.symbol,
        "weight_pct": 100.0,
        "stock_change_pct": round(price.change_pct, 4),
        "contribution_pct": round(price.change_pct, 4),
        "price_date": price.price_date,
        "stock_url": "",
    }
    return FundEstimate(
        fund=fund,
        analysis_date=trade_date,
        family_id=None,
        scheme_code=None,
        data_source="official_nav",
        reference_url="",
        holdings_month=f"Official NAV {price.price_date}",
        official_day_change_pct=None,
        estimated_change_pct=price.change_pct,
        equity_weight_pct=100.0,
        priced_weight_pct=100.0,
        missing_weight_pct=0.0,
        holdings_count=1,
        priced_count=1,
        missing_count=0,
        contributors=[contributor],
        missing=[],
    )


def estimate_benchmark_fund(
    fund: FundRow,
    config: dict[str, Any],
    cache: Cache,
    trade_date: str,
) -> FundEstimate | None:
    price = get_benchmark_change(config, cache, trade_date)
    if not price:
        return None
    name = str(config.get("name") or price.symbol)
    url = str(config.get("url") or "")
    contributor = {
        "holding": name,
        "symbol": price.symbol,
        "weight_pct": 100.0,
        "stock_change_pct": round(price.change_pct, 4),
        "contribution_pct": round(price.change_pct, 4),
        "price_date": price.price_date,
        "stock_url": url,
    }
    return FundEstimate(
        fund=fund,
        analysis_date=trade_date,
        family_id=None,
        scheme_code=None,
        data_source=f"benchmark:{config.get('source')}",
        reference_url=url,
        holdings_month=f"Benchmark {price.price_date}",
        official_day_change_pct=None,
        estimated_change_pct=price.change_pct,
        equity_weight_pct=100.0,
        priced_weight_pct=100.0,
        missing_weight_pct=0.0,
        holdings_count=1,
        priced_count=1,
        missing_count=0,
        contributors=[contributor],
        missing=[],
    )


def unique(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def estimate_from_holdings(
    fund: FundRow,
    family_id: int | None,
    scheme_code: int | None,
    data_source: str,
    reference_url: str,
    holdings_month: str,
    official_day_change_pct: float | None,
    holdings: list[Holding],
    cache: Cache,
    trade_date: str,
    min_weight: float,
    max_holdings: int,
    top: int,
) -> FundEstimate:
    holdings = [h for h in holdings if h.weight_pct >= min_weight]
    if max_holdings > 0:
        holdings = holdings[:max_holdings]

    contribution = 0.0
    priced_weight = 0.0
    equity_weight = sum(h.weight_pct for h in holdings)
    contributors: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for holding in holdings:
        price = (
            get_groww_price_change(holding, cache, trade_date)
            if data_source.startswith("groww") and trade_date == today_iso()
            else None
        )
        if price:
            contrib = holding.weight_pct * price.change_pct / 100.0
            contribution += contrib
            priced_weight += holding.weight_pct
            contributors.append(
                {
                    "holding": holding.name,
                    "symbol": price.symbol,
                    "weight_pct": round(holding.weight_pct, 4),
                    "stock_change_pct": round(price.change_pct, 4),
                    "contribution_pct": round(contrib, 4),
                    "price_date": price.price_date,
                    "stock_url": holding.stock_url,
                }
            )
        else:
            missing.append(
                {
                    "holding": holding.name,
                    "weight_pct": round(holding.weight_pct, 4),
                    "symbol_hint": holding.symbol_hint,
                    "stock_url": holding.stock_url,
                }
            )

    contributors.sort(key=lambda x: abs(x["contribution_pct"]), reverse=True)
    missing.sort(key=lambda x: x["weight_pct"], reverse=True)
    estimated_change_pct = contribution
    reported_contributors = contributors
    if priced_weight == 0 and official_day_change_pct is not None:
        estimated_change_pct = official_day_change_pct
        reported_contributors = [
            {
                "holding": "Groww fund 1D fallback",
                "symbol": "Groww:fund_1d",
                "weight_pct": 0.0,
                "stock_change_pct": round(official_day_change_pct, 4),
                "contribution_pct": round(official_day_change_pct, 4),
                "price_date": trade_date,
                "stock_url": reference_url,
            }
        ]
    return FundEstimate(
        fund=fund,
        analysis_date=trade_date,
        family_id=family_id,
        scheme_code=scheme_code,
        data_source=data_source,
        reference_url=reference_url,
        holdings_month=holdings_month,
        official_day_change_pct=official_day_change_pct,
        estimated_change_pct=estimated_change_pct,
        equity_weight_pct=equity_weight,
        priced_weight_pct=priced_weight,
        missing_weight_pct=max(equity_weight - priced_weight, 0.0),
        holdings_count=len(holdings),
        priced_count=len(contributors),
        missing_count=len(missing),
        contributors=reported_contributors[:top],
        missing=missing[:top],
    )


def estimate_mfdata_fund(
    fund: FundRow,
    family_id: int,
    scheme_code: int | None,
    cache: Cache,
    trade_date: str,
    min_weight: float,
    max_holdings: int,
    top: int,
) -> FundEstimate:
    month, holdings = get_holdings(family_id, cache)
    reference_url = f"{MF_DATA_BASE}/families/{family_id}/holdings?holding_type=equity"
    return estimate_from_holdings(
        fund,
        family_id,
        scheme_code,
        "mfdata",
        reference_url,
        month,
        None,
        holdings,
        cache,
        trade_date,
        min_weight,
        max_holdings,
        top,
    )


def estimate_groww_fund(
    fund: FundRow,
    groww_url: str,
    cache: Cache,
    trade_date: str,
    min_weight: float,
    max_holdings: int,
    top: int,
) -> FundEstimate:
    if trade_date != today_iso():
        nav_estimate = estimate_nav_fund(fund, cache, trade_date)
        if nav_estimate:
            return nav_estimate

    month, official_1d, holdings = get_groww_holdings(fund, groww_url, cache)
    return estimate_from_holdings(
        fund,
        None,
        None,
        "groww",
        groww_url,
        month,
        official_1d if trade_date == today_iso() else None,
        holdings,
        cache,
        trade_date,
        min_weight,
        max_holdings,
        top,
    )


def print_report(estimates: list[FundEstimate], unresolved: list[tuple[FundRow, str]]) -> None:
    has_values = any(est.invested_value is not None for est in estimates)
    headers = ["Date", "Fund"]
    if has_values:
        headers.append("Invested")
    headers.append("Est % today")
    if has_values:
        headers.append("Est Rs")
    headers.extend(["Benchmark", "Priced wt", "Missing wt", "Holdings", "Watchlist"])
    rows = []
    for est in estimates:
        row = [display_date(est.analysis_date), short_name(est.fund.name)]
        if has_values:
            row.append(format_money(est.invested_value))
        row.append(f"{est.estimated_change_pct:+.2f}%")
        if has_values:
            row.append(format_money(est.estimated_value_change))
        row.extend(
            [
                f"{est.benchmark_change_pct:+.2f}%" if est.benchmark_change_pct is not None else "",
                f"{est.priced_weight_pct:.1f}%",
                f"{est.missing_weight_pct:.1f}%",
                f"{est.priced_count}/{est.holdings_count}",
                " | ".join(est.watchlist_notes[:2]),
            ]
        )
        rows.append(row)
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(str(v))) for w, v in zip(widths, row)]
    print(" | ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(" | ".join(str(v).ljust(w) for v, w in zip(row, widths)))

    if unresolved:
        print("\nUnresolved funds:")
        for fund, reason in unresolved:
            print(f"- {fund.name} ({fund.isin}): {reason}")

    total_value = sum(est.invested_value or 0.0 for est in estimates)
    total_change = sum(est.estimated_value_change or 0.0 for est in estimates)
    if total_value:
        total_pct = total_change / total_value * 100.0
        print(f"\nEstimated value impact: {format_money(total_change)} ({total_pct:+.2f}%) on {format_money(total_value)}")
    elif estimates:
        avg_pct = sum(est.estimated_change_pct for est in estimates) / len(estimates)
        print(f"\nOverall equal-weight change: {avg_pct:+.2f}% across {len(estimates)} fund(s)")

    print("\nLargest contributors:")
    for est in estimates:
        top_items = est.contributors[:3]
        if not top_items:
            continue
        rendered = ", ".join(
            f"{i['holding']} {i['contribution_pct']:+.2f}pp" for i in top_items
        )
        print(f"- {short_name(est.fund.name)}: {rendered}")


def short_name(name: str) -> str:
    name = re.sub(r"\s*-\s*DIRECT PLAN", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:58]


def write_reports(
    estimates: list[FundEstimate],
    unresolved: list[tuple[FundRow, str]],
    reports_dir: Path,
    report_date: str,
) -> tuple[Path, Path]:
    reports_dir.mkdir(exist_ok=True)
    report_label = display_date(report_date)
    csv_path = reports_dir / f"mf_daily_change_{report_label}.csv"
    json_path = reports_dir / f"mf_daily_change_{report_label}.json"

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "fund",
                "analysis_date",
                "analysis_date_iso",
                "isin",
                "instrument_type",
                "reference_url",
                "invested_value",
                "estimated_value_change",
                "estimated_value_after_change",
                "estimated_change_pct",
                "benchmark_name",
                "benchmark_symbol",
                "benchmark_change_pct",
                "priced_weight_pct",
                "missing_weight_pct",
                "equity_weight_pct",
                "priced_holdings",
                "total_holdings",
                "watchlist_notes",
                "family_id",
                "scheme_code",
            ]
        )
        for est in estimates:
            writer.writerow(
                [
                    est.fund.name,
                    display_date(est.analysis_date),
                    est.analysis_date,
                    est.fund.isin,
                    est.fund.instrument_type,
                    est.reference_url,
                    "" if est.invested_value is None else round(est.invested_value, 6),
                    "" if est.estimated_value_change is None else round(est.estimated_value_change, 6),
                    "" if est.estimated_value_after_change is None else round(est.estimated_value_after_change, 6),
                    round(est.estimated_change_pct, 6),
                    est.benchmark_name,
                    est.benchmark_symbol,
                    "" if est.benchmark_change_pct is None else round(est.benchmark_change_pct, 6),
                    round(est.priced_weight_pct, 6),
                    round(est.missing_weight_pct, 6),
                    round(est.equity_weight_pct, 6),
                    est.priced_count,
                    est.holdings_count,
                    " | ".join(est.watchlist_notes),
                    est.family_id,
                    est.scheme_code,
                ]
            )

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "notes": [
            "Estimated change is holdings-weighted stock daily change, not official NAV movement.",
            "Groww is used first by default; MFData can be used for holdings fallback only.",
            "Arbitrage funds are excluded unless --include-arbitrage is used.",
        ],
        "funds": [
            {
                "fund": est.fund.__dict__,
                "analysis_date": display_date(est.analysis_date),
                "analysis_date_iso": est.analysis_date,
                "family_id": est.family_id,
                "scheme_code": est.scheme_code,
                "data_source": est.data_source,
                "reference_url": est.reference_url,
                "holdings_month": display_dates_in_text(est.holdings_month),
                "holdings_month_raw": est.holdings_month,
                "invested_value": est.invested_value,
                "estimated_value_change": est.estimated_value_change,
                "estimated_value_after_change": est.estimated_value_after_change,
                "estimated_change_pct": est.estimated_change_pct,
                "benchmark_name": est.benchmark_name,
                "benchmark_symbol": est.benchmark_symbol,
                "benchmark_change_pct": est.benchmark_change_pct,
                "underperformance_pct": est.underperformance_pct,
                "equity_weight_pct": est.equity_weight_pct,
                "priced_weight_pct": est.priced_weight_pct,
                "missing_weight_pct": est.missing_weight_pct,
                "holdings_count": est.holdings_count,
                "priced_count": est.priced_count,
                "missing_count": est.missing_count,
                "contributors": est.contributors,
                "missing": est.missing,
                "watchlist_notes": est.watchlist_notes,
            }
            for est in estimates
        ],
        "unresolved_funds": [{"fund": fund.__dict__, "reason": reason} for fund, reason in unresolved],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return csv_path, json_path


def estimate_to_dict(est: FundEstimate) -> dict[str, Any]:
    return {
        "fund": est.fund.__dict__,
        "analysis_date": display_date(est.analysis_date),
        "analysis_date_iso": est.analysis_date,
        "family_id": est.family_id,
        "scheme_code": est.scheme_code,
        "data_source": est.data_source,
        "reference_url": est.reference_url,
        "holdings_month": display_dates_in_text(est.holdings_month),
        "holdings_month_raw": est.holdings_month,
        "invested_value": est.invested_value,
        "estimated_value_change": est.estimated_value_change,
        "estimated_value_after_change": est.estimated_value_after_change,
        "estimated_change_pct": est.estimated_change_pct,
        "benchmark_name": est.benchmark_name,
        "benchmark_symbol": est.benchmark_symbol,
        "benchmark_change_pct": est.benchmark_change_pct,
        "underperformance_pct": est.underperformance_pct,
        "equity_weight_pct": est.equity_weight_pct,
        "priced_weight_pct": est.priced_weight_pct,
        "missing_weight_pct": est.missing_weight_pct,
        "holdings_count": est.holdings_count,
        "priced_count": est.priced_count,
        "missing_count": est.missing_count,
        "contributors": est.contributors,
        "missing": est.missing,
        "watchlist_notes": est.watchlist_notes,
    }


def write_interactive_report(
    estimates: list[FundEstimate],
    unresolved: list[dict[str, Any]],
    reports_dir: Path,
    report_label: str,
) -> Path:
    reports_dir.mkdir(exist_ok=True)
    html_path = reports_dir / f"mf_daily_change_{report_label}.html"
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "report_label": report_label,
        "funds": [estimate_to_dict(est) for est in estimates],
        "unresolved_funds": unresolved,
    }
    data_json = json.dumps(payload, ensure_ascii=True)
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mutual Fund Daily Change</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f3f5f7;
      --panel: #ffffff;
      --panel-soft: #f8fafb;
      --text: #1f2933;
      --muted: #66758a;
      --line: #d9e1ea;
      --pos: #087f5b;
      --neg: #c92a2a;
      --warn: #b7791f;
      --accent: #0f766e;
      --accent-2: #2f6f9f;
      --ink: #17212b;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }}
    body::before {{ content: ""; position: fixed; inset: 0 0 auto; height: 340px; background: radial-gradient(circle at 18% 20%, rgba(15, 118, 110, 0.18), transparent 28%), radial-gradient(circle at 86% 14%, rgba(47, 111, 159, 0.16), transparent 30%), linear-gradient(135deg, #17212b 0%, #243240 54%, #314153 100%); z-index: -1; }}
    header {{ padding: 26px 28px 18px; color: #f8fafc; }}
    .hero {{ display: grid; grid-template-columns: 1fr minmax(360px, 0.9fr); gap: 22px; align-items: stretch; max-width: 1500px; margin: 0 auto; }}
    .heroTitle {{ display: flex; flex-direction: column; justify-content: space-between; min-height: 176px; }}
    .eyebrow {{ width: fit-content; border: 1px solid rgba(255,255,255,0.18); background: rgba(255,255,255,0.08); border-radius: 999px; padding: 7px 10px; color: #dce7ef; font-size: 12px; font-weight: 760; }}
    h1 {{ margin: 14px 0 8px; font-size: clamp(30px, 4vw, 54px); line-height: 1.03; font-weight: 840; letter-spacing: 0; max-width: 820px; }}
    .sub {{ color: #bfd0dd; font-size: 14px; }}
    .heroActions {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 16px; }}
    .liveLink {{ color: #071317; border: 0; border-radius: 999px; padding: 10px 14px; background: #b7f4df; font-weight: 800; font-size: 13px; white-space: nowrap; box-shadow: 0 8px 24px rgba(0,0,0,0.16); }}
    .heroPanel {{ border: 1px solid rgba(255,255,255,0.15); background: rgba(255,255,255,0.09); border-radius: 18px; padding: 18px; backdrop-filter: blur(16px); box-shadow: 0 22px 60px rgba(0,0,0,0.22); }}
    .heroMetric {{ display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: center; }}
    .heroMetric .label {{ color: #bfd0dd; font-size: 12px; font-weight: 760; text-transform: uppercase; }}
    .heroMetric .value {{ font-size: 46px; line-height: 1; font-weight: 860; }}
    .sparkline {{ height: 54px; display: flex; align-items: end; gap: 5px; justify-content: flex-end; }}
    .sparkline span {{ width: 9px; min-height: 8px; border-radius: 999px 999px 0 0; background: #70e2bd; opacity: 0.95; }}
    .heroGrid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 18px; }}
    .heroMini {{ border: 1px solid rgba(255,255,255,0.12); border-radius: 12px; background: rgba(255,255,255,0.08); padding: 12px; min-height: 82px; }}
    .heroMini .label {{ color: #bfd0dd; font-size: 11px; font-weight: 760; text-transform: uppercase; }}
    .heroMini .value {{ margin-top: 6px; font-size: 17px; font-weight: 820; color: #fff; line-height: 1.25; }}
    main {{ padding: 0 28px 30px; max-width: 1500px; margin: 0 auto; }}
    .toolbar {{ margin-top: 8px; border: 1px solid rgba(217,225,234,0.85); background: rgba(255,255,255,0.88); backdrop-filter: blur(18px); border-radius: 16px; padding: 14px; box-shadow: 0 18px 50px rgba(31,41,51,0.10); }}
    .controls {{ display: grid; grid-template-columns: 1.7fr minmax(150px, 220px) minmax(170px, 230px) auto; gap: 10px; margin-bottom: 12px; }}
    input, select {{ width: 100%; border: 1px solid var(--line); border-radius: 11px; padding: 12px 13px; background: white; color: var(--text); font-size: 14px; outline: none; }}
    input:focus, select:focus {{ border-color: #74b7a6; box-shadow: 0 0 0 3px rgba(15,118,110,0.12); }}
    .viewToggle {{ display: inline-flex; border: 1px solid var(--line); background: #edf2f6; border-radius: 12px; padding: 3px; gap: 3px; height: 44px; }}
    .viewToggle button {{ border: 0; border-radius: 9px; padding: 8px 12px; background: transparent; color: #465568; font-weight: 800; }}
    .viewToggle button.active {{ background: #fff; color: var(--accent); box-shadow: 0 1px 4px rgba(31,41,51,0.10); }}
    .quickbar {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    .chipBtn {{ border: 1px solid var(--line); background: #fff; border-radius: 999px; padding: 8px 12px; cursor: pointer; color: #334155; font-size: 12px; font-weight: 750; }}
    .chipBtn:hover {{ background: #f2f7f6; }}
    .chipBtn.active {{ background: #dff5ef; border-color: #8ed5c1; color: var(--accent); }}
    .spacer {{ flex: 1; min-width: 16px; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 12px; margin: 16px 0; }}
    .stat {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 16px; box-shadow: 0 10px 30px rgba(31,41,51,0.07); }}
    .stat .label {{ color: var(--muted); font-size: 12px; margin-bottom: 7px; font-weight: 760; text-transform: uppercase; }}
    .stat .value {{ font-size: 26px; font-weight: 830; }}
    .stat .sub {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .sectionHead {{ display: flex; justify-content: space-between; align-items: end; gap: 12px; margin: 22px 0 10px; }}
    .sectionHead h2 {{ margin: 0; font-size: 18px; }}
    .sectionHead p {{ margin: 4px 0 0; color: var(--muted); font-size: 13px; }}
    .insights {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin: 0 0 18px; }}
    .fundCard {{ border: 1px solid var(--line); background: linear-gradient(180deg, #fff 0%, #fbfcfd 100%); border-radius: 16px; padding: 16px; cursor: pointer; min-height: 142px; display: grid; gap: 13px; box-shadow: 0 12px 32px rgba(31,41,51,0.07); position: relative; overflow: hidden; }}
    .fundCard::before {{ content: ""; position: absolute; inset: 0 auto 0 0; width: 5px; background: var(--pos); }}
    .fundCard.negBorder::before {{ background: var(--neg); }}
    .fundCard:hover {{ transform: translateY(-3px); box-shadow: 0 18px 42px rgba(31,41,51,0.13); border-color: #b8c8d8; }}
    .fundCard .top {{ display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }}
    .fundCard .name {{ font-size: 14px; font-weight: 830; color: #1f5f84; line-height: 1.2; padding-left: 2px; }}
    .fundCard .move {{ font-size: 26px; font-weight: 880; white-space: nowrap; }}
    .range {{ height: 10px; border-radius: 999px; background: #e8edf3; overflow: hidden; }}
    .range span {{ display: block; height: 100%; width: 0; border-radius: inherit; background: linear-gradient(90deg, #2b8a6e, #2f9e44); }}
    .fundCard.negBorder .range span {{ background: linear-gradient(90deg, #e03131, #f08c00); }}
    .metaLine {{ display: flex; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 12px; font-weight: 650; }}
    .tableWrap {{ background: var(--panel); border: 1px solid var(--line); border-radius: 16px; overflow: auto; max-height: calc(100vh - 260px); box-shadow: 0 12px 32px rgba(31,41,51,0.07); }}
    table {{ width: 100%; min-width: 1180px; border-collapse: separate; border-spacing: 0; font-size: 13px; }}
    th, td {{ padding: 12px 14px; border-bottom: 1px solid var(--line); vertical-align: middle; background: #fff; }}
    th {{ position: sticky; top: 0; z-index: 3; text-align: left; color: #334155; background: #f1f5f8; user-select: none; cursor: pointer; white-space: nowrap; box-shadow: inset 0 -1px 0 var(--line); }}
    th.sortActive::after {{ content: attr(data-dir); margin-left: 6px; color: var(--accent); }}
    tbody tr.mainRow:hover td {{ background: #f8fafc; }}
    .stickyDate {{ position: sticky; left: 0; z-index: 2; min-width: 118px; }}
    .stickyFund {{ position: sticky; left: 118px; z-index: 2; min-width: 320px; max-width: 380px; box-shadow: 1px 0 0 var(--line); }}
    th.stickyDate, th.stickyFund {{ z-index: 4; background: #f1f5f8; }}
    .mainRow.open td {{ background: #f8fbfd; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .dateCell {{ white-space: nowrap; }}
    .fundCell a {{ color: #1f5f84; font-weight: 700; }}
    .fundMeta {{ margin-top: 4px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; color: var(--muted); font-size: 11px; }}
    .pos {{ color: var(--pos); font-weight: 650; }}
    .neg {{ color: var(--neg); font-weight: 650; }}
    .muted {{ color: var(--muted); }}
    .pill {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 8px; background: #edf2f7; color: #475569; font-size: 11px; font-weight: 780; white-space: nowrap; }}
    .pill.pos {{ background: #e6f4ef; color: var(--pos); }}
    .pill.neg {{ background: #fdecec; color: var(--neg); }}
    .pill.warn {{ background: #fff4d6; color: #8a5a00; }}
    .watch {{ max-width: 260px; color: #475569; font-size: 12px; line-height: 1.3; }}
    .bar {{ height: 8px; min-width: 92px; border-radius: 999px; background: #e8edf3; overflow: hidden; }}
    .bar span {{ display: block; height: 100%; border-radius: inherit; background: #2b8a6e; }}
    .barWrap {{ display: grid; gap: 4px; justify-items: end; }}
    button {{ border: 1px solid var(--line); background: #fff; border-radius: 10px; padding: 8px 11px; cursor: pointer; color: var(--accent); font-weight: 760; }}
    button:hover {{ background: #f2f7f6; }}
    .details {{ display: none; background: #fbfcfe; }}
    .details.open {{ display: table-row; }}
    .details td {{ background: #fbfcfe; }}
    .detailGrid {{ display: grid; grid-template-columns: 1.15fr 1fr 0.9fr; gap: 14px; padding: 6px 0; }}
    .detailBox {{ border: 1px solid var(--line); border-radius: 8px; background: white; overflow: hidden; }}
    .detailBox h3 {{ margin: 0; padding: 10px 12px; font-size: 13px; background: #f1f5f9; }}
    .detailBox ul {{ margin: 10px 18px 12px; padding: 0 0 0 12px; color: #475569; }}
    .mini td, .mini th {{ padding: 8px 10px; font-size: 12px; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .empty {{ padding: 18px; color: var(--muted); }}
    body.noValues .valueCol {{ display: none; }}
    body.noValues table {{ min-width: 1080px; }}
    body.viewCards .tableSection {{ display: none; }}
    body.viewTable .cardsSection {{ display: none; }}
    body.dark {{
      --bg: #0f1720;
      --panel: #17212b;
      --panel-soft: #1d2935;
      --text: #e5edf3;
      --muted: #9cadbd;
      --line: #2c3b49;
      --pos: #5ce0b0;
      --neg: #ff7f7f;
      --warn: #f4c76d;
      --accent: #62d7bd;
      --accent-2: #8fc7ff;
      --ink: #0b1117;
    }}
    body.dark::before {{ background: radial-gradient(circle at 18% 20%, rgba(92,224,176,0.14), transparent 28%), radial-gradient(circle at 86% 14%, rgba(143,199,255,0.12), transparent 30%), linear-gradient(135deg, #071017 0%, #111d28 55%, #182634 100%); }}
    body.dark .toolbar {{ background: rgba(23,33,43,0.88); border-color: rgba(80,99,118,0.7); box-shadow: 0 18px 50px rgba(0,0,0,0.22); }}
    body.dark input, body.dark select {{ background: #101923; color: var(--text); border-color: var(--line); }}
    body.dark input::placeholder {{ color: #7f91a3; }}
    body.dark .viewToggle {{ background: #101923; border-color: var(--line); }}
    body.dark .viewToggle button {{ color: #aab8c8; }}
    body.dark .viewToggle button.active {{ background: #223142; color: var(--accent); }}
    body.dark .chipBtn, body.dark button {{ background: #17212b; color: #cfe4ec; border-color: var(--line); }}
    body.dark .chipBtn:hover, body.dark button:hover {{ background: #1f2d3a; }}
    body.dark .chipBtn.active {{ background: rgba(92,224,176,0.13); border-color: rgba(92,224,176,0.45); color: var(--accent); }}
    body.dark .stat, body.dark .fundCard, body.dark .tableWrap, body.dark .detailBox {{ background: #17212b; border-color: var(--line); box-shadow: 0 12px 34px rgba(0,0,0,0.22); }}
    body.dark .fundCard {{ background: linear-gradient(180deg, #17212b 0%, #141e28 100%); }}
    body.dark .fundCard .name, body.dark .fundCell a {{ color: #9bd4ff; }}
    body.dark .range, body.dark .bar {{ background: #273545; }}
    body.dark th, body.dark th.stickyDate, body.dark th.stickyFund {{ background: #1d2935; color: #cdd8e4; box-shadow: inset 0 -1px 0 var(--line); }}
    body.dark td {{ background: #17212b; border-color: var(--line); }}
    body.dark tbody tr.mainRow:hover td, body.dark .mainRow.open td, body.dark .details td {{ background: #1a2632; }}
    body.dark .details {{ background: #1a2632; }}
    body.dark .detailBox h3 {{ background: #1f2d3a; color: #e5edf3; }}
    body.dark .pill {{ background: #223142; color: #cdd8e4; }}
    body.dark .pill.pos {{ background: rgba(92,224,176,0.13); color: var(--pos); }}
    body.dark .pill.neg {{ background: rgba(255,127,127,0.13); color: var(--neg); }}
    body.dark .pill.warn {{ background: rgba(244,199,109,0.16); color: var(--warn); }}
    body.dark .watch, body.dark .detailBox ul {{ color: #aebdca; }}
    body.dark .empty {{ color: var(--muted); }}
    @media (max-width: 900px) {{
      main, header {{ padding-left: 14px; padding-right: 14px; }}
      .hero, .controls, .stats, .detailGrid, .heroGrid {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 34px; }}
      .tableWrap {{ max-height: none; }}
      .stickyDate, .stickyFund {{ position: static; min-width: auto; max-width: none; box-shadow: none; }}
      th:nth-child(7), td:nth-child(7), th:nth-child(9), td:nth-child(9) {{ display: none; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="hero">
      <div class="heroTitle">
        <div>
          <div class="eyebrow">Daily fund monitor</div>
          <h1>Mutual Fund Daily Change</h1>
          <div class="sub" id="generated"></div>
        </div>
        <div class="heroActions">
          <a class="liveLink" href="https://vipi-n.github.io/mf-daily-report/" target="_blank">Open live report</a>
          <button class="chipBtn" id="themeToggle" type="button">Dark mode</button>
        </div>
      </div>
      <aside class="heroPanel">
        <div class="heroMetric">
          <div>
            <div class="label">Overall move</div>
            <div class="value" id="heroOverall">--</div>
          </div>
          <div class="sparkline" id="sparkline"></div>
        </div>
        <div class="heroGrid">
          <div class="heroMini">
            <div class="label">Best fund</div>
            <div class="value" id="heroBest">--</div>
          </div>
          <div class="heroMini">
            <div class="label">Weakest fund</div>
            <div class="value" id="heroWorst">--</div>
          </div>
        </div>
      </aside>
    </div>
  </header>
  <main>
    <section class="toolbar">
      <section class="controls">
        <input id="search" placeholder="Search fund or contributor">
        <select id="dateFilter"></select>
        <select id="sortFilter">
          <option value="fund">Sort: Fund</option>
          <option value="change_desc">Sort: Highest change</option>
          <option value="change_asc">Sort: Lowest change</option>
          <option class="valueCol" value="value_change_desc">Sort: Highest Rs change</option>
          <option class="valueCol" value="value_change_asc">Sort: Lowest Rs change</option>
          <option value="missing_desc">Sort: Missing weight</option>
        </select>
        <div class="viewToggle" aria-label="View mode">
          <button id="viewCards" type="button" class="active">Cards</button>
          <button id="viewTable" type="button">Table</button>
        </div>
      </section>
      <section class="quickbar" id="quickbar">
        <button class="chipBtn active" data-filter="all">All</button>
        <button class="chipBtn" data-filter="gainers">Gainers</button>
        <button class="chipBtn" data-filter="losers">Losers</button>
        <button class="chipBtn" data-filter="watchlist">Watchlist</button>
        <button class="chipBtn" data-filter="missing">Missing Data</button>
        <span class="spacer"></span>
        <button class="chipBtn" id="clearFilters" type="button">Reset</button>
        <button class="chipBtn" id="expandAll" type="button">Expand All</button>
      </section>
    </section>
    <section class="stats" id="stats"></section>
    <section class="cardsSection">
      <div class="sectionHead">
        <div>
          <h2>Fund Board</h2>
          <p>Click a card to focus the table on that fund.</p>
        </div>
      </div>
      <section class="insights" id="insights"></section>
    </section>
    <section class="tableSection">
      <div class="sectionHead">
        <div>
          <h2>Detailed Table</h2>
          <p>Sort, expand rows, and inspect contributors or missing holdings.</p>
        </div>
      </div>
      <section class="tableWrap">
        <table>
          <thead>
            <tr>
              <th data-sort="analysis_date" class="stickyDate">Date</th>
              <th data-sort="fund" class="stickyFund">Fund</th>
              <th data-sort="invested_value" class="num valueCol">Invested</th>
              <th data-sort="estimated_value_change" class="num valueCol">Est Rs</th>
              <th data-sort="estimated_change_pct" class="num">Est %</th>
              <th data-sort="benchmark_change_pct" class="num">Benchmark</th>
              <th data-sort="priced_weight_pct" class="num">Priced Wt</th>
              <th data-sort="missing_weight_pct" class="num">Missing Wt</th>
              <th>Watchlist</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody id="rows"></tbody>
        </table>
      </section>
    </section>
  </main>
  <script id="report-data" type="application/json">{data_json}</script>
  <script>
    const report = JSON.parse(document.getElementById('report-data').textContent);
    const rowsEl = document.getElementById('rows');
    const searchEl = document.getElementById('search');
    const dateEl = document.getElementById('dateFilter');
    const sortEl = document.getElementById('sortFilter');
    const insightsEl = document.getElementById('insights');
    const viewCardsEl = document.getElementById('viewCards');
    const viewTableEl = document.getElementById('viewTable');
    const themeToggleEl = document.getElementById('themeToggle');
    const hasValues = report.funds.some(f => f.invested_value !== null && f.invested_value !== undefined && f.estimated_value_change !== null && f.estimated_value_change !== undefined);
    let sortKey = 'fund';
    let sortDir = 1;
    let activeQuick = 'all';
    let expandedAll = false;
    let viewMode = 'cards';

    const pct = v => v === null || v === undefined || v === '' ? '' : `${{Number(v).toFixed(2)}}%`;
    const money = v => v === null || v === undefined || v === '' ? '' : `Rs ${{Math.round(Number(v)).toLocaleString('en-IN')}}`;
    const cls = v => Number(v) >= 0 ? 'pos' : 'neg';
    const signed = v => Number(v || 0) >= 0 ? `+${{pct(v)}}` : pct(v);
    const rowId = f => `${{f.analysis_date_iso}}-${{fundName(f)}}`.replace(/[^a-z0-9]+/gi, '-').toLowerCase();
    const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    const fundName = f => (f.fund?.name || '').replace(/\\s*-\\s*DIRECT PLAN/i, '');
    const generatedDate = new Date(report.generated_at);
    const generatedText = `${{String(generatedDate.getDate()).padStart(2, '0')}}-${{String(generatedDate.getMonth() + 1).padStart(2, '0')}}-${{generatedDate.getFullYear()}}, ${{generatedDate.toLocaleTimeString()}}`;

    document.body.classList.toggle('noValues', !hasValues);
    document.body.classList.add('viewCards');
    document.getElementById('generated').textContent = `Generated ${{generatedText}} | ${{report.funds.length}} rows`;

    function applyTheme(theme) {{
      const dark = theme === 'dark';
      document.body.classList.toggle('dark', dark);
      themeToggleEl.textContent = dark ? 'Light mode' : 'Dark mode';
      themeToggleEl.setAttribute('aria-pressed', dark ? 'true' : 'false');
    }}

    const storedTheme = localStorage.getItem('mf-report-theme');
    applyTheme(storedTheme || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));

    function populateFilters() {{
      const dates = [...new Map(report.funds.map(f => [f.analysis_date_iso, f.analysis_date])).entries()].sort((a, b) => a[0].localeCompare(b[0]));
      dateEl.innerHTML = '<option value="">All dates</option>' + dates.map(d => `<option value="${{esc(d[1])}}">${{esc(d[1])}}</option>`).join('');
    }}

    function filteredRows() {{
      const q = searchEl.value.trim().toLowerCase();
      const date = dateEl.value;
      let rows = report.funds.filter(f => {{
        const hay = [fundName(f), f.holdings_month, ...(f.contributors || []).map(c => c.holding)].join(' ').toLowerCase();
        const quick =
          activeQuick === 'all' ||
          (activeQuick === 'gainers' && Number(f.estimated_change_pct || 0) > 0) ||
          (activeQuick === 'losers' && Number(f.estimated_change_pct || 0) < 0) ||
          (activeQuick === 'watchlist' && (f.watchlist_notes || []).length > 0) ||
          (activeQuick === 'missing' && Number(f.missing_weight_pct || 0) > 0);
        return quick && (!q || hay.includes(q)) && (!date || f.analysis_date === date);
      }});
      rows.sort((a, b) => {{
        if (sortEl.value === 'change_desc') return b.estimated_change_pct - a.estimated_change_pct;
        if (sortEl.value === 'change_asc') return a.estimated_change_pct - b.estimated_change_pct;
        if (sortEl.value === 'value_change_desc') return Number(b.estimated_value_change || 0) - Number(a.estimated_value_change || 0);
        if (sortEl.value === 'value_change_asc') return Number(a.estimated_value_change || 0) - Number(b.estimated_value_change || 0);
        if (sortEl.value === 'missing_desc') return b.missing_weight_pct - a.missing_weight_pct;
        const av = sortKey === 'fund' ? fundName(a) : sortKey === 'analysis_date' ? a.analysis_date_iso : a[sortKey];
        const bv = sortKey === 'fund' ? fundName(b) : sortKey === 'analysis_date' ? b.analysis_date_iso : b[sortKey];
        if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sortDir;
        return String(av ?? '').localeCompare(String(bv ?? '')) * sortDir;
      }});
      return rows;
    }}

    function renderSortState() {{
      document.querySelectorAll('th[data-sort]').forEach(th => {{
        th.classList.toggle('sortActive', th.dataset.sort === sortKey && sortEl.value === 'fund');
        th.dataset.dir = sortDir > 0 ? 'up' : 'down';
      }});
    }}

    function renderStats(rows) {{
      const avg = rows.length ? rows.reduce((s, r) => s + Number(r.estimated_change_pct || 0), 0) / rows.length : 0;
      const missing = rows.length ? rows.reduce((s, r) => s + Number(r.missing_weight_pct || 0), 0) / rows.length : 0;
      const gainers = rows.filter(r => Number(r.estimated_change_pct || 0) > 0).length;
      const losers = rows.filter(r => Number(r.estimated_change_pct || 0) < 0).length;
      const value = rows.reduce((s, r) => s + Number(r.invested_value || 0), 0);
      const valueChange = rows.reduce((s, r) => s + Number(r.estimated_value_change || 0), 0);
      const portfolioPct = value ? valueChange / value * 100 : 0;
      if (!hasValues) {{
        document.getElementById('stats').innerHTML = `
          <div class="stat"><div class="label">Visible Funds</div><div class="value">${{rows.length}}</div></div>
          <div class="stat"><div class="label">Overall Change</div><div class="value ${{cls(avg)}}">${{signed(avg)}}</div><div class="sub">Equal-weight average</div></div>
          <div class="stat"><div class="label">Market Breadth</div><div class="value">${{gainers}} / ${{losers}}</div><div class="sub">Gainers / Losers</div></div>
          <div class="stat"><div class="label">Average Missing</div><div class="value">${{pct(missing)}}</div></div>
          <div class="stat"><div class="label">Watchlist Flags</div><div class="value">${{rows.filter(r => (r.watchlist_notes || []).length > 0).length}}</div></div>
        `;
        return;
      }}
      document.getElementById('stats').innerHTML = `
        <div class="stat"><div class="label">Visible Funds</div><div class="value">${{rows.length}}</div></div>
        <div class="stat"><div class="label">Invested Value</div><div class="value">${{money(value)}}</div></div>
        <div class="stat"><div class="label">Estimated Rs Change</div><div class="value ${{cls(valueChange)}}">${{money(valueChange)}}</div></div>
        <div class="stat"><div class="label">Portfolio Estimate</div><div class="value ${{cls(portfolioPct)}}">${{pct(portfolioPct)}}</div><div class="sub">Avg fund estimate ${{pct(avg)}} | Avg missing ${{pct(missing)}}</div></div>
      `;
    }}

    function renderHero(rows) {{
      const allRows = rows.length ? rows : report.funds;
      const avg = allRows.length ? allRows.reduce((s, r) => s + Number(r.estimated_change_pct || 0), 0) / allRows.length : 0;
      const ranked = [...allRows].sort((a, b) => Number(b.estimated_change_pct || 0) - Number(a.estimated_change_pct || 0));
      const best = ranked[0];
      const worst = ranked[ranked.length - 1];
      document.getElementById('heroOverall').className = `value ${{cls(avg)}}`;
      document.getElementById('heroOverall').textContent = signed(avg);
      document.getElementById('heroBest').innerHTML = best ? `${{esc(fundName(best))}}<br><span class="${{cls(best.estimated_change_pct)}}">${{signed(best.estimated_change_pct)}}</span>` : '--';
      document.getElementById('heroWorst').innerHTML = worst ? `${{esc(fundName(worst))}}<br><span class="${{cls(worst.estimated_change_pct)}}">${{signed(worst.estimated_change_pct)}}</span>` : '--';
      const maxAbs = Math.max(0.1, ...allRows.map(r => Math.abs(Number(r.estimated_change_pct || 0))));
      document.getElementById('sparkline').innerHTML = allRows
        .slice()
        .sort((a, b) => Number(a.estimated_change_pct || 0) - Number(b.estimated_change_pct || 0))
        .map(r => {{
          const move = Number(r.estimated_change_pct || 0);
          const height = 12 + Math.abs(move) / maxAbs * 40;
          const color = move >= 0 ? '#70e2bd' : '#ff8a80';
          return `<span title="${{esc(fundName(r))}} ${{signed(move)}}" style="height:${{height}}px;background:${{color}}"></span>`;
        }}).join('');
    }}

    function renderInsights(rows) {{
      if (!rows.length) {{
        insightsEl.innerHTML = '';
        return;
      }}
      const maxAbs = Math.max(0.1, ...rows.map(r => Math.abs(Number(r.estimated_change_pct || 0))));
      const ranked = [...rows].sort((a, b) => Math.abs(Number(b.estimated_change_pct || 0)) - Math.abs(Number(a.estimated_change_pct || 0)));
      insightsEl.innerHTML = ranked.map(f => {{
        const move = Number(f.estimated_change_pct || 0);
        const width = Math.max(6, Math.abs(move) / maxAbs * 100);
        const benchmark = f.benchmark_change_pct == null ? `${{f.benchmark_name || 'Benchmark'}} pending` : `${{f.benchmark_name || 'Benchmark'}} ${{signed(f.benchmark_change_pct)}}`;
        const watch = (f.watchlist_notes || []).length ? '<span class="pill warn">Watchlist</span>' : '';
        return `
          <article class="fundCard ${{move >= 0 ? 'posBorder' : 'negBorder'}}" data-fund="${{esc(fundName(f))}}">
            <div class="top">
              <div class="name">${{esc(fundName(f))}}</div>
              <div class="move ${{cls(move)}}">${{signed(move)}}</div>
            </div>
            <div class="range"><span style="width:${{width}}%"></span></div>
            <div class="metaLine"><span>${{esc(benchmark)}}</span><span>Priced ${{pct(f.priced_weight_pct)}}</span></div>
            <div class="fundMeta">${{watch}}<span class="pill">${{esc(f.holdings_month || 'Latest data')}}</span></div>
          </article>
        `;
      }}).join('');
      insightsEl.querySelectorAll('.fundCard').forEach(card => card.addEventListener('click', () => {{
        searchEl.value = card.dataset.fund || '';
        activeQuick = 'all';
        document.querySelectorAll('#quickbar .chipBtn[data-filter]').forEach(b => b.classList.toggle('active', b.dataset.filter === 'all'));
        render();
      }}));
    }}

    function miniTable(items, emptyText) {{
      if (!items || !items.length) return `<div class="empty">${{emptyText}}</div>`;
      return `<table class="mini"><thead><tr><th>Name</th><th class="num">Weight</th><th class="num">Move</th><th class="num">Contribution</th></tr></thead><tbody>` +
        items.map(i => `<tr><td>${{i.stock_url ? `<a href="${{esc(i.stock_url)}}" target="_blank">${{esc(i.holding)}}</a>` : esc(i.holding)}}</td><td class="num">${{pct(i.weight_pct)}}</td><td class="num ${{cls(i.stock_change_pct)}}">${{pct(i.stock_change_pct)}}</td><td class="num ${{cls(i.contribution_pct)}}">${{pct(i.contribution_pct)}}</td></tr>`).join('') +
        `</tbody></table>`;
    }}

    function missingTable(items) {{
      if (!items || !items.length) return `<div class="empty">No missing priced holdings.</div>`;
      return `<table class="mini"><thead><tr><th>Name</th><th class="num">Weight</th><th>Hint</th></tr></thead><tbody>` +
        items.map(i => `<tr><td>${{i.stock_url ? `<a href="${{esc(i.stock_url)}}" target="_blank">${{esc(i.holding)}}</a>` : esc(i.holding)}}</td><td class="num">${{pct(i.weight_pct)}}</td><td>${{esc(i.symbol_hint || '')}}</td></tr>`).join('') +
        `</tbody></table>`;
    }}

    function notesList(items) {{
      if (!items || !items.length) return '<div class="empty">No watchlist flags.</div>';
      return `<ul>${{items.map(n => `<li>${{esc(n)}}</li>`).join('')}}</ul>`;
    }}

    function render() {{
      const rows = filteredRows();
      renderHero(rows);
      renderStats(rows);
      renderInsights(rows);
      renderSortState();
      rowsEl.innerHTML = rows.map((f) => {{
        const id = rowId(f);
        const isOpen = expandedAll;
        const confidence = Math.max(0, Math.min(100, Number(f.priced_weight_pct || 0)));
        const watch = (f.watchlist_notes || [])[0] || '';
        const benchmarkLabel = f.benchmark_name ? `<span class="pill">${{esc(f.benchmark_name)}}</span>` : '';
        return `
        <tr class="mainRow ${{isOpen ? 'open' : ''}}" data-row="${{esc(id)}}">
          <td class="dateCell stickyDate">${{esc(f.analysis_date)}}</td>
          <td class="fundCell stickyFund">
            ${{f.reference_url ? `<a href="${{esc(f.reference_url)}}" target="_blank">${{esc(fundName(f))}}</a>` : esc(fundName(f))}}
            <div class="fundMeta">
              ${{benchmarkLabel}}
              ${{watch ? '<span class="pill warn">Flag</span>' : ''}}
            </div>
          </td>
          <td class="num valueCol">${{money(f.invested_value)}}</td>
          <td class="num valueCol ${{cls(f.estimated_value_change)}}">${{money(f.estimated_value_change)}}</td>
          <td class="num"><span class="pill ${{cls(f.estimated_change_pct)}}">${{pct(f.estimated_change_pct)}}</span></td>
          <td class="num ${{f.benchmark_change_pct == null ? 'muted' : cls(f.benchmark_change_pct)}}">${{pct(f.benchmark_change_pct)}}</td>
          <td class="num"><div class="barWrap"><span>${{pct(f.priced_weight_pct)}}</span><div class="bar"><span style="width:${{confidence}}%"></span></div></div></td>
          <td class="num">${{pct(f.missing_weight_pct)}}</td>
          <td class="watch">${{esc((f.watchlist_notes || [])[0] || '')}}</td>
          <td><button data-row="${{esc(id)}}">${{isOpen ? 'Close' : 'Open'}}</button></td>
        </tr>
        <tr class="details ${{isOpen ? 'open' : ''}}" id="detail-${{esc(id)}}"><td colspan="10"><div class="detailGrid">
          <div class="detailBox"><h3>Largest Contributors</h3>${{miniTable(f.contributors, 'No priced contributors.')}}</div>
          <div class="detailBox"><h3>Missing Holdings</h3>${{missingTable(f.missing)}}</div>
          <div class="detailBox"><h3>Watchlist Notes</h3>${{notesList(f.watchlist_notes)}}</div>
        </div></td></tr>
      `; }}).join('') || '<tr><td colspan="10" class="empty">No rows match the current filters.</td></tr>';
      rowsEl.querySelectorAll('button[data-row]').forEach(btn => btn.addEventListener('click', () => {{
        const row = document.getElementById(`detail-${{btn.dataset.row}}`);
        const main = rowsEl.querySelector(`tr[data-row="${{btn.dataset.row}}"]`);
        row.classList.toggle('open');
        main?.classList.toggle('open', row.classList.contains('open'));
        btn.textContent = row.classList.contains('open') ? 'Close' : 'Open';
      }}));
    }}

    document.querySelectorAll('th[data-sort]').forEach(th => th.addEventListener('click', () => {{
      const next = th.dataset.sort;
      sortDir = sortKey === next ? -sortDir : 1;
      sortKey = next;
      sortEl.value = 'fund';
      render();
    }}));
    [searchEl, dateEl, sortEl].forEach(el => el.addEventListener('input', render));
    document.querySelectorAll('#quickbar .chipBtn[data-filter]').forEach(btn => btn.addEventListener('click', () => {{
      activeQuick = btn.dataset.filter;
      document.querySelectorAll('#quickbar .chipBtn[data-filter]').forEach(b => b.classList.toggle('active', b === btn));
      render();
    }}));
    document.getElementById('expandAll').addEventListener('click', () => {{
      expandedAll = !expandedAll;
      document.getElementById('expandAll').textContent = expandedAll ? 'Collapse All' : 'Expand All';
      render();
    }});
    document.getElementById('clearFilters').addEventListener('click', () => {{
      searchEl.value = '';
      dateEl.value = '';
      sortEl.value = 'fund';
      sortKey = 'fund';
      sortDir = 1;
      activeQuick = 'all';
      document.querySelectorAll('#quickbar .chipBtn[data-filter]').forEach(b => b.classList.toggle('active', b.dataset.filter === 'all'));
      render();
    }});
    function setViewMode(mode) {{
      viewMode = mode;
      document.body.classList.toggle('viewCards', mode === 'cards');
      document.body.classList.toggle('viewTable', mode === 'table');
      viewCardsEl.classList.toggle('active', mode === 'cards');
      viewTableEl.classList.toggle('active', mode === 'table');
    }}
    viewCardsEl.addEventListener('click', () => setViewMode('cards'));
    viewTableEl.addEventListener('click', () => setViewMode('table'));
    themeToggleEl.addEventListener('click', () => {{
      const next = document.body.classList.contains('dark') ? 'light' : 'dark';
      localStorage.setItem('mf-report-theme', next);
      applyTheme(next);
    }});
    populateFilters();
    setViewMode('cards');
    render();
  </script>
</body>
</html>
"""
    html_path.write_text(html, encoding="utf-8")
    return html_path


def main() -> int:
    args = parse_args()
    holdings_path = Path(args.input)
    if not holdings_path.exists():
        print(f"error: holdings file not found: {holdings_path}", file=sys.stderr)
        return 2

    save_default_json(
        Path("fund_overrides.json"),
        {
            "_comment": "Optional. Key by fund ISIN or exact fund name. Example: {'INF...': {'family_id': 8292, 'scheme_code': 122640}}"
        },
    )
    save_default_json(
        Path("groww_fund_urls.json"),
        {
            "_comment": "Optional. Key by fund ISIN or exact fund name. Value is Groww mutual fund URL.",
            **DEFAULT_GROWW_FUND_URLS,
        },
    )
    save_default_json(
        Path("benchmark_overrides.json"),
        {
            "_comment": "Optional. Key by fund ISIN or exact fund name. Sources: finology_index, google_finance, groww_page, manual. Set disabled=true to use holdings instead.",
            **DEFAULT_BENCHMARK_OVERRIDES,
        },
    )
    save_default_json(Path("investment_watchlist.json"), DEFAULT_WATCHLIST_CONFIG)
    sync_default_json(Path("investment_watchlist.json"), DEFAULT_WATCHLIST_CONFIG)

    fund_overrides = {k: v for k, v in load_overrides(Path("fund_overrides.json")).items() if not k.startswith("_")}
    groww_urls = {k: v for k, v in load_overrides(Path("groww_fund_urls.json")).items() if not k.startswith("_")}
    benchmark_overrides = {k: v for k, v in load_overrides(Path("benchmark_overrides.json")).items() if not k.startswith("_")}
    watchlist_config = {
        k: v
        for k, v in merge_defaults(load_overrides(Path("investment_watchlist.json")), DEFAULT_WATCHLIST_CONFIG).items()
        if not k.startswith("_")
    }
    cache = Cache(Path(args.cache_dir), refresh_holdings=args.refresh_holdings)
    funds = load_fund_rows(holdings_path, include_arbitrage=args.include_arbitrage)
    if args.list_funds:
        print(f"Parsed {len(funds)} fund(s) from {holdings_path}; arbitrage excluded: {not args.include_arbitrage}")
        for fund in funds:
            print(f"- {fund.name} | {fund.isin} | {fund.instrument_type}")
        return 0

    if args.date:
        run_dates = [parse_date_arg(args.date)]
    elif args.days and args.days > 1:
        end = dt.date.fromisoformat(today_iso())
        run_dates = [(end - dt.timedelta(days=offset)).isoformat() for offset in range(args.days - 1, -1, -1)]
    else:
        run_dates = [today_iso()]

    any_estimates = False
    all_estimates: list[FundEstimate] = []
    all_unresolved: list[dict[str, Any]] = []
    print(f"Loaded {len(funds)} fund(s) from {holdings_path}; arbitrage excluded: {not args.include_arbitrage}")
    for trade_date in run_dates:
        estimates: list[FundEstimate] = []
        unresolved: list[tuple[FundRow, str]] = []
        print(f"\n=== {display_date(trade_date)} ===")

        for fund in funds:
            print(f"Resolving {short_name(fund.name)} for {display_date(trade_date)} ...", file=sys.stderr)
            estimate = None
            benchmark_config = resolve_benchmark_override(fund, benchmark_overrides)
            if benchmark_config:
                try:
                    estimate = estimate_benchmark_fund(fund, benchmark_config, cache, trade_date)
                except Exception as exc:
                    print(f"warning: benchmark override failed for {fund.name}: {exc}", file=sys.stderr)
                if estimate is not None:
                    append_estimate(estimates, estimate, watchlist_config, cache)
                    continue

            groww_reason = ""
            if args.source in {"groww", "groww-mfdata"}:
                groww_url = resolve_groww_fund_url(fund, groww_urls)
                if groww_url:
                    try:
                        estimate = estimate_groww_fund(
                            fund,
                            groww_url,
                            cache,
                            trade_date,
                            min_weight=args.min_weight,
                            max_holdings=args.max_holdings,
                            top=args.top,
                        )
                    except Exception as exc:
                        groww_reason = f"groww: {exc}"
                        print(f"warning: Groww failed for {fund.name}: {exc}", file=sys.stderr)
                else:
                    groww_reason = "groww-url-not-found"

            if estimate is not None:
                append_estimate(estimates, estimate, watchlist_config, cache)
                continue

            if args.source == "groww":
                unresolved.append((fund, groww_reason or "groww-failed"))
                continue

            family_id, scheme_code, source = resolve_fund(fund, cache, fund_overrides)
            if not family_id:
                unresolved.append((fund, "; ".join(filter(None, [groww_reason, source]))))
                continue
            try:
                estimate = estimate_mfdata_fund(
                    fund,
                    family_id,
                    scheme_code,
                    cache,
                    trade_date,
                    min_weight=args.min_weight,
                    max_holdings=args.max_holdings,
                    top=args.top,
                )
                append_estimate(estimates, estimate, watchlist_config, cache)
            except Exception as exc:
                unresolved.append((fund, "; ".join(filter(None, [groww_reason, str(exc)]))))

        print_report(estimates, unresolved)
        csv_path, json_path = write_reports(estimates, unresolved, Path(args.reports_dir), trade_date)
        print(f"\nSaved: {csv_path}")
        print(f"Saved: {json_path}")
        any_estimates = any_estimates or bool(estimates)
        all_estimates.extend(estimates)
        all_unresolved.extend(
            {
                "analysis_date": display_date(trade_date),
                "analysis_date_iso": trade_date,
                "fund": fund.__dict__,
                "reason": reason,
            }
            for fund, reason in unresolved
        )

    if all_estimates or all_unresolved:
        report_label = (
            display_date(run_dates[0])
            if len(run_dates) == 1
            else f"{display_date(run_dates[0])}_to_{display_date(run_dates[-1])}"
        )
        html_path = write_interactive_report(all_estimates, all_unresolved, Path(args.reports_dir), report_label)
        print(f"Saved: {html_path}")
        if not args.no_open:
            opened = webbrowser.open(html_path.resolve().as_uri())
            if opened:
                print(f"Opened interactive report: {html_path}")
            else:
                print(f"Interactive report: {html_path}")

    return 0 if any_estimates else 1


if __name__ == "__main__":
    raise SystemExit(main())
