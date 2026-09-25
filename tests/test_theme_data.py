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
        html = '<h1>ARTY iShares Future AI & Tech ETF</h1><p>NAV as of Sep 24, 2026 $$78.64</p><div>Shares Outstanding 53,950,000 as of Sep 24, 2026</div>'
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
        result.text = '<h1>ARTY</h1>NAV as of Sep 23, 2026 $78.64 Shares Outstanding 53,950,000 as of Sep 23, 2026'
        with patch.object(t.yf, 'download', return_value=pd.DataFrame()), \
             patch.object(t.yf, 'Ticker', side_effect=RuntimeError('unavailable')), \
             patch.object(t.requests.Session, 'get', return_value=result):
            payload = t.build_theme_snapshot(core, last)
        self.assertEqual(payload['groups'][0]['instruments'][0]['status'], 'unavailable')
        self.assertIsNone(payload['issuer_flows']['ARTY']['5']['usd'])
        self.assertEqual(payload['groups'][0]['instruments'][2]['returns']['20'], 0)


if __name__ == '__main__':
    unittest.main()
