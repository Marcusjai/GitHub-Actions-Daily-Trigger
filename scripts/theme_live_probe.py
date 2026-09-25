"""Temporary branch-only smoke check; never writes published data."""
import requests
import re
import pandas as pd

from scripts.theme_data import ISSUER_FUNDS, parse_issuer_html, THEMES, VisibleText
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
        if 'response' in locals() and response.status_code == 200:
            parser = VisibleText(); parser.feed(response.text)
            text = re.sub(r'\s+', ' ', ' '.join(parser.parts))
            for needle in ('Shares Outstanding', 'NAV as of', 'NAV', ticker):
                for source, haystack in [('raw', response.text), ('visible', text)]:
                    positions = [m.start() for m in re.finditer(re.escape(needle), haystack, re.I)]
                    print(f'{ticker} {source} {needle}: {len(positions)} occurrences')
                    for pos in positions[:2]:
                        print(re.sub(r'\s+', ' ', haystack[max(0,pos-50):pos+190]))
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
