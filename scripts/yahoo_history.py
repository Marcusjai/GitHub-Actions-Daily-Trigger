"""Recover incomplete Yahoo downloads without hiding missing trading sessions.

Calendar: exchange_calendars XNYS; holidays/early closes are not inferred from
Yahoo's possibly incomplete date index. All 14 US ETFs use the same 21 completed
regular sessions, after a 30-minute publication buffer. No forward filling,
interpolation or synthetic prices are used.

If Yahoo later removes a previously published historical daily observation, a
strict recovery may reuse that already-validated Yahoo close. Exact daily
volume is reused when available; legacy snapshots without absolute volume carry
a conservative volume interval so downstream RVOL must remain invariant.
"""
from __future__ import annotations

import math
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import exchange_calendars as xcals
import pandas as pd
import yfinance as yf

from scripts.yahoo_chart import download_chart_history

PUBLICATION_BUFFER_MINUTES = 30
RETRY_DELAYS = (2, 6)


def completed_sessions(asof: Any = None) -> pd.DatetimeIndex:
    """Return exactly the last 21 due sessions, including the expected latest."""
    now = pd.Timestamp(
        asof if asof is not None else datetime.now(timezone.utc)
    )
    if pd.isna(now) or now.tzinfo is None:
        raise ValueError(
            "Calendar asof must be a valid timezone-aware timestamp"
        )
    now = now.tz_convert("UTC")
    today = now.tz_convert("America/New_York").date()
    calendar = xcals.get_calendar(
        "XNYS",
        start=str(today - pd.Timedelta(days=120)),
        end=str(today + pd.Timedelta(days=1)),
    )
    due = (
        calendar.closes
        <= now - pd.Timedelta(minutes=PUBLICATION_BUFFER_MINUTES)
    )
    dates = calendar.sessions[due.to_numpy()][-21:]
    if len(dates) != 21:
        raise ValueError(
            "Unable to establish 21 completed XNYS sessions"
        )
    return pd.DatetimeIndex(dates).tz_localize(None).normalize()


def _volume_interval_map(
    records: list[dict] | None,
) -> dict[str, tuple[int, int]]:
    mapping = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        date = record.get("market_date")
        lo = record.get("volume_min")
        hi = record.get("volume_max")
        if (
            not isinstance(date, str)
            or not isinstance(lo, int)
            or not isinstance(hi, int)
        ):
            continue
        if lo < 0 or hi < lo:
            continue
        mapping[date] = (lo, hi)
    return mapping


def validated_window(
    frame: pd.DataFrame,
    ticker: str,
    dates: pd.DatetimeIndex,
    historical_recoveries: list[dict] | None = None,
) -> pd.DataFrame:
    """Select required dates and reject every unsupported missing observation."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError(f"{ticker}: empty Yahoo history")
    if (
        not frame.columns.is_unique
        or not {"Close", "Volume"}.issubset(frame.columns)
    ):
        raise ValueError(
            f"{ticker}: missing/duplicate Close or Volume columns"
        )
    if (
        not isinstance(frame.index, pd.DatetimeIndex)
        or frame.index.hasnans
    ):
        raise ValueError(f"{ticker}: invalid daily date index")

    result = frame[["Close", "Volume"]].copy()
    index = result.index
    if index.tz is not None:
        index = (
            index.tz_convert("America/New_York").tz_localize(None)
        )
    result.index = index.normalize()
    if not result.index.is_unique:
        raise ValueError(f"{ticker}: duplicate daily dates")
    result = result.sort_index().apply(pd.to_numeric, errors="coerce")
    extras = result.index.difference(dates)
    if len(extras):
        print(
            f"{ticker}: ignoring non-required dates: "
            + ", ".join(extras.strftime("%Y-%m-%d")),
            flush=True,
        )
    result = result.reindex(dates)

    close_valid = result["Close"].map(
        lambda v: (
            pd.notna(v)
            and math.isfinite(float(v))
            and float(v) > 0
        )
    )
    if not close_valid.all():
        bad = ", ".join(
            result.index[~close_valid].strftime("%Y-%m-%d")
        )
        raise ValueError(
            f"{ticker}: invalid/missing Close on {bad}; "
            f"expected latest {dates[-1].date()}"
        )

    interval_map = _volume_interval_map(historical_recoveries)
    invalid_volume_dates = []
    for date, value in result["Volume"].items():
        exact = (
            pd.notna(value)
            and math.isfinite(float(value))
            and float(value) >= 0
        )
        if exact:
            continue
        if str(date.date()) not in interval_map:
            invalid_volume_dates.append(date)
    if invalid_volume_dates:
        bad = ", ".join(
            pd.DatetimeIndex(invalid_volume_dates).strftime("%Y-%m-%d")
        )
        raise ValueError(
            f"{ticker}: invalid/missing Volume on {bad}; "
            f"expected latest {dates[-1].date()}"
        )
    if pd.isna(result["Volume"].iloc[-1]):
        raise ValueError(
            f"{ticker}: latest session Volume must be exact"
        )

    if not result["Volume"].isna().any():
        if float(result["Volume"].iloc[:-1].mean()) <= 0:
            raise ValueError(
                f"{ticker}: previous 20-session mean volume "
                "must be positive"
            )
    return result


def _batch_frame(data: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if not isinstance(data, pd.DataFrame) or data.empty:
        raise ValueError(f"{ticker}: empty batch download")
    if (
        not isinstance(data.columns, pd.MultiIndex)
        or data.columns.nlevels != 2
    ):
        raise ValueError(
            f"{ticker}: expected grouped batch columns"
        )
    for level in (0, 1):
        if ticker in data.columns.get_level_values(level):
            return data.xs(ticker, level=level, axis=1)
    raise ValueError(f"{ticker}: missing from batch download")


def download_market_history(
    tickers: Sequence[str],
    *,
    asof: Any = None,
    previous_snapshot: dict | None = None,
) -> pd.DataFrame:
    """Batch download, bounded retries, then strict chart/snapshot recovery."""
    tickers = list(tickers)
    if not tickers or len(tickers) != len(set(tickers)):
        raise ValueError(
            "Expected a nonempty, unique ticker list"
        )

    now = pd.Timestamp(
        asof if asof is not None else datetime.now(timezone.utc)
    )
    dates = completed_sessions(now)
    start = str(dates[0].date())
    end = str((dates[-1] + pd.Timedelta(days=1)).date())
    print(
        f"Expected EOD session: {dates[-1].date()}; "
        f"exactly 21 XNYS sessions from {start}; "
        f"asof {now.isoformat()}",
        flush=True,
    )
    params = dict(
        start=start,
        end=end,
        interval="1d",
        auto_adjust=True,
        actions=False,
        prepost=False,
        repair=False,
        keepna=True,
        timeout=20,
    )
    try:
        batch = yf.download(
            tickers,
            group_by="ticker",
            multi_level_index=True,
            threads=False,
            ignore_tz=True,
            progress=False,
            **params,
        )
    except Exception as exc:
        print(
            f"Yahoo batch request failed "
            f"({type(exc).__name__}: {exc}); "
            "attempting independent symbol histories.",
            flush=True,
        )
        batch = pd.DataFrame()

    frames: dict[str, pd.DataFrame] = {}
    failures: dict[str, str] = {}
    recovered = []
    quote_recoveries = []
    historical_recoveries = []
    close_at = xcals.get_calendar(
        "XNYS", start=start, end=end
    ).session_close(dates[-1])

    for ticker in tickers:
        try:
            frames[ticker] = validated_window(
                _batch_frame(batch, ticker), ticker, dates
            )
            continue
        except ValueError as exc:
            failures[ticker] = str(exc)
            print(f"Yahoo validation: {exc}", flush=True)

        for attempt, delay in enumerate(RETRY_DELAYS, 1):
            print(
                f"{ticker}: independent history retry "
                f"{attempt}/{len(RETRY_DELAYS)} after {delay}s",
                flush=True,
            )
            time.sleep(delay)
            try:
                retry_params = {
                    **params,
                    "start": str(
                        (
                            dates[0]
                            - pd.Timedelta(days=attempt)
                        ).date()
                    ),
                }
                history = yf.Ticker(ticker).history(
                    raise_errors=True, **retry_params
                )
                frames[ticker] = validated_window(
                    history, ticker, dates
                )
            except Exception as exc:
                failures[ticker] = (
                    f"{type(exc).__name__}: {exc}"
                )
                print(
                    f"{ticker}: retry failed: "
                    f"{failures[ticker]}",
                    flush=True,
                )
            else:
                recovered.append(ticker)
                failures.pop(ticker, None)
                print(
                    f"{ticker}: recovered complete history "
                    f"through {dates[-1].date()}",
                    flush=True,
                )
                break

        if ticker in failures:
            try:
                (
                    history,
                    latest_record,
                    historical_records,
                ) = download_chart_history(
                    ticker,
                    dates,
                    close_at,
                    previous_snapshot,
                )
                frames[ticker] = validated_window(
                    history,
                    ticker,
                    dates,
                    historical_records,
                )
            except Exception as exc:
                failures[ticker] += (
                    f"; chart recovery rejected: "
                    f"{type(exc).__name__}: {exc}"
                )
                print(
                    f"{ticker}: chart recovery rejected: {exc}",
                    flush=True,
                )
            else:
                recovered.append(ticker)
                failures.pop(ticker, None)
                if latest_record:
                    quote_recoveries.append(latest_record)
                    print(
                        f"{ticker}: recovered latest close "
                        "from exact closing quote at "
                        f"{latest_record['quote_time_utc']}: "
                        f"{latest_record['price']}",
                        flush=True,
                    )
                if historical_records:
                    historical_recoveries.extend(
                        historical_records
                    )
                    for record in historical_records:
                        bound = (
                            str(record["volume_min"])
                            if record["volume_exact"]
                            else (
                                f"{record['volume_min']}.."
                                f"{record['volume_max']}"
                            )
                        )
                        print(
                            f"{ticker}: recovered historical "
                            f"{record['market_date']} "
                            f"close={record['adjusted_close']}; "
                            f"daily volume bound={bound}",
                            flush=True,
                        )
                if not latest_record and not historical_records:
                    print(
                        f"{ticker}: complete chart history "
                        "recovered without replacement",
                        flush=True,
                    )

    if failures:
        raise ValueError(
            "Yahoo history still incomplete after bounded retries; "
            "existing snapshot preserved. "
            + " | ".join(
                f"{ticker}: {error}"
                for ticker, error in failures.items()
            )
        )

    data = pd.concat(
        {ticker: frames[ticker] for ticker in tickers},
        axis=1,
    )
    data.attrs["history_validation"] = {
        "calendar": "XNYS",
        "calendar_version": xcals.__version__,
        "expected_market_date": str(dates[-1].date()),
        "first_required_session": start,
        "required_sessions": 21,
        "asof_utc": now.tz_convert("UTC").isoformat(),
        "publication_buffer_minutes": (
            PUBLICATION_BUFFER_MINUTES
        ),
        "recovered_tickers": recovered,
        "closing_quote_recoveries": quote_recoveries,
        "historical_recoveries": historical_recoveries,
        "missing_required_observations": 0,
        "fill_policy": (
            "no estimates or forward fill; latest close may use "
            "an exact-timestamp Yahoo closing quote; a prior "
            "published Yahoo daily observation may be reused "
            "only with corporate-action and minute-bar "
            "corroboration. Legacy daily volume stays an interval "
            "until exact source volume is available."
        ),
    }
    return data
