"""Strict recovery from Yahoo's timestamped regular-session closing quote.

Yahoo can return null daily Close AND Adj Close while the same response still
has regularMarketPrice timestamped at the scheduled session close. That quote
is an observed price, not an estimate. Recovery is allowed ONLY for the latest
bar, exact closing timestamp, matching symbol/USD/New York timezone, valid
same-day OHLC/volume and no later corporate actions. Prior adjusted prices are
retained from this SAME raw response. Provenance must accompany the result.
"""
from __future__ import annotations

import math
import re
from typing import Any

import exchange_calendars as xcals
import pandas as pd

from scripts.yahoo_intraday import download_intraday_consensus


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _result(payload: dict, ticker: str) -> dict:
    try:
        chart = payload["chart"]
        if chart.get("error") is not None or len(chart["result"]) != 1:
            raise ValueError("Yahoo returned an error or ambiguous chart result")
        result = chart["result"][0]
        meta = result["meta"]
        if (
            meta.get("symbol") != ticker
            or meta.get("currency") != "USD"
            or meta.get("exchangeTimezoneName") != "America/New_York"
        ):
            raise ValueError("Wrong symbol, currency or exchange timezone")
        return result
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError(f"Invalid Yahoo chart schema: {exc}") from exc


def _event_dates(result: dict) -> set:
    dates = set()
    for events in result.get("events", {}).values():
        if not isinstance(events, dict):
            raise ValueError("Invalid corporate-action container")
        for event in events.values():
            stamp = event.get("date")
            if not _finite(stamp):
                raise ValueError("Invalid corporate-action date")
            dates.add(
                pd.Timestamp(int(stamp), unit="s", tz="UTC")
                .tz_convert("America/New_York")
                .date()
            )
    return dates


def parse_chart(payload: dict, ticker: str, dates: pd.DatetimeIndex,
                expected_close: pd.Timestamp) -> tuple[pd.DataFrame, dict | None]:
    """Parse adjusted daily observations; never substitute a previous close."""
    try:
        chart = payload['chart']
        if chart.get('error') is not None or len(chart['result']) != 1:
            raise ValueError('Yahoo returned an error or ambiguous chart result')
        result = chart['result'][0]
        meta = result['meta']
        if (meta.get('symbol') != ticker or meta.get('currency') != 'USD' or
                meta.get('exchangeTimezoneName') != 'America/New_York'):
            raise ValueError('Wrong symbol, currency or exchange timezone')
        timestamps = result['timestamp']
        if not timestamps or not all(_finite(t) and t > 0 and int(t) == t for t in timestamps):
            raise ValueError('Invalid chart timestamps')
        index = pd.to_datetime(timestamps, unit='s', utc=True).tz_convert('America/New_York').tz_localize(None).normalize()
        if not index.is_unique or not index.is_monotonic_increasing:
            raise ValueError('Duplicate or unordered chart dates')
        quote = result['indicators']['quote'][0]
        adjusted = result['indicators']['adjclose'][0]['adjclose']
        arrays = [adjusted] + [quote[k] for k in ['open','high','low','close','volume']]
        if not all(isinstance(a, list) and len(a) == len(index) for a in arrays):
            raise ValueError('Chart arrays are missing or misaligned')
        data = pd.DataFrame({'Close': adjusted, 'Volume': quote['volume']}, index=index)
        provenance = None
        # A normal complete response needs no quote replacement. Caller still
        # checks every required session for finite values and positive prices.
        if index[-1] != dates[-1] or quote['close'][-1] is not None or adjusted[-1] is not None:
            return data, provenance
        stamp = meta.get('regularMarketTime')
        price = meta.get('regularMarketPrice')
        if expected_close.tzinfo is None:
            raise ValueError('Expected close must be timezone-aware')
        if not _finite(stamp) or stamp != expected_close.timestamp():
            raise ValueError('Closing quote is stale, intraday, or after-hours')
        if not _finite(price) or price <= 0:
            raise ValueError('Missing/invalid regular-session closing quote')
        opening, high, low, volume = (quote[k][-1] for k in ['open','high','low','volume'])
        if (not all(_finite(x) and x > 0 for x in [opening,high,low,volume]) or
                low > high or not low <= opening <= high or
                not low - 0.01 <= price <= high + 0.01):
            raise ValueError('Closing quote inconsistent with daily OHLC/volume')
        for events in result.get('events', {}).values():
            for event in events.values():
                event_stamp = event.get('date')
                if not _finite(event_stamp):
                    raise ValueError('Invalid corporate-action date')
                event_date = pd.Timestamp(event_stamp, unit='s', tz='UTC').tz_convert('America/New_York').date()
                if event_date > dates[-1].date():
                    raise ValueError('Later corporate action makes adjustment basis ambiguous')
        # Only the newest regular-session close is replaced. With no later
        # corporate action its adjustment factor is 1; older adjusted prices
        # are already supplied together in this same Yahoo chart response.
        data.loc[dates[-1], 'Close'] = float(price)
        provenance = {
            'ticker': ticker, 'market_date': str(dates[-1].date()),
            'method': 'yahoo_regular_market_quote_at_exact_close',
            'quote_time_utc': expected_close.tz_convert('UTC').isoformat(),
            'source_field': 'meta.regularMarketPrice', 'price': float(price),
            'reason': 'daily Close and Adj Close were null',
            'adjustment_basis': 'latest session factor 1; prior adjusted series from same response',
        }
        return data, provenance
    except (KeyError, TypeError, IndexError, OverflowError) as exc:
        raise ValueError(f'Invalid Yahoo chart schema: {exc}') from exc


def _snapshot_row(snapshot: dict, ticker: str, target: pd.Timestamp) -> dict:
    if not isinstance(snapshot, dict):
        raise ValueError("Previous snapshot is unavailable")
    target_text = str(target.date())
    if snapshot.get("market_date") != target_text:
        raise ValueError("Previous snapshot does not cover the missing session")
    validation = snapshot.get("history_validation")
    if not isinstance(validation, dict):
        raise ValueError("Previous snapshot lacks history validation metadata")
    if (
        validation.get("expected_market_date") != target_text
        or validation.get("missing_required_observations") != 0
    ):
        raise ValueError("Previous snapshot was not a complete validated session")
    rows = snapshot.get("indices", []) + snapshot.get("sectors", [])
    matches = [row for row in rows if row.get("ticker") == ticker]
    if len(matches) != 1:
        raise ValueError("Previous snapshot ticker row is missing or duplicated")
    row = matches[0]
    if row.get("market_date") != target_text or not _finite(row.get("price")):
        raise ValueError("Previous snapshot ticker price is invalid")
    if float(row["price"]) <= 0:
        raise ValueError("Previous snapshot ticker price must be positive")
    return row


def _previous_recovery(
    snapshot: dict, ticker: str, target: pd.Timestamp
) -> dict | None:
    validation = (
        snapshot.get("history_validation", {})
        if isinstance(snapshot, dict)
        else {}
    )
    records = validation.get("historical_recoveries", [])
    if not isinstance(records, list):
        return None
    target_text = str(target.date())
    matches = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("ticker") == ticker
        and record.get("market_date") == target_text
    ]
    if len(matches) > 1:
        raise ValueError("Previous snapshot has duplicate historical recoveries")
    if not matches:
        return None
    record = matches[0]
    if (
        not _finite(record.get("adjusted_close"))
        or float(record["adjusted_close"]) <= 0
    ):
        raise ValueError("Persisted historical recovery price is invalid")
    if (
        not _finite(record.get("volume_min"))
        or not _finite(record.get("volume_max"))
    ):
        raise ValueError("Persisted historical recovery volume bounds are invalid")
    lo = int(record["volume_min"])
    hi = int(record["volume_max"])
    if lo < 0 or hi < lo:
        raise ValueError("Persisted historical recovery volume bounds are invalid")
    return record


def _validate_snapshot_price_source(
    row: dict, target: pd.Timestamp, expected_close: pd.Timestamp
) -> None:
    source = row.get("priceSource")
    if source == "yahoo_adjusted_daily_history":
        return
    if source != "yahoo_closing_quote":
        raise ValueError(
            "Previous snapshot price was not from an approved Yahoo source"
        )
    record = row.get("priceRecovery")
    if not isinstance(record, dict):
        raise ValueError("Previous closing-quote recovery lacks provenance")
    if (
        record.get("method") != "yahoo_regular_market_quote_at_exact_close"
        or record.get("market_date") != str(target.date())
        or record.get("quote_time_utc")
        != expected_close.tz_convert("UTC").isoformat()
        or not _finite(record.get("price"))
        or not math.isclose(
            float(record["price"]),
            float(row["price"]),
            rel_tol=0,
            abs_tol=1e-8,
        )
    ):
        raise ValueError("Previous closing-quote provenance does not validate")


def _legacy_volume_bounds(
    row: dict, data: pd.DataFrame, target: pd.Timestamp
) -> tuple[int, int, dict]:
    """Infer only a conservative integer range from the old rounded RVOL field."""
    if not _finite(row.get("rvol")) or float(row["rvol"]) < 0:
        raise ValueError("Legacy snapshot lacks a valid RVOL")
    calendar = xcals.get_calendar(
        "XNYS",
        start=str((target - pd.Timedelta(days=60)).date()),
        end=str((target + pd.Timedelta(days=1)).date()),
    )
    sessions = (
        pd.DatetimeIndex(calendar.sessions).tz_localize(None).normalize()
    )
    prior = sessions[sessions < target][-20:]
    if len(prior) != 20:
        raise ValueError(
            "Unable to establish prior 20 sessions for legacy RVOL"
        )
    volumes = pd.to_numeric(data.reindex(prior)["Volume"], errors="coerce")
    if volumes.isna().any() or not all(
        math.isfinite(float(v)) and float(v) >= 0 for v in volumes
    ):
        raise ValueError("Prior 20 daily volumes are incomplete")
    avg = float(volumes.mean())
    if avg <= 0:
        raise ValueError("Prior 20-session mean volume must be positive")

    rounded = float(row["rvol"])
    lower_ratio = max(0.0, rounded - 0.005000001)
    upper_ratio = rounded + 0.005000001
    lo = max(0, math.floor(lower_ratio * avg))
    hi = max(lo, math.ceil(upper_ratio * avg))
    return lo, hi, {
        "legacy_snapshot_rvol": rounded,
        "legacy_prior20_mean_volume": avg,
        "derivation": (
            "bounds implied by two-decimal RVOL; exact daily volume not invented"
        ),
    }


def _validate_snapshot_return(
    row: dict, data: pd.DataFrame, target: pd.Timestamp
) -> float:
    if not _finite(row.get("return")):
        raise ValueError("Previous snapshot lacks a valid daily return")
    prior = data.index[data.index < target]
    if not len(prior):
        raise ValueError("No previous daily close for return cross-check")
    previous = pd.to_numeric(
        pd.Series([data.loc[prior[-1], "Close"]]), errors="coerce"
    ).iloc[0]
    if not _finite(previous) or float(previous) <= 0:
        raise ValueError("Previous adjusted close is invalid")
    calculated = (float(row["price"]) / float(previous) - 1.0) * 100.0
    if abs(calculated - float(row["return"])) > 0.03:
        raise ValueError(
            "Previous snapshot return does not reproduce from current history"
        )
    return calculated


def recover_previous_snapshot_gap(
    payload: dict,
    data: pd.DataFrame,
    ticker: str,
    dates: pd.DatetimeIndex,
    previous_snapshot: dict | None,
) -> tuple[pd.DataFrame, list[dict]]:
    """Recover one older required session from a prior validated publication."""
    if previous_snapshot is None:
        return data, []
    required = data.reindex(dates)
    bad = []
    for date, row in required.iterrows():
        close_bad = (
            not _finite(row.get("Close")) or float(row["Close"]) <= 0
        )
        volume_bad = (
            not _finite(row.get("Volume")) or float(row["Volume"]) < 0
        )
        if close_bad or volume_bad:
            bad.append(pd.Timestamp(date))
    bad = [date for date in bad if date != dates[-1]]
    if not bad:
        return data, []
    if len(bad) != 1:
        raise ValueError(
            "More than one historical required session is incomplete"
        )
    target = bad[0]

    result = _result(payload, ticker)
    later_actions = sorted(
        event_date
        for event_date in _event_dates(result)
        if target.date() < event_date <= dates[-1].date()
    )
    if later_actions:
        raise ValueError(
            "Corporate action after cached session changes adjusted-price basis: "
            + ", ".join(map(str, later_actions))
        )

    calendar = xcals.get_calendar(
        "XNYS",
        start=str((target - pd.Timedelta(days=1)).date()),
        end=str((target + pd.Timedelta(days=2)).date()),
    )
    session_open = calendar.session_open(target)
    session_close = calendar.session_close(target)

    prior_record = _previous_recovery(previous_snapshot, ticker, target)
    if prior_record is not None:
        adjusted_close = float(prior_record["adjusted_close"])
        volume_min = int(prior_record["volume_min"])
        volume_max = int(prior_record["volume_max"])
        source_detail = {
            "source_method": "persisted_historical_recovery",
            "source_snapshot_last_updated": previous_snapshot.get(
                "last_updated"
            ),
        }
    else:
        row = _snapshot_row(previous_snapshot, ticker, target)
        _validate_snapshot_price_source(row, target, session_close)
        reproduced_return = _validate_snapshot_return(row, data, target)
        adjusted_close = float(row["price"])
        if _finite(row.get("volume")) and float(row["volume"]) >= 0:
            exact_volume = int(float(row["volume"]))
            if float(exact_volume) != float(row["volume"]):
                raise ValueError(
                    "Previous snapshot daily volume is not an integer"
                )
            volume_min = volume_max = exact_volume
            volume_detail = {"source_volume_field": "volume"}
        else:
            volume_min, volume_max, volume_detail = _legacy_volume_bounds(
                row, data, target
            )
        source_detail = {
            "source_method": "previous_validated_snapshot",
            "source_snapshot_last_updated": previous_snapshot.get(
                "last_updated"
            ),
            "source_price_field": "price",
            "source_price_source": row.get("priceSource"),
            "reproduced_daily_return_pct": reproduced_return,
            **volume_detail,
        }

    intraday = download_intraday_consensus(
        ticker, session_open, session_close
    )
    relative_gap = (
        abs(intraday["last_minute_close"] - adjusted_close) / adjusted_close
    )
    if relative_gap > 0.005:
        raise ValueError(
            "Previous daily close is not corroborated by final minute"
        )
    if intraday["regular_volume_lower_bound"] > volume_max:
        raise ValueError(
            "Intraday volume exceeds persisted daily-volume bound"
        )
    if volume_min == volume_max and volume_min > 0:
        if (
            volume_min / max(1, intraday["regular_volume_lower_bound"])
            > 1.35
        ):
            raise ValueError(
                "Persisted daily volume is implausible versus intraday lower bound"
            )

    recovered = data.copy()
    recovered.loc[target, "Close"] = adjusted_close
    if volume_min == volume_max:
        recovered.loc[target, "Volume"] = volume_min
    else:
        recovered.loc[target, "Volume"] = float("nan")

    record = {
        "ticker": ticker,
        "market_date": str(target.date()),
        "method": "previous_validated_yahoo_daily_snapshot",
        "adjusted_close": adjusted_close,
        "volume_min": volume_min,
        "volume_max": volume_max,
        "volume_exact": volume_min == volume_max,
        "corporate_actions_after_session": [],
        "intraday_corroboration": intraday,
        "close_vs_last_minute_relative_gap": relative_gap,
        **source_detail,
    }
    return recovered, [record]


def download_chart_history(
    ticker: str,
    dates: pd.DatetimeIndex,
    expected_close: pd.Timestamp,
    previous_snapshot: dict | None = None,
) -> tuple[pd.DataFrame, dict | None, list[dict]]:
    """Fetch daily chart, recover latest quote and one prior persisted gap."""
    if not re.fullmatch(r"[A-Z]{1,6}", ticker):
        raise ValueError("Invalid ticker")
    from curl_cffi import requests

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    with requests.Session(impersonate="chrome") as session:
        response = session.get(
            url,
            params={
                "range": "3mo",
                "interval": "1d",
                "includeAdjustedClose": "true",
                "events": "div,splits",
            },
            timeout=20,
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()

    data, latest_record = parse_chart(
        payload, ticker, dates, expected_close
    )
    if latest_record is not None:
        latest_record["source_url"] = url
    data, historical_records = recover_previous_snapshot_gap(
        payload, data, ticker, dates, previous_snapshot
    )
    for record in historical_records:
        record["daily_source_url"] = url
    return data, latest_record, historical_records
