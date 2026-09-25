"""Validate that rotation stays distinct from actual ETF net issuance."""
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from scripts import theme_data as t


class ThemeDataTests(unittest.TestCase):
    def setUp(self):
        self.dates = t.completed_sessions(pd.Timestamp('2026-09-25 03:00:00Z'))
        self.last = str(self.dates[-1].date())

    def test_issuer_dates_and_values_must_match(self):
        html = '<title>iShares Future AI & Tech ETF | ARTY</title><h1>ARTY iShares Future AI & Tech ETF</h1><p>NAV as of Sep 24, 2026 $ $ 78.64</p><div>Shares Outstanding 53,950,000 as of Sep 24, 2026</div>'
        item = t.parse_issuer_html(html, 'ARTY')
        self.assertEqual((item['date'], item['nav'], item['shares']), ('2026-09-24', 78.64, 53950000))
        with self.assertRaisesRegex(ValueError, 'dates differ'):
            t.parse_issuer_html(html.replace('53,950,000 as of Sep 24', '53,950,000 as of Sep 23'), 'ARTY')
        with self.assertRaises(ValueError):
            t.parse_issuer_html(html.replace('ARTY', 'SPY'), 'ARTY')

    def test_flow_requires_every_observation_and_uses_each_days_nav(self):
        dates = self.dates[-6:]
        observations = [dict(date=str(day.date()), shares=1_000_000 + i*1000,
                             nav=100+i, source=t.ISSUER_FUNDS['ARTY']) for i, day in enumerate(dates)]
        flow = t.flow_windows(observations, self.dates)
        self.assertEqual(flow['5']['status'], 'available')
        self.assertEqual(flow['5']['usd'], sum(1000*(100+i) for i in range(1, 6)))
        self.assertEqual(flow['20']['status'], 'insufficient_history')
        self.assertIsNone(flow['20']['usd'])
        self.assertIsNone(t.flow_windows(observations[1:], self.dates)['5']['usd'])

    def test_split_does_not_masquerade_as_flow(self):
        dates = self.dates[-6:]
        obs = [dict(date=str(day.date()), shares=1_000_000, nav=100,
                    source=t.ISSUER_FUNDS['ARTY']) for day in dates]
        obs[-1]['shares'] = 2_000_000
        obs[-1]['nav'] = 50
        self.assertEqual(t.flow_windows(obs, self.dates)['5']['status'], 'possible_split')

    def test_stale_issuer_and_missing_theme_data_stay_unavailable(self):
        last = self.last
        dates = self.dates
        core = pd.concat({'SPY': pd.DataFrame({'Close':[100.0]*21,'Volume':[1000]*21},index=dates),
                          'SMH': pd.DataFrame({'Close':[100.0]*21,'Volume':[1000]*21},index=dates)},axis=1)
        result = Mock()
        result.text = '<title>iShares Future AI & Tech ETF | ARTY</title><h1>ARTY</h1>NAV as of Sep 23, 2026 $78.64 Shares Outstanding 53,950,000 as of Sep 23, 2026'
        with patch.object(t.yf, 'download', return_value=pd.DataFrame()), \
             patch.object(t.yf, 'Ticker', side_effect=RuntimeError('unavailable')), \
             patch.object(t.requests.Session, 'get', return_value=result):
            payload = t.build_theme_snapshot(core, last)
        self.assertEqual(payload['groups'][0]['instruments'][0]['status'], 'unavailable')
        self.assertIsNone(payload['issuer_flows']['ARTY']['5']['usd'])
        self.assertEqual(payload['groups'][0]['instruments'][2]['returns']['20'], 0)

    def test_internal_yahoo_gap_preserves_endpoint_returns_but_hides_rvol(self):
        dates = self.dates
        frame = pd.DataFrame({'Close': [100.0]*20+[110.0],
                              'Volume': [1000.0]*20+[1500.0]}, index=dates)
        frame.loc[dates[-3], ['Close', 'Volume']] = float('nan')
        window = t.theme_window(frame, 'ARTY', dates)
        spy = pd.DataFrame({'Close': [100.0]*21}, index=dates)
        result = t._metrics(window, spy, 'ARTY', self.last)
        self.assertEqual(result['returns']['5'], 10.0)
        self.assertEqual(result['returns']['20'], 10.0)
        self.assertIsNone(result['rvol'])
        self.assertIn(str(dates[-3].date()), result['missing_close_dates'])
        frame.loc[dates[-6], 'Close'] = float('nan')
        self.assertIsNone(t._metrics(t.theme_window(frame, 'ARTY', dates), spy, 'ARTY', self.last)['returns']['5'])

    def test_one_session_delayed_issuer_date_is_recorded_not_relabelled(self):
        dates = self.dates
        prior = str(dates[-2].date())
        core = pd.concat({'SPY': pd.DataFrame({'Close':[100.0]*21,'Volume':[1000]*21},index=dates),
                          'SMH': pd.DataFrame({'Close':[100.0]*21,'Volume':[1000]*21},index=dates)},axis=1)
        response = Mock()
        response.text = f'<title>iShares Future AI & Tech ETF | ARTY</title>NAV as of {pd.Timestamp(prior).strftime("%b %d, %Y")} $78.64 Shares Outstanding 53,950,000 as of {pd.Timestamp(prior).strftime("%b %d, %Y")}'
        with patch.object(t.yf, 'download', return_value=pd.DataFrame()), \
             patch.object(t.yf, 'Ticker', side_effect=RuntimeError('unavailable')), \
             patch.object(t.requests.Session, 'get', return_value=response):
            data = t.build_theme_snapshot(core, self.last)
        self.assertEqual(data['issuer_observations']['ARTY'][-1]['date'], prior)
        self.assertEqual(data['issuer_flows']['ARTY']['5']['end'], prior)


if __name__ == '__main__':
    unittest.main()
