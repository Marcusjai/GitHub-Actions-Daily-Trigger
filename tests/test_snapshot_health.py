"""Recovery must use completed exchange sessions and correct HKT weekdays."""
import copy
import json
import os
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pandas as pd

from scripts import snapshot_health as health

ASOF = pd.Timestamp('2026-09-30T00:00:00Z')


def snapshot(asof=ASOF):
    date = str(health.completed_sessions(asof)[-1].date())
    rows = [dict(ticker=t, market_date=date, price=100.0, volume=1000,
                 alpha=0.0, rvol=1.0, rvolLookback=20, **{'return': 0.0},
                 darkPool=0.0, darkPoolDate=date, darkPoolStatus='available',
                 darkPoolMetric='off_exchange_day_pct') for t in health.WATCHLIST]
    groups = []
    for key, info in health.THEMES.items():
        instruments = [dict(ticker=t, date=date, price=100.0, rvol=1.0,
                            status='available', returns={'1': 0.0, '5': 0.0, '20': 0.0},
                            vs_spy={'1': 0.0, '5': 0.0, '20': 0.0})
                       for t in info['etfs'] + info['stocks']]
        groups.append(dict(id=key, instruments=instruments))
    return dict(market_date=date, asof=date, indices=rows[:4], sectors=rows[4:],
                history_validation={'expected_market_date': date,
                                    'missing_required_observations': 0}, groups=groups,
                issuer_flows={'ARTY': {'5': {'status': 'insufficient_history'}}})


class SnapshotHealthTests(unittest.TestCase):
    def test_complete_current_session_requires_no_refresh(self):
        self.assertEqual(health.inspect_snapshot(snapshot(), ASOF), (True, []))

    def test_new_generation_timestamp_cannot_hide_stale_session(self):
        payload = snapshot(); payload['market_date'] = '2026-09-28'
        payload['last_updated'] = '2026-09-30 00:00:00 UTC'
        current, reasons = health.inspect_snapshot(payload, ASOF)
        self.assertFalse(current)
        self.assertIn('2026-09-29', reasons[0])

    def test_missing_sources_request_recovery_without_invalidating_core(self):
        payload = snapshot()
        payload['sectors'][0]['darkPool'] = None
        payload['groups'][0]['instruments'][0] = {'ticker': 'ARTY', 'date': payload['market_date'],
                                                'status': 'unavailable'}
        current, reasons = health.inspect_snapshot(payload, ASOF)
        self.assertTrue(current)
        self.assertEqual(len(reasons), 2)

    def test_known_verification_block_does_not_repeat_scraping_a_current_session(self):
        payload = snapshot()
        payload['sectors'][0].update(darkPool=None, darkPoolStatus='blocked')
        self.assertEqual(health.inspect_snapshot(payload, ASOF), (True, []))
        self.assertFalse(health.inspect_snapshot(payload, '2026-10-01T00:00:00Z')[0])

    def test_wrong_core_date_duplicate_ticker_and_nan_fail_publication_gate(self):
        for edit in ('date', 'duplicate', 'nan', 'history'):
            payload = snapshot()
            if edit == 'date': payload['indices'][0]['market_date'] = '2026-09-28'
            if edit == 'duplicate': payload['indices'][1]['ticker'] = 'SPY'
            if edit == 'nan': payload['indices'][0]['price'] = float('nan')
            if edit == 'history': payload['history_validation']['missing_required_observations'] = 1
            with self.subTest(edit=edit):
                self.assertFalse(health.inspect_snapshot(payload, ASOF)[0])

    def test_missing_theme_groups_and_partial_history_still_request_recovery(self):
        payload = snapshot(); payload['groups'] = []
        self.assertTrue(health.inspect_snapshot(payload, ASOF)[1])
        payload = snapshot(); payload['groups'][1]['instruments'][0]['returns']['20'] = None
        self.assertTrue(health.inspect_snapshot(payload, ASOF)[1])

    def test_weekend_holiday_early_close_and_winter_use_exchange_calendar(self):
        cases = {
            '2026-10-04T00:00:00Z': '2026-10-02',
            '2026-09-08T00:00:00Z': '2026-09-04',
            '2026-11-27T18:31:00Z': '2026-11-27',
            '2026-12-01T21:29:00Z': '2026-11-30',
            '2026-12-01T21:30:00Z': '2026-12-01',
        }
        for moment, expected in cases.items():
            with self.subTest(moment=moment):
                self.assertEqual(str(health.completed_sessions(moment)[-1].date()), expected)

    def test_recovery_crons_cover_hkt_tuesday_to_saturday(self):
        workflow = Path('.github/workflows/update_dashboard.yml').read_text()
        crons = re.findall(r"cron: '([^']+)'", workflow)
        def due(moment):
            utc = pd.Timestamp(moment, tz='Asia/Hong_Kong').tz_convert('UTC')
            weekday = (utc.dayofweek + 1) % 7
            for cron in crons:
                minute, hours, _, _, days = cron.split()
                first, last = map(int, days.split('-'))
                if utc.minute == int(minute) and utc.hour in map(int, hours.split(',')) and first <= weekday <= last:
                    return True
            return False
        for day in ('2026-09-30', '2026-10-03'):
            for hour in ('05:30', '06:17', '07:17', '08:17'):
                self.assertTrue(due(f'{day} {hour}'), f'{day} {hour}')
        self.assertFalse(due('2026-10-04 08:17'))
        self.assertFalse(due('2026-09-28 05:30'))

    def test_source_freshness_does_not_hide_stale_pages(self):
        payload = snapshot()
        response = Mock(); response.json.return_value = payload
        with patch.object(health.requests, 'get', return_value=response) as get:
            self.assertTrue(health.public_snapshot_matches(payload, 'https://example.github.io/market/'))
            self.assertEqual(get.call_args.args[0], 'https://example.github.io/market/data.json')
            response.json.return_value = {'market_date': '2026-09-28'}
            self.assertFalse(health.public_snapshot_matches(payload, 'https://example.github.io/market/'))
        with patch.object(health.requests, 'get', side_effect=health.requests.Timeout()):
            self.assertFalse(health.public_snapshot_matches(payload, 'https://example.github.io/market/'))

    def test_schedule_skips_only_after_public_verification_and_manual_run_refreshes(self):
        payload = snapshot()
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'data.json'; path.write_text(json.dumps(payload))
            output = Path(folder) / 'output'
            env = {'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': '',
                   'GITHUB_EVENT_NAME': 'schedule', 'PAGES_URL': 'https://example.github.io/market/'}
            with patch.dict(os.environ, env), patch.object(sys, 'argv', ['health', '--path', str(path), '--github-output']), \
                 patch.object(health, 'completed_sessions', return_value=health.completed_sessions(ASOF)), \
                 patch.object(health, 'public_snapshot_matches', return_value=True):
                health.main()
                self.assertEqual(output.read_text(), 'refresh=false\n')
                output.write_text('')
                os.environ['GITHUB_EVENT_NAME'] = 'workflow_dispatch'
                health.main()
                self.assertEqual(output.read_text(), 'refresh=true\n')


if __name__ == '__main__':
    unittest.main()
