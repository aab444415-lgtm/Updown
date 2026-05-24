import unittest
import json
from datetime import date, datetime, timedelta, timezone

import stock_recommender.data_sources as data_sources
from stock_recommender.backtest import PricePoint, parse_yahoo_history
from stock_recommender.technical import (
    bollinger_bands,
    build_technical_snapshot,
    lookback_return,
    moving_average,
    previous_swing_high,
    rsi,
    trend_label,
    volume_profile_zone,
)


class TechnicalAnalysisTests(unittest.TestCase):
    def test_moving_average_calculates_window_values(self):
        values = list(range(1, 22))
        averages = moving_average(values, 20)

        self.assertIsNone(averages[18])
        self.assertAlmostEqual(averages[19], 10.5)
        self.assertAlmostEqual(averages[20], 11.5)

    def test_rsi_detects_positive_momentum(self):
        values = [100 + index for index in range(20)]

        self.assertEqual(rsi(values, 14), 100.0)

    def test_range_position_and_returns_are_in_snapshot(self):
        points = _history(start_price=100, daily_return=0.002, count=260)
        snapshot = build_technical_snapshot(points)

        self.assertEqual(len(snapshot.prices), 252)
        self.assertIsNotNone(snapshot.rsi14)
        self.assertIsNotNone(snapshot.one_month_return_pct)
        self.assertIsNotNone(snapshot.three_month_return_pct)
        self.assertIsNotNone(snapshot.six_month_return_pct)
        self.assertGreater(snapshot.range_position_pct, 90)
        self.assertEqual(snapshot.trend_label, "상승 추세")

    def test_volume_metrics_are_in_snapshot(self):
        points = _history(start_price=100, daily_return=0.002, count=260, volume_start=1_000_000)
        snapshot = build_technical_snapshot(points)

        self.assertIsNotNone(snapshot.ma20_distance_pct)
        self.assertIsNotNone(snapshot.ma60_distance_pct)
        self.assertIsNotNone(snapshot.ma120_distance_pct)
        self.assertIsNotNone(snapshot.ma150_distance_pct)
        self.assertIsNotNone(snapshot.ma200_distance_pct)
        self.assertIsNotNone(snapshot.ma20_slope_pct)
        self.assertIsNotNone(snapshot.ma150_slope_pct)
        self.assertIsNotNone(snapshot.ma200_slope_pct)
        self.assertIsNotNone(snapshot.rsi14)
        self.assertIsNotNone(snapshot.latest_volume)
        self.assertIsNotNone(snapshot.avg_volume_20)
        self.assertIsNotNone(snapshot.volume_ratio)
        self.assertIsNotNone(snapshot.twenty_day_breakout_pct)
        self.assertIsNotNone(snapshot.sixty_day_breakout_pct)
        self.assertIsNotNone(snapshot.bollinger_upper)
        self.assertIsNotNone(snapshot.bollinger_middle)
        self.assertIsNotNone(snapshot.bollinger_lower)
        self.assertIsNotNone(snapshot.bollinger_bandwidth_pct)
        self.assertIsNotNone(snapshot.bollinger_percent_b)
        self.assertIsNotNone(snapshot.volume_zone_lower)
        self.assertIsNotNone(snapshot.volume_zone_upper)
        self.assertIsNotNone(snapshot.volume_zone_strength)

    def test_bollinger_bands_use_twenty_day_two_sigma_window(self):
        values = [100.0] * 19 + [110.0]
        upper, middle, lower = bollinger_bands(values)

        self.assertIsNone(upper[18])
        self.assertAlmostEqual(middle[-1], 100.5)
        self.assertGreater(upper[-1], middle[-1])
        self.assertLess(lower[-1], middle[-1])

    def test_yahoo_price_history_parses_ohlcv(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1735689600, 1735776000],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [99.0, 101.0],
                                    "high": [103.0, 106.0],
                                    "low": [98.0, 100.0],
                                    "close": [100.0, 104.0],
                                    "volume": [12345, 23456],
                                }
                            ]
                        },
                    }
                ]
            }
        }

        points = parse_yahoo_history(payload)

        self.assertEqual(points[0].close, 100.0)
        self.assertEqual(points[0].open, 99.0)
        self.assertEqual(points[0].high, 103.0)
        self.assertEqual(points[0].low, 98.0)
        self.assertEqual(points[0].volume, 12345.0)
        self.assertEqual(points[1].volume, 23456.0)

    def test_volume_profile_and_swing_high_detect_chart_levels(self):
        highs = [
            99,
            100,
            101,
            102,
            103,
            102,
            101,
            102,
            104,
            107,
            104,
            103,
            106,
            108,
            111,
            109,
            107,
            106,
            105,
            104,
            103,
            102,
        ]
        lows = [value - 3 for value in highs]
        closes = [value - 1 for value in highs]
        volumes = [100_000] * 18 + [900_000, 950_000, 980_000, 1_000_000]

        zone = volume_profile_zone(highs, lows, closes, volumes, bins=12, lookback=22)

        self.assertIsNotNone(zone)
        assert zone is not None
        self.assertGreaterEqual(zone.strength, 90)
        self.assertTrue(zone.contains_latest)
        self.assertEqual(previous_swing_high(highs, lookback=22, pivot_window=2), 111)

    def test_fetch_momentum_uses_one_year_chart_and_calculates_indicators(self):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        closes = [100 + index * 0.4 for index in range(230)]
        opens = [close - 0.2 for close in closes]
        highs = [close + 1.0 for close in closes]
        lows = [close - 1.0 for close in closes]
        volumes = [1_000_000 + index * 2_500 for index in range(230)]
        timestamps = [int((start + timedelta(days=index)).timestamp()) for index in range(230)]
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": timestamps,
                        "indicators": {
                            "quote": [{"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}],
                        },
                    }
                ]
            }
        }
        called_urls: list[str] = []
        original_open = data_sources._open_url

        def fake_open(url: str, timeout: float = 8.0):
            called_urls.append(url)
            return _FakeResponse(payload)

        try:
            data_sources._open_url = fake_open
            momentum = data_sources.fetch_momentum("TEST")
        finally:
            data_sources._open_url = original_open

        self.assertIn("range=1y", called_urls[0])
        self.assertIsNotNone(momentum.ma20)
        self.assertIsNotNone(momentum.ma60)
        self.assertIsNotNone(momentum.ma120)
        self.assertIsNotNone(momentum.ma150)
        self.assertIsNotNone(momentum.ma200)
        self.assertIsNotNone(momentum.rsi14)
        self.assertEqual(momentum.latest_high, highs[-1])
        self.assertEqual(momentum.latest_low, lows[-1])
        self.assertIsNotNone(momentum.ma20_distance_pct)
        self.assertIsNotNone(momentum.ma150_distance_pct)
        self.assertIsNotNone(momentum.ma200_distance_pct)
        self.assertIsNotNone(momentum.volume_ratio)
        self.assertIsNotNone(momentum.twenty_day_breakout_pct)
        self.assertIsNotNone(momentum.bollinger_upper)
        self.assertIsNotNone(momentum.bollinger_middle)
        self.assertIsNotNone(momentum.bollinger_lower)
        self.assertIsNotNone(momentum.bollinger_percent_b)
        self.assertIsNotNone(momentum.volume_zone_lower)
        self.assertIsNotNone(momentum.volume_zone_upper)
        self.assertIsNotNone(momentum.volume_zone_strength)
        self.assertGreater(momentum.ohlcv_coverage_pct or 0, 90)

    def test_data_shortage_returns_safe_defaults(self):
        snapshot = build_technical_snapshot(_history(start_price=100, daily_return=0.0, count=10))

        self.assertEqual(snapshot.trend_label, "데이터 부족")
        self.assertIsNone(snapshot.rsi14)
        self.assertIsNone(snapshot.one_month_return_pct)

    def test_trend_label_detects_downtrend_and_neutral(self):
        down = [200 - index for index in range(140)]
        flat = [100 + (index % 2) for index in range(140)]

        self.assertEqual(
            trend_label(down, moving_average(down, 20), moving_average(down, 60), moving_average(down, 120)),
            "하락 추세",
        )
        self.assertEqual(
            trend_label(flat, moving_average(flat, 20), moving_average(flat, 60), moving_average(flat, 120)),
            "중립",
        )

    def test_lookback_return_handles_short_or_invalid_series(self):
        self.assertIsNone(lookback_return([100, 101], 21))
        self.assertIsNone(lookback_return([0, 100, 101], 1))
        self.assertAlmostEqual(lookback_return([100, 110, 121], 1), 10.0)


def _history(
    start_price: float,
    daily_return: float,
    count: int,
    volume_start: float | None = None,
) -> tuple[PricePoint, ...]:
    current = date(2025, 1, 1)
    price = start_price
    points: list[PricePoint] = []
    while len(points) < count:
        if current.weekday() < 5:
            price *= 1 + daily_return
            volume = None
            if volume_start is not None:
                volume = volume_start + len(points) * 2_500
            points.append(PricePoint(current, price, volume))
        current += timedelta(days=1)
    return tuple(points)


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
