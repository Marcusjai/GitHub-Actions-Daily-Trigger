"""Reject stale/intraday/after-hours prices instead of treating them as closes."""
import copy
import unittest
from unittest.mock import patch
import pandas as pd
from scripts.yahoo_chart import parse_chart
from scripts import yahoo_history as h
from test_yahoo_history import batch, frame, ASOF

CLOSE = pd.Timestamp('2026-09-21T20:00:00Z')


def fixture():
    dates = h.completed_sessions(ASOF)
    stamps = [int((d.tz_localize('America/New_York') + pd.Timedelta(hours=9, minutes=30)).timestamp()) for d in dates]
    quote = {'open':[99.]*20+[101.], 'low':[98.]*20+[100.], 'high':[101.]*20+[104.],
             'close':[100.]*20+[None], 'volume':[1000]*20+[1500]}
    return {'chart':{'error':None,'result':[{
        'meta':{'symbol':'SPY','currency':'USD','exchangeTimezoneName':'America/New_York',
                'regularMarketTime':int(CLOSE.timestamp()),'regularMarketPrice':102.5},
        'timestamp':stamps, 'indicators':{'quote':[quote], 'adjclose':[{'adjclose':[99.5]*20+[None]}]},
        'events':{},
    }]}}


class ChartTests(unittest.TestCase):
    def parse(self, payload):
        return parse_chart(payload,'SPY',h.completed_sessions(ASOF),CLOSE)

    def test_exact_closing_quote_recovers_with_provenance(self):
        payload=fixture(); before=copy.deepcopy(payload)
        data, provenance=self.parse(payload)
        checked=h.validated_window(data,'SPY',h.completed_sessions(ASOF))
        self.assertEqual(checked['Close'].iloc[-1],102.5)
        self.assertEqual(checked['Close'].iloc[-2],99.5)
        self.assertEqual(checked['Volume'].iloc[-1],1500)
        self.assertEqual(provenance['source_field'],'meta.regularMarketPrice')
        self.assertEqual(provenance['quote_time_utc'],CLOSE.isoformat())
        self.assertEqual(payload,before)

    def test_complete_history_is_not_overridden(self):
        p=fixture();r=p['chart']['result'][0]
        r['indicators']['quote'][0]['close'][-1]=102.7
        r['indicators']['adjclose'][0]['adjclose'][-1]=102.7
        data, provenance=self.parse(p)
        self.assertEqual(data['Close'].iloc[-1],102.7);self.assertIsNone(provenance)

    def test_old_quote_same_clock_time_rejected(self):
        p=fixture();p['chart']['result'][0]['meta']['regularMarketTime']-=3*86400
        with self.assertRaisesRegex(ValueError,'stale'):self.parse(p)

    def test_intraday_and_afterhours_rejected_even_on_same_date(self):
        for offset in [-60,60,3600]:
            with self.subTest(offset=offset):
                p=fixture();p['chart']['result'][0]['meta']['regularMarketTime']+=offset
                with self.assertRaisesRegex(ValueError,'intraday'):self.parse(p)

    def test_missing_or_nonfinite_metadata_price_rejected(self):
        for value in [None,0,-1,float('nan'),float('inf'),'102.5',True]:
            with self.subTest(value=value):
                p=fixture();p['chart']['result'][0]['meta']['regularMarketPrice']=value
                with self.assertRaises(ValueError):self.parse(p)

    def test_price_outside_day_range_rejected(self):
        for value in [97,105]:
            p=fixture();p['chart']['result'][0]['meta']['regularMarketPrice']=value
            with self.assertRaisesRegex(ValueError,'inconsistent'):self.parse(p)

    def test_wrong_symbol_currency_timezone_rejected(self):
        for field,value in [('symbol','QQQ'),('currency','HKD'),('exchangeTimezoneName','UTC')]:
            p=fixture();p['chart']['result'][0]['meta'][field]=value
            with self.assertRaises(ValueError):self.parse(p)

    def test_absent_volume_or_ohlc_not_reconstructed(self):
        for field in ['volume','low','high','open']:
            p=fixture();p['chart']['result'][0]['indicators']['quote'][0][field][-1]=None
            with self.assertRaises(ValueError):self.parse(p)

    def test_missing_past_session_remains_invalid(self):
        p=fixture();p['chart']['result'][0]['indicators']['adjclose'][0]['adjclose'][3]=None
        data,_=self.parse(p)
        with self.assertRaisesRegex(ValueError,'invalid/missing'):
            h.validated_window(data,'SPY',h.completed_sessions(ASOF))

    def test_missing_only_adjustment_cannot_invent_adjusted_price(self):
        p=fixture();p['chart']['result'][0]['indicators']['quote'][0]['close'][-1]=102.5
        data,record=self.parse(p)
        self.assertIsNone(record)
        with self.assertRaises(ValueError):h.validated_window(data,'SPY',h.completed_sessions(ASOF))

    def test_later_corporate_action_rejected(self):
        p=fixture();p['chart']['result'][0]['events']={'splits':{'x':{'date':int((CLOSE+pd.Timedelta(days=1)).timestamp())}}}
        with self.assertRaisesRegex(ValueError,'adjustment basis'):self.parse(p)

    def test_past_corporate_action_keeps_same_response_adjustments(self):
        p=fixture();p['chart']['result'][0]['events']={'dividends':{'x':{'date':int((CLOSE-pd.Timedelta(days=3)).timestamp()),'amount':.5}}}
        data,_=self.parse(p)
        self.assertEqual(data['Close'].iloc[0],99.5)

    def test_arrays_must_align_and_adjusted_column_must_exist(self):
        for mutate in [lambda r:r['timestamp'].pop(), lambda r:r['indicators'].pop('adjclose')]:
            p=fixture();mutate(p['chart']['result'][0])
            with self.assertRaises(ValueError):self.parse(p)

    def test_duplicate_chart_dates_rejected(self):
        p=fixture();p['chart']['result'][0]['timestamp'][-1]=p['chart']['result'][0]['timestamp'][-2]
        with self.assertRaisesRegex(ValueError,'Duplicate'):self.parse(p)

    def test_invalid_schema_rejected(self):
        for p in [{}, {'chart':{'error':{'description':'bad'},'result':None}}]:
            with self.assertRaises(ValueError):self.parse(p)

    def test_helper_accepts_validated_quote_recovery_after_bounded_retries(self):
        bad=frame();bad.iloc[-1,0]=float('nan')
        recovered, provenance=self.parse(fixture())
        with patch.object(h.yf,'download',return_value=pd.concat({'SPY':bad},axis=1)), \
             patch.object(h.yf,'Ticker',create=True) as ticker, \
             patch.object(h.time,'sleep'), \
             patch.object(h,'download_chart_history',return_value=(recovered,provenance)):
            ticker.return_value.history.return_value=bad
            data=h.download_market_history(['SPY'],asof=ASOF)
            self.assertEqual(ticker.return_value.history.call_count,2)
            self.assertEqual(data['SPY']['Close'].iloc[-1],102.5)
            self.assertEqual(data.attrs['history_validation']['closing_quote_recoveries'][0]['ticker'],'SPY')


if __name__=='__main__':unittest.main()
