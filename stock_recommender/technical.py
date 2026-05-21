from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class TechnicalPoint:
    date: str
    value: float | None


@dataclass(frozen=True)
class TechnicalSnapshot:
    prices: tuple[TechnicalPoint, ...]
    ma20: tuple[TechnicalPoint, ...]
    ma60: tuple[TechnicalPoint, ...]
    ma120: tuple[TechnicalPoint, ...]
    rsi14: float | None
    one_month_return_pct: float | None
    three_month_return_pct: float | None
    six_month_return_pct: float | None
    fifty_two_week_high: float | None
    fifty_two_week_low: float | None
    range_position_pct: float | None
    ma20_distance_pct: float | None
    ma60_distance_pct: float | None
    ma120_distance_pct: float | None
    ma20_slope_pct: float | None
    ma60_slope_pct: float | None
    latest_volume: float | None
    avg_volume_20: float | None
    volume_ratio: float | None
    twenty_day_breakout_pct: float | None
    sixty_day_breakout_pct: float | None
    trend_label: str


def build_technical_snapshot(points: Iterable[object], max_points: int = 252) -> TechnicalSnapshot:
    ordered = sorted(
        (point for point in points if _valid_close(getattr(point, "close", None))),
        key=lambda point: getattr(point, "date"),
    )[-max_points:]
    dates = [getattr(point, "date").isoformat() for point in ordered]
    closes = [float(getattr(point, "close")) for point in ordered]
    volumes = [_valid_volume(getattr(point, "volume", None)) for point in ordered]
    prices = tuple(TechnicalPoint(date, close) for date, close in zip(dates, closes))

    high = max(closes) if closes else None
    low = min(closes) if closes else None
    latest = closes[-1] if closes else None
    range_position = _range_position(latest, low, high)
    ma20_values = moving_average(closes, 20)
    ma60_values = moving_average(closes, 60)
    ma120_values = moving_average(closes, 120)
    latest_volume = volumes[-1] if volumes else None
    avg_volume_20 = average_recent_volume(volumes, 20)

    return TechnicalSnapshot(
        prices=prices,
        ma20=_series(dates, ma20_values),
        ma60=_series(dates, ma60_values),
        ma120=_series(dates, ma120_values),
        rsi14=rsi(closes, 14),
        one_month_return_pct=lookback_return(closes, 21),
        three_month_return_pct=lookback_return(closes, 63),
        six_month_return_pct=lookback_return(closes, 126),
        fifty_two_week_high=high,
        fifty_two_week_low=low,
        range_position_pct=range_position,
        ma20_distance_pct=distance_from_average(latest, _last_finite(ma20_values)),
        ma60_distance_pct=distance_from_average(latest, _last_finite(ma60_values)),
        ma120_distance_pct=distance_from_average(latest, _last_finite(ma120_values)),
        ma20_slope_pct=moving_average_slope(ma20_values),
        ma60_slope_pct=moving_average_slope(ma60_values),
        latest_volume=latest_volume,
        avg_volume_20=avg_volume_20,
        volume_ratio=volume_ratio(latest_volume, avg_volume_20),
        twenty_day_breakout_pct=breakout_pct(closes, 20),
        sixty_day_breakout_pct=breakout_pct(closes, 60),
        trend_label=trend_label(closes, ma20_values, ma60_values, ma120_values),
    )


def technical_snapshot_to_dict(snapshot: TechnicalSnapshot) -> dict:
    return {
        "prices": [_point_to_dict(point, "close") for point in snapshot.prices],
        "ma20": [_point_to_dict(point) for point in snapshot.ma20],
        "ma60": [_point_to_dict(point) for point in snapshot.ma60],
        "ma120": [_point_to_dict(point) for point in snapshot.ma120],
        "rsi14": _round_or_none(snapshot.rsi14),
        "oneMonthReturnPct": _round_or_none(snapshot.one_month_return_pct),
        "threeMonthReturnPct": _round_or_none(snapshot.three_month_return_pct),
        "sixMonthReturnPct": _round_or_none(snapshot.six_month_return_pct),
        "fiftyTwoWeekHigh": _round_or_none(snapshot.fifty_two_week_high),
        "fiftyTwoWeekLow": _round_or_none(snapshot.fifty_two_week_low),
        "rangePositionPct": _round_or_none(snapshot.range_position_pct),
        "ma20DistancePct": _round_or_none(snapshot.ma20_distance_pct),
        "ma60DistancePct": _round_or_none(snapshot.ma60_distance_pct),
        "ma120DistancePct": _round_or_none(snapshot.ma120_distance_pct),
        "ma20SlopePct": _round_or_none(snapshot.ma20_slope_pct),
        "ma60SlopePct": _round_or_none(snapshot.ma60_slope_pct),
        "latestVolume": _round_or_none(snapshot.latest_volume),
        "avgVolume20": _round_or_none(snapshot.avg_volume_20),
        "volumeRatio": _round_or_none(snapshot.volume_ratio),
        "twentyDayBreakoutPct": _round_or_none(snapshot.twenty_day_breakout_pct),
        "sixtyDayBreakoutPct": _round_or_none(snapshot.sixty_day_breakout_pct),
        "trendLabel": snapshot.trend_label,
    }


def moving_average(values: list[float] | tuple[float, ...], window: int) -> tuple[float | None, ...]:
    if window <= 0:
        raise ValueError("window must be positive")
    if not values:
        return ()
    results: list[float | None] = []
    rolling_sum = 0.0
    for index, value in enumerate(values):
        rolling_sum += value
        if index >= window:
            rolling_sum -= values[index - window]
        if index + 1 < window:
            results.append(None)
        else:
            results.append(rolling_sum / window)
    return tuple(results)


def rsi(values: list[float] | tuple[float, ...], window: int = 14) -> float | None:
    if len(values) <= window:
        return None
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    recent = changes[-window:]
    gains = [change for change in recent if change > 0]
    losses = [-change for change in recent if change < 0]
    average_gain = sum(gains) / window
    average_loss = sum(losses) / window
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def lookback_return(values: list[float] | tuple[float, ...], lookback: int) -> float | None:
    if len(values) <= lookback:
        return None
    if any(not _valid_close(value) for value in values):
        return None
    start = values[-lookback - 1]
    end = values[-1]
    return ((end / start) - 1) * 100


def average_recent_volume(values: list[float | None] | tuple[float | None, ...], window: int = 20) -> float | None:
    recent = [value for value in values[-window:] if _valid_volume(value)]
    if not recent:
        return None
    return sum(float(value) for value in recent) / len(recent)


def volume_ratio(latest: float | None, average: float | None) -> float | None:
    if not _valid_volume(latest) or not _valid_volume(average):
        return None
    return latest / average


def breakout_pct(values: list[float] | tuple[float, ...], window: int) -> float | None:
    if len(values) <= window:
        return None
    latest = values[-1]
    prior_high = max(values[-window - 1 : -1])
    if not _valid_close(latest) or not _valid_close(prior_high):
        return None
    return ((latest / prior_high) - 1) * 100


def distance_from_average(latest: float | None, average: float | None) -> float | None:
    if not _valid_close(latest) or not _valid_close(average):
        return None
    return ((latest / average) - 1) * 100


def moving_average_slope(values: tuple[float | None, ...], lookback: int = 5) -> float | None:
    latest_index = _last_finite_index(values)
    if latest_index is None:
        return None
    prior_index = latest_index - lookback
    if prior_index < 0:
        return None
    latest = values[latest_index]
    prior = values[prior_index]
    if not _valid_close(latest) or not _valid_close(prior):
        return None
    return ((latest / prior) - 1) * 100


def trend_label(
    closes: list[float] | tuple[float, ...],
    ma20: tuple[float | None, ...],
    ma60: tuple[float | None, ...],
    ma120: tuple[float | None, ...],
) -> str:
    if len(closes) < 120 or not ma20 or not ma60 or not ma120:
        return "데이터 부족"
    latest = closes[-1]
    latest_ma20 = ma20[-1]
    latest_ma60 = ma60[-1]
    latest_ma120 = ma120[-1]
    if latest_ma20 is None or latest_ma60 is None or latest_ma120 is None:
        return "데이터 부족"
    if latest > latest_ma20 > latest_ma60 and latest > latest_ma120:
        return "상승 추세"
    if latest < latest_ma20 < latest_ma60 and latest < latest_ma120:
        return "하락 추세"
    return "중립"


def _range_position(latest: float | None, low: float | None, high: float | None) -> float | None:
    if latest is None or low is None or high is None:
        return None
    if high <= low:
        return 50.0
    return max(0.0, min(100.0, (latest - low) / (high - low) * 100))


def _series(dates: list[str], values: tuple[float | None, ...]) -> tuple[TechnicalPoint, ...]:
    return tuple(TechnicalPoint(date, value) for date, value in zip(dates, values))


def _point_to_dict(point: TechnicalPoint, value_key: str = "value") -> dict:
    return {"date": point.date, value_key: _round_or_none(point.value)}


def _round_or_none(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, 2)


def _valid_close(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def _valid_volume(value: object) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def _last_finite(values: tuple[float | None, ...]) -> float | None:
    index = _last_finite_index(values)
    return values[index] if index is not None else None


def _last_finite_index(values: tuple[float | None, ...]) -> int | None:
    for index in range(len(values) - 1, -1, -1):
        value = values[index]
        if isinstance(value, (int, float)) and math.isfinite(value):
            return index
    return None
