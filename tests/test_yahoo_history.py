"""Deterministic tests for the #34 failure and recovery without stale fallback.

All Yahoo/network calls are mocked. The real exchange calendar is exercised,
including 2026 NYSE holidays and early closes checked against nyse.com.
"""
import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pandas as pd
try:
    import yfinance
except ModuleNotFoundError:
    module = types.ModuleType('yfinance')
    module.download = Mock(side_effect=RuntimeError('Network must be mocked'))
    sys.modules['yfinance'] = module
from scripts import yahoo_history as h
import generate_data as g

ASOF = '2026-09-22T00:14:00Z'


def frame(dates=None):
    dates = h.completed_sessions(ASOF) if dates is None else dates
    return pd.DataFrame({'Close': [100.]*(len(dates)-1)+[102.],
                         'Volume': [1000.]*(len(dates)-1)+[1500.]}, index=dates)


def batch():
    return pd.concat({'SPY':frame(), 'QQQ':frame()}, axis=1)


class CalendarTests(unittest.TestCase):
    def test_expected_session_after_midnight_utc(self):
        d = h.completed_sessions(ASOF)
        self.assertEqual((len(d), str(d[-1].date())), (21,'2026-09-21'))

    def test_weekend_uses_friday(self):
        self.assertEqual(str(h.completed_sessions('2026-09-20T16:00Z')[-1].date()),'2026-09-18')

    def test_labor_day_is_not_a_required_observation(self):
        d = h.completed_sessions(ASOF)
        self.assertNotIn(pd.Timestamp('2026-09-07'),d)
        self.assertEqual(str(h.completed_sessions('2026-09-07T23:00Z')[-1].date()),'2026-09-04')

    def test_intraday_and_publication_buffer(self):
        for now in ['2026-09-21T15:00Z','2026-09-21T20:29:59Z']:
            with self.subTest(now=now):
                self.assertEqual(str(h.completed_sessions(now)[-1].date()),'2026-09-18')
        self.assertEqual(str(h.completed_sessions('2026-09-21T20:30:00Z')[-1].date()),'2026-09-21')

    def test_winter_close_uses_dst_correctly(self):
        self.assertEqual(str(h.completed_sessions('2026-11-16T21:29Z')[-1].date()),'2026-11-13')
        self.assertEqual(str(h.completed_sessions('2026-11-16T21:30Z')[-1].date()),'2026-11-16')

    def test_black_friday_early_close(self):
        self.assertEqual(str(h.completed_sessions('2026-11-27T18:29Z')[-1].date()),'2026-11-25')
        self.assertEqual(str(h.completed_sessions('2026-11-27T18:30Z')[-1].date()),'2026-11-27')

    def test_naive_asof_is_rejected(self):
        with self.assertRaises(ValueError): h.completed_sessions('2026-09-22')

    def test_timezone_equivalent_asof(self):
        self.assertTrue(h.completed_sessions(ASOF).equals(h.completed_sessions('2026-09-22T08:14:00+08:00')))


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.dates = h.completed_sessions(ASOF)

    def test_extra_non_session_and_future_nan_rows_are_ignored(self):
        f = frame()
        for date in ['2026-09-07','2026-09-20','2026-09-22']:
            f.loc[pd.Timestamp(date)] = [float('nan'),float('nan')]
        actual = h.validated_window(f,'SPY',self.dates)
        pd.testing.assert_frame_equal(actual,frame(),check_freq=False)

    def test_missing_latest_never_moves_window_back(self):
        f=frame().iloc[:-1]
        with self.assertRaisesRegex(ValueError,'2026-09-21'):
            h.validated_window(f,'SPY',self.dates)

    def test_missing_interior_session_never_gets_dropped(self):
        f=frame(); date=f.index[4]; f.loc[date,'Close']=float('nan')
        with self.assertRaisesRegex(ValueError,str(date.date())):
            h.validated_window(f,'SPY',self.dates)

    def test_stale_but_finite_history_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'2026-09-21'):
            h.validated_window(frame(h.completed_sessions('2026-09-20T16:00Z')),'SPY',self.dates)

    def test_invalid_price_and_volume(self):
        for column, value in [('Close',0),('Close',float('inf')),('Volume',-1),('Volume',float('nan'))]:
            with self.subTest(column=column,value=value):
                f=frame(); f.loc[f.index[-1],column]=value
                with self.assertRaisesRegex(ValueError,column):h.validated_window(f,'SPY',self.dates)

    def test_new_york_and_utc_instants_preserve_exchange_dates(self):
        for tz in ['America/New_York','UTC']:
            f=frame(); f.index=f.index.tz_localize('America/New_York').tz_convert(tz)
            pd.testing.assert_frame_equal(h.validated_window(f,'SPY',self.dates),frame(),check_freq=False)

    def test_duplicate_date_rejected(self):
        f=pd.concat([frame(),frame().iloc[-1:]])
        with self.assertRaisesRegex(ValueError,'duplicate'):h.validated_window(f,'SPY',self.dates)

    def test_all_missing_and_nonnumeric_are_rejected(self):
        f=frame().astype(object); f.loc[:,'Close']='not a price'
        with self.assertRaises(ValueError):h.validated_window(f,'SPY',self.dates)

    def test_field_first_multiindex_is_supported(self):
        pd.testing.assert_frame_equal(h._batch_frame(batch().swaplevel(axis=1),'SPY'),frame())


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack=contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.chart=self.stack.enter_context(patch.object(h,'download_chart_history',side_effect=ValueError('No closing quote in this fixture')))
        self.sleep=self.stack.enter_context(patch.object(h.time,'sleep'))
        self.download=self.stack.enter_context(patch.object(h.yf,'download',return_value=batch()))
        self.ticker=self.stack.enter_context(patch.object(h.yf,'Ticker',create=True))
        self.ticker.return_value.history.return_value=frame()

    def test_successful_batch_uses_explicit_session_dates(self):
        data=h.download_market_history(['SPY','QQQ'],asof=ASOF)
        self.assertEqual(str(data.index[-1].date()),'2026-09-21')
        self.ticker.assert_not_called();self.sleep.assert_not_called()
        params=self.download.call_args.kwargs
        self.assertEqual(params['end'],'2026-09-22')
        self.assertFalse(params['threads']);self.assertFalse(params['repair'])
        self.assertEqual(data.attrs['history_validation']['required_sessions'],21)

    def test_transient_spy_nan_is_refetched_not_filled(self):
        bad=batch();bad.loc[bad.index[-1],('SPY','Close')]=float('nan')
        self.download.return_value=bad
        actual=h.download_market_history(['SPY','QQQ'],asof=ASOF)
        self.ticker.assert_called_once_with('SPY')
        self.assertEqual(actual.attrs['history_validation']['recovered_tickers'],['SPY'])
        self.assertEqual(actual['SPY']['Close'].iloc[-1],102)

    def test_first_single_retry_fails_second_succeeds(self):
        self.download.return_value=pd.DataFrame()
        self.ticker.return_value.history.side_effect=[TimeoutError('temporary'),frame()]
        h.download_market_history(['SPY'],asof=ASOF)
        self.assertEqual(self.ticker.return_value.history.call_count,2)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list],[2,6])
        starts=[c.kwargs['start'] for c in self.ticker.return_value.history.call_args_list]
        self.assertEqual(len(set(starts)),2)
        self.assertNotIn(self.download.call_args.kwargs['start'],starts)
        self.assertTrue(all(c.kwargs['end']=='2026-09-22'
                            for c in self.ticker.return_value.history.call_args_list))

    def test_persistent_nan_fails_after_two_retries(self):
        bad=frame();bad.iloc[-1,0]=float('nan')
        self.download.return_value=pd.concat({'SPY':bad},axis=1)
        self.ticker.return_value.history.return_value=bad
        with self.assertRaisesRegex(ValueError,'existing snapshot preserved'):
            h.download_market_history(['SPY'],asof=ASOF)
        self.assertEqual(self.ticker.return_value.history.call_count,2)

    def test_entire_adjusted_series_is_replaced_not_only_nan(self):
        bad=batch();bad.loc[bad.index[-1],('SPY','Close')]=float('nan')
        self.download.return_value=bad
        revised=frame();revised['Close']*=0.95
        self.ticker.return_value.history.return_value=revised
        actual=h.download_market_history(['SPY','QQQ'],asof=ASOF)
        pd.testing.assert_frame_equal(actual['SPY'],revised,check_freq=False)
        pd.testing.assert_frame_equal(actual['QQQ'],frame(),check_freq=False)

    def test_batch_exception_recovers_independently(self):
        self.download.side_effect=TimeoutError('batch timeout')
        actual=h.download_market_history(['SPY','QQQ'],asof=ASOF)
        self.assertEqual(self.ticker.call_count,2)
        self.assertEqual(actual.attrs['history_validation']['recovered_tickers'],['SPY','QQQ'])

    def test_missing_symbol_recovers_only_that_symbol(self):
        self.download.return_value=batch().drop(columns='QQQ',level=0)
        h.download_market_history(['SPY','QQQ'],asof=ASOF)
        self.ticker.assert_called_once_with('QQQ')

    def test_single_retries_cannot_return_stale_history(self):
        stale=frame(h.completed_sessions('2026-09-20T16:00Z'))
        self.download.return_value=pd.concat({'SPY':stale},axis=1)
        self.ticker.return_value.history.return_value=stale
        with self.assertRaisesRegex(ValueError,'expected latest 2026-09-21'):
            h.download_market_history(['SPY'],asof=ASOF)

    def test_flat_batch_is_not_reused_for_every_symbol(self):
        self.download.return_value=frame()
        h.download_market_history(['SPY','QQQ'],asof=ASOF)
        self.assertEqual(self.ticker.call_count,2)

    def test_retry_failure_does_not_touch_existing_json(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'data.json';path.write_text('previous snapshot')
            with patch.object(g,'download_market_history',side_effect=ValueError('required session missing')):
                with self.assertRaises(ValueError):g.generate_real_market_json(path)
            self.assertEqual(path.read_text(),'previous snapshot')

    def test_duplicate_ticker_list_rejected(self):
        with self.assertRaises(ValueError):h.download_market_history(['SPY','SPY'],asof=ASOF)


if __name__=='__main__':unittest.main()
