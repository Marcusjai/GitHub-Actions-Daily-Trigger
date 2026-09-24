"""Tests for recovering one interior Yahoo daily gap without inventing data."""
import unittest
from unittest.mock import patch

import pandas as pd

from scripts import yahoo_history as h
from scripts.yahoo_chart import recover_previous_snapshot_gap
import generate_data as g


ASOF = "2026-09-24T00:00:00Z"


def sessions22():
    dates = h.completed_sessions(ASOF)
    cal_dates = h.completed_sessions("2026-09-22T23:00:00Z")
    # Include one extra session before the 21-session output window so the
    # Sep 22 legacy RVOL denominator has all 20 prior sessions available.
    extra = pd.Timestamp("2026-08-24")
    return dates, pd.DatetimeIndex([extra] + list(dates))


def daily_frame():
    dates, extended = sessions22()
    f = pd.DataFrame(
        {"Close": [100.0] * len(extended), "Volume": [1000.0] * len(extended)},
        index=extended,
    )
    target = pd.Timestamp("2026-09-22")
    f.loc[target, ["Close", "Volume"]] = [float("nan"), float("nan")]
    f.loc[dates[-1], ["Close", "Volume"]] = [101.0, 1500.0]
    return f


def payload(ticker="SPY", event_date=None):
    events = {}
    if event_date is not None:
        stamp = int(
            pd.Timestamp(event_date, tz="America/New_York")
            .tz_convert("UTC").timestamp()
        )
        events = {"dividends": {"x": {"date": stamp, "amount": 0.5}}}
    return {
        "chart": {
            "error": None,
            "result": [{
                "meta": {
                    "symbol": ticker,
                    "currency": "USD",
                    "exchangeTimezoneName": "America/New_York",
                },
                "events": events,
            }],
        }
    }


def snapshot(ticker="SPY", *, rvol=1.0, price=100.0, ret=0.0):
    return {
        "last_updated": "2026-09-22 23:43:58 UTC",
        "market_date": "2026-09-22",
        "history_validation": {
            "expected_market_date": "2026-09-22",
            "missing_required_observations": 0,
        },
        "indices": [{
            "ticker": ticker,
            "price": price,
            "return": ret,
            "rvol": rvol,
            "market_date": "2026-09-22",
            "priceSource": "yahoo_adjusted_daily_history",
        }],
        "sectors": [],
    }


def intraday(ticker="SPY", last=100.0, lower=900):
    return {
        "ticker": ticker,
        "market_date": "2026-09-22",
        "last_minute_close": last,
        "last_minute_utc": "2026-09-22T19:59:00+00:00",
        "regular_volume_lower_bound": lower,
        "bar_count": 390,
        "source_urls": ["q1", "q2"],
        "consensus": "query1_query2_exact",
        "volume_note": "lower bound",
    }


class HistoricalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.dates, _ = sessions22()
        self.target = pd.Timestamp("2026-09-22")

    def test_legacy_snapshot_recovers_close_but_keeps_volume_as_interval(self):
        with patch(
            "scripts.yahoo_chart.download_intraday_consensus",
            return_value=intraday(),
        ):
            recovered, records = recover_previous_snapshot_gap(
                payload(), daily_frame(), "SPY", self.dates, snapshot()
            )
        self.assertEqual(recovered.loc[self.target, "Close"], 100.0)
        self.assertTrue(pd.isna(recovered.loc[self.target, "Volume"]))
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertFalse(record["volume_exact"])
        self.assertLess(record["volume_min"], record["volume_max"])
        self.assertLessEqual(record["regular_volume_lower_bound"]
                            if "regular_volume_lower_bound" in record else
                            record["intraday_corroboration"]["regular_volume_lower_bound"],
                            record["volume_max"])
        self.assertEqual(
            record["source_method"], "previous_validated_snapshot"
        )

    def test_later_corporate_action_rejects_cached_adjusted_close(self):
        with patch(
            "scripts.yahoo_chart.download_intraday_consensus",
            return_value=intraday(),
        ):
            with self.assertRaisesRegex(ValueError, "Corporate action"):
                recover_previous_snapshot_gap(
                    payload(event_date="2026-09-23"),
                    daily_frame(), "SPY", self.dates, snapshot()
                )

    def test_final_minute_must_corroborate_previous_close(self):
        with patch(
            "scripts.yahoo_chart.download_intraday_consensus",
            return_value=intraday(last=90.0),
        ):
            with self.assertRaisesRegex(ValueError, "corroborated"):
                recover_previous_snapshot_gap(
                    payload(), daily_frame(), "SPY", self.dates, snapshot()
                )

    def test_intraday_lower_bound_cannot_exceed_daily_volume_bound(self):
        with patch(
            "scripts.yahoo_chart.download_intraday_consensus",
            return_value=intraday(lower=2000),
        ):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                recover_previous_snapshot_gap(
                    payload(), daily_frame(), "SPY", self.dates, snapshot()
                )

    def test_wrong_snapshot_return_is_rejected(self):
        with patch(
            "scripts.yahoo_chart.download_intraday_consensus",
            return_value=intraday(),
        ):
            with self.assertRaisesRegex(ValueError, "return"):
                recover_previous_snapshot_gap(
                    payload(), daily_frame(), "SPY", self.dates,
                    snapshot(ret=1.0)
                )

    def test_wrong_price_source_is_rejected(self):
        s = snapshot()
        s["indices"][0]["priceSource"] = "unknown"
        with patch(
            "scripts.yahoo_chart.download_intraday_consensus",
            return_value=intraday(),
        ):
            with self.assertRaisesRegex(ValueError, "approved Yahoo"):
                recover_previous_snapshot_gap(
                    payload(), daily_frame(), "SPY", self.dates, s
                )

    def test_persisted_recovery_survives_after_snapshot_date_advances(self):
        s = snapshot()
        s["market_date"] = "2026-09-23"
        s["history_validation"] = {
            "expected_market_date": "2026-09-23",
            "missing_required_observations": 0,
            "historical_recoveries": [{
                "ticker": "SPY",
                "market_date": "2026-09-22",
                "adjusted_close": 100.0,
                "volume_min": 995,
                "volume_max": 1005,
            }],
        }
        s["indices"][0]["market_date"] = "2026-09-23"
        with patch(
            "scripts.yahoo_chart.download_intraday_consensus",
            return_value=intraday(),
        ):
            recovered, records = recover_previous_snapshot_gap(
                payload(), daily_frame(), "SPY", self.dates, s
            )
        self.assertEqual(recovered.loc[self.target, "Close"], 100.0)
        self.assertEqual(records[0]["source_method"],
                         "persisted_historical_recovery")

    def test_build_market_items_accepts_interval_only_when_display_rvol_is_invariant(self):
        dates = pd.bdate_range("2026-08-25", periods=21)
        frames = {}
        for ticker in g.WATCHLIST:
            frames[ticker] = pd.DataFrame(
                {"Close": [100.0] * 20 + [102.0],
                 "Volume": [1000.0] * 20 + [1500.0]},
                index=dates,
            )
        data = pd.concat(frames, axis=1)
        target = dates[-2]
        data.loc[target, ("QQQ", "Volume")] = float("nan")
        data.attrs["history_validation"] = {
            "historical_recoveries": [{
                "ticker": "QQQ",
                "market_date": str(target.date()),
                "volume_min": 995,
                "volume_max": 1005,
            }]
        }
        items, _ = g.build_market_items(data)
        qqq = next(item for item in items if item["ticker"] == "QQQ")
        self.assertEqual(qqq["rvol"], 1.5)
        self.assertFalse(qqq["rvolExact"])
        self.assertIn("rvolRange", qqq)

    def test_build_market_items_rejects_wide_volume_uncertainty(self):
        dates = pd.bdate_range("2026-08-25", periods=21)
        frames = {
            ticker: pd.DataFrame(
                {"Close": [100.0] * 20 + [102.0],
                 "Volume": [1000.0] * 20 + [1500.0]},
                index=dates,
            )
            for ticker in g.WATCHLIST
        }
        data = pd.concat(frames, axis=1)
        target = dates[-2]
        data.loc[target, ("QQQ", "Volume")] = float("nan")
        data.attrs["history_validation"] = {
            "historical_recoveries": [{
                "ticker": "QQQ",
                "market_date": str(target.date()),
                "volume_min": 800,
                "volume_max": 1200,
            }]
        }
        with self.assertRaisesRegex(ValueError, "RVOL is ambiguous"):
            g.build_market_items(data)


if __name__ == "__main__":
    unittest.main()
