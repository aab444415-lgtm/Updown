import unittest
from datetime import date, timedelta

from stock_recommender.backtest import PricePoint
from stock_recommender.technical import (
    build_technical_snapshot,
    lookback_return,
    moving_average,
    rsi,
    trend_label,
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


def _history(start_price: float, daily_return: float, count: int) -> tuple[PricePoint, ...]:
    current = date(2025, 1, 1)
    price = start_price
    points: list[PricePoint] = []
    while len(points) < count:
        if current.weekday() < 5:
            price *= 1 + daily_return
            points.append(PricePoint(current, price))
        current += timedelta(days=1)
    return tuple(points)


if __name__ == "__main__":
    unittest.main()
