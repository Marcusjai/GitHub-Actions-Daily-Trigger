"""Strict regular-session intraday cross-checks for Yahoo daily gaps.

Minute data is used only to corroborate a persisted official daily observation.
Its summed volume is treated as a lower bound, never as a replacement for
Yahoo's daily volume because closing-auction volume can be absent.
"""
from __future__ import annotations

import math
import re
from typing import Any

import pandas as pd

HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def parse_regular_session_intraday(
    payload: dict,
    ticker: str,
    session_open: pd.Timestamp,
    session_close: pd.Timestamp,
) -> dict[str, Any]:
    """Return a strict last-minute close and regular-session volume lower bound."""
    if session_open.tzinfo is None or session_close.tzinfo is None:
        raise ValueError("Session bounds must be timezone-aware")
    session_open = session_open.tz_convert("UTC")
    session_close = session_close.tz_convert("UTC")
    if session_close <= session_open:
        raise ValueError("Invalid session bounds")

    try:
        chart = payload["chart"]
        if chart.get("error") is not None or len(chart["result"]) != 1:
            raise ValueError("Yahoo returned an error or ambiguous intraday result")
        result = chart["result"][0]
        meta = result["meta"]
        if (
            meta.get("symbol") != ticker
            or meta.get("currency") != "USD"
            or meta.get("exchangeTimezoneName") != "America/New_York"
        ):
            raise ValueError("Wrong intraday symbol, currency or exchange timezone")

        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        arrays = [quote[k] for k in ("open", "high", "low", "close", "volume")]
        if not timestamps or not all(
            isinstance(a, list) and len(a) == len(timestamps) for a in arrays
        ):
            raise ValueError("Intraday arrays are missing or misaligned")

        bars: list[tuple[pd.Timestamp, float, float, float, float, int]] = []
        for i, raw_stamp in enumerate(timestamps):
            if not _finite(raw_stamp) or int(raw_stamp) != raw_stamp:
                raise ValueError("Invalid intraday timestamp")
            stamp = pd.Timestamp(int(raw_stamp), unit="s", tz="UTC")
            if not (session_open <= stamp < session_close):
                continue
            opening, high, low, close, volume = (
                quote[k][i] for k in ("open", "high", "low", "close", "volume")
            )
            if not all(
                _finite(v) and float(v) > 0 for v in (opening, high, low, close)
            ):
                raise ValueError("Missing/invalid regular-session OHLC minute bar")
            if (
                not _finite(volume)
                or float(volume) < 0
                or int(float(volume)) != float(volume)
            ):
                raise ValueError("Missing/invalid regular-session minute volume")
            opening = float(opening)
            high = float(high)
            low = float(low)
            close = float(close)
            if low > high or not (
                low <= opening <= high and low <= close <= high
            ):
                raise ValueError("Inconsistent regular-session minute OHLC")
            bars.append((stamp, opening, high, low, close, int(volume)))

        expected_minutes = int(
            (session_close - session_open) / pd.Timedelta(minutes=1)
        )
        if len(bars) != expected_minutes:
            raise ValueError(
                f"Expected {expected_minutes} one-minute regular-session bars; "
                f"got {len(bars)}"
            )
        if bars[0][0] != session_open:
            raise ValueError(
                "Intraday series does not start at regular-session open"
            )
        expected_last = session_close - pd.Timedelta(minutes=1)
        if bars[-1][0] != expected_last:
            raise ValueError(
                "Intraday series does not end at the final regular-session minute"
            )
        if any(
            bars[i][0] >= bars[i + 1][0] for i in range(len(bars) - 1)
        ):
            raise ValueError("Intraday timestamps are duplicate or unordered")

        return {
            "ticker": ticker,
            "market_date": str(
                session_open.tz_convert("America/New_York").date()
            ),
            "last_minute_close": bars[-1][4],
            "last_minute_utc": bars[-1][0].isoformat(),
            "regular_volume_lower_bound": sum(bar[5] for bar in bars),
            "bar_count": len(bars),
        }
    except (KeyError, TypeError, IndexError, OverflowError) as exc:
        raise ValueError(f"Invalid Yahoo intraday schema: {exc}") from exc


def download_intraday_consensus(
    ticker: str,
    session_open: pd.Timestamp,
    session_close: pd.Timestamp,
) -> dict[str, Any]:
    """Require query1/query2 to agree before using minute data as corroboration."""
    if not re.fullmatch(r"[A-Z]{1,6}", ticker):
        raise ValueError("Invalid ticker")
    if session_open.tzinfo is None or session_close.tzinfo is None:
        raise ValueError("Session bounds must be timezone-aware")

    from curl_cffi import requests

    params = {
        "period1": int(
            (session_open - pd.Timedelta(minutes=2)).timestamp()
        ),
        "period2": int(
            (session_close + pd.Timedelta(minutes=2)).timestamp()
        ),
        "interval": "1m",
        "includePrePost": "false",
        "events": "div,splits",
    }
    observations = []
    urls = []
    for host in HOSTS:
        url = f"https://{host}/v8/finance/chart/{ticker}"
        with requests.Session(impersonate="chrome") as session:
            response = session.get(
                url, params=params, timeout=20, allow_redirects=False
            )
            response.raise_for_status()
            observations.append(
                parse_regular_session_intraday(
                    response.json(), ticker, session_open, session_close
                )
            )
            urls.append(url)

    first, second = observations
    comparable = (
        first["market_date"] == second["market_date"]
        and first["last_minute_utc"] == second["last_minute_utc"]
        and first["bar_count"] == second["bar_count"]
        and first["regular_volume_lower_bound"]
        == second["regular_volume_lower_bound"]
        and math.isclose(
            first["last_minute_close"],
            second["last_minute_close"],
            rel_tol=0,
            abs_tol=1e-8,
        )
    )
    if not comparable:
        raise ValueError(
            "Yahoo query1/query2 intraday observations disagree"
        )
    return {
        **first,
        "source_urls": urls,
        "consensus": "query1_query2_exact",
        "volume_note": (
            "1-minute regular-session sum is a lower bound, not daily volume"
        ),
    }
