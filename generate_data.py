"""Build docs/data.json without inventing missing market observations.

Requires Python 3.11+ and the dependencies in requirements.txt.
Run from the repository root: python generate_data.py

The legacy key 'darkPool' stores ChartExchange's OFF-EXCHANGE daily percentage,
not a pure dark-pool measure. It can be None (JSON null). Frontends must handle
null and display data_quality / source dates. This is not a trading strategy
validation. Prices use completed sessions after a 30-minute publication buffer.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests
from scripts.yahoo_history import download_market_history

# Retained from the supplied script. Failed/changed routes yield missing data.
EXCHANGE_MAP = {
    "SPY": "nyse", "QQQ": "nasdaq", "IWM": "nyse", "DIA": "nyse",
    "SMH": "nasdaq", "URA": "nyse", "XLU": "nyse", "XLK": "nyse",
    "XLI": "nyse", "XLF": "nyse", "XBI": "nyse", "XLE": "nyse",
    "XLV": "nyse", "XLP": "nyse",
}
WATCHLIST = {
    "SPY": {"name": "標普 500 ETF", "is_index": True},
    "QQQ": {"name": "納斯達克 100 ETF", "is_index": True},
    "IWM": {"name": "羅素 2000 細盤股", "is_index": True},
    "DIA": {"name": "道瓊斯工業 ETF", "is_index": True},
    "SMH": {"name": "半導體 ETF", "cat": "TECH"},
    "URA": {"name": "核能與鈾礦 ETF", "cat": "POWER"},
    "XLU": {"name": "電力公用事業 ETF", "cat": "POWER"},
    "XLK": {"name": "科技板塊 ETF", "cat": "TECH"},
    "XLI": {"name": "工業板塊 ETF", "cat": "CYCLICAL"},
    "XLF": {"name": "金融板塊 ETF", "cat": "CYCLICAL"},
    "XBI": {"name": "生科生技 ETF", "cat": "TECH"},
    "XLE": {"name": "傳統能源 ETF", "cat": "CYCLICAL"},
    "XLV": {"name": "醫療保健 ETF", "cat": "DEFENSIVE"},
    "XLP": {"name": "必選消費 ETF", "cat": "DEFENSIVE"},
}


class VisibleText(HTMLParser):
    """Extract text without scripts/styles; no browser or bypass mechanism."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def parse_off_exchange(html: str, ticker: str) -> tuple[float, str, str]:
    """Accept only a dated, ticker-specific daily summary; never guess a column.

    Returns (daily_percentage, source_date, source_heading_timestamp).
    Changed/missing/ambiguous page content raises ValueError.
    """
    parser = VisibleText()
    parser.feed(html)
    parser.close()
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    date_pattern = r"[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}"
    headings = list(re.finditer(
        rf"\b{re.escape(ticker)}\s+Volume\s+({date_pattern})"
        r"(?:\s+(\d{1,2}:\d{2}(?::\d{2})?\s+[AP]M\s+(?:EST|EDT)))?",
        text,
    ))
    if not headings:
        raise ValueError("Ticker-specific volume date not found")
    dates = {datetime.strptime(h.group(1), "%b %d, %Y").date().isoformat()
             for h in headings}
    if len(dates) != 1:
        raise ValueError("Ambiguous volume dates")
    # Restrict parsing to the ticker-specific summary, not a random site number.
    summary = re.search(
        rf"\b{re.escape(ticker)}\s+Off\s+Exchange\s*&\s*Dark\s+Pool\s+Summary",
        text, flags=re.IGNORECASE,
    )
    if not summary:
        raise ValueError("Ticker-specific off-exchange summary not found")
    segment = text[summary.end():summary.end() + 700]
    match = re.search(
        r"Off\s+Exchange\s*&\s*Dark\s+Pool\s+volume\s+is\s+"
        r"[\d,]+(?:\.\d+)?\s*,\s*which\s+is\s+(\d+(?:\.\d+)?)\s*%",
        segment, flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("Unambiguous daily off-exchange percentage not found")
    value = float(match.group(1))
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        raise ValueError("Off-exchange percentage outside 0..100")
    stamp = headings[0].group(1)
    if headings[0].group(2):
        stamp += " " + headings[0].group(2)
    return value, next(iter(dates)), stamp


def fetch_off_exchange(session: requests.Session, ticker: str,
                       market_date: str) -> dict[str, Any]:
    url = (f"https://chartexchange.com/symbol/"
           f"{EXCHANGE_MAP[ticker]}-{ticker.lower()}/exchange-volume/")
    result: dict[str, Any] = {
        "darkPool": None, "darkPoolStatus": "unavailable",
        "darkPoolDate": None, "darkPoolAsOf": None,
        "darkPoolSource": url, "darkPoolMetric": "off_exchange_day_pct",
        "darkPoolError": None,
    }
    try:
        response = session.get(url, timeout=(5, 15))
        response.raise_for_status()
        value, source_date, stamp = parse_off_exchange(response.text, ticker)
        result.update(darkPoolDate=source_date, darkPoolAsOf=stamp)
        if source_date != market_date:
            result.update(darkPoolStatus="date_mismatch",
                          darkPoolError=f"Source date {source_date}; price date {market_date}")
        else:
            result.update(darkPool=round(value, 2), darkPoolStatus="available")
    except (requests.RequestException, ValueError) as exc:
        # Missing observations remain missing. Do not substitute 0 or a seed.
        result["darkPoolError"] = str(exc)
    return result


def price_frame(data: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Require the grouped multi-ticker result requested from yf.download."""
    if not isinstance(data.columns, pd.MultiIndex):
        raise ValueError("Expected ticker-grouped Yahoo columns")
    if ticker not in data.columns.get_level_values(0):
        raise ValueError(f"{ticker}: missing from Yahoo download")
    frame = data[ticker]
    if not {"Close", "Volume"}.issubset(frame.columns):
        raise ValueError(f"{ticker}: Close/Volume columns missing")
    frame = frame[["Close", "Volume"]].copy()
    if not isinstance(frame.index, pd.DatetimeIndex) or not frame.index.is_unique:
        raise ValueError(f"{ticker}: invalid or duplicate date index")
    frame = frame.sort_index()
    for col in ("Close", "Volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def build_market_items(data: pd.DataFrame) -> tuple[list[dict[str, Any]], str]:
    """Validate all 14 symbols before producing a new output file.

    Use the same final 21 SPY session dates for every symbol. Never silently
    compare different daily-return periods or label a <20-bar mean '20-day'.
    """
    if data is None or data.empty:
        raise ValueError("Yahoo returned no data; existing JSON will be kept")
    spy = price_frame(data, "SPY")
    if len(spy) < 21:
        raise ValueError("SPY: at least 21 daily rows are required")
    dates = spy.index[-21:]
    frames: dict[str, pd.DataFrame] = {}
    for ticker in WATCHLIST:
        original = price_frame(data, ticker)
        if original.index[-1] != dates[-1]:
            raise ValueError(f"{ticker}: latest date differs from SPY")
        window = original.reindex(dates)
        for col in ("Close", "Volume"):
            if not all(math.isfinite(float(v)) for v in window[col]):
                raise ValueError(f"{ticker}: missing/non-finite {col} in 21-session window")
        if (window["Close"] <= 0).any() or (window["Volume"] < 0).any():
            raise ValueError(f"{ticker}: invalid price or volume")
        if float(window["Volume"].iloc[:-1].mean()) <= 0:
            raise ValueError(f"{ticker}: prior 20-session mean volume must be positive")
        frames[ticker] = window
    market_date = dates[-1].date().isoformat()
    spy_close = frames["SPY"]["Close"]
    spy_return = (float(spy_close.iloc[-1]) / float(spy_close.iloc[-2]) - 1) * 100
    items = []
    for ticker, info in WATCHLIST.items():
        frame = frames[ticker]
        latest = float(frame["Close"].iloc[-1])
        previous = float(frame["Close"].iloc[-2])
        daily_return = (latest / previous - 1) * 100
        alpha = daily_return - spy_return  # Daily excess return, not risk-adjusted alpha.
        rvol = float(frame["Volume"].iloc[-1]) / float(frame["Volume"].iloc[:-1].mean())
        signal = "NEUTRAL"
        # Retained user thresholds. These labels are heuristics, not fund-flow facts.
        if alpha > 0.5 and rvol >= 1.1:
            signal = "ACCUMULATION"
        elif alpha < -0.8:
            signal = "DISTRIBUTION"
        items.append({
            "ticker": ticker, "name": info["name"], "cat": info.get("cat", "INDEX"),
            "price": round(latest, 2), "return": round(daily_return, 2),
            "change": f"{latest - previous:+.2f}", "alpha": round(alpha, 2),
            "rvol": round(rvol, 2), "signal": signal,
            "market_date": market_date, "rvolLookback": 20,
        })
    return items, market_date


def write_json_atomic(payload: dict[str, Any], path: Path) -> None:
    # Validate JSON BEFORE touching the existing data file.
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, suffix=".tmp", delete=False) as f:
            temp_path = Path(f.name)
            f.write(text)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def generate_real_market_json(output_path: Path = Path("docs/data.json")) -> dict[str, Any]:
    print("Fetching and validating completed Yahoo daily sessions...", flush=True)
    data = download_market_history(list(WATCHLIST))
    items, market_date = build_market_items(data)
    missing = []
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Marcus-Market-Dashboard/1.0"})
        for item in items:
            item.update(fetch_off_exchange(session, item["ticker"], market_date))
            if item["darkPool"] is None:
                missing.append(item["ticker"])
                print(f"::warning::{item['ticker']}: off-exchange unavailable "
                      f"({item['darkPoolStatus']}); writing null, not an estimate.", flush=True)
    indices = [item for item in items if WATCHLIST[item["ticker"]].get("is_index")]
    sectors = [item for item in items if not WATCHLIST[item["ticker"]].get("is_index")]
    sectors.sort(key=lambda item: item["alpha"], reverse=True)
    payload = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "market_date": market_date,
        "data_quality": "partial" if missing else "available_not_independently_verified",
        "missing_off_exchange": missing,
        "price_basis": "Yahoo auto_adjust=True; adjusted daily Close",
        "session_note": "Prices use the latest completed regular session after a 30-minute publication buffer; off-exchange snapshots may be partial.",
        "history_validation": data.attrs.get("history_validation", {}),
        "indices": indices, "sectors": sectors,
    }
    write_json_atomic(payload, Path(output_path))
    print(f"Generated {output_path}: {len(indices)} indices, {len(sectors)} sectors; "
          f"market date {market_date}; {len(missing)} off-exchange values unavailable.", flush=True)
    return payload


if __name__ == "__main__":
    try:
        generate_real_market_json()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
