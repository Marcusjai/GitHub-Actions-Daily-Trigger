"""Temporary branch-only smoke check; never writes published data."""
import requests
import pandas as pd

from scripts.theme_data import ISSUER_FUNDS, parse_issuer_html, THEMES
from scripts.yahoo_history import completed_sessions
import yfinance as yf

for ticker, url in ISSUER_FUNDS.items():
    try:
        response = requests.get(url, timeout=(5, 15), headers={"User-Agent": "Mozilla/5.0"})
        print(f"{ticker} issuer: HTTP {response.status_code}, length {len(response.content)}")
        response.raise_for_status()
        print(f"{ticker} parsed observation: {parse_issuer_html(response.text, ticker)}")
    except Exception as exc:
        print(f"{ticker} issuer unavailable: {type(exc).__name__}: {exc}")
dates = completed_sessions()
tickers = list(dict.fromkeys(t for group in THEMES.values() for t in group['etfs'] + group['stocks'] if t != 'SMH'))
try:
    data = yf.download(tickers, start=str(dates[0].date()),
                       end=str((dates[-1] + pd.Timedelta(days=1)).date()),
                       group_by="ticker", multi_level_index=True, threads=False,
                       ignore_tz=True, progress=False, interval="1d", auto_adjust=True,
                       actions=False, prepost=False, repair=False, keepna=True, timeout=20)
    for ticker in tickers:
        row = data[ticker].loc[str(dates[-1].date())]
        print(f"{ticker}: Close={row.get('Close')} Volume={row.get('Volume')}")
except Exception as exc:
    print(f"Theme batch unavailable: {type(exc).__name__}: {exc}")
