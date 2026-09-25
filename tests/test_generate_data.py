"""Offline regression tests; network responses are mocked, never called.

CI installs real yfinance. A stub allows these same offline tests in an
isolated review environment without that dependency.
"""
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
import json
import pandas as pd
import requests
try:
    import yfinance
except ModuleNotFoundError:
    module = types.ModuleType('yfinance')
    module.download = Mock(side_effect=RuntimeError('Network must be mocked'))
    sys.modules['yfinance'] = module
import generate_data as g


def prices():
    dates = pd.bdate_range('2026-08-03', periods=22)
    frames = {}
    for ticker in g.WATCHLIST:
        frame = pd.DataFrame({'Close': [100.0]*21+[102.0], 'Volume': [1000.0]*21+[1500.0]}, index=dates)
        if ticker == 'SPY': frame.iloc[-1, 0] = 101.0
        frames[ticker] = frame
    return pd.concat(frames, axis=1)


def html(pct='43.16', ticker='SPY'):
    return f'<h1>{ticker} Volume Sep 01, 2026 4:00 PM EDT</h1><h2>{ticker} Off Exchange &amp; Dark Pool Summary</h2><p>Off Exchange &amp; Dark Pool volume is 1,000,000, which is {pct}%</p>'


class MarketTests(unittest.TestCase):
    def test_return_alpha_and_rvol(self):
        items, date = g.build_market_items(prices())
        self.assertEqual(len(items), 14)
        spy = next(i for i in items if i['ticker'] == 'SPY')
        qqq = next(i for i in items if i['ticker'] == 'QQQ')
        self.assertEqual((spy['return'], spy['alpha']), (1.0, 0.0))
        self.assertEqual((qqq['return'], qqq['alpha'], qqq['rvol']), (2.0, 1.0, 1.5))
        self.assertEqual(qqq['signal'], 'ACCUMULATION')
        self.assertEqual(date, prices().index[-1].date().isoformat())

    def test_distribution(self):
        data = prices(); data.loc[data.index[-1], ('QQQ','Close')] = 99
        items, _ = g.build_market_items(data)
        self.assertEqual(next(i for i in items if i['ticker']=='QQQ')['signal'], 'DISTRIBUTION')

    def test_empty_download_rejected(self):
        with self.assertRaises(ValueError): g.build_market_items(pd.DataFrame())

    def test_short_history_rejected(self):
        with self.assertRaises(ValueError): g.build_market_items(prices().iloc[-20:])

    def test_missing_symbol_rejected(self):
        with self.assertRaises(ValueError): g.build_market_items(prices().drop(columns='QQQ', level=0))

    def test_nan_latest_rejected(self):
        data = prices(); data.loc[data.index[-1], ('QQQ','Close')] = float('nan')
        with self.assertRaises(ValueError): g.build_market_items(data)

    def test_zero_prior_volume_rejected(self):
        data = prices(); data.loc[data.index[:-1], ('QQQ','Volume')] = 0
        with self.assertRaises(ValueError): g.build_market_items(data)

    def test_exact_dated_off_exchange(self):
        value, date, _ = g.parse_off_exchange(html(), 'SPY')
        self.assertEqual((value, date), (43.16, '2026-09-01'))

    def test_zero_and_hundred_are_valid_observations(self):
        for pct in ['0','100']:
            with self.subTest(pct=pct): self.assertEqual(g.parse_off_exchange(html(pct), 'SPY')[0], float(pct))

    def test_invalid_percentage_rejected(self):
        with self.assertRaises(ValueError): g.parse_off_exchange(html('101'), 'SPY')

    def test_wrong_ticker_rejected(self):
        with self.assertRaises(ValueError): g.parse_off_exchange(html(ticker='QQQ'), 'SPY')

    def test_date_mismatch_returns_null(self):
        session = Mock(); session.get.return_value.text = html()
        item = g.fetch_off_exchange(session, 'SPY', '2026-09-02')
        self.assertIsNone(item['darkPool']); self.assertEqual(item['darkPoolStatus'], 'date_mismatch')

    def test_http_failure_returns_null(self):
        session = Mock(); session.get.side_effect = requests.HTTPError('403')
        item = g.fetch_off_exchange(session, 'SPY', '2026-09-01')
        self.assertIsNone(item['darkPool']); self.assertEqual(item['darkPoolStatus'], 'unavailable')

    def test_atomic_writer_rejects_nan_keeps_old_file(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'data.json'; path.write_text('old', encoding='utf-8')
            with self.assertRaises(ValueError): g.write_json_atomic({'x':float('nan')}, path)
            self.assertEqual(path.read_text(), 'old')

    def test_full_pipeline_partial_data(self):
        with TemporaryDirectory() as folder, patch.object(g, 'download_market_history', return_value=prices()), patch.object(g, 'fetch_off_exchange', return_value={'darkPool':None, 'darkPoolStatus':'unavailable'}), patch.object(g, 'build_theme_snapshot', return_value={'groups': [], 'issuer_flows': {}, 'issuer_observations': {}}):
            path = Path(folder)/'docs/data.json'
            result = g.generate_real_market_json(path)
            self.assertEqual(result['data_quality'], 'partial')
            self.assertEqual((len(result['indices']), len(result['sectors'])), (4,10))
            self.assertEqual(len(result['missing_off_exchange']), 14)
            self.assertEqual(json.loads(path.read_text()), result)

    def test_failed_pipeline_preserves_old_file(self):
        with TemporaryDirectory() as folder, patch.object(g, 'download_market_history', return_value=pd.DataFrame()):
            path = Path(folder)/'data.json'; path.write_text('old')
            with self.assertRaises(ValueError): g.generate_real_market_json(path)
            self.assertEqual(path.read_text(), 'old')

if __name__ == '__main__': unittest.main()
