from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class TechnicalPoint:
    date: str
    value: float | None


@dataclass(frozen=True)
class VolumeProfileZone:
    lower: float
    upper: float
    strength: float
    contains_latest: bool


@dataclass(frozen=True)
class PriceLevelCluster:
    lower: float
    upper: float
    center: float
    count: int


@dataclass(frozen=True)
class StructureZone:
    lower: float
    upper: float
    strength: float
    support_lower: float | None
    support_upper: float | None
    nearest_resistance: float | None
    major_resistance: float | None
    rejection_from_zone: bool
    support_retest_active: bool


@dataclass(frozen=True)
class TechnicalSnapshot:
    prices: tuple[TechnicalPoint, ...]
    ma20: tuple[TechnicalPoint, ...]
    ma60: tuple[TechnicalPoint, ...]
    ma120: tuple[TechnicalPoint, ...]
    ma150: tuple[TechnicalPoint, ...]
    ma200: tuple[TechnicalPoint, ...]
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
    ma150_distance_pct: float | None
    ma200_distance_pct: float | None
    ma20_slope_pct: float | None
    ma60_slope_pct: float | None
    ma150_slope_pct: float | None
    ma200_slope_pct: float | None
    latest_volume: float | None
    avg_volume_20: float | None
    volume_ratio: float | None
    twenty_day_breakout_pct: float | None
    sixty_day_breakout_pct: float | None
    bollinger_upper: float | None
    bollinger_middle: float | None
    bollinger_lower: float | None
    bollinger_bandwidth_pct: float | None
    bollinger_percent_b: float | None
    volume_zone_lower: float | None
    volume_zone_upper: float | None
    volume_zone_strength: float | None
    volume_zone_contains_latest: bool
    previous_swing_high: float | None
    previous_swing_high_distance_pct: float | None
    structure_zone_lower: float | None
    structure_zone_upper: float | None
    structure_zone_strength: float | None
    support_retest_lower: float | None
    support_retest_upper: float | None
    nearest_resistance: float | None
    major_resistance: float | None
    rejection_from_structure_zone: bool
    support_retest_active: bool
    ohlcv_coverage_pct: float | None
    trend_label: str


def build_technical_snapshot(points: Iterable[object], max_points: int = 252) -> TechnicalSnapshot:
    ordered = sorted(
        (point for point in points if _valid_close(getattr(point, "close", None))),
        key=lambda point: getattr(point, "date"),
    )[-max_points:]
    dates = [getattr(point, "date").isoformat() for point in ordered]
    closes = [float(getattr(point, "close")) for point in ordered]
    highs = [_valid_price_or_close(getattr(point, "high", None), close) for point, close in zip(ordered, closes)]
    lows = [_valid_price_or_close(getattr(point, "low", None), close) for point, close in zip(ordered, closes)]
    volumes = [_valid_volume(getattr(point, "volume", None)) for point in ordered]
    prices = tuple(TechnicalPoint(date, close) for date, close in zip(dates, closes))

    high = max(highs) if highs else None
    low = min(lows) if lows else None
    latest = closes[-1] if closes else None
    range_position = _range_position(latest, low, high)
    ma20_values = moving_average(closes, 20)
    ma60_values = moving_average(closes, 60)
    ma120_values = moving_average(closes, 120)
    ma150_values = moving_average(closes, 150)
    ma200_values = moving_average(closes, 200)
    bollinger_upper_values, bollinger_middle_values, bollinger_lower_values = bollinger_bands(
        closes, 20, 2.0
    )
    latest_bollinger_upper = _last_finite(bollinger_upper_values)
    latest_bollinger_middle = _last_finite(bollinger_middle_values)
    latest_bollinger_lower = _last_finite(bollinger_lower_values)
    latest_volume = volumes[-1] if volumes else None
    avg_volume_20 = average_recent_volume(volumes, 20)
    volume_zone = volume_profile_zone(highs, lows, closes, volumes)
    structure = structure_zone(highs, lows, closes, volumes, volume_zone=volume_zone)
    swing_high = previous_swing_high(highs)

    return TechnicalSnapshot(
        prices=prices,
        ma20=_series(dates, ma20_values),
        ma60=_series(dates, ma60_values),
        ma120=_series(dates, ma120_values),
        ma150=_series(dates, ma150_values),
        ma200=_series(dates, ma200_values),
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
        ma150_distance_pct=distance_from_average(latest, _last_finite(ma150_values)),
        ma200_distance_pct=distance_from_average(latest, _last_finite(ma200_values)),
        ma20_slope_pct=moving_average_slope(ma20_values),
        ma60_slope_pct=moving_average_slope(ma60_values),
        ma150_slope_pct=moving_average_slope(ma150_values),
        ma200_slope_pct=moving_average_slope(ma200_values),
        latest_volume=latest_volume,
        avg_volume_20=avg_volume_20,
        volume_ratio=volume_ratio(latest_volume, avg_volume_20),
        twenty_day_breakout_pct=breakout_pct(closes, 20),
        sixty_day_breakout_pct=breakout_pct(closes, 60),
        bollinger_upper=latest_bollinger_upper,
        bollinger_middle=latest_bollinger_middle,
        bollinger_lower=latest_bollinger_lower,
        bollinger_bandwidth_pct=bollinger_bandwidth_pct(
            latest_bollinger_upper, latest_bollinger_middle, latest_bollinger_lower
        ),
        bollinger_percent_b=bollinger_percent_b(
            latest, latest_bollinger_upper, latest_bollinger_lower
        ),
        volume_zone_lower=volume_zone.lower if volume_zone else None,
        volume_zone_upper=volume_zone.upper if volume_zone else None,
        volume_zone_strength=volume_zone.strength if volume_zone else None,
        volume_zone_contains_latest=volume_zone.contains_latest if volume_zone else False,
        previous_swing_high=swing_high,
        previous_swing_high_distance_pct=distance_from_average(latest, swing_high),
        structure_zone_lower=structure.lower if structure else None,
        structure_zone_upper=structure.upper if structure else None,
        structure_zone_strength=structure.strength if structure else None,
        support_retest_lower=structure.support_lower if structure else None,
        support_retest_upper=structure.support_upper if structure else None,
        nearest_resistance=structure.nearest_resistance if structure else None,
        major_resistance=structure.major_resistance if structure else None,
        rejection_from_structure_zone=structure.rejection_from_zone if structure else False,
        support_retest_active=structure.support_retest_active if structure else False,
        ohlcv_coverage_pct=ohlcv_coverage_pct(ordered),
        trend_label=trend_label(closes, ma20_values, ma60_values, ma120_values),
    )


def technical_snapshot_to_dict(snapshot: TechnicalSnapshot) -> dict:
    return {
        "prices": [_point_to_dict(point, "close") for point in snapshot.prices],
        "ma20": [_point_to_dict(point) for point in snapshot.ma20],
        "ma60": [_point_to_dict(point) for point in snapshot.ma60],
        "ma120": [_point_to_dict(point) for point in snapshot.ma120],
        "ma150": [_point_to_dict(point) for point in snapshot.ma150],
        "ma200": [_point_to_dict(point) for point in snapshot.ma200],
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
        "ma150DistancePct": _round_or_none(snapshot.ma150_distance_pct),
        "ma200DistancePct": _round_or_none(snapshot.ma200_distance_pct),
        "ma20SlopePct": _round_or_none(snapshot.ma20_slope_pct),
        "ma60SlopePct": _round_or_none(snapshot.ma60_slope_pct),
        "ma150SlopePct": _round_or_none(snapshot.ma150_slope_pct),
        "ma200SlopePct": _round_or_none(snapshot.ma200_slope_pct),
        "latestVolume": _round_or_none(snapshot.latest_volume),
        "avgVolume20": _round_or_none(snapshot.avg_volume_20),
        "volumeRatio": _round_or_none(snapshot.volume_ratio),
        "twentyDayBreakoutPct": _round_or_none(snapshot.twenty_day_breakout_pct),
        "sixtyDayBreakoutPct": _round_or_none(snapshot.sixty_day_breakout_pct),
        "bollingerUpper": _round_or_none(snapshot.bollinger_upper),
        "bollingerMiddle": _round_or_none(snapshot.bollinger_middle),
        "bollingerLower": _round_or_none(snapshot.bollinger_lower),
        "bollingerBandwidthPct": _round_or_none(snapshot.bollinger_bandwidth_pct),
        "bollingerPercentB": _round_or_none(snapshot.bollinger_percent_b),
        "volumeZoneLower": _round_or_none(snapshot.volume_zone_lower),
        "volumeZoneUpper": _round_or_none(snapshot.volume_zone_upper),
        "volumeZoneStrength": _round_or_none(snapshot.volume_zone_strength),
        "volumeZoneContainsLatest": snapshot.volume_zone_contains_latest,
        "previousSwingHigh": _round_or_none(snapshot.previous_swing_high),
        "previousSwingHighDistancePct": _round_or_none(snapshot.previous_swing_high_distance_pct),
        "structureZoneLower": _round_or_none(snapshot.structure_zone_lower),
        "structureZoneUpper": _round_or_none(snapshot.structure_zone_upper),
        "structureZoneStrength": _round_or_none(snapshot.structure_zone_strength),
        "supportRetestLower": _round_or_none(snapshot.support_retest_lower),
        "supportRetestUpper": _round_or_none(snapshot.support_retest_upper),
        "nearestResistance": _round_or_none(snapshot.nearest_resistance),
        "majorResistance": _round_or_none(snapshot.major_resistance),
        "rejectionFromStructureZone": snapshot.rejection_from_structure_zone,
        "supportRetestActive": snapshot.support_retest_active,
        "ohlcvCoveragePct": _round_or_none(snapshot.ohlcv_coverage_pct),
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


def bollinger_bands(
    values: list[float] | tuple[float, ...], window: int = 20, deviations: float = 2.0
) -> tuple[tuple[float | None, ...], tuple[float | None, ...], tuple[float | None, ...]]:
    if window <= 0:
        raise ValueError("window must be positive")
    if not values:
        return (), (), ()
    upper: list[float | None] = []
    middle: list[float | None] = []
    lower: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < window:
            upper.append(None)
            middle.append(None)
            lower.append(None)
            continue
        window_values = values[index + 1 - window : index + 1]
        mean = sum(window_values) / window
        variance = sum((value - mean) ** 2 for value in window_values) / window
        standard_deviation = math.sqrt(variance)
        middle.append(mean)
        upper.append(mean + standard_deviation * deviations)
        lower.append(mean - standard_deviation * deviations)
    return tuple(upper), tuple(middle), tuple(lower)


def bollinger_bandwidth_pct(
    upper: float | None, middle: float | None, lower: float | None
) -> float | None:
    if not _valid_close(upper) or not _valid_close(middle) or not _finite_number(lower):
        return None
    return ((upper - lower) / middle) * 100


def bollinger_percent_b(
    latest: float | None, upper: float | None, lower: float | None
) -> float | None:
    if not _valid_close(latest) or not _valid_close(upper) or not _finite_number(lower):
        return None
    width = upper - lower
    if width <= 0:
        return 50.0
    return (latest - lower) / width * 100


def volume_profile_zone(
    highs: list[float] | tuple[float, ...],
    lows: list[float] | tuple[float, ...],
    closes: list[float] | tuple[float, ...],
    volumes: list[float | None] | tuple[float | None, ...],
    bins: int = 40,
    lookback: int = 252,
) -> VolumeProfileZone | None:
    if bins <= 0:
        raise ValueError("bins must be positive")
    bars = [
        (
            _valid_price_or_close(high, close),
            _valid_price_or_close(low, close),
            close,
            _profile_volume(volume),
        )
        for high, low, close, volume in zip(highs[-lookback:], lows[-lookback:], closes[-lookback:], volumes[-lookback:])
        if _valid_close(close)
    ]
    if len(bars) < 20:
        return None
    profile_low = min(min(high, low) for high, low, _, _ in bars)
    profile_high = max(max(high, low) for high, low, _, _ in bars)
    if profile_high <= profile_low:
        return None
    width = (profile_high - profile_low) / bins
    volumes_by_bin = [0.0 for _ in range(bins)]
    for high, low, close, volume in bars:
        bar_low = min(high, low, close)
        bar_high = max(high, low, close)
        start = _bin_index(bar_low, profile_low, width, bins)
        end = _bin_index(bar_high, profile_low, width, bins)
        span = max(1, end - start + 1)
        for index in range(start, end + 1):
            volumes_by_bin[index] += volume / span

    positive = sorted((value for value in volumes_by_bin if value > 0), reverse=True)
    if not positive:
        return None
    top_count = max(1, math.ceil(len(volumes_by_bin) * 0.25))
    threshold = positive[min(top_count - 1, len(positive) - 1)]
    core_indexes = [index for index, value in enumerate(volumes_by_bin) if value >= threshold and value > 0]
    if not core_indexes:
        return None

    zones: list[tuple[int, int, float]] = []
    start = previous = core_indexes[0]
    zone_volume = volumes_by_bin[start]
    for index in core_indexes[1:]:
        if index == previous + 1:
            zone_volume += volumes_by_bin[index]
            previous = index
            continue
        zones.append((start, previous, zone_volume))
        start = previous = index
        zone_volume = volumes_by_bin[index]
    zones.append((start, previous, zone_volume))

    latest_high, latest_low, latest_close, _ = bars[-1]
    candle_low = min(latest_high, latest_low, latest_close)
    candle_high = max(latest_high, latest_low, latest_close)
    max_zone_volume = max(zone[2] for zone in zones)
    selected = None
    selected_contains_latest = False
    for zone in zones:
        lower = profile_low + zone[0] * width
        upper = profile_low + (zone[1] + 1) * width
        contains = candle_high >= lower and candle_low <= upper
        if contains and (selected is None or zone[2] > selected[2]):
            selected = zone
            selected_contains_latest = True
    if selected is None:
        latest_mid = (candle_low + candle_high) / 2
        selected = min(
            zones,
            key=lambda zone: abs(latest_mid - (profile_low + ((zone[0] + zone[1] + 1) * width / 2))),
        )
    lower = profile_low + selected[0] * width
    upper = profile_low + (selected[1] + 1) * width
    strength = selected[2] / max_zone_volume * 100 if max_zone_volume > 0 else 0
    return VolumeProfileZone(
        lower=lower,
        upper=upper,
        strength=strength,
        contains_latest=selected_contains_latest,
    )


def structure_zone(
    highs: list[float] | tuple[float, ...],
    lows: list[float] | tuple[float, ...],
    closes: list[float] | tuple[float, ...],
    volumes: list[float | None] | tuple[float | None, ...],
    volume_zone: VolumeProfileZone | None = None,
    lookback: int = 252,
) -> StructureZone | None:
    bars = [
        (
            _valid_price_or_close(high, close),
            _valid_price_or_close(low, close),
            float(close),
            _profile_volume(volume),
        )
        for high, low, close, volume in zip(highs[-lookback:], lows[-lookback:], closes[-lookback:], volumes[-lookback:])
        if _valid_close(close)
    ]
    if len(bars) < 20:
        return None
    local_highs = [bar[0] for bar in bars]
    local_lows = [bar[1] for bar in bars]
    local_closes = [bar[2] for bar in bars]
    local_volumes = [bar[3] for bar in bars]
    volume_zone = volume_zone or volume_profile_zone(local_highs, local_lows, local_closes, local_volumes)

    resistance_clusters = price_level_clusters(_pivot_levels(local_highs, "high") + _upper_wick_levels(bars))
    low_clusters = price_level_clusters(_pivot_levels(local_lows, "low"))
    clusters = tuple(resistance_clusters + low_clusters)
    if volume_zone is None and not clusters:
        return None

    latest_high, latest_low, latest_close, _ = bars[-1]
    candle_low = min(latest_high, latest_low, latest_close)
    candle_high = max(latest_high, latest_low, latest_close)

    if volume_zone is not None:
        lower = volume_zone.lower
        upper = volume_zone.upper
        base_strength = volume_zone.strength
    else:
        selected = min(resistance_clusters or clusters, key=lambda item: abs(item.center - latest_close))
        lower = selected.lower
        upper = selected.upper
        base_strength = min(100.0, 45.0 + selected.count * 12.0)

    base_lower = lower
    base_upper = upper
    merge_low = base_lower * 0.975
    merge_high = base_upper * 1.055
    relevant = [
        cluster
        for cluster in resistance_clusters
        if cluster.count >= 1 and cluster.center >= merge_low and cluster.center <= merge_high
    ]
    if relevant:
        lower_candidates = [base_lower, *(cluster.center for cluster in relevant if cluster.center < base_lower)]
        upper_candidates = [
            base_upper,
            *(
                _resistance_zone_upper(cluster)
                for cluster in relevant
                if cluster.center > base_upper or cluster.upper > base_upper
            ),
        ]
        lower = min(lower_candidates)
        upper = max(upper_candidates)

    if upper <= lower:
        return None

    support_cluster = _support_cluster_below_zone(resistance_clusters, lower, latest_close)
    if support_cluster is None:
        support_cluster = _support_cluster_below_zone(low_clusters, lower, latest_close)
    if support_cluster is None:
        support_lower = lower * 0.960
        support_upper = lower * 0.985
    else:
        support_lower, support_upper = _cluster_zone_bounds(support_cluster)

    target1_candidates = [lower]
    target1_candidates.extend(
        cluster.center
        for cluster in resistance_clusters
        if cluster.center >= support_upper * 1.005 and cluster.center <= upper * 1.02
    )
    nearest_resistance = min(target1_candidates)
    major_resistance = _select_major_resistance(resistance_clusters, nearest_resistance, upper, volume_zone)

    support_active = candle_high >= support_lower and candle_low <= support_upper
    rejection = _rejection_from_structure(bars, lower, upper)
    pivot_strength = min(35.0, sum(cluster.count for cluster in relevant) * 4.0)
    strength = _clamp_float(base_strength * 0.75 + pivot_strength, 0.0, 100.0)

    return StructureZone(
        lower=lower,
        upper=upper,
        strength=strength,
        support_lower=support_lower,
        support_upper=support_upper,
        nearest_resistance=nearest_resistance,
        major_resistance=major_resistance,
        rejection_from_zone=rejection,
        support_retest_active=support_active,
    )


def _resistance_zone_upper(cluster: PriceLevelCluster) -> float:
    if cluster.upper <= cluster.lower:
        return cluster.center * 1.008
    return cluster.upper * 1.005


def _cluster_zone_bounds(cluster: PriceLevelCluster, min_width_pct: float = 0.012) -> tuple[float, float]:
    lower = cluster.lower
    upper = cluster.upper
    min_width = cluster.center * min_width_pct
    if upper - lower < min_width:
        midpoint = cluster.center
        half_width = min_width / 2
        lower = midpoint - half_width
        upper = midpoint + half_width
    return lower, upper


def _select_major_resistance(
    clusters: list[PriceLevelCluster],
    nearest_resistance: float,
    zone_upper: float,
    volume_zone: VolumeProfileZone | None,
) -> float:
    target_floor = nearest_resistance * 1.04
    if volume_zone is not None:
        target_floor = max(target_floor, volume_zone.upper * 1.005)
    candidates = [
        cluster
        for cluster in clusters
        if cluster.center > target_floor and cluster.center <= zone_upper * 1.01
    ]
    reliable = [cluster for cluster in candidates if cluster.count >= 2]
    selected_from = reliable or candidates
    if selected_from:
        return min(selected_from, key=lambda cluster: cluster.center).center
    return zone_upper


def price_level_clusters(
    levels: list[float] | tuple[float, ...],
    tolerance_pct: float = 0.018,
) -> list[PriceLevelCluster]:
    values = sorted(float(value) for value in levels if _valid_close(value))
    if not values:
        return []
    clusters: list[list[float]] = []
    current = [values[0]]
    for value in values[1:]:
        center = sum(current) / len(current)
        if abs(value - center) / center <= tolerance_pct:
            current.append(value)
        else:
            clusters.append(current)
            current = [value]
    clusters.append(current)
    return [
        PriceLevelCluster(
            lower=min(cluster),
            upper=max(cluster),
            center=sum(cluster) / len(cluster),
            count=len(cluster),
        )
        for cluster in clusters
    ]


def previous_swing_high(
    highs: list[float] | tuple[float, ...],
    lookback: int = 126,
    pivot_window: int = 3,
) -> float | None:
    values = [float(value) for value in highs[-lookback:] if _valid_close(value)]
    if len(values) < pivot_window * 2 + 2:
        return None
    pivots: list[float] = []
    for index in range(pivot_window, len(values) - pivot_window):
        value = values[index]
        left = values[index - pivot_window : index]
        right = values[index + 1 : index + 1 + pivot_window]
        if all(value > item for item in left) and all(value >= item for item in right):
            pivots.append(value)
    return pivots[-1] if pivots else None


def _pivot_levels(
    values: list[float] | tuple[float, ...],
    kind: str,
    pivot_window: int = 3,
) -> list[float]:
    valid = [float(value) for value in values if _valid_close(value)]
    if len(valid) < pivot_window * 2 + 2:
        return []
    levels: list[float] = []
    for index in range(pivot_window, len(valid) - pivot_window):
        value = valid[index]
        left = valid[index - pivot_window : index]
        right = valid[index + 1 : index + 1 + pivot_window]
        if kind == "high" and all(value > item for item in left) and all(value >= item for item in right):
            levels.append(value)
        elif kind == "low" and all(value < item for item in left) and all(value <= item for item in right):
            levels.append(value)
    return levels


def _upper_wick_levels(bars: list[tuple[float, float, float, float]]) -> list[float]:
    levels: list[float] = []
    for high, low, close, _ in bars:
        width = high - low
        if width <= 0:
            continue
        if (high - close) / width >= 0.35:
            levels.append(high)
    return levels


def _support_cluster_below_zone(
    clusters: list[PriceLevelCluster],
    zone_lower: float,
    latest_close: float,
) -> PriceLevelCluster | None:
    candidates = [
        cluster
        for cluster in clusters
        if cluster.center < zone_lower * 0.998 and cluster.center >= zone_lower * 0.88
    ]
    if not candidates:
        return None
    if latest_close < zone_lower:
        return min(candidates, key=lambda cluster: abs(cluster.center - latest_close))
    return max(candidates, key=lambda cluster: cluster.center)


def _rejection_from_structure(
    bars: list[tuple[float, float, float, float]],
    zone_lower: float,
    zone_upper: float,
    lookback: int = 30,
) -> bool:
    if len(bars) < 2:
        return False
    recent = bars[-(lookback + 1) : -1]
    if not recent:
        return False
    latest_close = bars[-1][2]
    for high, low, close, _ in recent:
        width = high - low
        touched_zone = high >= zone_lower * 0.995 and low <= zone_upper * 1.02
        upper_rejection = width > 0 and high >= zone_lower * 0.995 and (high - close) / width >= 0.28
        pulled_back = latest_close <= high * 0.975
        if touched_zone and pulled_back:
            return True
        if upper_rejection and latest_close <= zone_upper:
            return True
    return False


def ohlcv_coverage_pct(points: Iterable[object]) -> float | None:
    total = 0
    covered = 0
    for point in points:
        total += 1
        close = getattr(point, "close", None)
        if (
            _valid_close(close)
            and _valid_close(getattr(point, "high", close))
            and _valid_close(getattr(point, "low", close))
            and _valid_volume(getattr(point, "volume", None)) is not None
        ):
            covered += 1
    if total == 0:
        return None
    return covered / total * 100


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


def _clamp_float(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


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


def _valid_price_or_close(value: object, close: float) -> float:
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    return float(close)


def _profile_volume(value: object) -> float:
    valid = _valid_volume(value)
    return valid if valid is not None else 1.0


def _bin_index(value: float, low: float, width: float, bins: int) -> int:
    return max(0, min(bins - 1, int((value - low) / width)))


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


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
