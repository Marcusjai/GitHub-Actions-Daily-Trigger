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

import pandas as pd


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


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


def download_chart_history(ticker: str, dates: pd.DatetimeIndex,
                           expected_close: pd.Timestamp) -> tuple[pd.DataFrame, dict | None]:
    """One additional HTTPS request; no account tokens or proxy service."""
    if not re.fullmatch(r'[A-Z]{1,6}', ticker):
        raise ValueError('Invalid ticker')
    from curl_cffi import requests
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}'
    with requests.Session(impersonate='chrome') as session:
        response = session.get(url, params={'range':'3mo','interval':'1d',
                                            'includeAdjustedClose':'true','events':'div,splits'},
                               timeout=20, allow_redirects=False)
        response.raise_for_status()
        data, record = parse_chart(response.json(), ticker, dates, expected_close)
    if record is not None:
        record['source_url'] = url
    return data, record
