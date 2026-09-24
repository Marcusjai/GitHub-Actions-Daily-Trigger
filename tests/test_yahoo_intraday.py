"""Deterministic tests for strict Yahoo one-minute corroboration."""
import unittest
import pandas as pd

from scripts.yahoo_intraday import parse_regular_session_intraday


OPEN = pd.Timestamp("2026-09-22T13:30:00Z")
CLOSE = pd.Timestamp("2026-09-22T20:00:00Z")


def fixture(*, drop_index=None, wrong_symbol=False, extra_after_close=True):
    stamps = list(pd.date_range(OPEN, periods=390, freq="min"))
    if drop_index is not None:
        stamps.pop(drop_index)
    if extra_after_close:
        stamps.append(pd.Timestamp("2026-09-23T20:00:00Z"))
    n = len(stamps)
    quote = {
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.25] * n,
        "volume": [1000] * n,
    }
    return {
        "chart": {
            "error": None,
            "result": [{
                "meta": {
                    "symbol": "QQQ" if wrong_symbol else "SPY",
                    "currency": "USD",
                    "exchangeTimezoneName": "America/New_York",
                },
                "timestamp": [int(x.timestamp()) for x in stamps],
                "indicators": {"quote": [quote]},
            }],
        }
    }


class IntradayParserTests(unittest.TestCase):
    def test_full_regular_session_is_accepted_and_after_close_row_ignored(self):
        result = parse_regular_session_intraday(
            fixture(), "SPY", OPEN, CLOSE
        )
        self.assertEqual(result["market_date"], "2026-09-22")
        self.assertEqual(result["bar_count"], 390)
        self.assertEqual(result["last_minute_utc"], "2026-09-22T19:59:00+00:00")
        self.assertEqual(result["last_minute_close"], 100.25)
        self.assertEqual(result["regular_volume_lower_bound"], 390000)

    def test_missing_one_minute_bar_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Expected 390"):
            parse_regular_session_intraday(
                fixture(drop_index=100), "SPY", OPEN, CLOSE
            )

    def test_wrong_symbol_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Wrong intraday"):
            parse_regular_session_intraday(
                fixture(wrong_symbol=True), "SPY", OPEN, CLOSE
            )

    def test_invalid_ohlc_is_rejected(self):
        payload = fixture()
        payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][20] = None
        with self.assertRaisesRegex(ValueError, "OHLC"):
            parse_regular_session_intraday(payload, "SPY", OPEN, CLOSE)

    def test_invalid_volume_is_rejected(self):
        payload = fixture()
        payload["chart"]["result"][0]["indicators"]["quote"][0]["volume"][20] = -1
        with self.assertRaisesRegex(ValueError, "minute volume"):
            parse_regular_session_intraday(payload, "SPY", OPEN, CLOSE)

    def test_session_bounds_must_be_timezone_aware(self):
        with self.assertRaises(ValueError):
            parse_regular_session_intraday(
                fixture(), "SPY", pd.Timestamp("2026-09-22 09:30"),
                pd.Timestamp("2026-09-22 16:00")
            )


if __name__ == "__main__":
    unittest.main()
