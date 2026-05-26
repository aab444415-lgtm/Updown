from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Iterable

from .data_sources import momentum_to_score
from .models import (
    CorrelationPair,
    CorrelationProfile,
    DetailedValuationProfile,
    EarningsEstimateProfile,
    LiquidityProfile,
    MarketHistoryPoint,
    Momentum,
    SepaProfile,
    StockProfile,
    StockScore,
    ValuationRange,
)


def build_liquidity_profiles(
    stocks: Iterable[StockProfile],
    histories: dict[str, tuple[MarketHistoryPoint, ...]],
    momentums: dict[str, Momentum],
) -> dict[str, LiquidityProfile]:
    return {
        stock.ticker.upper(): liquidity_profile_for_stock(
            stock,
            histories.get(stock.ticker.upper(), ()),
            momentums.get(stock.ticker.upper(), Momentum()),
        )
        for stock in stocks
    }


def liquidity_profile_for_stock(
    stock: StockProfile,
    history: tuple[MarketHistoryPoint, ...],
    momentum: Momentum | None = None,
) -> LiquidityProfile:
    momentum = momentum or Momentum()
    recent = tuple(point for point in history[-63:] if _finite(point.close) and _finite(point.volume))
    warnings: list[str] = []
    if recent:
        volumes = [float(point.volume or 0) for point in recent if point.volume and point.volume > 0]
        dollar_volumes = [point.close * float(point.volume or 0) for point in recent if point.volume and point.volume > 0]
        closes = [point.close for point in recent]
    else:
        volumes = []
        dollar_volumes = []
        closes = []

    if not dollar_volumes and _finite(momentum.latest_close) and _finite(momentum.avg_volume_20):
        dollar_volumes = [float(momentum.latest_close or 0) * float(momentum.avg_volume_20 or 0)]
        volumes = [float(momentum.avg_volume_20 or 0)]
        closes = [float(momentum.latest_close or 0)]
        warnings.append("가격/거래량 1년 히스토리가 부족해 최신 평균 거래량으로 대체했습니다.")

    if not dollar_volumes:
        return LiquidityProfile(
            ticker=stock.ticker,
            score=35.0,
            grade="데이터 부족",
            avg_daily_volume=None,
            avg_dollar_volume=None,
            volume_stability_score=None,
            amihud_illiquidity=None,
            market_impact_bps=None,
            observations=0,
            source="none",
            warnings=("거래량 데이터가 부족해 유동성 리스크를 보수적으로 봅니다.",),
        )

    avg_volume = statistics.fmean(volumes) if volumes else None
    avg_dollar_volume = statistics.fmean(dollar_volumes)
    volume_cv = _coefficient_of_variation(volumes)
    volume_stability = _inverse_scale(volume_cv, low=0.4, high=1.8) if volume_cv is not None else None
    returns = _simple_returns(closes)
    amihud = _amihud_illiquidity(returns, dollar_volumes[1:]) if len(dollar_volumes) > 1 else None
    daily_vol = _stddev(returns)
    impact_bps = daily_vol * math.sqrt(0.01) * 10000 if daily_vol is not None else None

    dollar_score = _dollar_volume_score(avg_dollar_volume)
    amihud_score = _inverse_scale(amihud, low=0.01, high=5.0) if amihud is not None else 50.0
    impact_score = _inverse_scale(impact_bps, low=10, high=120) if impact_bps is not None else 50.0
    stability_score = volume_stability if volume_stability is not None else 50.0
    score = _clamp(
        dollar_score * 0.48 + amihud_score * 0.22 + impact_score * 0.20 + stability_score * 0.10,
        0,
        100,
    )
    if avg_dollar_volume < 500_000:
        score = min(score, 34.0)
    elif avg_dollar_volume < 5_000_000:
        score = min(score, 51.0)
    elif avg_dollar_volume < 50_000_000:
        score = min(score, 67.0)
    grade = _liquidity_grade(score, avg_dollar_volume)
    if score < 45:
        warnings.append("체결 비용과 슬리피지가 추천 성과를 훼손할 수 있습니다.")

    return LiquidityProfile(
        ticker=stock.ticker,
        score=round(score, 1),
        grade=grade,
        avg_daily_volume=round(avg_volume, 1) if avg_volume is not None else None,
        avg_dollar_volume=round(avg_dollar_volume, 1),
        volume_stability_score=round(volume_stability, 1) if volume_stability is not None else None,
        amihud_illiquidity=round(amihud, 4) if amihud is not None else None,
        market_impact_bps=round(impact_bps, 1) if impact_bps is not None else None,
        observations=len(recent) if recent else len(dollar_volumes),
        source="Yahoo Finance chart",
        warnings=tuple(dict.fromkeys(warnings)),
    )


def build_sepa_profiles(
    stock_scores: Iterable[StockScore],
    momentums: dict[str, Momentum],
) -> dict[str, SepaProfile]:
    return {
        item.stock.ticker.upper(): sepa_profile_for_stock(
            item,
            momentums.get(item.stock.ticker.upper(), Momentum()),
        )
        for item in stock_scores
    }


def sepa_profile_for_stock(item: StockScore, momentum: Momentum) -> SepaProfile:
    price = momentum.latest_close
    checks = (
        ("가격 > MA150/MA200", _gt(price, momentum.ma150) and _gt(price, momentum.ma200)),
        ("MA150 > MA200", _gt(momentum.ma150, momentum.ma200)),
        ("MA200 상승", _gt(momentum.ma200_slope_pct, 0)),
        ("MA60 > MA150/MA200", _gt(momentum.ma60, momentum.ma150) and _gt(momentum.ma60, momentum.ma200)),
        ("가격 > MA60", _gt(price, momentum.ma60)),
        ("저점 대비 30% 이상 회복", _ge(momentum.range_position_pct, 30)),
        ("고점 25% 이내", _ge(momentum.drawdown_from_high_pct, -25)),
        ("상대강도 70 이상", _ge(momentum_to_score(momentum), 70)),
    )
    pass_count = sum(1 for _, ok in checks if ok)
    stage2 = pass_count >= 6 and all(ok for _, ok in checks[:5])
    stage = "Stage 2" if stage2 else ("Stage 1/전환" if pass_count >= 4 else "Stage 3/4 또는 데이터 부족")
    stage_label = "매수 가능 추세권" if stage2 else "대기"
    ma_alignment = _ma_alignment_label(momentum)
    price_position = _price_position_label(momentum)
    pivot = _pivot_label(momentum)
    breakout = _breakout_quality_label(momentum)
    volume_bonus = _scale(momentum.volume_ratio, low=0.8, high=2.0) * 0.12
    breakout_bonus = _scale(momentum.twenty_day_breakout_pct, low=-8, high=5) * 0.13
    score = _clamp(pass_count / len(checks) * 75 + volume_bonus + breakout_bonus, 0, 100)
    cautions: list[str] = []
    if not stage2:
        cautions.append("SEPA 기준 Stage 2 조건이 충분하지 않아 진입 타이밍은 보수적으로 봅니다.")
    if _gt(momentum.rsi14, 75):
        cautions.append("RSI 과열권입니다.")
    if not _finite(momentum.latest_close):
        cautions.append("가격 히스토리 데이터가 부족합니다.")
    reasons = [
        f"Trend template {pass_count}/{len(checks)} 통과",
        ma_alignment,
        price_position,
        pivot,
        breakout,
    ]
    return SepaProfile(
        ticker=item.stock.ticker,
        score=round(score, 1),
        stage=stage,
        stage_label=stage_label,
        trend_template_passes=pass_count,
        trend_template_total=len(checks),
        trend_template_checks=tuple(f"{name}: {'통과' if ok else '미달'}" for name, ok in checks),
        ma_alignment=ma_alignment,
        price_position_label=price_position,
        pivot_label=pivot,
        breakout_quality_label=breakout,
        reasons=tuple(reasons),
        cautions=tuple(cautions),
    )


def build_detailed_valuation_profiles(
    stock_scores: Iterable[StockScore],
) -> dict[str, DetailedValuationProfile]:
    return {
        item.stock.ticker.upper(): detailed_valuation_profile_for_stock(item)
        for item in stock_scores
    }


def detailed_valuation_profile_for_stock(item: StockScore) -> DetailedValuationProfile:
    fundamentals = item.stock.fundamentals
    valuation_range = item.valuation_range
    relative_mid = _mid(valuation_range.market_cap_low, valuation_range.market_cap_high)
    dcf = _simple_dcf_market_cap(item)
    fair_mid = _blend(relative_mid, dcf)
    upside_mid = _upside_pct(fair_mid, fundamentals.market_cap)
    bear = _blend(valuation_range.market_cap_low, dcf * 0.78 if dcf else None)
    bull = _blend(valuation_range.market_cap_high, dcf * 1.22 if dcf else None)
    warnings: list[str] = []
    notes = [valuation_range.note]
    if dcf is None:
        warnings.append("FCF 또는 시가총액 데이터가 부족해 DCF는 보조 계산에서 제외했습니다.")
    else:
        notes.append("DCF는 5년 FCF 성장 후 2.5% 영구성장, 10% 할인율을 적용한 보수적 보조값입니다.")
    if relative_mid is None:
        warnings.append("상대 멀티플 기반 적정 시총 범위가 부족합니다.")
    sensitivity = _valuation_sensitivity(fair_mid)
    return DetailedValuationProfile(
        ticker=item.stock.ticker,
        score=item.valuation_score,
        method="상대 멀티플 + 보조 DCF",
        fair_market_cap_mid=round(fair_mid, 1) if fair_mid is not None else None,
        upside_mid_pct=round(upside_mid, 1) if upside_mid is not None else None,
        dcf_market_cap=round(dcf, 1) if dcf is not None else None,
        relative_market_cap=round(relative_mid, 1) if relative_mid is not None else None,
        bear_market_cap=round(bear, 1) if bear is not None else None,
        bull_market_cap=round(bull, 1) if bull is not None else None,
        sensitivity=sensitivity,
        notes=tuple(dict.fromkeys(notes)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def build_earnings_estimate_profiles(
    stock_scores: Iterable[StockScore],
) -> dict[str, EarningsEstimateProfile]:
    return {
        item.stock.ticker.upper(): earnings_estimate_profile_for_stock(item)
        for item in stock_scores
    }


def earnings_estimate_profile_for_stock(item: StockScore) -> EarningsEstimateProfile:
    fundamentals = item.stock.fundamentals
    revenue_yoy = fundamentals.latest_quarter_revenue_yoy_pct
    op_yoy = fundamentals.latest_quarter_operating_income_yoy_pct
    streak = fundamentals.quarterly_revenue_yoy_streak
    score = 50.0
    warnings: list[str] = ["외부 추정치 API가 연결되지 않아 공식 분기 실적 추세만 표시합니다."]
    if _finite(revenue_yoy):
        score += _scale(revenue_yoy, low=-10, high=35) * 0.22 - 11
    if _finite(op_yoy):
        score += _scale(op_yoy, low=-20, high=50) * 0.18 - 9
    if _finite(streak):
        score += _scale(streak, low=0, high=4) * 0.12 - 6
    score = _clamp(score, 0, 100)
    if _finite(revenue_yoy) and _finite(op_yoy):
        revision = "실적 추세 개선" if revenue_yoy >= 10 and op_yoy >= 10 else "실적 추세 확인 필요"
        event_risk = "낮음" if score >= 68 else ("중간" if score >= 48 else "높음")
        beat_summary = f"최근 분기 매출 YoY {revenue_yoy:.1f}%, 영업익 YoY {op_yoy:.1f}%"
    else:
        revision = "추정치 데이터 없음"
        event_risk = "확인 필요"
        beat_summary = "컨센서스와 서프라이즈 이력은 아직 연결되지 않았습니다."
    return EarningsEstimateProfile(
        ticker=item.stock.ticker,
        score=round(score, 1),
        event_risk_label=event_risk,
        next_earnings_date=None,
        eps_consensus=None,
        revenue_consensus=None,
        eps_revision_label=revision,
        beat_miss_summary=beat_summary,
        analyst_coverage=None,
        source="SEC/OpenDART quarterly trend",
        warnings=tuple(dict.fromkeys(warnings)),
    )


def build_correlation_profile(
    stock_scores: Iterable[StockScore],
    histories: dict[str, tuple[MarketHistoryPoint, ...]],
    top_n: int = 10,
) -> CorrelationProfile:
    top_scores = tuple(stock_scores)[:top_n]
    pairs: list[CorrelationPair] = []
    for left_index, left in enumerate(top_scores):
        for right in top_scores[left_index + 1 :]:
            corr, observations = _return_correlation(
                histories.get(left.stock.ticker.upper(), ()),
                histories.get(right.stock.ticker.upper(), ()),
            )
            if corr is not None:
                pairs.append(
                    CorrelationPair(
                        ticker_a=left.stock.ticker,
                        ticker_b=right.stock.ticker,
                        correlation=round(corr, 3),
                        observations=observations,
                    )
                )
    pairs_sorted = tuple(sorted(pairs, key=lambda item: abs(item.correlation), reverse=True))
    correlations = [abs(pair.correlation) for pair in pairs_sorted]
    avg_corr = statistics.fmean(correlations) if correlations else None
    max_corr = correlations[0] if correlations else None
    max_pair = (pairs_sorted[0].ticker_a, pairs_sorted[0].ticker_b) if pairs_sorted else None
    crowded_industries = tuple(
        industry
        for industry, count in Counter(item.stock.industry for item in top_scores).items()
        if count >= 3
    )
    label = _correlation_label(avg_corr, max_corr, crowded_industries)
    hints = _diversification_hints(top_scores, pairs_sorted, crowded_industries)
    total_possible = len(top_scores) * (len(top_scores) - 1) / 2
    coverage = (len(pairs_sorted) / total_possible * 100) if total_possible else 0
    warnings = ()
    if coverage < 50:
        warnings = ("상관관계 계산 가능한 가격 히스토리가 부족합니다.",)
    return CorrelationProfile(
        label=label,
        average_correlation=round(avg_corr, 3) if avg_corr is not None else None,
        max_correlation=round(max_corr, 3) if max_corr is not None else None,
        max_pair=max_pair,
        coverage_pct=round(coverage, 1),
        crowded_industries=crowded_industries,
        diversification_hints=hints,
        pairs=pairs_sorted[:12],
        warnings=warnings,
    )


def _dollar_volume_score(value: float) -> float:
    if value >= 500_000_000:
        return 95
    if value >= 50_000_000:
        return 82
    if value >= 5_000_000:
        return 64
    if value >= 500_000:
        return 42
    return 22


def _liquidity_grade(score: float, dollar_volume: float) -> str:
    if dollar_volume < 500_000:
        return "매우 낮음"
    if dollar_volume < 5_000_000:
        return "낮음"
    if dollar_volume < 50_000_000 and score < 68:
        return "보통"
    if score >= 82 and dollar_volume >= 50_000_000:
        return "매우 높음"
    if score >= 68:
        return "높음"
    if score >= 52:
        return "보통"
    if score >= 35:
        return "낮음"
    return "매우 낮음"


def _simple_returns(values: list[float]) -> list[float]:
    returns: list[float] = []
    for previous, current in zip(values, values[1:]):
        if previous > 0 and current > 0:
            returns.append((current / previous) - 1)
    return returns


def _amihud_illiquidity(returns: list[float], dollar_volumes: list[float]) -> float | None:
    values = [
        abs(ret) / dollar_volume * 1_000_000_000
        for ret, dollar_volume in zip(returns, dollar_volumes)
        if dollar_volume > 0 and math.isfinite(ret)
    ]
    return statistics.fmean(values) if values else None


def _return_correlation(
    left: tuple[MarketHistoryPoint, ...],
    right: tuple[MarketHistoryPoint, ...],
) -> tuple[float | None, int]:
    left_returns = _returns_by_date(left)
    right_returns = _returns_by_date(right)
    common_dates = sorted(set(left_returns) & set(right_returns))
    if len(common_dates) < 40:
        return None, len(common_dates)
    left_values = [left_returns[date] for date in common_dates]
    right_values = [right_returns[date] for date in common_dates]
    corr = _pearson(left_values, right_values)
    return corr, len(common_dates)


def _returns_by_date(history: tuple[MarketHistoryPoint, ...]) -> dict[str, float]:
    ordered = sorted((point for point in history if point.close > 0), key=lambda point: point.date)
    result: dict[str, float] = {}
    for previous, current in zip(ordered, ordered[1:]):
        if previous.close > 0 and current.close > 0:
            result[current.date] = math.log(current.close / previous.close)
    return result


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    mean_left = statistics.fmean(left)
    mean_right = statistics.fmean(right)
    numerator = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    denom_left = math.sqrt(sum((a - mean_left) ** 2 for a in left))
    denom_right = math.sqrt(sum((b - mean_right) ** 2 for b in right))
    if denom_left <= 0 or denom_right <= 0:
        return None
    return numerator / (denom_left * denom_right)


def _correlation_label(avg_corr: float | None, max_corr: float | None, crowded: tuple[str, ...]) -> str:
    if avg_corr is None:
        return "상관 데이터 부족"
    if (max_corr is not None and max_corr >= 0.82) or avg_corr >= 0.65 or crowded:
        return "상관 과밀"
    if avg_corr >= 0.45:
        return "보통"
    return "분산 양호"


def _diversification_hints(
    stock_scores: tuple[StockScore, ...],
    pairs: tuple[CorrelationPair, ...],
    crowded_industries: tuple[str, ...],
) -> tuple[str, ...]:
    hints: list[str] = []
    for industry in crowded_industries:
        hints.append(f"{industry} 후보가 3개 이상이라 업종 상한을 점검하세요.")
    low_corr = [pair for pair in pairs if abs(pair.correlation) < 0.35]
    if low_corr:
        pair = low_corr[0]
        hints.append(f"{pair.ticker_a}/{pair.ticker_b} 조합은 상대적으로 분산 효과가 있습니다.")
    if not hints and stock_scores:
        hints.append("상위 후보 간 과밀 신호는 제한적입니다.")
    return tuple(hints[:4])


def _ma_alignment_label(momentum: Momentum) -> str:
    if _gt(momentum.latest_close, momentum.ma60) and _gt(momentum.ma60, momentum.ma150) and _gt(momentum.ma150, momentum.ma200):
        return "MA 정배열"
    if _gt(momentum.latest_close, momentum.ma150) and _gt(momentum.ma150, momentum.ma200):
        return "장기선 우상향"
    return "MA 배열 미흡"


def _price_position_label(momentum: Momentum) -> str:
    if _ge(momentum.drawdown_from_high_pct, -15) and _ge(momentum.range_position_pct, 70):
        return "고점 근처 강세"
    if _ge(momentum.range_position_pct, 35):
        return "중간권 회복"
    return "저점권 또는 데이터 부족"


def _pivot_label(momentum: Momentum) -> str:
    distance = momentum.previous_swing_high_distance_pct
    if _finite(distance) and -5 <= distance <= 3:
        return "피벗 근접"
    if _finite(momentum.twenty_day_breakout_pct) and momentum.twenty_day_breakout_pct >= 0:
        return "20일 돌파"
    return "피벗 대기"


def _breakout_quality_label(momentum: Momentum) -> str:
    if _finite(momentum.twenty_day_breakout_pct) and momentum.twenty_day_breakout_pct >= 0 and _ge(momentum.volume_ratio, 1.5):
        return "거래량 동반 돌파"
    if _ge(momentum.volume_ratio, 1.2):
        return "거래량 개선"
    return "돌파 확인 부족"


def _simple_dcf_market_cap(item: StockScore) -> float | None:
    fundamentals = item.stock.fundamentals
    fcf = fundamentals.free_cash_flow
    market_cap = fundamentals.market_cap
    if not (_finite(fcf) and fcf > 0 and _finite(market_cap) and market_cap > 0):
        return None
    growth = fundamentals.revenue_growth_pct if _finite(fundamentals.revenue_growth_pct) else 5.0
    start_growth = _clamp(float(growth), 2.0, 18.0) / 100
    terminal_growth = 0.025
    discount = 0.10
    current_fcf = float(fcf)
    pv = 0.0
    for year in range(1, 6):
        year_growth = start_growth + (terminal_growth - start_growth) * ((year - 1) / 4)
        current_fcf *= 1 + year_growth
        pv += current_fcf / ((1 + discount) ** year)
    terminal = current_fcf * (1 + terminal_growth) / (discount - terminal_growth)
    pv += terminal / ((1 + discount) ** 5)
    return max(0.0, pv)


def _valuation_sensitivity(fair_mid: float | None) -> tuple[dict, ...]:
    if fair_mid is None:
        return ()
    return tuple(
        {"case": label, "marketCap": round(fair_mid * multiplier, 1)}
        for label, multiplier in (("Bear", 0.82), ("Base", 1.0), ("Bull", 1.18))
    )


def _mid(left: float | None, right: float | None) -> float | None:
    if _finite(left) and _finite(right):
        return (float(left) + float(right)) / 2
    return None


def _blend(left: float | None, right: float | None) -> float | None:
    values = [float(value) for value in (left, right) if _finite(value)]
    return statistics.fmean(values) if values else None


def _upside_pct(fair_value: float | None, current: float | None) -> float | None:
    if not (_finite(fair_value) and _finite(current) and current > 0):
        return None
    return ((float(fair_value) / float(current)) - 1) * 100


def _coefficient_of_variation(values: list[float]) -> float | None:
    clean = [value for value in values if _finite(value) and value > 0]
    if len(clean) < 2:
        return None
    mean = statistics.fmean(clean)
    if mean <= 0:
        return None
    return statistics.pstdev(clean) / mean


def _stddev(values: list[float]) -> float | None:
    clean = [value for value in values if _finite(value)]
    if len(clean) < 2:
        return None
    return statistics.pstdev(clean)


def _scale(value: float | None, low: float, high: float) -> float:
    if not _finite(value):
        return 50.0
    if high <= low:
        return 50.0
    return _clamp((float(value) - low) / (high - low) * 100, 0, 100)


def _inverse_scale(value: float | None, low: float, high: float) -> float:
    return 100 - _scale(value, low, high)


def _gt(left: float | None, right: float | None) -> bool:
    return _finite(left) and _finite(right) and float(left) > float(right)


def _ge(left: float | None, right: float) -> bool:
    return _finite(left) and float(left) >= right


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
