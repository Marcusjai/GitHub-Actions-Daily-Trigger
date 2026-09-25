"""Optional, dated theme rotation observations and issuer ETF issuance estimates.

Price/volume strength is never labelled as net buying. Issuance uses only
same-date issuer NAV and shares outstanding, collected prospectively.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

import pandas as pd
import requests
import yfinance as yf
import exchange_calendars as xcals

from scripts.yahoo_history import _batch_frame, completed_sessions

THEMES = {
    "AI": {
        "label": "AI / 人工智能",
        "etfs": ["ARTY", "AIQ", "SMH"],
        "stocks": ["NVDA", "AMD", "AVGO", "PLTR", "MSFT"],
        "flow_proxy": "ARTY",
    },
    "BTC": {
        "label": "Bitcoin / 加密概念",
        "etfs": ["IBIT", "FBTC", "BKCH"],
        "stocks": ["COIN", "MSTR", "MARA", "RIOT"],
        "flow_proxy": "IBIT",
    },
}
ISSUER_FUNDS = {
    "ARTY": "https://www.ishares.com/us/products/297905/ishares-future-ai-tech-etf",
    "IBIT": "https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf",
}
ETF_TYPES = {
    "ARTY": "AI ETF", "AIQ": "AI ETF", "SMH": "半導體 ETF",
    "IBIT": "現貨 Bitcoin ETP", "FBTC": "現貨 Bitcoin ETP",
    "BKCH": "區塊鏈股票 ETF",
}
STOCK_NAMES = {
    "NVDA": "NVIDIA", "AMD": "AMD", "AVGO": "Broadcom",
    "PLTR": "Palantir", "MSFT": "Microsoft", "COIN": "Coinbase",
    "MSTR": "Strategy", "MARA": "MARA", "RIOT": "Riot Platforms",
}


class VisibleText(HTMLParser):
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


def parse_issuer_html(html: str, ticker: str) -> dict[str, Any]:
    """Extract NAV and exact shares only when both issuer dates agree."""
    if ticker not in ISSUER_FUNDS:
        raise ValueError("Unknown issuer fund")
    reader = VisibleText()
    reader.feed(html)
    reader.close()
    visible = re.sub(r"\s+", " ", " ".join(reader.parts))
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not title or not re.search(rf"\b{ticker}\b", title.group(1)):
        raise ValueError("Issuer fund ticker not found")
    date = r"([A-Z][a-z]{2} \d{1,2},? \d{4})"
    nav = re.search(
        rf"\bNAV as of {date}\s+(?:\$\s*){{1,2}}([\d,]+(?:\.\d+)?)\b",
        visible, re.I,
    )
    shares = re.search(
        rf"\bShares Outstanding\s+([\d,]+)\s+as of {date}\b",
        visible, re.I,
    )
    if not nav or not shares:
        raise ValueError("Dated issuer NAV/shares not found")
    def parse_date(value: str) -> str:
        return datetime.strptime(value.replace(",", ""), "%b %d %Y").date().isoformat()
    nav_date = parse_date(nav.group(1))
    shares_date = parse_date(shares.group(2))
    if nav_date != shares_date:
        raise ValueError("Issuer NAV and shares dates differ")
    price = float(nav.group(2).replace(",", ""))
    count = int(shares.group(1).replace(",", ""))
    if not math.isfinite(price) or price <= 0 or count <= 0:
        raise ValueError("Invalid issuer NAV/shares")
    return {
        "date": nav_date, "nav": price, "shares": count,
        "source": ISSUER_FUNDS[ticker],
    }


def _verified_past(records: Any, ticker: str, dates: pd.DatetimeIndex) -> dict[str, dict]:
    """Retain previous issuer observations only for permitted trading dates."""
    allowed = {str(day.date()) for day in dates}
    clean = {}
    if not isinstance(records, list):
        return clean
    for record in records:
        if not isinstance(record, dict):
            continue
        d, n, s = record.get("date"), record.get("nav"), record.get("shares")
        if (d in allowed and isinstance(n, (int, float)) and not isinstance(n, bool)
                and math.isfinite(n) and n > 0 and type(s) is int and s > 0
                and record.get("source") == ISSUER_FUNDS[ticker]):
            clean[d] = {"date": d, "nav": n, "shares": s, "source": ISSUER_FUNDS[ticker]}
    return clean


def _looks_like_split(before: dict, after: dict) -> bool:
    ratio = after["shares"] / before["shares"]
    if not (ratio > 1.5 or ratio < 0.67):
        return False
    for factor in (2, 3, 4, 5, 10):
        if (abs(ratio - factor) / factor < .02 or
                abs(ratio - 1 / factor) * factor < .02):
            assets_ratio = after["shares"] * after["nav"] / (before["shares"] * before["nav"])
            return abs(assets_ratio - 1) < .10
    return False


def flow_windows(records: list[dict], dates: pd.DatetimeIndex) -> dict[str, dict]:
    """Sum every daily net share creation at that session's issuer NAV."""
    lookup = {r["date"]: r for r in records}
    out = {}
    for width in (5, 20):
        window = [str(d.date()) for d in dates[-(width + 1):]]
        valid = [lookup[d] for d in window if d in lookup]
        missing = len(window) - len(valid)
        value = None
        status = "insufficient_history" if missing else "available"
        if not missing:
            for before, after in zip(valid, valid[1:]):
                if _looks_like_split(before, after):
                    status = "possible_split"
                    break
            else:
                value = round(sum(
                    (after["shares"] - before["shares"]) * after["nav"]
                    for before, after in zip(valid, valid[1:])
                ))
        out[str(width)] = {
            "usd": value, "status": status, "observations": len(valid),
            "required": width + 1,
            "start": window[0], "end": window[-1],
        }
    return out


def theme_window(frame: pd.DataFrame, ticker: str, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Preserve holes; period returns require exact endpoints, RVOL all volumes."""
    if (not isinstance(frame, pd.DataFrame) or frame.empty or
            not {'Close', 'Volume'}.issubset(frame.columns) or
            not frame.columns.is_unique or not isinstance(frame.index, pd.DatetimeIndex) or
            frame.index.hasnans):
        raise ValueError(f'{ticker}: invalid Yahoo theme frame')
    result = frame[['Close', 'Volume']].copy()
    index = result.index
    if index.tz is not None:
        index = index.tz_convert('America/New_York').tz_localize(None)
    result.index = index.normalize()
    if not result.index.is_unique:
        raise ValueError(f'{ticker}: duplicate Yahoo theme dates')
    result = result.sort_index().apply(pd.to_numeric, errors='coerce').reindex(dates)
    latest = result['Close'].iloc[-1]
    if pd.isna(latest) or not math.isfinite(float(latest)) or latest <= 0:
        raise ValueError(f'{ticker}: latest close missing on {dates[-1].date()}')
    return result


def _metrics(frame: pd.DataFrame, spy: pd.DataFrame, ticker: str, date: str) -> dict:
    closes = frame["Close"]
    volume = frame["Volume"]
    returns = {}
    relative = {}
    for length in (1, 5, 20):
        current = float(closes.iloc[-1]); start = closes.iloc[-1 - length]
        spy_now = float(spy["Close"].iloc[-1]); spy_start = float(spy["Close"].iloc[-1 - length])
        valid = pd.notna(start) and math.isfinite(float(start)) and float(start) > 0
        ret = (current / float(start) - 1) * 100 if valid else None
        baseline = (spy_now / spy_start - 1) * 100
        returns[str(length)] = round(ret, 2) if valid else None
        relative[str(length)] = round(ret - baseline, 2) if valid else None
    complete_volume = all(pd.notna(v) and math.isfinite(float(v)) and float(v) >= 0
                          for v in volume)
    latest_volume = float(volume.iloc[-1]) if pd.notna(volume.iloc[-1]) else None
    if latest_volume is not None and (not math.isfinite(latest_volume) or latest_volume < 0 or latest_volume != int(latest_volume)):
        latest_volume = None
    mean = float(volume.iloc[:-1].mean()) if complete_volume else None
    rvol = round(latest_volume / mean, 2) if complete_volume and mean and mean > 0 else None
    close_gaps = [str(d.date()) for d,v in closes.items() if pd.isna(v) or not math.isfinite(float(v)) or float(v) <= 0]
    volume_gaps = [str(d.date()) for d,v in volume.items() if pd.isna(v) or not math.isfinite(float(v)) or float(v) < 0]
    return {
        "ticker": ticker, "name": STOCK_NAMES.get(ticker, ticker),
        "kind": ETF_TYPES.get(ticker, "股票"),
        "date": date, "price": round(float(closes.iloc[-1]), 2),
        "volume": int(latest_volume) if latest_volume is not None else None,
        "rvol": rvol,
        "returns": returns, "vs_spy": relative,
        "status": "partial" if close_gaps or volume_gaps else "available",
        "missing_close_dates": close_gaps, "missing_volume_dates": volume_gaps,
        "source": "yahoo_adjusted_daily_history",
    }


def build_theme_snapshot(core: pd.DataFrame, market_date: str,
                         previous: dict | None = None) -> dict:
    """Independent theme feed: missing symbols are explicit, core data unaffected."""
    dates = completed_sessions(pd.Timestamp(market_date, tz="America/New_York") + pd.Timedelta(hours=23))
    if str(dates[-1].date()) != market_date:
        raise ValueError("Theme date must equal latest completed US market session")
    spy = core["SPY"].reindex(dates)
    if spy["Close"].isna().any():
        raise ValueError("SPY theme benchmark history incomplete")
    symbols = list(dict.fromkeys(t for info in THEMES.values() for t in info["etfs"] + info["stocks"] if t not in core.columns.get_level_values(0)))
    params = dict(start=str(dates[0].date()), end=str((dates[-1] + pd.Timedelta(days=1)).date()),
                  interval="1d", auto_adjust=True, actions=False, prepost=False,
                  repair=False, keepna=True, timeout=20)
    try:
        batch = yf.download(symbols, group_by="ticker", multi_level_index=True,
                            threads=False, ignore_tz=True, progress=False, **params)
    except Exception as exc:
        print(f"::warning::Theme batch failed: {exc}", flush=True)
        batch = pd.DataFrame()
    results = {}
    for ticker in [t for info in THEMES.values() for t in info["etfs"] + info["stocks"]]:
        if ticker in results:
            continue
        try:
            if ticker in core.columns.get_level_values(0):
                frame = theme_window(core[ticker], ticker, dates)
            else:
                try:
                    frame = theme_window(_batch_frame(batch, ticker), ticker, dates)
                except Exception:
                    history = yf.Ticker(ticker).history(raise_errors=True, **params)
                    frame = theme_window(history, ticker, dates)
            results[ticker] = _metrics(frame, spy, ticker, market_date)
        except Exception as exc:
            print(f"::warning::Theme {ticker} unavailable: {exc}", flush=True)
            results[ticker] = {"ticker": ticker, "name": STOCK_NAMES.get(ticker, ticker),
                               "kind": ETF_TYPES.get(ticker, "股票"), "date": market_date,
                               "status": "unavailable", "error": str(exc)[:180]}
    past = (previous or {}).get("issuer_observations", {})
    if not isinstance(past, dict):
        past = {}
    calendar = xcals.get_calendar('XNYS',
        start=str((dates[-1] - pd.Timedelta(days=90)).date()),
        end=str((dates[-1] + pd.Timedelta(days=1)).date()))
    issuer_dates = pd.DatetimeIndex(calendar.sessions).tz_localize(None).normalize()[-26:]
    recent_issuer_dates = {str(d.date()) for d in issuer_dates[-3:]}
    issuer = {}; flows = {}
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; MarketDashboard/1.0)"})
        for ticker, url in ISSUER_FUNDS.items():
            records = _verified_past(past.get(ticker), ticker, issuer_dates)
            try:
                response = session.get(url, timeout=(5, 15))
                response.raise_for_status()
                record = parse_issuer_html(response.text, ticker)
                if record["date"] not in recent_issuer_dates:
                    raise ValueError(f"issuer date {record['date']} too old or ahead of {market_date}")
                records[record["date"]] = record
            except (ValueError, requests.RequestException) as exc:
                print(f"::warning::{ticker} issuer observation unavailable: {exc}", flush=True)
            issuer[ticker] = [records[k] for k in sorted(records)]
            latest_issuer_date = max(records) if records else market_date
            issuer_window = completed_sessions(pd.Timestamp(latest_issuer_date,
                                  tz="America/New_York") + pd.Timedelta(hours=23))
            flows[ticker] = flow_windows(issuer[ticker], issuer_window)
    return {
        "asof": market_date, "sessions": [str(d.date()) for d in dates],
        "groups": [{"id": key, **info,
                    "instruments": [results[t] for t in info["etfs"] + info["stocks"]]}
                   for key, info in THEMES.items()],
        "issuer_observations": issuer,
        "issuer_flows": flows,
        "flow_method": "sum over each session of (shares outstanding today - prior session) × issuer NAV today; all session observations required; possible splits rejected",
        "rotation_method": "adjusted close 1/5/20-session return minus SPY return; price/volume signal, not investor net buying",
    }
