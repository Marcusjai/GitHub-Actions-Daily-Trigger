"""Build docs/data.json without inventing missing market observations.

Requires Python 3.11+ and the dependencies in requirements.txt.
Run from the repository root: python generate_data.py

The legacy key 'darkPool' stores ChartExchange's OFF-EXCHANGE daily percentage,
not a pure dark-pool measure. It can be None (JSON null). Frontends must handle
null and display data_quality / source dates. Prices use completed sessions
after a 30-minute publication buffer.
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
    """Accept only a dated, ticker-specific daily summary; never guess a column."""
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
    dates = {
        datetime.strptime(h.group(1), "%b %d, %Y").date().isoformat()
        for h in headings
    }
    if len(dates) != 1:
        raise ValueError("Ambiguous volume dates")
    summary = re.search(
        rf"\b{re.escape(ticker)}\s+Off\s+Exchange\s*&\s*Dark\s+Pool\s+Summary",
        text,
        flags=re.IGNORECASE,
    )
    if not summary:
        raise ValueError("Ticker-specific off-exchange summary not found")
    segment = text[summary.end():summary.end() + 700]
    match = re.search(
        r"Off\s+Exchange\s*&\s*Dark\s+Pool\s+volume\s+is\s+"
        r"[\d,]+(?:\.\d+)?\s*,\s*which\s+is\s+(\d+(?:\.\d+)?)\s*%",
        segment,
        flags=re.IGNORECASE,
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


def fetch_off_exchange(
    session: requests.Session, ticker: str, market_date: str
) -> dict[str, Any]:
    url = (
        f"https://chartexchange.com/symbol/"
        f"{EXCHANGE_MAP[ticker]}-{ticker.lower()}/exchange-volume/"
    )
    result: dict[str, Any] = {
        "darkPool": None,
        "darkPoolStatus": "unavailable",
        "darkPoolDate": None,
        "darkPoolAsOf": None,
        "darkPoolSource": url,
        "darkPoolMetric": "off_exchange_day_pct",
        "darkPoolError": None,
    }
    try:
        response = session.get(url, timeout=(5, 15))
        response.raise_for_status()
        value, source_date, stamp = parse_off_exchange(
            response.text, ticker
        )
        result.update(
            darkPoolDate=source_date,
            darkPoolAsOf=stamp,
        )
        if source_date != market_date:
            result.update(
                darkPoolStatus="date_mismatch",
                darkPoolError=(
                    f"Source date {source_date}; "
                    f"price date {market_date}"
                ),
            )
        else:
            result.update(
                darkPool=round(value, 2),
                darkPoolStatus="available",
            )
    except (requests.RequestException, ValueError) as exc:
        result["darkPoolError"] = str(exc)
    return result


def price_frame(data: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Require the grouped multi-ticker validated history."""
    if not isinstance(data.columns, pd.MultiIndex):
        raise ValueError(
            "Expected ticker-grouped Yahoo columns"
        )
    if ticker not in data.columns.get_level_values(0):
        raise ValueError(
            f"{ticker}: missing from Yahoo download"
        )
    frame = data[ticker]
    if not {"Close", "Volume"}.issubset(frame.columns):
        raise ValueError(
            f"{ticker}: Close/Volume columns missing"
        )
    frame = frame[["Close", "Volume"]].copy()
    if (
        not isinstance(frame.index, pd.DatetimeIndex)
        or not frame.index.is_unique
    ):
        raise ValueError(
            f"{ticker}: invalid or duplicate date index"
        )
    frame = frame.sort_index()
    for col in ("Close", "Volume"):
        frame[col] = pd.to_numeric(
            frame[col], errors="coerce"
        )
    return frame


def _historical_volume_map(
    data: pd.DataFrame,
) -> dict[tuple[str, str], tuple[int, int]]:
    validation = data.attrs.get("history_validation", {})
    records = validation.get(
        "historical_recoveries", []
    )
    mapping = {}
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        ticker = record.get("ticker")
        date = record.get("market_date")
        lo = record.get("volume_min")
        hi = record.get("volume_max")
        if (
            isinstance(ticker, str)
            and isinstance(date, str)
            and isinstance(lo, int)
            and isinstance(hi, int)
            and 0 <= lo <= hi
        ):
            key = (ticker, date)
            if key in mapping:
                raise ValueError(
                    f"{ticker}: duplicate historical "
                    f"volume recovery for {date}"
                )
            mapping[key] = (lo, hi)
    return mapping


def _volume_bounds(
    value: Any,
    ticker: str,
    date: pd.Timestamp,
    recovered: dict[tuple[str, str], tuple[int, int]],
) -> tuple[float, float, bool]:
    if (
        pd.notna(value)
        and math.isfinite(float(value))
        and float(value) >= 0
    ):
        return float(value), float(value), True
    key = (ticker, str(date.date()))
    if key not in recovered:
        raise ValueError(
            f"{ticker}: missing volume without a validated "
            f"recovery on {date.date()}"
        )
    lo, hi = recovered[key]
    return float(lo), float(hi), lo == hi


def build_market_items(
    data: pd.DataFrame,
) -> tuple[list[dict[str, Any]], str]:
    """Calculate RVOL only when bounded uncertainty cannot change the output."""
    if data is None or data.empty:
        raise ValueError(
            "Yahoo returned no data; existing JSON will be kept"
        )
    spy = price_frame(data, "SPY")
    if len(spy) < 21:
        raise ValueError(
            "SPY: at least 21 daily rows are required"
        )
    dates = spy.index[-21:]
    recovery_map = _historical_volume_map(data)

    frames: dict[str, pd.DataFrame] = {}
    rvol_bounds: dict[
        str, tuple[float, float, bool]
    ] = {}
    for ticker in WATCHLIST:
        original = price_frame(data, ticker)
        if original.index[-1] != dates[-1]:
            raise ValueError(
                f"{ticker}: latest date differs from SPY"
            )
        window = original.reindex(dates)
        close_valid = window["Close"].map(
            lambda v: (
                pd.notna(v)
                and math.isfinite(float(v))
                and float(v) > 0
            )
        )
        if not close_valid.all():
            bad = ", ".join(
                window.index[~close_valid].strftime(
                    "%Y-%m-%d"
                )
            )
            raise ValueError(
                f"{ticker}: missing/non-finite Close on {bad}"
            )

        latest_volume = window["Volume"].iloc[-1]
        if (
            pd.isna(latest_volume)
            or not math.isfinite(float(latest_volume))
            or float(latest_volume) < 0
        ):
            raise ValueError(
                f"{ticker}: latest daily Volume must be exact"
            )

        lows = []
        highs = []
        exact_window = True
        for date, value in (
            window["Volume"].iloc[:-1].items()
        ):
            lo, hi, exact = _volume_bounds(
                value, ticker, date, recovery_map
            )
            lows.append(lo)
            highs.append(hi)
            exact_window &= exact
        low_mean = sum(lows) / 20.0
        high_mean = sum(highs) / 20.0
        if low_mean <= 0 or high_mean <= 0:
            raise ValueError(
                f"{ticker}: previous 20-session mean "
                "volume must be positive"
            )
        latest_volume = float(latest_volume)
        rvol_min = latest_volume / high_mean
        rvol_max = latest_volume / low_mean
        if round(rvol_min, 2) != round(rvol_max, 2):
            raise ValueError(
                f"{ticker}: RVOL is ambiguous at two decimals "
                "because historical volume is bounded only "
                f"({rvol_min:.5f}..{rvol_max:.5f})"
            )
        rvol_bounds[ticker] = (
            rvol_min, rvol_max, exact_window
        )
        frames[ticker] = window

    market_date = dates[-1].date().isoformat()
    spy_close = frames["SPY"]["Close"]
    spy_return = (
        float(spy_close.iloc[-1])
        / float(spy_close.iloc[-2])
        - 1
    ) * 100
    items = []
    for ticker, info in WATCHLIST.items():
        frame = frames[ticker]
        latest = float(frame["Close"].iloc[-1])
        previous = float(frame["Close"].iloc[-2])
        daily_return = (
            latest / previous - 1
        ) * 100
        alpha = daily_return - spy_return
        rvol_min, rvol_max, exact_window = (
            rvol_bounds[ticker]
        )
        rvol = round(rvol_min, 2)

        signal = "NEUTRAL"
        if alpha > 0.5:
            if rvol_min < 1.1 <= rvol_max:
                raise ValueError(
                    f"{ticker}: accumulation signal is "
                    "ambiguous because RVOL bounds straddle "
                    f"1.1 ({rvol_min:.5f}..{rvol_max:.5f})"
                )
            if rvol_min >= 1.1:
                signal = "ACCUMULATION"
        elif alpha < -0.8:
            signal = "DISTRIBUTION"

        latest_volume = float(
            frame["Volume"].iloc[-1]
        )
        if (
            latest_volume < 0
            or int(latest_volume) != latest_volume
        ):
            raise ValueError(
                f"{ticker}: latest daily Volume must be "
                "a nonnegative integer"
            )

        item = {
            "ticker": ticker,
            "name": info["name"],
            "cat": info.get("cat", "INDEX"),
            "price": round(latest, 2),
            "return": round(daily_return, 2),
            "change": f"{latest - previous:+.2f}",
            "alpha": round(alpha, 2),
            "volume": int(latest_volume),
            "rvol": rvol,
            "rvolExact": exact_window,
            "signal": signal,
            "market_date": market_date,
            "rvolLookback": 20,
        }
        if not exact_window:
            item["rvolRange"] = [
                round(rvol_min, 5),
                round(rvol_max, 5),
            ]
        items.append(item)
    return items, market_date


def write_json_atomic(
    payload: dict[str, Any], path: Path
) -> None:
    text = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    path.parent.mkdir(
        parents=True, exist_ok=True
    )
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = Path(f.name)
            f.write(text)
        os.replace(temp_path, path)
    finally:
        if (
            temp_path is not None
            and temp_path.exists()
        ):
            temp_path.unlink()


def _load_previous_snapshot(
    path: Path,
) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    return (
        value if isinstance(value, dict) else None
    )


def generate_real_market_json(
    output_path: Path = Path("docs/data.json"),
) -> dict[str, Any]:
    output_path = Path(output_path)
    previous_snapshot = _load_previous_snapshot(
        output_path
    )
    print(
        "Fetching and validating completed Yahoo "
        "daily sessions...",
        flush=True,
    )
    data = download_market_history(
        list(WATCHLIST),
        previous_snapshot=previous_snapshot,
    )
    items, market_date = build_market_items(data)

    validation = data.attrs.get(
        "history_validation", {}
    )
    quote_sources = {
        record["ticker"]: record
        for record in validation.get(
            "closing_quote_recoveries", []
        )
        if (
            isinstance(record, dict)
            and "ticker" in record
        )
    }
    for item in items:
        item["priceSource"] = (
            "yahoo_closing_quote"
            if item["ticker"] in quote_sources
            else "yahoo_adjusted_daily_history"
        )
        if item["ticker"] in quote_sources:
            item["priceRecovery"] = (
                quote_sources[item["ticker"]]
            )

    missing = []
    with requests.Session() as session:
        session.headers.update(
            {
                "User-Agent": (
                    "Marcus-Market-Dashboard/1.0"
                )
            }
        )
        for item in items:
            item.update(
                fetch_off_exchange(
                    session,
                    item["ticker"],
                    market_date,
                )
            )
            if item["darkPool"] is None:
                missing.append(item["ticker"])
                print(
                    f"::warning::{item['ticker']}: "
                    "off-exchange unavailable "
                    f"({item['darkPoolStatus']}); "
                    "writing null, not an estimate.",
                    flush=True,
                )

    indices = [
        item
        for item in items
        if WATCHLIST[item["ticker"]].get(
            "is_index"
        )
    ]
    sectors = [
        item
        for item in items
        if not WATCHLIST[item["ticker"]].get(
            "is_index"
        )
    ]
    sectors.sort(
        key=lambda item: item["alpha"],
        reverse=True,
    )
    payload = {
        "last_updated": datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "market_date": market_date,
        "data_quality": (
            "partial"
            if missing
            else "available_not_independently_verified"
        ),
        "missing_off_exchange": missing,
        "price_basis": (
            "Yahoo adjusted daily Close; exact "
            "closing-quote recovery and prior validated "
            "historical recovery are explicitly recorded "
            "in provenance"
        ),
        "session_note": (
            "Prices use the latest completed regular "
            "session after a 30-minute publication buffer; "
            "off-exchange snapshots may be partial."
        ),
        "history_validation": validation,
        "indices": indices,
        "sectors": sectors,
    }
    write_json_atomic(
        payload, output_path
    )
    print(
        f"Generated {output_path}: "
        f"{len(indices)} indices, "
        f"{len(sectors)} sectors; "
        f"market date {market_date}; "
        f"{len(missing)} off-exchange "
        "values unavailable.",
        flush=True,
    )
    return payload


if __name__ == "__main__":
    try:
        generate_real_market_json()
    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
