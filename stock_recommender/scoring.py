from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .data_sources import average_industry_momentum, momentum_to_score
from .macro_data import industry_macro_data_score
from .models import (
    BeneficiaryIndustryProfile,
    BeneficiaryIndustryScore,
    DataQuality,
    FUNDAMENTAL_SOURCE_BY_ATTR,
    EarlyGrowthScore,
    Fundamentals,
    IndustryProfile,
    IndustryScore,
    LegendStrategyScore,
    LongTermScore,
    MacroSnapshot,
    MediumTermScore,
    Momentum,
    NewsItem,
    RecommendationReport,
    ShortTermScore,
    StockProfile,
    StockScore,
    TradeTimingSignal,
    ValuationRange,
)
from .time_utils import now_in_app_timezone


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9가-힣][A-Za-z0-9가-힣+.-]*")
LEGEND_DEFAULT_WEIGHTS = {
    "lynch": 0.25,
    "oneil": 0.35,
    "greenblatt": 0.25,
    "fisher": 0.15,
}
OFFICIAL_FUNDAMENTAL_SOURCES = {"SEC EDGAR", "OpenDART"}
OFFICIAL_COVERAGE_FIELDS = (
    "revenue_growth_pct",
    "operating_margin_pct",
    "roe_pct",
    "debt_to_equity_pct",
    "revenue",
    "operating_income",
    "net_income",
    "operating_cash_flow",
    "free_cash_flow",
    "current_ratio_pct",
)
STYLE_WEIGHT_PROFILES = {
    "고성장주": {
        "growth_quality": 0.30,
        "quality": 0.25,
        "valuation": 0.15,
        "momentum": 0.15,
        "industry": 0.10,
        "role": 0.05,
    },
    "가치/금융주": {
        "growth_quality": 0.10,
        "quality": 0.30,
        "valuation": 0.30,
        "momentum": 0.10,
        "industry": 0.15,
        "role": 0.05,
    },
    "경기민감주": {
        "growth_quality": 0.15,
        "quality": 0.20,
        "valuation": 0.20,
        "momentum": 0.15,
        "industry": 0.25,
        "role": 0.05,
    },
    "기본형": {
        "growth_quality": 0.10,
        "quality": 0.35,
        "valuation": 0.25,
        "momentum": 0.10,
        "industry": 0.15,
        "role": 0.05,
    },
}
PORTFOLIO_WEIGHT_RULES = {
    "매수 후보": (8.0, 10.0),
    "관심": (4.0, 6.0),
    "관망": (0.0, 3.0),
    "제외": (0.0, 0.0),
}
HIGH_TRUST_NEWS_SOURCES = (
    "reuters",
    "bloomberg",
    "financial times",
    "wall street journal",
    "wsj",
    "associated press",
    "ap news",
    "cnbc",
    "marketwatch",
)
LOW_TRUST_NEWS_SOURCES = (
    "pr newswire",
    "globenewswire",
    "business wire",
    "accesswire",
    "ein presswire",
)


@dataclass(frozen=True)
class _BeneficiaryNewsSignal:
    score: float
    recent_score: float
    baseline_score: float
    acceleration_score: float
    coverage_label: str
    top_sources: tuple[str, ...]


def build_report(
    macro_context: str,
    industries: Iterable[IndustryProfile],
    stocks: Iterable[StockProfile],
    news_items: Iterable[NewsItem],
    momentums: dict[str, Momentum] | None = None,
    macro_snapshot: MacroSnapshot | None = None,
    data_quality: DataQuality | None = None,
    created_at: datetime | None = None,
    source_events: Iterable[dict] | None = None,
    beneficiary_industries: Iterable[BeneficiaryIndustryProfile] = (),
) -> RecommendationReport:
    industries_tuple = tuple(industries)
    stocks_tuple = tuple(stocks)
    news_tuple = tuple(news_items)
    momentums = momentums or {}
    report_created_at = created_at or now_in_app_timezone()

    industry_scores = score_industries(
        macro_context=macro_context,
        industries=industries_tuple,
        stocks=stocks_tuple,
        news_items=news_tuple,
        momentums=momentums,
        macro_snapshot=macro_snapshot,
    )
    beneficiary_scores = score_beneficiary_industries(
        beneficiary_industries=beneficiary_industries,
        industry_scores=industry_scores,
        stocks=stocks_tuple,
        news_items=news_tuple,
        momentums=momentums,
        macro_context=macro_context,
        macro_snapshot=macro_snapshot,
        created_at=report_created_at,
    )
    stock_scores = score_stocks(stocks_tuple, industry_scores, momentums)
    early_growth_scores = score_early_growth_candidates(stock_scores, momentums)
    short_term_scores = score_short_term_candidates(
        stock_scores, industry_scores, news_tuple, momentums
    )
    medium_term_scores = score_medium_term_candidates(
        stock_scores, industry_scores, news_tuple, momentums
    )
    long_term_scores = score_long_term_candidates(
        stock_scores, industry_scores, news_tuple, momentums, macro_snapshot
    )
    legend_strategy_scores = score_legend_strategy_candidates(stock_scores, momentums)
    return RecommendationReport(
        created_at=report_created_at,
        macro_context=macro_context,
        industry_scores=tuple(sorted(industry_scores, key=lambda item: item.score, reverse=True)),
        stock_scores=tuple(sorted(stock_scores, key=lambda item: item.score, reverse=True)),
        news_items=news_tuple,
        early_growth_scores=early_growth_scores,
        short_term_scores=short_term_scores,
        medium_term_scores=medium_term_scores,
        long_term_scores=long_term_scores,
        legend_strategy_scores=legend_strategy_scores,
        beneficiary_industry_scores=beneficiary_scores,
        macro_snapshot=macro_snapshot,
        data_quality=data_quality or DataQuality(),
        momentums=dict(momentums),
        source_events=tuple(source_events or ()),
    )


def score_industries(
    macro_context: str,
    industries: Iterable[IndustryProfile],
    stocks: Iterable[StockProfile],
    news_items: Iterable[NewsItem],
    momentums: dict[str, Momentum],
    macro_snapshot: MacroSnapshot | None = None,
) -> tuple[IndustryScore, ...]:
    news_text = " ".join(
        " ".join(part for part in (item.title, item.summary or "") if part) for item in news_items
    )
    macro_counter = _counter(macro_context)
    news_counter = _counter(news_text)
    stocks_tuple = tuple(stocks)

    scores: list[IndustryScore] = []
    for industry in industries:
        text_macro_score = _term_score(macro_counter, industry.macro_terms, baseline=40, scale=13)
        data_macro_score = industry_macro_data_score(industry.name, macro_snapshot)
        macro_score = text_macro_score * 0.60 + data_macro_score * 0.40
        news_score = _term_score(news_counter, industry.news_terms, baseline=35, scale=9)
        market_score = average_industry_momentum(industry.name, stocks_tuple, momentums)
        if market_score is None:
            market_score = 50

        total = macro_score * 0.35 + news_score * 0.30 + market_score * 0.35
        evidence = _industry_evidence(
            industry,
            macro_score,
            news_score,
            market_score,
            data_macro_score,
            macro_snapshot,
        )
        scores.append(
            IndustryScore(
                industry=industry,
                score=round(total, 1),
                news_score=round(news_score, 1),
                macro_score=round(macro_score, 1),
                market_score=round(market_score, 1),
                evidence=evidence,
            )
        )
    return tuple(scores)


def score_beneficiary_industries(
    beneficiary_industries: Iterable[BeneficiaryIndustryProfile],
    industry_scores: Iterable[IndustryScore],
    stocks: Iterable[StockProfile],
    news_items: Iterable[NewsItem],
    momentums: dict[str, Momentum],
    macro_context: str,
    macro_snapshot: MacroSnapshot | None = None,
    created_at: datetime | None = None,
) -> tuple[BeneficiaryIndustryScore, ...]:
    industry_score_by_name = {item.industry.name: item for item in industry_scores}
    news_tuple = tuple(news_items)
    reference_time = created_at or now_in_app_timezone()
    macro_counter = _counter(
        " ".join(part for part in (macro_context, macro_snapshot.summary if macro_snapshot else "") if part)
    )
    results: list[BeneficiaryIndustryScore] = []
    for profile in beneficiary_industries:
        source_score = industry_score_by_name.get(profile.source_industry)
        if source_score is None:
            continue
        connection = _clamp(profile.connection_strength, 0, 100)
        macro = _beneficiary_macro_score(profile, source_score, macro_counter)
        news_signal = _beneficiary_news_signal(profile, news_tuple, reference_time)
        news = news_signal.score
        market, proxy_coverage_pct = _beneficiary_proxy_market_score(profile, momentums)
        total = (
            source_score.score * 0.45
            + connection * 0.25
            + macro * 0.15
            + news * 0.10
            + market * 0.05
        )
        display_summary = (
            f"{profile.source_industry} 활황이 {profile.time_horizon} 안에 "
            f"{profile.name} 수요로 번질 가능성을 점검합니다."
        )
        results.append(
            BeneficiaryIndustryScore(
                profile=profile,
                score=round(_clamp(total, 0, 100), 1),
                source_industry_score=source_score.score,
                connection_score=round(connection, 1),
                macro_score=round(macro, 1),
                news_score=round(news, 1),
                market_score=round(market, 1),
                evidence=_beneficiary_evidence(
                    profile,
                    source_score,
                    macro,
                    news_signal,
                    market,
                    proxy_coverage_pct,
                ),
                display_summary=display_summary,
                proxy_momentum_score=round(market, 1),
                proxy_coverage_pct=round(proxy_coverage_pct, 1),
                news_recent_score=round(news_signal.recent_score, 1),
                news_baseline_score=round(news_signal.baseline_score, 1),
                news_acceleration_score=round(news_signal.acceleration_score, 1),
                news_coverage_label=news_signal.coverage_label,
                news_top_sources=news_signal.top_sources,
            )
        )
    return tuple(sorted(results, key=lambda item: item.score, reverse=True))


def score_stocks(
    stocks: Iterable[StockProfile],
    industry_scores: Iterable[IndustryScore],
    momentums: dict[str, Momentum],
) -> tuple[StockScore, ...]:
    industry_score_by_name = {item.industry.name: item for item in industry_scores}
    results: list[StockScore] = []
    for stock in stocks:
        industry_score = industry_score_by_name[stock.industry]
        growth_quality = growth_quality_score(stock.fundamentals)
        quality = quality_score(stock.fundamentals)
        valuation = valuation_score(stock.fundamentals)
        momentum = momentum_to_score(momentums.get(stock.ticker.upper(), Momentum()))
        if momentum is None:
            momentum = 50
        role = 65 if stock.role == "core" else 55
        analysis_style = analysis_style_for_stock(stock)
        weight_profile = weight_profile_for_stock(stock, analysis_style)
        risk_gate, risk_gate_reasons = risk_gate_for_stock(
            stock,
            quality=quality,
            valuation=valuation,
            momentum=momentum,
            growth_quality=growth_quality,
            analysis_style=analysis_style,
        )
        valuation_note = valuation_note_for_stock(stock, valuation, analysis_style)
        valuation_range = valuation_range_for_stock(stock, analysis_style)
        total = style_weighted_stock_score(
            industry=industry_score.score,
            quality=quality,
            growth_quality=growth_quality,
            valuation=valuation,
            momentum=momentum,
            role=role,
            weight_profile=weight_profile,
        )
        total = risk_adjusted_stock_score(total, risk_gate, risk_gate_reasons)
        data_cap, data_cautions = data_coverage_gate_for_stock(stock.fundamentals)
        total = min(total, data_cap)
        reasons = _stock_reasons(
            stock,
            quality,
            growth_quality,
            valuation,
            momentum,
            industry_score,
            analysis_style,
            valuation_note,
        )
        analysis_checks = analysis_checks_for_stock(stock, valuation_note, valuation_range)
        second_order_checks = second_order_checks_for_stock(stock, industry_score, analysis_style)
        cautions = tuple(
            dict.fromkeys(
                (
                    *stock.risks,
                    *industry_score.industry.risks[:1],
                    *(() if risk_gate == "Pass" else risk_gate_reasons),
                    *data_cautions,
                    *risk_cautions_for_stock(stock, analysis_style),
                )
            )
        )
        risk_level = risk_level_for_stock(stock, quality, valuation, momentum, risk_gate)
        decision_grade = decision_grade_for_stock(
            total, quality, valuation, momentum, risk_level, risk_gate
        )
        portfolio_signal, target_weight_pct, max_weight_pct = portfolio_rule_for_stock(
            decision_grade=decision_grade,
            risk_level=risk_level,
            risk_gate=risk_gate,
        )
        sell_signals = sell_signals_for_stock(
            stock=stock,
            decision_grade=decision_grade,
            risk_level=risk_level,
            risk_gate=risk_gate,
            quality=quality,
            valuation=valuation,
            momentum=momentum,
        )
        valuation_label = valuation_label_for_score(valuation)
        results.append(
            StockScore(
                stock=stock,
                score=round(total, 1),
                industry_score=industry_score.score,
                quality_score=round(quality, 1),
                growth_quality_score=round(growth_quality, 1),
                valuation_score=round(valuation, 1),
                momentum_score=round(momentum, 1),
                role_score=role,
                reasons=reasons,
                cautions=cautions,
                decision_grade=decision_grade,
                risk_level=risk_level,
                risk_gate=risk_gate,
                risk_gate_reasons=risk_gate_reasons,
                valuation_label=valuation_label,
                analysis_style=analysis_style,
                weight_profile=weight_profile,
                portfolio_signal=portfolio_signal,
                target_weight_pct=target_weight_pct,
                max_weight_pct=max_weight_pct,
                sell_signals=sell_signals,
                valuation_note=valuation_note,
                valuation_range=valuation_range,
                analysis_checks=analysis_checks,
                second_order_checks=second_order_checks,
            )
        )
    return tuple(results)


def score_early_growth_candidates(
    stock_scores: Iterable[StockScore], momentums: dict[str, Momentum]
) -> tuple[EarlyGrowthScore, ...]:
    results: list[EarlyGrowthScore] = []
    for item in stock_scores:
        stock = item.stock
        fundamentals = stock.fundamentals
        momentum = momentums.get(stock.ticker.upper(), Momentum())
        size = company_size_score(fundamentals.market_cap, fundamentals.market_cap_currency)
        growth = early_revenue_growth_score(fundamentals.revenue_growth_pct)
        pullback = pullback_entry_score(momentum)
        quality_anchor = _early_quality_anchor_score(fundamentals, item.quality_score)
        valuation_anchor = _early_valuation_anchor_score(item)
        total = (
            growth * 0.25
            + size * 0.27
            + pullback * 0.23
            + quality_anchor * 0.15
            + valuation_anchor * 0.07
            + item.industry_score * 0.03
        )
        total -= _early_growth_penalty(fundamentals, momentum, size, growth, pullback)
        score = _clamp(total, 0, 100)
        results.append(
            EarlyGrowthScore(
                stock_score=item,
                score=round(score, 1),
                size_score=round(size, 1),
                growth_score=round(growth, 1),
                pullback_score=round(pullback, 1),
                quality_anchor_score=round(quality_anchor, 1),
                valuation_anchor_score=round(valuation_anchor, 1),
                entry_label=early_growth_entry_label(score, pullback, growth, size),
                reasons=early_growth_reasons(stock, momentum, size, growth, pullback, quality_anchor),
                cautions=early_growth_cautions(stock, momentum, size, pullback, valuation_anchor),
            )
        )
    return tuple(sorted(results, key=lambda item: item.score, reverse=True))


def score_short_term_candidates(
    stock_scores: Iterable[StockScore],
    industry_scores: Iterable[IndustryScore],
    news_items: Iterable[NewsItem],
    momentums: dict[str, Momentum],
) -> tuple[ShortTermScore, ...]:
    industry_score_by_name = {item.industry.name: item for item in industry_scores}
    news_tuple = tuple(news_items)
    news_counter = _counter(
        " ".join(
            " ".join(part for part in (item.title, item.summary or "") if part)
            for item in news_tuple
        )
    )
    results: list[ShortTermScore] = []
    for item in stock_scores:
        stock = item.stock
        industry_score = industry_score_by_name[stock.industry]
        momentum = momentums.get(stock.ticker.upper(), Momentum())
        news = short_term_news_score(stock, industry_score.industry, news_counter, bool(news_tuple))
        market = short_term_market_score(momentum, industry_score.market_score)
        chart = short_term_chart_score(momentum)
        volume = short_term_volume_score(momentum)
        company = short_term_company_score(stock, item.quality_score)
        confidence = candidate_confidence_score(stock, momentum, bool(news_tuple), "short")
        total = chart * 0.45 + volume * 0.20 + market * 0.20 + news * 0.10 + company * 0.05
        total -= short_term_penalty(stock, momentum, news_tuple, market, chart, volume, company)
        score = _apply_short_term_caps(_clamp(total, 0, 100), momentum)
        results.append(
            ShortTermScore(
                stock_score=item,
                score=round(score, 1),
                news_score=round(news, 1),
                market_score=round(market, 1),
                chart_score=round(chart, 1),
                volume_score=round(volume, 1),
                company_score=round(company, 1),
                confidence_score=round(confidence, 1),
                confidence_label=confidence_label(confidence),
                signal_label=short_term_signal_label(score, market, chart, volume, news, confidence),
                setup_label=short_term_setup_label(momentum, chart, volume),
                time_horizon="당일~2주",
                reasons=short_term_reasons(stock, momentum, news, market, chart, volume, company),
                cautions=short_term_cautions(stock, momentum, news_tuple, market, chart, volume, company),
                trade_signal=trade_timing_signal_for_stock(
                    item,
                    momentum,
                    horizon="short",
                    score=score,
                    confidence=confidence,
                    chart=chart,
                    market=market,
                    volume=volume,
                ),
            )
        )
    return tuple(sorted(results, key=lambda item: item.score, reverse=True))


def score_medium_term_candidates(
    stock_scores: Iterable[StockScore],
    industry_scores: Iterable[IndustryScore],
    news_items: Iterable[NewsItem],
    momentums: dict[str, Momentum],
) -> tuple[MediumTermScore, ...]:
    industry_score_by_name = {item.industry.name: item for item in industry_scores}
    news_tuple = tuple(news_items)
    news_counter = _counter(
        " ".join(
            " ".join(part for part in (item.title, item.summary or "") if part)
            for item in news_tuple
        )
    )
    results: list[MediumTermScore] = []
    for item in stock_scores:
        stock = item.stock
        industry_score = industry_score_by_name[stock.industry]
        momentum = momentums.get(stock.ticker.upper(), Momentum())
        company = medium_term_company_score(stock, item.quality_score, item.valuation_score)
        market = medium_term_market_score(momentum, industry_score.market_score)
        chart = medium_term_chart_score(momentum)
        news = medium_term_news_score(stock, industry_score.industry, news_counter, bool(news_tuple))
        total = company * 0.30 + chart * 0.30 + market * 0.25 + news * 0.15
        total -= medium_term_penalty(stock, momentum, news_tuple, company, market, chart)
        score = _clamp(total, 0, 100)
        confidence = candidate_confidence_score(stock, momentum, bool(news_tuple), "medium")
        results.append(
            MediumTermScore(
                stock_score=item,
                score=round(score, 1),
                company_score=round(company, 1),
                market_score=round(market, 1),
                chart_score=round(chart, 1),
                news_score=round(news, 1),
                confidence_score=round(confidence, 1),
                confidence_label=confidence_label(confidence),
                signal_label=medium_term_signal_label(score, company, market, chart, confidence),
                time_horizon="2주~3개월",
                reasons=medium_term_reasons(stock, momentum, company, market, chart, news),
                cautions=medium_term_cautions(stock, momentum, news_tuple, company, market, chart),
                trade_signal=trade_timing_signal_for_stock(
                    item,
                    momentum,
                    horizon="medium",
                    score=score,
                    confidence=confidence,
                    chart=chart,
                    market=market,
                ),
            )
        )
    return tuple(sorted(results, key=lambda item: item.score, reverse=True))


def score_long_term_candidates(
    stock_scores: Iterable[StockScore],
    industry_scores: Iterable[IndustryScore],
    news_items: Iterable[NewsItem],
    momentums: dict[str, Momentum],
    macro_snapshot: MacroSnapshot | None = None,
) -> tuple[LongTermScore, ...]:
    industry_score_by_name = {item.industry.name: item for item in industry_scores}
    news_tuple = tuple(news_items)
    news_counter = _counter(
        " ".join(
            " ".join(part for part in (item.title, item.summary or "") if part)
            for item in news_tuple
        )
    )
    results: list[LongTermScore] = []
    for item in stock_scores:
        stock = item.stock
        industry_score = industry_score_by_name[stock.industry]
        momentum = momentums.get(stock.ticker.upper(), Momentum())
        company = long_term_company_score(stock, item.quality_score, item.valuation_score)
        market = long_term_market_score(momentum, industry_score, macro_snapshot)
        chart = long_term_chart_score(momentum)
        news = long_term_news_score(stock, industry_score.industry, news_counter, bool(news_tuple))
        total = company * 0.50 + market * 0.25 + news * 0.15 + chart * 0.10
        total -= long_term_penalty(stock, momentum, news_tuple, company, market, chart)
        score = _clamp(total, 0, 100)
        confidence = candidate_confidence_score(stock, momentum, bool(news_tuple), "long")
        results.append(
            LongTermScore(
                stock_score=item,
                score=round(score, 1),
                company_score=round(company, 1),
                market_score=round(market, 1),
                chart_score=round(chart, 1),
                news_score=round(news, 1),
                confidence_score=round(confidence, 1),
                confidence_label=confidence_label(confidence),
                signal_label=long_term_signal_label(score, company, market, confidence),
                time_horizon="3개월~1년 이상",
                reasons=long_term_reasons(stock, momentum, company, market, chart, news),
                cautions=long_term_cautions(stock, momentum, news_tuple, company, market, chart),
            )
        )
    return tuple(sorted(results, key=lambda item: item.score, reverse=True))


def score_legend_strategy_candidates(
    stock_scores: Iterable[StockScore],
    momentums: dict[str, Momentum],
) -> tuple[LegendStrategyScore, ...]:
    results: list[LegendStrategyScore] = []
    for item in stock_scores:
        ticker = item.stock.ticker.upper()
        momentum = momentums.get(ticker, Momentum())
        lynch = lynch_strategy_score(item)
        oneil = oneil_strategy_score(item, momentum)
        greenblatt = greenblatt_strategy_score(item)
        fisher = fisher_strategy_score(item)
        composite = (
            lynch * LEGEND_DEFAULT_WEIGHTS["lynch"]
            + oneil * LEGEND_DEFAULT_WEIGHTS["oneil"]
            + greenblatt * LEGEND_DEFAULT_WEIGHTS["greenblatt"]
            + fisher * LEGEND_DEFAULT_WEIGHTS["fisher"]
        )
        results.append(
            LegendStrategyScore(
                stock_score=item,
                lynch_score=round(lynch, 1),
                oneil_score=round(oneil, 1),
                greenblatt_score=round(greenblatt, 1),
                fisher_score=round(fisher, 1),
                composite_score=round(composite, 1),
                reasons=legend_strategy_reasons(item, momentum, lynch, oneil, greenblatt, fisher),
                warnings=legend_strategy_warnings(item, momentum),
            )
        )
    return tuple(sorted(results, key=lambda item: item.composite_score, reverse=True))


def lynch_strategy_score(item: StockScore) -> float:
    fundamentals = item.stock.fundamentals
    growth = _scale(fundamentals.revenue_growth_pct, low=0, high=35)
    peg = 50
    leverage = _inverse_scale(fundamentals.debt_to_equity_pct, low=40, high=200)
    size = company_size_score(fundamentals.market_cap, fundamentals.market_cap_currency)
    understandable = 68 if item.stock.thesis else 50
    score = peg * 0.36 + growth * 0.24 + leverage * 0.18 + size * 0.14 + understandable * 0.08
    if fundamentals.revenue_growth_pct is not None and fundamentals.revenue_growth_pct < 0:
        score -= 8
    if fundamentals.debt_to_equity_pct is not None and fundamentals.debt_to_equity_pct > 200:
        score -= 6
    return _clamp(score, 0, 100)


def oneil_strategy_score(item: StockScore, momentum: Momentum) -> float:
    fundamentals = item.stock.fundamentals
    growth = _scale(fundamentals.revenue_growth_pct, low=0, high=35)
    relative_strength = momentum_to_score(momentum) or 50
    catalyst = 72 if item.stock.recent_issues else 45
    supply = company_size_score(fundamentals.market_cap, fundamentals.market_cap_currency)
    leadership = 68 if item.stock.role == "core" else 56
    market = item.industry_score
    score = (
        growth * 0.28
        + relative_strength * 0.26
        + catalyst * 0.18
        + supply * 0.10
        + leadership * 0.10
        + market * 0.08
    )
    if _finite(momentum.one_month_pct) and momentum.one_month_pct < -12:
        score -= 8
    if _finite(momentum.three_month_pct) and momentum.three_month_pct < -20:
        score -= 8
    return _clamp(score, 0, 100)


def greenblatt_strategy_score(item: StockScore) -> float:
    fundamentals = item.stock.fundamentals
    if _finite(fundamentals.roic_pct):
        capital_return = _scale(fundamentals.roic_pct, low=0, high=30)
    else:
        capital_return = _scale(fundamentals.roe_pct, low=0, high=35)
        if fundamentals.operating_margin_pct is not None and math.isfinite(fundamentals.operating_margin_pct):
            capital_return = capital_return * 0.72 + _scale(fundamentals.operating_margin_pct, low=0, high=35) * 0.28
    if _finite(fundamentals.earnings_yield_pct):
        earnings_yield = _scale(fundamentals.earnings_yield_pct, low=1, high=12)
    else:
        earnings_yield = _earnings_yield_proxy_score(fundamentals)
    score = capital_return * 0.55 + earnings_yield * 0.45
    if fundamentals.operating_income is not None and fundamentals.operating_income < 0:
        score -= 12
    if fundamentals.net_income is not None and fundamentals.net_income < 0:
        score -= 8
    return _clamp(score, 0, 100)


def fisher_strategy_score(item: StockScore) -> float:
    fundamentals = item.stock.fundamentals
    durable_growth = _scale(fundamentals.revenue_growth_pct, low=-5, high=30)
    margin = _scale(fundamentals.operating_margin_pct, low=0, high=35)
    rd_score = _scale(fundamentals.rd_to_revenue_pct, low=0, high=18) if _finite(fundamentals.rd_to_revenue_pct) else 50
    management = (
        _scale(fundamentals.roe_pct, low=0, high=35) * 0.70
        + _inverse_scale(fundamentals.debt_to_equity_pct, low=40, high=200) * 0.30
    )
    score = durable_growth * 0.35 + margin * 0.25 + rd_score * 0.15 + management * 0.25
    if fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0:
        score -= 5
    if fundamentals.operating_margin_pct is not None and fundamentals.operating_margin_pct < 0:
        score -= 8
    return _clamp(score, 0, 100)


def legend_strategy_reasons(
    item: StockScore,
    momentum: Momentum,
    lynch: float,
    oneil: float,
    greenblatt: float,
    fisher: float,
) -> tuple[str, ...]:
    fundamentals = item.stock.fundamentals
    greenblatt_basis = _greenblatt_basis_text(fundamentals)
    fisher_basis = _fisher_basis_text(fundamentals)
    return (
        f"린치 {lynch:.1f}/100: EPS 성장률 미연결로 PEG는 제외, {_growth_check(fundamentals)}",
        f"오닐 {oneil:.1f}/100: 상대강도 {(_momentum_label(momentum))}, 최근 이슈 {'있음' if item.stock.recent_issues else '미확인'}",
        f"그린블라트 {greenblatt:.1f}/100: {greenblatt_basis}",
        f"피셔 {fisher:.1f}/100: {fisher_basis}",
    )


def legend_strategy_warnings(item: StockScore, momentum: Momentum) -> tuple[str, ...]:
    fundamentals = item.stock.fundamentals
    warnings: list[str] = []
    official_coverage = official_fundamental_coverage_pct(fundamentals)
    if official_coverage < 30:
        warnings.append(f"공식 재무 데이터 커버리지 {official_coverage:.0f}%로 정량 판단 신뢰도 제한")
    warnings.append("EPS 성장률 데이터가 없어 PEG 항목은 점수에 포함하지 않음")
    if not _has_momentum_data(momentum):
        warnings.append("가격 상대강도 데이터가 부족해 오닐 모멘텀 항목은 중립 처리")
    warnings.append("기관 신규 매수 데이터는 아직 연결되지 않아 점수에 포함하지 않음")
    if fundamentals.rd_to_revenue_pct is None:
        warnings.append("R&D 투자 데이터가 없어 피셔 연구개발 항목은 중립 처리")
    warnings.append("경영진 정성 평가는 아직 연결되지 않아 점수에 포함하지 않음")
    if fundamentals.roic_pct is None:
        warnings.append("ROIC 원천 데이터가 부족해 실제 ROE/마진 지표만 반영")
    if fundamentals.earnings_yield_pct is None or fundamentals.ev_to_ebit is None:
        warnings.append("EV/EBIT 원천 데이터가 부족해 실제 이익/시가총액 지표만 반영")
    roic_source = fundamentals.sources.get("roic") if isinstance(fundamentals.sources, dict) else None
    if isinstance(roic_source, dict) and roic_source.get("taxRateDefault"):
        default_rate = roic_source.get("defaultTaxRate")
        default_pct = f"{float(default_rate) * 100:.0f}%" if isinstance(default_rate, (int, float)) else "기본"
        warnings.append(f"법인세 데이터가 부족해 {default_pct} 법인세율로 ROIC를 계산")
    return tuple(dict.fromkeys(warnings))


def _greenblatt_basis_text(fundamentals: Fundamentals) -> str:
    parts: list[str] = []
    if _finite(fundamentals.roic_pct):
        parts.append(f"실제 ROIC {fundamentals.roic_pct:.1f}%")
    else:
        parts.append("ROIC 미연결, 실제 ROE/마진")
    if _finite(fundamentals.earnings_yield_pct):
        if _finite(fundamentals.ev_to_ebit):
            parts.append(
                f"EBIT/EV {fundamentals.earnings_yield_pct:.1f}%"
                f"(EV/EBIT {fundamentals.ev_to_ebit:.1f}배)"
            )
        else:
            parts.append(f"이익수익률 {fundamentals.earnings_yield_pct:.1f}%")
    else:
        parts.append("EV/EBIT 미연결, 실제 이익/시가총액")
    return "와 ".join(parts) + "를 결합"


def _fisher_basis_text(fundamentals: Fundamentals) -> str:
    if _finite(fundamentals.rd_to_revenue_pct):
        rd_text = f"R&D/매출 {fundamentals.rd_to_revenue_pct:.1f}%"
    else:
        rd_text = "R&D 중립값"
    return f"장기 성장성, 영업이익률, {rd_text}, ROE/부채비율 기반 재무 건전성을 반영"


def early_revenue_growth_score(growth_pct: float | None) -> float:
    if growth_pct is None or not math.isfinite(growth_pct):
        return 45
    return _clamp(_scale(growth_pct, low=0, high=45), 0, 100)


def company_size_score(market_cap: float | None, currency: str) -> float:
    if market_cap is None or not math.isfinite(market_cap) or market_cap <= 0:
        return 35
    if currency.upper() == "KRW":
        if market_cap < 100_000_000_000:
            return 45
        if market_cap <= 1_000_000_000_000:
            return 100
        if market_cap <= 5_000_000_000_000:
            return 92
        if market_cap <= 15_000_000_000_000:
            return 74
        if market_cap <= 50_000_000_000_000:
            return 55
        if market_cap <= 150_000_000_000_000:
            return 18
        return 8
    if market_cap < 250_000_000:
        return 45
    if market_cap <= 2_000_000_000:
        return 100
    if market_cap <= 10_000_000_000:
        return 92
    if market_cap <= 25_000_000_000:
        return 74
    if market_cap <= 50_000_000_000:
        return 55
    if market_cap <= 100_000_000_000:
        return 18
    return 4


def pullback_entry_score(momentum: Momentum) -> float:
    position = momentum.range_position_pct
    drawdown = momentum.drawdown_from_high_pct
    one_month = momentum.one_month_pct
    three_month = momentum.three_month_pct
    six_month = momentum.six_month_pct
    if all(value is None or not math.isfinite(value) for value in (position, drawdown, one_month, three_month, six_month)):
        return 50

    score = 50.0
    if position is not None and math.isfinite(position):
        if 15 <= position <= 55:
            score += 30
        elif 0 <= position < 15:
            score += 18
        elif 55 < position <= 75:
            score += 5
        else:
            score -= 18

    if drawdown is not None and math.isfinite(drawdown):
        decline = abs(min(drawdown, 0))
        if 10 <= decline <= 35:
            score += 18
        elif 5 <= decline < 10:
            score += 8
        elif 35 < decline <= 55:
            score += 4
        elif decline > 55:
            score -= 18

    if one_month is not None and math.isfinite(one_month):
        if -8 <= one_month <= 18:
            score += 8
        if 0 <= one_month <= 15:
            score += 6
        if one_month < -18:
            score -= 18

    if three_month is not None and math.isfinite(three_month):
        if -25 <= three_month <= 12:
            score += 5
        if three_month < -35:
            score -= 12

    if (
        six_month is not None
        and math.isfinite(six_month)
        and six_month > 70
        and position is not None
        and position > 70
    ):
        score -= 22

    return _clamp(score, 0, 100)


def short_term_news_score(
    stock: StockProfile,
    industry: IndustryProfile,
    news_counter: Counter[str],
    has_live_news: bool,
) -> float:
    if not has_live_news:
        return 50 if stock.recent_issues else 45

    direct_terms = (stock.ticker, stock.name)
    industry_terms = (*industry.news_terms, industry.name)
    issue_terms = stock.recent_issues or (stock.thesis,)
    direct_score = _term_score(news_counter, direct_terms, baseline=35, scale=18)
    industry_score = _term_score(news_counter, industry_terms, baseline=35, scale=9)
    issue_score = _term_score(news_counter, issue_terms, baseline=35, scale=4)
    return _clamp(direct_score * 0.45 + industry_score * 0.35 + issue_score * 0.20, 0, 100)


def short_term_market_score(momentum: Momentum, industry_market_score: float) -> float:
    if not _has_momentum_data(momentum):
        return 50

    score = 50.0
    one_month = momentum.one_month_pct
    three_month = momentum.three_month_pct
    six_month = momentum.six_month_pct
    position = momentum.range_position_pct

    if _finite(one_month):
        if 2 <= one_month <= 18:
            score += 25
        elif 0 <= one_month < 2:
            score += 10
        elif 18 < one_month <= 35:
            score += 15
        elif one_month > 35:
            score += 4
        elif -8 <= one_month < 0:
            score -= 5
        else:
            score -= 24

    if _finite(three_month):
        if 5 <= three_month <= 35:
            score += 14
        elif 0 <= three_month < 5:
            score += 5
        elif three_month > 55:
            score -= 5
        elif three_month < -15:
            score -= 14

    if _finite(six_month):
        if 0 <= six_month <= 70:
            score += 6
        elif six_month > 100 and _finite(position) and position > 85:
            score -= 10
        elif six_month < -25:
            score -= 8

    if _finite(industry_market_score):
        score += (industry_market_score - 50) * 0.18

    return _clamp(score, 0, 100)


def short_term_chart_score(momentum: Momentum) -> float:
    if not _has_momentum_data(momentum):
        return 50

    score = 50.0
    position = momentum.range_position_pct
    drawdown = momentum.drawdown_from_high_pct
    one_month = momentum.one_month_pct
    rsi14 = momentum.rsi14

    if _finite(position):
        if 45 <= position <= 85:
            score += 24
        elif 25 <= position < 45:
            score += 12
        elif 85 < position <= 95:
            score += 7
        elif position > 95:
            score -= 8
        elif position < 15:
            score -= 15

    if _finite(drawdown):
        decline = abs(min(drawdown, 0))
        if 3 <= decline <= 18:
            score += 18
        elif 0 <= decline < 3:
            score += 10
        elif 18 < decline <= 35:
            score += 5
        elif decline > 35:
            score -= 15

    if _finite(one_month):
        if one_month > 0:
            score += 8
        elif one_month < -10:
            score -= 12

    if _short_term_ma_stack(momentum):
        score += 16
    elif _finite(momentum.ma20_distance_pct) and momentum.ma20_distance_pct > 0:
        score += 6
    elif _finite(momentum.ma20_distance_pct) and momentum.ma20_distance_pct < -8:
        score -= 8

    if _finite(momentum.ma20_slope_pct):
        if momentum.ma20_slope_pct > 1.5:
            score += 10
        elif momentum.ma20_slope_pct > 0:
            score += 5
        elif momentum.ma20_slope_pct < -2:
            score -= 9

    if _finite(rsi14):
        if 45 <= rsi14 <= 70:
            score += 10
        elif 70 < rsi14 <= 78:
            score += 2
        elif rsi14 > 78:
            score -= 12
        elif rsi14 < 35:
            score -= 8

    if _finite(momentum.twenty_day_breakout_pct):
        if 0 <= momentum.twenty_day_breakout_pct <= 15:
            score += 10
        elif momentum.twenty_day_breakout_pct > 28:
            score -= 8

    return _clamp(score, 0, 100)


def short_term_volume_score(momentum: Momentum) -> float:
    ratio = momentum.volume_ratio
    if ratio is None and momentum.latest_volume is not None and momentum.avg_volume_20 not in (None, 0):
        ratio = momentum.latest_volume / momentum.avg_volume_20
    if ratio is None or not math.isfinite(ratio):
        return 50
    score = 50.0
    if 1.3 <= ratio <= 2.8:
        score += 30
    elif 1.0 <= ratio < 1.3:
        score += 12
    elif 2.8 < ratio <= 4.5:
        score += 16
    elif ratio > 4.5:
        score += 4
    elif ratio < 0.65:
        score -= 14

    if _finite(momentum.twenty_day_breakout_pct):
        if 0 <= momentum.twenty_day_breakout_pct <= 15:
            score += 12
        elif momentum.twenty_day_breakout_pct > 28:
            score -= 8
        elif momentum.twenty_day_breakout_pct < -10:
            score -= 8
    if _finite(momentum.sixty_day_breakout_pct):
        if 0 <= momentum.sixty_day_breakout_pct <= 24:
            score += 8
        elif momentum.sixty_day_breakout_pct > 40:
            score -= 6
        elif momentum.sixty_day_breakout_pct < -16:
            score -= 6
    return _clamp(score, 0, 100)


def short_term_company_score(stock: StockProfile, quality: float) -> float:
    fundamentals = stock.fundamentals
    growth = _scale(fundamentals.revenue_growth_pct, low=-5, high=35)
    margin = _scale(fundamentals.operating_margin_pct, low=-10, high=25)
    leverage = _inverse_scale(fundamentals.debt_to_equity_pct, low=40, high=240)
    catalyst = 62 if stock.recent_issues else 45
    score = quality * 0.40 + growth * 0.25 + margin * 0.18 + leverage * 0.10 + catalyst * 0.07
    if fundamentals.operating_margin_pct is not None and fundamentals.operating_margin_pct < 0:
        score -= 7
    if fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0:
        score -= 4
    return _clamp(score, 0, 100)


def short_term_penalty(
    stock: StockProfile,
    momentum: Momentum,
    news_items: tuple[NewsItem, ...],
    market: float,
    chart: float,
    volume: float,
    company: float,
) -> float:
    penalty = 0.0
    if not news_items:
        penalty += 3
    if not _has_momentum_data(momentum):
        penalty += 6
    if market < 35:
        penalty += 8
    if chart < 35:
        penalty += 8
    if volume < 38:
        penalty += 5
    if company < 35:
        penalty += 5
    if _finite(momentum.rsi14) and momentum.rsi14 > 78:
        penalty += 6
    if _finite(momentum.range_position_pct) and momentum.range_position_pct > 96:
        penalty += 5
    if stock.fundamentals.operating_margin_pct is not None and stock.fundamentals.operating_margin_pct < -15:
        penalty += 4
    return penalty


def short_term_signal_label(score: float, market: float, chart: float, volume: float, news: float, confidence: float) -> str:
    if confidence < 50 and score >= 58:
        return "데이터 확인"
    if score >= 78 and chart >= 66 and volume >= 62 and market >= 58:
        return "단기 강세 후보"
    if score >= 68 and chart >= 60 and (market >= 56 or news >= 65):
        return "단기 관심"
    if score >= 58:
        return "관찰"
    return "후순위"


def short_term_setup_label(momentum: Momentum, chart: float, volume: float) -> str:
    if not _has_momentum_data(momentum):
        return "차트 데이터 부족"
    if (
        _finite(momentum.twenty_day_breakout_pct)
        and momentum.twenty_day_breakout_pct >= 0
        and volume >= 62
    ):
        return "거래 동반 돌파"
    if _short_term_ma_stack(momentum) and chart >= 62:
        return "추세 지속"
    if pullback_entry_score(momentum) >= 68 and chart >= 55:
        return "눌림목 관찰"
    if _finite(momentum.rsi14) and momentum.rsi14 > 74:
        return "과열 주의"
    if chart >= 58:
        return "관찰"
    return "후순위"


def candidate_confidence_score(
    stock: StockProfile,
    momentum: Momentum,
    has_news: bool,
    horizon: str,
) -> float:
    score = 22.0
    if _has_momentum_data(momentum):
        score += 30 if horizon == "short" else 24
    if _finite(momentum.latest_close):
        score += 8
    if horizon == "short":
        if _has_volume_data(momentum):
            score += 18
        if _finite(momentum.ma20) and _finite(momentum.ma60):
            score += 8
    elif _finite(momentum.ma60) or _finite(momentum.ma120):
        score += 10
    if has_news:
        score += 8 if horizon == "short" else 6
    score += _fundamental_coverage_score(stock.fundamentals) * (0.12 if horizon == "short" else 0.24)
    if momentum.stale:
        score -= 10
    return _clamp(score, 0, 100)


def confidence_label(score: float) -> str:
    if score >= 78:
        return "높음"
    if score >= 62:
        return "보통"
    if score >= 45:
        return "확인 필요"
    return "낮음"


def _apply_short_term_caps(score: float, momentum: Momentum) -> float:
    if not _has_momentum_data(momentum):
        return min(score, 55)
    if not _has_volume_data(momentum):
        return min(score, 72)
    return score


def _has_volume_data(momentum: Momentum) -> bool:
    return _finite(momentum.latest_volume) and _finite(momentum.avg_volume_20) and _finite(momentum.volume_ratio)


def _short_term_ma_stack(momentum: Momentum) -> bool:
    return (
        _finite(momentum.latest_close)
        and _finite(momentum.ma20)
        and _finite(momentum.ma60)
        and momentum.latest_close > momentum.ma20 > momentum.ma60
    )


def _fundamental_coverage_score(fundamentals: Fundamentals) -> float:
    fields = (
        fundamentals.revenue_growth_pct,
        fundamentals.operating_margin_pct,
        fundamentals.roe_pct,
        fundamentals.debt_to_equity_pct,
        fundamentals.pe if fundamentals.forward_pe is None else fundamentals.forward_pe,
        fundamentals.market_cap,
        fundamentals.free_cash_flow,
        fundamentals.current_ratio_pct,
    )
    covered = sum(1 for value in fields if _finite(value))
    return covered / len(fields) * 100


def official_fundamental_coverage_pct(fundamentals: Fundamentals) -> float:
    covered = 0
    for attr in OFFICIAL_COVERAGE_FIELDS:
        value = getattr(fundamentals, attr)
        source_key = FUNDAMENTAL_SOURCE_BY_ATTR.get(attr)
        source = fundamentals.sources.get(source_key) if source_key else None
        if _finite(value) and _official_source(source):
            covered += 1
    return covered / len(OFFICIAL_COVERAGE_FIELDS) * 100


def data_coverage_gate_for_stock(fundamentals: Fundamentals) -> tuple[float, tuple[str, ...]]:
    coverage = official_fundamental_coverage_pct(fundamentals)
    if coverage == 0:
        return 58.0, ("공식 재무 데이터가 없어 가격/시총 중심 후보로만 봐야 합니다.",)
    if coverage < 30:
        return 68.0, (f"공식 재무 데이터 커버리지 {coverage:.0f}%로 점수 상한을 적용했습니다.",)
    return 100.0, ()


def _official_source(source: object) -> bool:
    return isinstance(source, dict) and source.get("source") in OFFICIAL_FUNDAMENTAL_SOURCES


def short_term_reasons(
    stock: StockProfile,
    momentum: Momentum,
    news: float,
    market: float,
    chart: float,
    volume: float,
    company: float,
) -> tuple[str, ...]:
    return (
        f"차트 점수 {chart:.1f}/100: {_short_term_chart_reason(momentum)}",
        f"거래량 점수 {volume:.1f}/100: {_short_term_volume_reason(momentum)}",
        f"시장 데이터 점수 {market:.1f}/100: {_short_term_momentum_reason(momentum)}",
        f"뉴스/이슈 점수 {news:.1f}/100: {_short_term_news_reason(stock)}",
        f"기업 데이터 점수 {company:.1f}/100: {_growth_check(stock.fundamentals)}",
    )


def short_term_cautions(
    stock: StockProfile,
    momentum: Momentum,
    news_items: tuple[NewsItem, ...],
    market: float,
    chart: float,
    volume: float,
    company: float,
) -> tuple[str, ...]:
    cautions: list[str] = []
    if not news_items:
        cautions.append("라이브 뉴스가 없으면 단기 이슈 점수는 보수적으로 해석")
    if not _has_momentum_data(momentum):
        cautions.append("실시간 가격 모멘텀 데이터 부족으로 시장/차트 신호 확인 필요")
    if market < 45:
        cautions.append("단기 가격 모멘텀이 약해 추세 반전 확인 전 진입 주의")
    if chart < 45:
        cautions.append("차트 위치가 약하거나 과열되어 지지선/저항선 확인 필요")
    if not _has_volume_data(momentum):
        cautions.append("거래량 데이터가 부족해 돌파 신호는 실제 거래량으로 재확인 필요")
    elif volume < 45:
        cautions.append("거래량 확인이 약해 돌파 지속성 확인 필요")
    if company < 45:
        cautions.append("단기 악재에 취약할 수 있어 실적과 재무 안정성 재확인 필요")
    cautions.extend(stock.risks[:1])
    return tuple(dict.fromkeys(cautions))


def trade_timing_signal_for_stock(
    item: StockScore,
    momentum: Momentum,
    horizon: str,
    score: float,
    confidence: float,
    chart: float,
    market: float,
    volume: float | None = None,
) -> TradeTimingSignal:
    technical_ready = _trade_timing_data_ready(momentum)
    signal_confidence = _trade_timing_confidence(momentum, confidence, horizon)
    cautions: list[str] = []
    if momentum.stale:
        cautions.append("가격 데이터가 만료 캐시라 최신 차트로 재확인 필요")
    if item.risk_gate != "Pass":
        cautions.extend(item.risk_gate_reasons[:2])
    if item.decision_grade == "제외":
        cautions.append("종합 등급이 제외라 차트가 좋아도 강한 매수 신호를 차단")

    if item.risk_gate == "Hard Fail":
        return _trade_signal(
            horizon,
            "sell",
            12,
            min(signal_confidence, 40),
            "리스크 게이트 매도",
            ("리스크 게이트 Hard Fail로 차트 신호보다 위험 관리가 우선",),
            tuple(dict.fromkeys(cautions or item.risk_gate_reasons)),
            momentum,
            invalidation_rule="Hard Fail 해소와 MA200 회복 전 재진입 보류",
        )

    if not technical_ready:
        return _trade_signal(
            horizon,
            "hold",
            min(score, 50),
            min(signal_confidence, 44),
            "매물대/MA200 데이터 부족",
            ("1년 거래량 매물대 또는 MA200 계산에 필요한 가격 데이터가 부족",),
            tuple(dict.fromkeys((*cautions, "OHLCV와 200일 이상 가격 데이터 확보 후 신호 재평가"))),
            momentum,
            invalidation_rule="거래량 매물대와 MA200이 모두 계산된 뒤 판단",
        )

    latest = momentum.latest_close or 0
    ma200 = momentum.ma200 or 0
    high = _latest_high(momentum)
    low = _latest_low(momentum)
    below_ma200 = latest < ma200
    in_zone = bool(momentum.volume_zone_contains_latest)
    excluded = item.decision_grade == "제외"

    action = "hold"
    setup = _hold_setup(momentum)
    raw_score = 50.0
    invalidation = "핵심 매물대 재진입 또는 MA200 위치 재확인"
    target_price: float | None = None
    target_type: str | None = None
    partial_take_profit_pct: float | None = None
    remaining_exit_rule = ""

    if _target_reached(high, latest, momentum.previous_swing_high):
        action = "take_profit_half"
        setup = "전 고점 50% 익절"
        raw_score = 78
        target_price = momentum.previous_swing_high
        target_type = "previous_swing_high"
        partial_take_profit_pct = 50
        remaining_exit_rule = "잔여 50%는 MA200 종가 이탈 시 정리"
        invalidation = "익절 후 잔여 물량은 MA200 종가 이탈 전까지 추세 추적"
    elif _ma200_take_profit_reached(momentum):
        action = "take_profit_half"
        setup = "MA200 1차 익절"
        raw_score = 74
        target_price = ma200
        target_type = "ma200"
        partial_take_profit_pct = 50
        remaining_exit_rule = "잔여 50%는 MA200 종가 이탈 시 정리"
        invalidation = "MA200 위 안착 실패 시 잔여 물량 축소"
    elif _ma200_trailing_break(momentum):
        action = "sell"
        setup = "MA200 트레일링 이탈"
        raw_score = 20
        invalidation = "MA200 종가 회복 전 잔여 물량 재확대 보류"
    elif _volume_zone_break(momentum):
        action = "reduce"
        setup = "핵심 매물대 이탈"
        raw_score = 34
        invalidation = "핵심 매물대 하단 회복 전 비중 확대 보류"
    elif in_zone and not excluded:
        if below_ma200:
            target_price = ma200
            target_type = "ma200"
            upside = _target_upside_pct(latest, ma200)
            if _finite(upside) and upside >= 4 and signal_confidence >= 55:
                action = "buy"
                setup = "매물대 진입 / MA200 1차 목표"
                raw_score = 82
                invalidation = "핵심 매물대 하단 이탈 시 매수 신호 무효"
            elif _finite(upside) and upside > 0:
                action = "scale_buy"
                setup = "매물대 진입 / MA200 여력 제한"
                raw_score = 64
                invalidation = "MA200 목표 여력 축소 또는 매물대 하단 이탈 시 중단"
            else:
                setup = "매물대 진입 / 목표 여력 부족"
                raw_score = 52
                target_price = None
                target_type = None
                invalidation = "MA200까지 여력이 생길 때 재평가"
        else:
            target_price = _next_target_above(latest, momentum.previous_swing_high)
            target_type = "previous_swing_high" if target_price else None
            if _ma200_buy_support(momentum) and signal_confidence >= 55:
                action = "buy"
                setup = "MA200 위 매물대 매수"
                raw_score = 80
                invalidation = "MA200 종가 이탈 또는 핵심 매물대 하단 이탈 시 신호 무효"
            else:
                action = "scale_buy"
                setup = "MA200 위 매물대 분할매수"
                raw_score = 66
                invalidation = "MA200 지지 확인 실패 시 분할매수 중단"

    if action in {"buy", "scale_buy"} and signal_confidence < 50:
        action = "hold"
        setup = "데이터 확인"
        raw_score = min(raw_score, 56)
        cautions.append("차트/매물대 신뢰도가 낮아 매수 신호를 관망으로 제한")
        target_price = None
        target_type = None

    reasons = _trade_timing_reasons(momentum, chart, market, volume, action, target_price, target_type)
    return _trade_signal(
        horizon,
        action,
        raw_score * 0.72 + score * 0.16 + signal_confidence * 0.12,
        signal_confidence,
        setup,
        reasons,
        tuple(dict.fromkeys(cautions)),
        momentum,
        invalidation_rule=invalidation,
        target_price=target_price,
        target_type=target_type,
        partial_take_profit_pct=partial_take_profit_pct,
        remaining_exit_rule=remaining_exit_rule,
    )


def _trade_signal(
    horizon: str,
    action: str,
    score: float,
    confidence: float,
    setup: str,
    reasons: tuple[str, ...],
    cautions: tuple[str, ...],
    momentum: Momentum,
    invalidation_rule: str,
    target_price: float | None = None,
    target_type: str | None = None,
    partial_take_profit_pct: float | None = None,
    remaining_exit_rule: str = "",
) -> TradeTimingSignal:
    return TradeTimingSignal(
        horizon=horizon,
        action=action,
        label=_trade_action_label(action),
        score=round(_clamp(score, 0, 100), 1),
        confidence=round(_clamp(confidence, 0, 100), 1),
        setup=setup,
        reasons=tuple(dict.fromkeys(reasons)),
        cautions=tuple(dict.fromkeys(cautions)),
        reference_price=momentum.latest_close,
        ma150=momentum.ma150,
        ma200=momentum.ma200,
        bollinger_upper=momentum.bollinger_upper,
        bollinger_middle=momentum.bollinger_middle,
        bollinger_lower=momentum.bollinger_lower,
        volume_zone_lower=momentum.volume_zone_lower,
        volume_zone_upper=momentum.volume_zone_upper,
        volume_zone_strength=momentum.volume_zone_strength,
        target_price=target_price,
        target_type=target_type,
        partial_take_profit_pct=partial_take_profit_pct,
        remaining_exit_rule=remaining_exit_rule,
        invalidation_rule=invalidation_rule,
    )


def _trade_action_label(action: str) -> str:
    return {
        "buy": "매수",
        "scale_buy": "분할매수",
        "hold": "관망",
        "reduce": "비중축소",
        "sell": "매도",
        "take_profit_half": "50% 익절",
    }.get(action, "관망")


def _trade_timing_data_ready(momentum: Momentum) -> bool:
    return all(
        _finite(value)
        for value in (
            momentum.latest_close,
            momentum.ma200,
            momentum.latest_high,
            momentum.latest_low,
            momentum.volume_zone_lower,
            momentum.volume_zone_upper,
            momentum.volume_zone_strength,
        )
    )


def _trade_timing_confidence(momentum: Momentum, base_confidence: float, horizon: str) -> float:
    score = base_confidence
    if _finite(momentum.ma200):
        score += 8
    else:
        score -= 22
    if _finite(momentum.volume_zone_lower) and _finite(momentum.volume_zone_upper):
        score += 12
    else:
        score -= 18
    if momentum.volume_zone_contains_latest:
        score += 6
    if _finite(momentum.ohlcv_coverage_pct) and momentum.ohlcv_coverage_pct < 60:
        score -= 8
    if horizon == "short" and not _has_volume_data(momentum):
        score -= 8
    if momentum.stale:
        score -= 10
    return _clamp(score, 0, 100)


def _hold_setup(momentum: Momentum) -> str:
    if not momentum.volume_zone_contains_latest:
        return "매물대 밖 관망"
    if _finite(momentum.latest_close) and _finite(momentum.ma200):
        if momentum.latest_close < momentum.ma200:
            return "MA200 목표 여력 확인"
        return "MA200 지지 확인"
    return "확인 대기"


def _trade_timing_reasons(
    momentum: Momentum,
    chart: float,
    market: float,
    volume: float | None,
    action: str,
    target_price: float | None,
    target_type: str | None,
) -> tuple[str, ...]:
    parts = [
        _volume_zone_reason(momentum),
        _ma200_regime_reason(momentum),
        _target_reason(target_price, target_type),
        f"차트 점수 {chart:.1f}/100, 시장 모멘텀 {market:.1f}/100",
    ]
    if volume is not None:
        parts.append(f"거래량 확인 점수 {volume:.1f}/100")
    if _finite(momentum.rsi14):
        parts.append(f"RSI {momentum.rsi14:.1f}")
    if action == "take_profit_half":
        parts.append("목표 도달 시 50% 익절, 잔여 물량은 MA200 기준으로 추적")
    elif action in {"reduce", "sell"}:
        parts.append("핵심 매물대 또는 MA200 이탈을 우선 반영")
    parts.append(_bollinger_reference(momentum))
    return tuple(part for part in parts if part)


def _volume_zone_reason(momentum: Momentum) -> str:
    if not (_finite(momentum.volume_zone_lower) and _finite(momentum.volume_zone_upper)):
        return "거래량 매물대 데이터 부족"
    status = "진입" if momentum.volume_zone_contains_latest else "밖"
    strength = _pct_text(momentum.volume_zone_strength)
    return (
        f"핵심 매물대 {momentum.volume_zone_lower:.2f}~{momentum.volume_zone_upper:.2f}, "
        f"현재 캔들 {status}, 강도 {strength}"
    )


def _ma200_regime_reason(momentum: Momentum) -> str:
    if not (_finite(momentum.latest_close) and _finite(momentum.ma200)):
        return "MA200 데이터 부족"
    regime = "MA200 아래: MA200을 1차 익절 목표로 사용" if momentum.latest_close < momentum.ma200 else "MA200 위: MA200을 매수 지지선으로 사용"
    return f"현재가 {momentum.latest_close:.2f}, MA200 {momentum.ma200:.2f}({_pct_text(momentum.ma200_distance_pct)}), {regime}"


def _target_reason(target_price: float | None, target_type: str | None) -> str:
    if not _finite(target_price):
        return "목표가 미확정"
    return f"목표가 {target_price:.2f} ({_target_type_label(target_type)})"


def _bollinger_reference(momentum: Momentum) -> str:
    if not _finite(momentum.bollinger_percent_b):
        return ""
    return f"볼린저는 참고값: %B {momentum.bollinger_percent_b:.1f}"


def _target_type_label(target_type: str | None) -> str:
    return {
        "ma200": "MA200",
        "previous_swing_high": "전 고점",
    }.get(target_type or "", "참고 목표")


def _latest_high(momentum: Momentum) -> float:
    return momentum.latest_high if _finite(momentum.latest_high) else (momentum.latest_close or 0)


def _latest_low(momentum: Momentum) -> float:
    return momentum.latest_low if _finite(momentum.latest_low) else (momentum.latest_close or 0)


def _target_reached(high: float, latest: float, target: float | None) -> bool:
    return _finite(target) and (high >= target * 0.995 or latest >= target * 0.995)


def _ma200_take_profit_reached(momentum: Momentum) -> bool:
    if not (_finite(momentum.previous_close) and _finite(momentum.ma200)):
        return False
    return momentum.previous_close < momentum.ma200 <= _latest_high(momentum)


def _ma200_trailing_break(momentum: Momentum) -> bool:
    if not (_finite(momentum.previous_close) and _finite(momentum.latest_close) and _finite(momentum.ma200)):
        return False
    return momentum.previous_close >= momentum.ma200 and momentum.latest_close < momentum.ma200


def _volume_zone_break(momentum: Momentum) -> bool:
    if not (_finite(momentum.latest_close) and _finite(momentum.volume_zone_lower)):
        return False
    return momentum.latest_close < momentum.volume_zone_lower * 0.985


def _target_upside_pct(latest: float, target: float | None) -> float | None:
    if latest <= 0 or not _finite(target):
        return None
    return ((target / latest) - 1) * 100


def _ma200_buy_support(momentum: Momentum) -> bool:
    if not (_finite(momentum.latest_close) and _finite(momentum.ma200)):
        return False
    zone_above_ma200 = _finite(momentum.volume_zone_lower) and momentum.volume_zone_lower >= momentum.ma200 * 0.98
    candle_tests_ma200 = _latest_low(momentum) <= momentum.ma200 * 1.03 <= momentum.latest_close * 1.03
    return momentum.latest_close >= momentum.ma200 and (zone_above_ma200 or candle_tests_ma200)


def _next_target_above(latest: float, target: float | None) -> float | None:
    if _finite(target) and target > latest * 1.01:
        return target
    return None


def _pct_text(value: float | None) -> str:
    return f"{value:.1f}%" if _finite(value) else "N/A"


def medium_term_company_score(
    stock: StockProfile, quality: float, valuation: float
) -> float:
    fundamentals = stock.fundamentals
    growth = _scale(fundamentals.revenue_growth_pct, low=-5, high=35)
    margin = _scale(fundamentals.operating_margin_pct, low=0, high=30)
    roe = _scale(fundamentals.roe_pct, low=0, high=30)
    stability = _inverse_scale(fundamentals.debt_to_equity_pct, low=40, high=220)
    cash_flow_margin = _ratio_pct(fundamentals.free_cash_flow, fundamentals.revenue)
    cash_flow = _scale(cash_flow_margin, low=-8, high=18)
    score = (
        quality * 0.32
        + growth * 0.22
        + margin * 0.16
        + roe * 0.12
        + valuation * 0.10
        + stability * 0.05
        + cash_flow * 0.03
    )
    if fundamentals.operating_margin_pct is not None and fundamentals.operating_margin_pct < 0:
        score -= 8
    if fundamentals.debt_to_equity_pct is not None and fundamentals.debt_to_equity_pct > 220:
        score -= 6
    if fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0:
        score -= 5
    return _clamp(score, 0, 100)


def medium_term_market_score(momentum: Momentum, industry_market_score: float) -> float:
    if not _has_momentum_data(momentum):
        return 50

    score = 50.0
    one_month = momentum.one_month_pct
    three_month = momentum.three_month_pct
    six_month = momentum.six_month_pct
    position = momentum.range_position_pct

    if _finite(three_month):
        if 8 <= three_month <= 45:
            score += 26
        elif 0 <= three_month < 8:
            score += 9
        elif 45 < three_month <= 75:
            score += 12
        elif three_month > 75:
            score -= 5
        elif -12 <= three_month < 0:
            score -= 5
        else:
            score -= 24

    if _finite(one_month):
        if 0 <= one_month <= 20:
            score += 12
        elif 20 < one_month <= 35:
            score += 5
        elif one_month > 35:
            score -= 6
        elif one_month < -10:
            score -= 10

    if _finite(six_month):
        if 5 <= six_month <= 80:
            score += 10
        elif six_month > 120 and _finite(position) and position > 90:
            score -= 10
        elif six_month < -25:
            score -= 10

    if _finite(industry_market_score):
        score += (industry_market_score - 50) * 0.22

    return _clamp(score, 0, 100)


def medium_term_chart_score(momentum: Momentum) -> float:
    if not _has_momentum_data(momentum):
        return 50

    score = 50.0
    position = momentum.range_position_pct
    drawdown = momentum.drawdown_from_high_pct
    one_month = momentum.one_month_pct
    three_month = momentum.three_month_pct

    if _finite(position):
        if 35 <= position <= 82:
            score += 22
        elif 82 < position <= 93:
            score += 8
        elif 20 <= position < 35:
            score += 7
        elif position > 93:
            score -= 7
        elif position < 20:
            score -= 12

    if _finite(drawdown):
        decline = abs(min(drawdown, 0))
        if 4 <= decline <= 20:
            score += 16
        elif 0 <= decline < 4:
            score += 8
        elif 20 < decline <= 35:
            score += 3
        elif decline > 35:
            score -= 14

    if _finite(one_month) and _finite(three_month):
        if one_month > 0 and three_month > 0:
            score += 10
        elif one_month < 0 and three_month < 0:
            score -= 10

    if _short_term_ma_stack(momentum):
        score += 10
    elif _finite(momentum.ma20_distance_pct) and momentum.ma20_distance_pct < -8:
        score -= 8
    if _finite(momentum.ma60_slope_pct):
        if momentum.ma60_slope_pct > 1:
            score += 6
        elif momentum.ma60_slope_pct < -1.5:
            score -= 7

    return _clamp(score, 0, 100)


def medium_term_news_score(
    stock: StockProfile,
    industry: IndustryProfile,
    news_counter: Counter[str],
    has_live_news: bool,
) -> float:
    if not has_live_news:
        return 50 if stock.recent_issues else 45

    direct_terms = (stock.ticker, stock.name)
    industry_terms = (*industry.news_terms, industry.name)
    issue_terms = stock.recent_issues or (stock.thesis,)
    direct_score = _term_score(news_counter, direct_terms, baseline=35, scale=12)
    industry_score = _term_score(news_counter, industry_terms, baseline=35, scale=10)
    issue_score = _term_score(news_counter, issue_terms, baseline=35, scale=5)
    return _clamp(direct_score * 0.30 + industry_score * 0.50 + issue_score * 0.20, 0, 100)


def medium_term_penalty(
    stock: StockProfile,
    momentum: Momentum,
    news_items: tuple[NewsItem, ...],
    company: float,
    market: float,
    chart: float,
) -> float:
    penalty = 0.0
    if not news_items:
        penalty += 2
    if not _has_momentum_data(momentum):
        penalty += 5
    if company < 42:
        penalty += 6
    if market < 38:
        penalty += 7
    if chart < 38:
        penalty += 6
    if stock.fundamentals.debt_to_equity_pct is not None and stock.fundamentals.debt_to_equity_pct > 220:
        penalty += 5
    if stock.fundamentals.operating_margin_pct is not None and stock.fundamentals.operating_margin_pct < -5:
        penalty += 5
    return penalty


def medium_term_signal_label(score: float, company: float, market: float, chart: float, confidence: float) -> str:
    if confidence < 52 and score >= 67:
        return "중기 데이터 확인"
    if score >= 76 and company >= 62 and market >= 62 and chart >= 58:
        return "중기 강세 후보"
    if score >= 67 and company >= 55 and (market >= 58 or chart >= 58):
        return "중기 관심"
    if score >= 58:
        return "추세 관찰"
    return "후순위"


def medium_term_reasons(
    stock: StockProfile,
    momentum: Momentum,
    company: float,
    market: float,
    chart: float,
    news: float,
) -> tuple[str, ...]:
    return (
        f"기업 데이터 점수 {company:.1f}/100: {_medium_term_company_reason(stock)}",
        f"시장 데이터 점수 {market:.1f}/100: {_short_term_momentum_reason(momentum)}",
        f"차트 점수 {chart:.1f}/100: {_medium_term_chart_reason(momentum)}",
        f"뉴스/산업 이슈 점수 {news:.1f}/100: {_medium_term_news_reason(stock)}",
    )


def medium_term_cautions(
    stock: StockProfile,
    momentum: Momentum,
    news_items: tuple[NewsItem, ...],
    company: float,
    market: float,
    chart: float,
) -> tuple[str, ...]:
    cautions: list[str] = []
    if not news_items:
        cautions.append("라이브 뉴스가 없으면 중기 산업 이슈 점수는 보수적으로 해석")
    if not _has_momentum_data(momentum):
        cautions.append("가격 데이터 부족으로 20일/60일 추세와 섹터 상대강도 재확인 필요")
    if company < 50:
        cautions.append("실적 방향이나 재무 버팀목이 약해 2~3개월 보유 리스크 확인 필요")
    if market < 50:
        cautions.append("3개월 가격 흐름이 약하면 추세 회복 확인 전 비중 확대 주의")
    if chart < 50:
        cautions.append("중기 차트 위치가 애매해 20일/60일선 지지 여부 확인 필요")
    cautions.append("컨센서스 추정치가 아직 없어 분기 실적 전망 변화는 별도 확인 필요")
    cautions.extend(stock.risks[:1])
    return tuple(dict.fromkeys(cautions))


def long_term_company_score(
    stock: StockProfile, quality: float, valuation: float
) -> float:
    fundamentals = stock.fundamentals
    growth = _scale(fundamentals.revenue_growth_pct, low=-5, high=30)
    margin = _scale(fundamentals.operating_margin_pct, low=0, high=35)
    roe = _scale(fundamentals.roe_pct, low=0, high=30)
    leverage = _inverse_scale(fundamentals.debt_to_equity_pct, low=30, high=200)
    fcf_margin = _ratio_pct(fundamentals.free_cash_flow, fundamentals.revenue)
    cash_flow = _scale(fcf_margin, low=-5, high=20)
    liquidity = _scale(fundamentals.current_ratio_pct, low=90, high=220)
    interest_safety = _scale(fundamentals.interest_coverage, low=2, high=10)
    score = (
        quality * 0.26
        + growth * 0.20
        + margin * 0.16
        + roe * 0.13
        + valuation * 0.12
        + leverage * 0.06
        + cash_flow * 0.04
        + liquidity * 0.02
        + interest_safety * 0.01
    )
    if fundamentals.operating_margin_pct is not None and fundamentals.operating_margin_pct < 0:
        score -= 10
    if fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0:
        score -= 8
    if fundamentals.debt_to_equity_pct is not None and fundamentals.debt_to_equity_pct > 220:
        score -= 8
    if fundamentals.interest_coverage is not None and fundamentals.interest_coverage < 3:
        score -= 5
    return _clamp(score, 0, 100)


def long_term_market_score(
    momentum: Momentum,
    industry_score: IndustryScore,
    macro_snapshot: MacroSnapshot | None,
) -> float:
    score = industry_score.score * 0.55 + industry_score.market_score * 0.25
    if macro_snapshot is not None:
        macro_average = (
            macro_snapshot.growth_score * 0.35
            + macro_snapshot.defensive_score * 0.20
            + macro_snapshot.infrastructure_score * 0.25
            + macro_snapshot.korea_fx_score * 0.20
        )
        score += macro_average * 0.20
    else:
        score += 50 * 0.20

    if _finite(momentum.six_month_pct):
        if 0 <= momentum.six_month_pct <= 80:
            score += 8
        elif momentum.six_month_pct > 120 and _finite(momentum.range_position_pct) and momentum.range_position_pct > 90:
            score -= 8
        elif momentum.six_month_pct < -30:
            score -= 8
    if _finite(momentum.three_month_pct):
        if momentum.three_month_pct > 0:
            score += 4
        elif momentum.three_month_pct < -20:
            score -= 5

    return _clamp(score, 0, 100)


def long_term_chart_score(momentum: Momentum) -> float:
    if not _has_momentum_data(momentum):
        return 50

    score = 50.0
    position = momentum.range_position_pct
    drawdown = momentum.drawdown_from_high_pct
    three_month = momentum.three_month_pct
    six_month = momentum.six_month_pct

    if _finite(position):
        if 35 <= position <= 78:
            score += 20
        elif 20 <= position < 35:
            score += 12
        elif 78 < position <= 90:
            score += 6
        elif position > 90:
            score -= 5
        elif position < 20:
            score -= 8

    if _finite(drawdown):
        decline = abs(min(drawdown, 0))
        if 8 <= decline <= 28:
            score += 16
        elif 0 <= decline < 8:
            score += 8
        elif 28 < decline <= 45:
            score += 2
        elif decline > 45:
            score -= 12

    if _finite(three_month) and _finite(six_month):
        if three_month > 0 and six_month > 0:
            score += 9
        elif three_month < 0 and six_month < 0:
            score -= 9

    if _finite(momentum.ma120_distance_pct):
        if momentum.ma120_distance_pct > 0:
            score += 7
        elif momentum.ma120_distance_pct < -12:
            score -= 7
    if _finite(momentum.ma60_slope_pct):
        if momentum.ma60_slope_pct > 0:
            score += 5
        elif momentum.ma60_slope_pct < -2:
            score -= 6

    return _clamp(score, 0, 100)


def long_term_news_score(
    stock: StockProfile,
    industry: IndustryProfile,
    news_counter: Counter[str],
    has_live_news: bool,
) -> float:
    if not has_live_news:
        return 50 if stock.recent_issues else 45

    direct_terms = (stock.ticker, stock.name)
    industry_terms = (*industry.news_terms, industry.name, *industry.macro_terms)
    issue_terms = stock.recent_issues or (stock.thesis,)
    direct_score = _term_score(news_counter, direct_terms, baseline=35, scale=8)
    industry_score = _term_score(news_counter, industry_terms, baseline=35, scale=9)
    issue_score = _term_score(news_counter, issue_terms, baseline=35, scale=4)
    return _clamp(direct_score * 0.20 + industry_score * 0.60 + issue_score * 0.20, 0, 100)


def long_term_penalty(
    stock: StockProfile,
    momentum: Momentum,
    news_items: tuple[NewsItem, ...],
    company: float,
    market: float,
    chart: float,
) -> float:
    penalty = 0.0
    if not news_items:
        penalty += 1
    if not _has_momentum_data(momentum):
        penalty += 3
    if company < 45:
        penalty += 8
    if market < 40:
        penalty += 5
    if chart < 35:
        penalty += 4
    fundamentals = stock.fundamentals
    if fundamentals.operating_margin_pct is not None and fundamentals.operating_margin_pct < 0:
        penalty += 6
    if fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0:
        penalty += 5
    if fundamentals.debt_to_equity_pct is not None and fundamentals.debt_to_equity_pct > 220:
        penalty += 6
    return penalty


def long_term_signal_label(score: float, company: float, market: float, confidence: float) -> str:
    if confidence < 55 and score >= 67:
        return "장기 데이터 확인"
    if score >= 76 and company >= 68 and market >= 55:
        return "장기 핵심 후보"
    if score >= 67 and company >= 60:
        return "장기 관심"
    if score >= 58:
        return "장기 관찰"
    return "후순위"


def long_term_reasons(
    stock: StockProfile,
    momentum: Momentum,
    company: float,
    market: float,
    chart: float,
    news: float,
) -> tuple[str, ...]:
    return (
        f"기업 가치 점수 {company:.1f}/100: {_long_term_company_reason(stock)}",
        f"시장/산업 점수 {market:.1f}/100: {_long_term_market_reason(stock)}",
        f"장기 차트 점수 {chart:.1f}/100: {_long_term_chart_reason(momentum)}",
        f"구조적 이슈 점수 {news:.1f}/100: {_long_term_news_reason(stock)}",
    )


def long_term_cautions(
    stock: StockProfile,
    momentum: Momentum,
    news_items: tuple[NewsItem, ...],
    company: float,
    market: float,
    chart: float,
) -> tuple[str, ...]:
    cautions: list[str] = []
    if not news_items:
        cautions.append("라이브 뉴스가 없으면 장기 산업 변화 점수는 보수적으로 해석")
    if not _has_momentum_data(momentum):
        cautions.append("가격 데이터 부족으로 월봉/주봉과 120일/200일선 추세 확인 필요")
    if company < 55:
        cautions.append("장기 보유 전 매출 성장, 수익성, 현금흐름의 지속성 추가 확인 필요")
    if market < 50:
        cautions.append("산업/거시 점수가 약해 구조적 성장 지속성 확인 필요")
    if chart < 45:
        cautions.append("장기 차트 추세가 약해 분할 접근 또는 추세 회복 확인 필요")
    cautions.append("장기 판단에는 경쟁력, 시장점유율, 컨센서스 장기 추정치를 별도 확인해야 함")
    cautions.extend(stock.risks[:1])
    return tuple(dict.fromkeys(cautions))


def quality_score(fundamentals: Fundamentals) -> float:
    revenue_growth = _scale(fundamentals.revenue_growth_pct, low=-10, high=35)
    margin = _scale(fundamentals.operating_margin_pct, low=-5, high=45)
    roe = _scale(fundamentals.roe_pct, low=-10, high=45)
    leverage = _inverse_scale(fundamentals.debt_to_equity_pct, low=20, high=220)
    fcf_margin = _ratio_pct(fundamentals.free_cash_flow, fundamentals.revenue)
    cash_flow = _scale(fcf_margin, low=-10, high=25)
    liquidity = _scale(fundamentals.current_ratio_pct, low=80, high=220)
    interest_safety = _scale(fundamentals.interest_coverage, low=1, high=10)
    base = (
        revenue_growth * 0.26
        + margin * 0.25
        + roe * 0.22
        + leverage * 0.12
        + cash_flow * 0.08
        + liquidity * 0.04
        + interest_safety * 0.03
    )
    return base * 0.70 + growth_quality_score(fundamentals) * 0.30


def growth_quality_score(fundamentals: Fundamentals) -> float:
    revenue_cagr = _first_finite(
        fundamentals.revenue_cagr_5y_pct,
        fundamentals.revenue_cagr_3y_pct,
        fundamentals.revenue_growth_pct,
    )
    operating_growth = _average_finite(
        fundamentals.operating_income_growth_pct,
        fundamentals.operating_income_cagr_3y_pct,
    )
    quarter_growth = _average_finite(
        fundamentals.latest_quarter_revenue_yoy_pct,
        fundamentals.latest_quarter_operating_income_yoy_pct,
    )
    revenue_score = _scale(revenue_cagr, low=0, high=30)
    operating_score = _scale(operating_growth, low=0, high=45)
    leverage_score = _scale(fundamentals.operating_leverage_spread_pct, low=-10, high=20)
    quarter_score = _scale(quarter_growth, low=-5, high=35)
    revenue_streak = _scale(fundamentals.quarterly_revenue_yoy_streak, low=0, high=4)
    leverage_streak = _scale(fundamentals.quarterly_operating_leverage_streak, low=0, high=4)
    streak_score = (revenue_streak + leverage_streak) / 2
    return _clamp(
        revenue_score * 0.25
        + operating_score * 0.25
        + leverage_score * 0.20
        + quarter_score * 0.20
        + streak_score * 0.10,
        0,
        100,
    )


def valuation_score(fundamentals: Fundamentals) -> float:
    pe = fundamentals.forward_pe if fundamentals.forward_pe is not None else fundamentals.pe
    if pe is None or pe <= 0:
        return 45
    if pe <= 12:
        base = 86
    elif pe <= 20:
        base = 76
    elif pe <= 35:
        base = 63
    elif pe <= 55:
        base = 48
    elif pe <= 85:
        base = 34
    else:
        base = 22

    growth = fundamentals.revenue_growth_pct
    margin = fundamentals.operating_margin_pct
    roe = fundamentals.roe_pct
    debt_to_equity = fundamentals.debt_to_equity_pct
    growth_quality = growth_quality_score(fundamentals)

    if growth is not None and math.isfinite(growth):
        if pe > 35 and growth >= 25 and _at_least(margin, 10):
            base += min(14, (growth - 20) * 0.35)
        elif pe > 35 and growth < 12:
            base -= 10

        if pe <= 14 and growth < 5:
            base -= 12
        elif pe <= 22 and growth >= 20:
            base += 6

    if pe > 25 and margin is not None and math.isfinite(margin) and margin < 8:
        base -= 8
    if margin is not None and math.isfinite(margin) and margin < 0:
        base -= 12
    if debt_to_equity is not None and math.isfinite(debt_to_equity) and debt_to_equity > 220:
        base -= 8
    if roe is not None and math.isfinite(roe) and roe > 25 and pe <= 30:
        base += 6
    if fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0 and pe > 35:
        base -= 6
    if fundamentals.interest_coverage is not None and fundamentals.interest_coverage < 3:
        base -= 5
    if pe > 35:
        if growth_quality >= 72:
            base += 8
        elif growth_quality < 48:
            base -= 8

    return _clamp(base, 0, 100)


def analysis_style_for_stock(stock: StockProfile) -> str:
    fundamentals = stock.fundamentals
    pe = fundamentals.forward_pe if fundamentals.forward_pe is not None else fundamentals.pe
    growth = fundamentals.revenue_growth_pct
    margin = fundamentals.operating_margin_pct

    if _is_cyclical_industry(stock.industry) and pe is not None and pe <= 14:
        if _at_least(growth, 20):
            return "사이클 회복 성장주"
        return "경기민감 저PER 관찰"
    if _at_least(growth, 25):
        return "성장주"
    if pe is not None and pe >= 45:
        return "고멀티플 검증"
    if pe is not None and pe <= 20 and quality_score(fundamentals) >= 60:
        return "가치/퀄리티"
    if margin is not None and margin < 0 and _at_least(growth, 15):
        return "턴어라운드 관찰"
    return "균형형"


def weight_profile_for_stock(stock: StockProfile, analysis_style: str) -> str:
    if analysis_style in {"성장주", "고멀티플 검증", "턴어라운드 관찰"}:
        return "고성장주"
    if analysis_style == "가치/퀄리티" or "금융" in stock.industry or "은행" in stock.industry:
        return "가치/금융주"
    if analysis_style in {"경기민감 저PER 관찰", "사이클 회복 성장주"} or _is_cyclical_industry(stock.industry):
        return "경기민감주"
    return "기본형"


def style_weighted_stock_score(
    industry: float,
    quality: float,
    growth_quality: float,
    valuation: float,
    momentum: float,
    role: float,
    weight_profile: str,
) -> float:
    weights = STYLE_WEIGHT_PROFILES.get(weight_profile, STYLE_WEIGHT_PROFILES["기본형"])
    return _clamp(
        growth_quality * weights["growth_quality"]
        + quality * weights["quality"]
        + valuation * weights["valuation"]
        + momentum * weights["momentum"]
        + industry * weights["industry"]
        + role * weights["role"],
        0,
        100,
    )


def risk_gate_for_stock(
    stock: StockProfile,
    quality: float,
    valuation: float,
    momentum: float,
    growth_quality: float,
    analysis_style: str,
) -> tuple[str, tuple[str, ...]]:
    fundamentals = stock.fundamentals
    hard_fail_reasons: list[str] = []
    caution_reasons: list[str] = []

    if _at_least(fundamentals.debt_to_equity_pct, 400):
        hard_fail_reasons.append(f"부채비율 {fundamentals.debt_to_equity_pct:.1f}%로 극단적")
    elif _at_least(fundamentals.debt_to_equity_pct, 220):
        caution_reasons.append(f"부채비율 {fundamentals.debt_to_equity_pct:.1f}%로 차환 부담 점검")

    if fundamentals.interest_coverage is not None and math.isfinite(fundamentals.interest_coverage):
        if fundamentals.interest_coverage < 1.5:
            hard_fail_reasons.append(f"이자보상배율 {fundamentals.interest_coverage:.1f}배로 위험")
        elif fundamentals.interest_coverage < 3:
            caution_reasons.append(f"이자보상배율 {fundamentals.interest_coverage:.1f}배로 낮음")

    if fundamentals.current_ratio_pct is not None and math.isfinite(fundamentals.current_ratio_pct):
        if fundamentals.current_ratio_pct < 70:
            hard_fail_reasons.append(f"유동비율 {fundamentals.current_ratio_pct:.1f}%로 단기 지급능력 위험")
        elif fundamentals.current_ratio_pct < 100:
            caution_reasons.append(f"유동비율 {fundamentals.current_ratio_pct:.1f}%로 낮음")

    operating_loss = (
        (fundamentals.operating_income is not None and fundamentals.operating_income < 0)
        or (fundamentals.operating_margin_pct is not None and fundamentals.operating_margin_pct < 0)
    )
    if operating_loss and fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0:
        hard_fail_reasons.append("영업적자와 FCF 음수가 동시에 발생")
    elif fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0:
        caution_reasons.append("FCF 음수로 현금 소진 속도 확인")

    if hard_fail_reasons:
        return "Hard Fail", tuple(dict.fromkeys(hard_fail_reasons))

    high_growth = _at_least(fundamentals.revenue_growth_pct, 25) or growth_quality >= 70
    cash_flow_anchor = (
        fundamentals.free_cash_flow is not None
        and fundamentals.free_cash_flow >= 0
    ) or (
        fundamentals.operating_cash_flow is not None
        and fundamentals.operating_cash_flow > 0
    )
    if caution_reasons and high_growth and cash_flow_anchor and quality >= 48:
        return "Aggressive Allow", tuple(dict.fromkeys(caution_reasons))
    if caution_reasons and analysis_style in {"성장주", "고멀티플 검증"} and momentum >= 55:
        return "Aggressive Allow", tuple(dict.fromkeys(caution_reasons))
    if caution_reasons:
        return "Watch", tuple(dict.fromkeys(caution_reasons))
    if valuation < 35 and momentum < 35:
        return "Watch", ("밸류 부담과 약한 모멘텀이 동시에 확인",)
    return "Pass", ("선제 리스크 필터 통과",)


def risk_adjusted_stock_score(total: float, risk_gate: str, risk_gate_reasons: tuple[str, ...]) -> float:
    if risk_gate == "Hard Fail":
        return min(total, 49)
    if risk_gate == "Watch":
        return max(total - 3, 0)
    if risk_gate == "Aggressive Allow":
        return max(total - 1.5, 0)
    return total


def valuation_note_for_stock(stock: StockProfile, valuation: float, analysis_style: str) -> str:
    fundamentals = stock.fundamentals
    pe = fundamentals.forward_pe if fundamentals.forward_pe is not None else fundamentals.pe
    if pe is None or pe <= 0:
        return "이익 멀티플 데이터가 부족해 보수적으로 중립 이하로 봅니다."

    if analysis_style == "경기민감 저PER 관찰":
        return "낮은 PER은 매력보다 이익 정점 신호일 수 있어 업황 둔화 여부를 먼저 확인합니다."
    if analysis_style == "사이클 회복 성장주":
        return "낮은 멀티플과 강한 성장률이 함께 보이지만 사이클 회복 지속성을 확인해야 합니다."
    if pe >= 45 and valuation < 45:
        return "높은 멀티플은 미래 이익 개선이 계속될 때만 정당화됩니다."
    if pe <= 20 and valuation >= 70:
        return "낮은 멀티플이 긍정적이나 성장 둔화나 재무 부담이 숨어 있는지 확인합니다."
    if valuation >= 63:
        return "현재 멀티플은 성장성과 수익성 대비 무리하지 않은 구간으로 봅니다."
    return "멀티플 부담이 있어 실적 상향이나 산업 전망 개선의 근거가 필요합니다."


def valuation_range_for_stock(stock: StockProfile, analysis_style: str) -> ValuationRange:
    fundamentals = stock.fundamentals
    multiple = fundamentals.forward_pe if fundamentals.forward_pe is not None else fundamentals.pe
    profit_metric, profit_value = _profit_base_for_valuation(fundamentals, multiple)
    if profit_value is None or profit_value <= 0 or multiple is None or multiple <= 0:
        return ValuationRange(
            profit_metric=profit_metric,
            profit_value=profit_value,
            multiple_low=None,
            multiple_high=None,
            market_cap_low=None,
            market_cap_high=None,
            upside_low_pct=None,
            upside_high_pct=None,
            note="이익 규모 또는 멀티플 데이터가 부족해 적정 시가총액 범위를 계산하지 않았습니다.",
        )

    multiple_low, multiple_high = _multiple_range(multiple, analysis_style, stock.role)
    market_cap_low = profit_value * multiple_low
    market_cap_high = profit_value * multiple_high
    upside_low = _upside_pct(market_cap_low, fundamentals.market_cap)
    upside_high = _upside_pct(market_cap_high, fundamentals.market_cap)
    note = (
        f"{profit_metric}을 기준 이익으로 두고 {multiple_low:.1f}~{multiple_high:.1f}배 멀티플을 적용한 약식 범위입니다."
    )
    return ValuationRange(
        profit_metric=profit_metric,
        profit_value=profit_value,
        multiple_low=multiple_low,
        multiple_high=multiple_high,
        market_cap_low=market_cap_low,
        market_cap_high=market_cap_high,
        upside_low_pct=upside_low,
        upside_high_pct=upside_high,
        note=note,
    )


def analysis_checks_for_stock(
    stock: StockProfile, valuation_note: str, valuation_range: ValuationRange
) -> tuple[str, ...]:
    fundamentals = stock.fundamentals
    return (
        _growth_check(fundamentals),
        _growth_quality_check(fundamentals),
        _profitability_check(fundamentals),
        _cash_flow_check(fundamentals),
        _stability_check(fundamentals),
        f"멀티플 해석: {valuation_note}",
        _valuation_range_check(valuation_range),
    )


def second_order_checks_for_stock(
    stock: StockProfile, industry_score: IndustryScore, analysis_style: str
) -> tuple[str, ...]:
    leadership_check = (
        f"{stock.name}의 선두 프리미엄이 경쟁사 대비 타당한지 확인"
        if stock.role == "core"
        else f"{stock.name}이 선두 기업과의 격차를 줄일 수 있는 구체적 이유 확인"
    )
    return (
        f"{stock.industry} 성장률이 몇 년 지속될지와 현재 산업 점수 {industry_score.score:.1f}점의 지속성 확인",
        "미래 이익 규모와 적용할 멀티플을 각각 범위로 잡아 상승 여력을 재계산",
        leadership_check,
        _style_specific_second_order_check(analysis_style),
    )


def risk_cautions_for_stock(stock: StockProfile, analysis_style: str) -> tuple[str, ...]:
    cautions: list[str] = []
    if analysis_style == "경기민감 저PER 관찰":
        cautions.append("낮은 PER이 이익 정점 구간에서 나타난 착시인지 확인 필요")
    if analysis_style in {"고멀티플 검증", "성장주"}:
        cautions.append("성장 기대가 이미 가격에 반영된 정도 확인 필요")
    if stock.fundamentals.debt_to_equity_pct is not None and stock.fundamentals.debt_to_equity_pct > 200:
        cautions.append("부채비율이 높아 이자보상비율과 현금흐름 추가 확인 필요")
    if stock.fundamentals.current_ratio_pct is not None and stock.fundamentals.current_ratio_pct < 100:
        cautions.append("유동비율이 낮아 단기 지급능력 확인 필요")
    if stock.fundamentals.free_cash_flow is not None and stock.fundamentals.free_cash_flow < 0:
        cautions.append("잉여현금흐름이 음수라 투자/운전자본 부담 확인 필요")
    if (
        stock.fundamentals.revenue_growth_pct is not None
        and stock.fundamentals.revenue_growth_pct > 8
        and stock.fundamentals.operating_income_growth_pct is not None
        and stock.fundamentals.operating_income_growth_pct < stock.fundamentals.revenue_growth_pct
    ):
        cautions.append("매출은 성장하지만 영업이익 성장 속도가 더 느려 마진 압박 여부 확인 필요")
    if stock.fundamentals.quarterly_revenue_yoy_streak == 0:
        cautions.append("최근 분기 YoY 성장 지속성이 끊겨 다음 실적 확인 필요")
    if (
        analysis_style in {"고멀티플 검증", "성장주"}
        and stock.fundamentals.revenue_cagr_3y_pct is not None
        and stock.fundamentals.revenue_cagr_3y_pct < 10
    ):
        cautions.append("CAGR 대비 현재 멀티플 부담이 커 성장 재가속 근거 확인 필요")
    return tuple(cautions)


def decision_grade_for_stock(
    total_score: float,
    quality: float,
    valuation: float,
    momentum: float,
    risk_level: str,
    risk_gate: str = "Pass",
) -> str:
    if risk_gate == "Hard Fail":
        return "제외"
    adjusted = total_score
    if risk_level == "높음":
        adjusted -= 4
    if valuation < 40 and quality < 55:
        adjusted -= 3
    if momentum < 35:
        adjusted -= 3
    if risk_gate == "Aggressive Allow" and adjusted < 57 <= total_score + 5:
        return "관망"
    if adjusted >= 75:
        return "매수 후보"
    if adjusted >= 67:
        return "관심"
    if adjusted >= 57:
        return "관망"
    return "제외"


def risk_level_for_stock(
    stock: StockProfile,
    quality: float,
    valuation: float,
    momentum: float,
    risk_gate: str = "Pass",
) -> str:
    if risk_gate in {"Hard Fail", "Aggressive Allow"}:
        return "높음"
    risk_points = 0
    if valuation <= 40:
        risk_points += 2
    elif valuation <= 50:
        risk_points += 1
    if momentum < 35:
        risk_points += 1
    if quality < 40:
        risk_points += 1
    debt_to_equity = stock.fundamentals.debt_to_equity_pct
    if debt_to_equity is not None and debt_to_equity > 220:
        risk_points += 2
    current_ratio = stock.fundamentals.current_ratio_pct
    if current_ratio is not None and current_ratio < 100:
        risk_points += 1
    interest_coverage = stock.fundamentals.interest_coverage
    if interest_coverage is not None and interest_coverage < 3:
        risk_points += 1
    free_cash_flow = stock.fundamentals.free_cash_flow
    if free_cash_flow is not None and free_cash_flow < 0:
        risk_points += 1
    if _is_cyclical_low_pe(stock):
        risk_points += 1
    operating_margin = stock.fundamentals.operating_margin_pct
    if operating_margin is not None and operating_margin < 0:
        risk_points += 1
    if risk_points >= 3:
        return "높음"
    if risk_points >= 1:
        return "중간"
    return "낮음"


def portfolio_rule_for_stock(
    decision_grade: str,
    risk_level: str,
    risk_gate: str,
) -> tuple[str, float, float]:
    target, maximum = PORTFOLIO_WEIGHT_RULES.get(decision_grade, (0.0, 0.0))
    if risk_gate == "Hard Fail":
        return "제외", 0.0, 0.0
    if risk_gate == "Aggressive Allow":
        target = min(target, 3.0)
        maximum = min(maximum, 4.0)
    elif risk_level == "높음":
        target = min(target, 3.0)
        maximum = min(maximum, 5.0)

    if decision_grade == "매수 후보":
        signal = "편입 후보"
    elif decision_grade == "관심":
        signal = "분할 관찰"
    elif decision_grade == "관망":
        signal = "보유 점검"
    else:
        signal = "제외"
    if risk_gate == "Aggressive Allow" and signal != "제외":
        signal = "소액 관찰"
    return signal, round(target, 1), round(maximum, 1)


def sell_signals_for_stock(
    stock: StockProfile,
    decision_grade: str,
    risk_level: str,
    risk_gate: str,
    quality: float,
    valuation: float,
    momentum: float,
) -> tuple[str, ...]:
    signals: list[str] = []
    fundamentals = stock.fundamentals
    if risk_gate == "Hard Fail":
        signals.append("즉시 제외: 리스크 게이트 Hard Fail")
    if decision_grade == "제외":
        signals.append("즉시 제외: 현재 종합 등급 제외")
    if quality < 45:
        signals.append("비중 축소: 성장성·수익성 훼손 확인")
    if valuation < 45 and momentum < 40:
        signals.append("비중 축소: 밸류 부담과 모멘텀 둔화 동시 발생")
    if risk_level == "높음" and risk_gate != "Hard Fail":
        signals.append("비중 제한: 고위험 라벨로 최대 비중 축소")
    if (
        fundamentals.revenue_growth_pct is not None
        and fundamentals.revenue_growth_pct < 0
        and fundamentals.operating_margin_pct is not None
        and fundamentals.operating_margin_pct < 5
    ):
        signals.append("비중 축소: 매출 감소와 낮은 마진 동시 발생")
    if not signals:
        signals.append("유지: 성장·수익성 훼손 전까지 월점검, 분기조정")
    return tuple(dict.fromkeys(signals))


def _early_quality_anchor_score(fundamentals: Fundamentals, base_quality: float) -> float:
    score = base_quality
    if fundamentals.operating_margin_pct is not None and math.isfinite(fundamentals.operating_margin_pct):
        if fundamentals.operating_margin_pct < 0:
            score -= 14
        elif fundamentals.operating_margin_pct >= 15:
            score += 5
    if fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0:
        score -= 7
    if fundamentals.current_ratio_pct is not None and fundamentals.current_ratio_pct >= 160:
        score += 4
    return _clamp(score, 0, 100)


def _early_valuation_anchor_score(item: StockScore) -> float:
    score = item.valuation_score
    upside_high = item.valuation_range.upside_high_pct
    upside_low = item.valuation_range.upside_low_pct
    if upside_high is not None and math.isfinite(upside_high):
        if upside_high >= 30:
            score += 8
        elif upside_high < 0:
            score -= 10
    if upside_low is not None and math.isfinite(upside_low) and upside_low < -35:
        score -= 6
    return _clamp(score, 0, 100)


def _early_growth_penalty(
    fundamentals: Fundamentals,
    momentum: Momentum,
    size_score: float,
    growth_score: float,
    pullback_score: float,
) -> float:
    penalty = 0.0
    growth = fundamentals.revenue_growth_pct
    if growth is not None and math.isfinite(growth) and growth < 8:
        penalty += 9
    if size_score <= 20:
        penalty += 18
    elif size_score <= 35:
        penalty += 8
    if pullback_score < 38:
        penalty += 7
    if fundamentals.debt_to_equity_pct is not None and fundamentals.debt_to_equity_pct > 220:
        penalty += 8
    if fundamentals.operating_margin_pct is not None and fundamentals.operating_margin_pct < -10:
        penalty += 8
    if fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0:
        penalty += 4
    if growth_score < 35:
        penalty += 6
    if (
        momentum.range_position_pct is not None
        and momentum.range_position_pct > 80
        and momentum.six_month_pct is not None
        and momentum.six_month_pct > 50
    ):
        penalty += 10
    return penalty


def early_growth_entry_label(score: float, pullback: float, growth: float, size: float) -> str:
    if score >= 76 and pullback >= 62 and growth >= 60 and size >= 55:
        return "저점 성장 후보"
    if score >= 68 and growth >= 55 and size >= 45:
        return "분할 관찰"
    if score >= 58:
        return "관찰"
    return "후순위"


def early_growth_reasons(
    stock: StockProfile,
    momentum: Momentum,
    size_score: float,
    growth_score: float,
    pullback_score: float,
    quality_anchor: float,
) -> tuple[str, ...]:
    fundamentals = stock.fundamentals
    reasons = [
        f"규모 점수 {size_score:.1f}/100: {_size_reason(fundamentals.market_cap, fundamentals.market_cap_currency)}",
        f"매출 성장 점수 {growth_score:.1f}/100: {_growth_check(fundamentals)}",
        f"저점 진입 점수 {pullback_score:.1f}/100: {_pullback_reason(momentum)}",
        f"재무 버팀목 {quality_anchor:.1f}/100: 영업이익률과 부채 부담을 함께 반영",
    ]
    reasons.extend(stock.recent_issues[:1])
    return tuple(reasons)


def early_growth_cautions(
    stock: StockProfile,
    momentum: Momentum,
    size_score: float,
    pullback_score: float,
    valuation_anchor: float,
) -> tuple[str, ...]:
    fundamentals = stock.fundamentals
    cautions: list[str] = []
    if fundamentals.market_cap is None:
        cautions.append("시가총액 데이터가 부족해 규모 필터를 다시 확인해야 함")
    if size_score <= 30:
        cautions.append("이미 대형주에 가까워 작은 회사 리레이팅 효과는 제한적일 수 있음")
    if pullback_score < 45:
        cautions.append("저점/반등 신호가 약해 추격 매수 또는 하락 지속 가능성 확인 필요")
    if (
        momentum.range_position_pct is not None
        and momentum.range_position_pct <= 15
        and momentum.one_month_pct is not None
        and momentum.one_month_pct < 0
    ):
        cautions.append("가격이 6개월 저점권에 있지만 아직 하락 중일 수 있음")
    if fundamentals.operating_margin_pct is not None and fundamentals.operating_margin_pct < 0:
        cautions.append("영업적자 기업은 매출 성장보다 현금 소진 속도를 먼저 확인")
    if fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0:
        cautions.append("FCF가 음수라 추가 자금 조달 가능성 확인 필요")
    if fundamentals.debt_to_equity_pct is not None and fundamentals.debt_to_equity_pct > 180:
        cautions.append("부채 부담이 높아 금리와 차환 리스크 점검 필요")
    if valuation_anchor < 45:
        cautions.append("성장성 대비 밸류에이션 부담이 커 실적 상향 근거 필요")
    cautions.extend(stock.risks[:1])
    return tuple(dict.fromkeys(cautions))


def valuation_label_for_score(score: float) -> str:
    if score >= 76:
        return "저평가/합리"
    if score >= 63:
        return "적정"
    if score >= 48:
        return "약간 고평가"
    return "고평가"


def _industry_evidence(
    industry: IndustryProfile,
    macro_score: float,
    news_score: float,
    market_score: float,
    data_macro_score: float,
    macro_snapshot: MacroSnapshot | None,
) -> tuple[str, ...]:
    evidence = [
        f"거시 테마 적합도 {macro_score:.1f}/100",
        f"실제 거시지표 반영 점수 {data_macro_score:.1f}/100",
        f"뉴스 언급 강도 {news_score:.1f}/100",
        f"산업 내 가격 모멘텀 {market_score:.1f}/100",
    ]
    if macro_snapshot is not None:
        evidence.append(macro_snapshot.summary)
    evidence.extend(industry.tailwinds[:2])
    return tuple(evidence)


def _beneficiary_macro_score(
    profile: BeneficiaryIndustryProfile,
    source_score: IndustryScore,
    macro_counter: Counter[str],
) -> float:
    text_score = _term_score(
        macro_counter,
        (*profile.keywords, profile.name, *source_score.industry.macro_terms),
        baseline=40,
        scale=7,
    )
    return _clamp(text_score * 0.65 + source_score.macro_score * 0.35, 0, 100)


def _beneficiary_proxy_market_score(
    profile: BeneficiaryIndustryProfile,
    momentums: dict[str, Momentum],
) -> tuple[float, float]:
    total_weight = 0.0
    covered_weight = 0.0
    weighted_score = 0.0
    for proxy in profile.market_proxies:
        weight = max(proxy.weight, 0)
        if weight <= 0:
            continue
        total_weight += weight
        score = momentum_to_score(momentums.get(proxy.ticker.upper(), Momentum()))
        if score is None:
            continue
        covered_weight += weight
        weighted_score += score * weight
    if total_weight <= 0 or covered_weight <= 0:
        return 50, 0
    return weighted_score / covered_weight, (covered_weight / total_weight) * 100


def _beneficiary_news_signal(
    profile: BeneficiaryIndustryProfile,
    news_items: Iterable[NewsItem],
    reference_time: datetime,
) -> _BeneficiaryNewsSignal:
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    recent_strength = 0.0
    baseline_strength = 0.0
    weighted_source_score = 0.0
    total_source_strength = 0.0
    matched_count = 0
    source_counter: Counter[str] = Counter()
    for item in news_items:
        match_strength = _beneficiary_news_match_strength(profile, item)
        if match_strength <= 0:
            continue
        published_at = _parse_news_datetime(item.published, reference_time)
        age_days = max((reference_time - published_at).total_seconds() / 86_400, 0)
        if age_days > 30:
            continue
        source_weight = _news_source_weight(item.source)
        recency_weight = _news_recency_weight(age_days)
        strength = min(match_strength, 4) * source_weight * recency_weight
        baseline_strength += strength
        matched_count += 1
        source_name = item.source or "Unknown"
        source_counter[source_name] += strength
        weighted_source_score += _source_weight_to_score(source_weight) * strength
        total_source_strength += strength
        if age_days <= 7:
            recent_strength += strength

    recent_score = _clamp(35 + recent_strength * 11, 0, 100)
    baseline_score = _clamp(35 + baseline_strength * 5, 0, 100)
    if matched_count < 2 or baseline_strength < 1:
        acceleration_score = 50.0
        coverage_label = "30일 데이터 부족"
    else:
        recent_daily = recent_strength / 7
        baseline_daily = baseline_strength / 30
        ratio = recent_daily / max(baseline_daily, 0.05)
        acceleration_score = _clamp(50 + math.log2(max(ratio, 0.05)) * 18, 0, 100)
        if ratio >= 1.35:
            coverage_label = "7일 뉴스 증가"
        elif ratio <= 0.75:
            coverage_label = "7일 뉴스 둔화"
        else:
            coverage_label = "7일/30일 보통"

    source_score = (
        weighted_source_score / total_source_strength
        if total_source_strength > 0
        else 50.0
    )
    score = _clamp(
        recent_score * 0.40
        + baseline_score * 0.20
        + acceleration_score * 0.25
        + source_score * 0.15,
        0,
        100,
    )
    top_sources = tuple(source for source, _ in source_counter.most_common(3))
    return _BeneficiaryNewsSignal(
        score=score,
        recent_score=recent_score,
        baseline_score=baseline_score,
        acceleration_score=acceleration_score,
        coverage_label=coverage_label,
        top_sources=top_sources,
    )


def _beneficiary_news_match_strength(profile: BeneficiaryIndustryProfile, item: NewsItem) -> float:
    text = " ".join(part for part in (item.title, item.summary or "") if part)
    counter = _counter(text)
    return _term_match_count(counter, (*profile.keywords, profile.name))


def _parse_news_datetime(value: str | None, reference_time: datetime) -> datetime:
    fallback_tz = reference_time.tzinfo or timezone.utc
    if not value:
        return reference_time
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return reference_time
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=fallback_tz)
    return parsed.astimezone(fallback_tz)


def _news_recency_weight(age_days: float) -> float:
    if age_days <= 7:
        return _clamp(1.0 - age_days * 0.035, 0.72, 1.0)
    return _clamp(0.72 - (age_days - 7) * 0.015, 0.35, 0.72)


def _news_source_weight(source: str | None) -> float:
    normalized = (source or "").lower()
    if any(name in normalized for name in HIGH_TRUST_NEWS_SOURCES):
        return 1.3
    if any(name in normalized for name in LOW_TRUST_NEWS_SOURCES):
        return 0.7
    return 1.0


def _source_weight_to_score(weight: float) -> float:
    return _clamp(50 + (weight - 1.0) * 90, 0, 100)


def _beneficiary_evidence(
    profile: BeneficiaryIndustryProfile,
    source_score: IndustryScore,
    macro_score: float,
    news_signal: _BeneficiaryNewsSignal,
    market_score: float,
    proxy_coverage_pct: float,
) -> tuple[str, ...]:
    evidence = [
        f"원인 산업 {profile.source_industry} 활황 점수 {source_score.score:.1f}/100",
        f"산업 연결 강도 {profile.connection_strength:.1f}/100",
        f"거시 적합도 {macro_score:.1f}/100",
        f"뉴스 신호 {news_signal.score:.1f}/100 ({news_signal.coverage_label})",
        f"대표 ETF/종목 모멘텀 {market_score:.1f}/100, 커버리지 {proxy_coverage_pct:.0f}%",
        profile.mechanism,
    ]
    if news_signal.top_sources:
        evidence.append("주요 뉴스 출처: " + ", ".join(news_signal.top_sources))
    evidence.extend(source_score.evidence[:2])
    return tuple(dict.fromkeys(evidence))


def _stock_reasons(
    stock: StockProfile,
    quality: float,
    growth_quality: float,
    valuation: float,
    momentum: float,
    industry_score: IndustryScore,
    analysis_style: str,
    valuation_note: str,
) -> tuple[str, ...]:
    role_text = "핵심 기업" if stock.role == "core" else "부가/연관 기업"
    reasons = [
        f"{industry_score.industry.name} 산업의 {role_text}",
        stock.thesis,
        f"분석 스타일: {analysis_style}",
        f"기본적 분석 점수 {quality:.1f}/100, 성장 품질 {growth_quality:.1f}/100, 밸류에이션 점수 {valuation:.1f}/100",
        f"공식 재무 데이터 커버리지 {official_fundamental_coverage_pct(stock.fundamentals):.0f}%",
        _growth_quality_reason(stock.fundamentals),
        valuation_note,
        f"가격 모멘텀 점수 {momentum:.1f}/100",
    ]
    reasons.extend(stock.recent_issues[:1])
    return tuple(reasons)


def _size_reason(market_cap: float | None, currency: str) -> str:
    if market_cap is None or not math.isfinite(market_cap):
        return "시가총액 데이터 부족"
    size_text = _compact_market_cap(market_cap, currency)
    particle = "으로" if currency.upper() == "KRW" else "로"
    score = company_size_score(market_cap, currency)
    if score >= 90:
        return f"시가총액 {size_text}{particle} 소형/중소형 성장주 구간"
    if score >= 55:
        return f"시가총액 {size_text}{particle} 중형 성장주 구간"
    if score <= 12:
        return f"시가총액 {size_text}{particle} 이미 대형주 구간"
    return f"시가총액 {size_text}{particle} 작은 회사 프리미엄은 제한적"


def _pullback_reason(momentum: Momentum) -> str:
    position = momentum.range_position_pct
    drawdown = momentum.drawdown_from_high_pct
    one_month = momentum.one_month_pct
    if position is None and drawdown is None and one_month is None:
        return "가격 위치 데이터 부족으로 중립 처리"

    parts: list[str] = []
    if position is not None and math.isfinite(position):
        if position <= 35:
            zone = "저점권"
        elif position <= 65:
            zone = "중단 박스권"
        else:
            zone = "고점권"
        parts.append(f"6개월 가격 위치 {position:.1f}% ({zone})")
    if drawdown is not None and math.isfinite(drawdown):
        parts.append(f"고점 대비 {drawdown:.1f}%")
    if one_month is not None and math.isfinite(one_month):
        parts.append(f"1개월 {one_month:.1f}%")
    return ", ".join(parts)


def _short_term_news_reason(stock: StockProfile) -> str:
    if stock.recent_issues:
        return stock.recent_issues[0]
    return "라이브 뉴스와 산업 키워드 언급 강도를 반영"


def _short_term_momentum_reason(momentum: Momentum) -> str:
    if not _has_momentum_data(momentum):
        return "가격 모멘텀 데이터 부족으로 중립 처리"

    parts: list[str] = []
    if _finite(momentum.one_month_pct):
        parts.append(f"1개월 {momentum.one_month_pct:.1f}%")
    if _finite(momentum.three_month_pct):
        parts.append(f"3개월 {momentum.three_month_pct:.1f}%")
    if _finite(momentum.six_month_pct):
        parts.append(f"6개월 {momentum.six_month_pct:.1f}%")
    return ", ".join(parts) if parts else "가격 변화율 데이터 부족"


def _short_term_chart_reason(momentum: Momentum) -> str:
    if not _has_momentum_data(momentum):
        return "차트 데이터 부족으로 중립 처리"

    parts: list[str] = []
    if _finite(momentum.range_position_pct):
        if momentum.range_position_pct >= 85:
            zone = "고점권"
        elif momentum.range_position_pct >= 45:
            zone = "상단 추세권"
        elif momentum.range_position_pct >= 25:
            zone = "중단 회복권"
        else:
            zone = "저점권"
        parts.append(f"6개월 위치 {momentum.range_position_pct:.1f}% ({zone})")
    if _finite(momentum.drawdown_from_high_pct):
        parts.append(f"고점 대비 {momentum.drawdown_from_high_pct:.1f}%")
    if _finite(momentum.rsi14):
        parts.append(f"RSI {momentum.rsi14:.1f}")
    if _finite(momentum.ma20_distance_pct):
        parts.append(f"MA20 대비 {momentum.ma20_distance_pct:.1f}%")
    return ", ".join(parts) if parts else "가격 위치 데이터 부족"


def _short_term_volume_reason(momentum: Momentum) -> str:
    if not _has_volume_data(momentum):
        return "거래량 데이터 부족으로 중립 처리"
    parts = [
        f"20일 평균 대비 {momentum.volume_ratio:.2f}배",
        f"최근 거래량 {_compact_amount(momentum.latest_volume)}",
    ]
    if _finite(momentum.twenty_day_breakout_pct):
        parts.append(f"20일 돌파율 {momentum.twenty_day_breakout_pct:.1f}%")
    return ", ".join(parts)


def _medium_term_company_reason(stock: StockProfile) -> str:
    fundamentals = stock.fundamentals
    checks = [_growth_check(fundamentals), _profitability_check(fundamentals)]
    pe = fundamentals.forward_pe if fundamentals.forward_pe is not None else fundamentals.pe
    if pe is not None and math.isfinite(pe):
        checks.append(f"PER 기준 {pe:.1f}배")
    return " / ".join(checks)


def _medium_term_chart_reason(momentum: Momentum) -> str:
    if not _has_momentum_data(momentum):
        return "차트 데이터 부족으로 중립 처리"

    parts: list[str] = []
    if _finite(momentum.range_position_pct):
        if momentum.range_position_pct >= 82:
            zone = "상단 추세권"
        elif momentum.range_position_pct >= 35:
            zone = "중기 상승/회복권"
        elif momentum.range_position_pct >= 20:
            zone = "중기 저점 확인권"
        else:
            zone = "하단 약세권"
        parts.append(f"6개월 위치 {momentum.range_position_pct:.1f}% ({zone})")
    if _finite(momentum.drawdown_from_high_pct):
        parts.append(f"고점 대비 {momentum.drawdown_from_high_pct:.1f}%")
    if _finite(momentum.three_month_pct):
        parts.append(f"3개월 {momentum.three_month_pct:.1f}%")
    return ", ".join(parts) if parts else "가격 위치 데이터 부족"


def _medium_term_news_reason(stock: StockProfile) -> str:
    if stock.recent_issues:
        return stock.recent_issues[0]
    return "산업 키워드와 기업 이슈의 지속 가능성을 반영"


def _long_term_company_reason(stock: StockProfile) -> str:
    fundamentals = stock.fundamentals
    checks = [
        _growth_check(fundamentals),
        _profitability_check(fundamentals),
        _cash_flow_check(fundamentals),
        _stability_check(fundamentals),
    ]
    return " / ".join(checks)


def _long_term_market_reason(stock: StockProfile) -> str:
    return f"{stock.industry}의 구조적 성장성과 거시 민감도를 반영"


def _long_term_chart_reason(momentum: Momentum) -> str:
    if not _has_momentum_data(momentum):
        return "차트 데이터 부족으로 중립 처리"

    parts: list[str] = []
    if _finite(momentum.range_position_pct):
        if momentum.range_position_pct >= 80:
            zone = "장기 상단권"
        elif momentum.range_position_pct >= 35:
            zone = "장기 추세권"
        elif momentum.range_position_pct >= 20:
            zone = "장기 저점 확인권"
        else:
            zone = "장기 약세권"
        parts.append(f"6개월 위치 {momentum.range_position_pct:.1f}% ({zone})")
    if _finite(momentum.drawdown_from_high_pct):
        parts.append(f"고점 대비 {momentum.drawdown_from_high_pct:.1f}%")
    if _finite(momentum.six_month_pct):
        parts.append(f"6개월 {momentum.six_month_pct:.1f}%")
    return ", ".join(parts) if parts else "가격 위치 데이터 부족"


def _long_term_news_reason(stock: StockProfile) -> str:
    if stock.recent_issues:
        return stock.recent_issues[0]
    return "일회성 뉴스보다 산업 구조 변화와 장기 성장 스토리를 반영"


def _growth_check(fundamentals: Fundamentals) -> str:
    growth = fundamentals.revenue_growth_pct
    if growth is None or not math.isfinite(growth):
        return "매출 성장: 데이터 부족으로 산업 성장성과 공시 확인 필요"
    if growth >= 25:
        tone = "강한 확장"
    elif growth >= 8:
        tone = "완만한 성장"
    elif growth >= 0:
        tone = "성장 둔화"
    else:
        tone = "매출 감소"
    return f"매출 성장: {growth:.1f}%로 {tone} 흐름"


def _growth_quality_check(fundamentals: Fundamentals) -> str:
    cagr = _first_finite(fundamentals.revenue_cagr_5y_pct, fundamentals.revenue_cagr_3y_pct)
    leverage = fundamentals.operating_leverage_spread_pct
    quarter = fundamentals.latest_quarter_revenue_yoy_pct
    parts: list[str] = []
    if cagr is not None:
        parts.append(f"장기 CAGR {cagr:.1f}%")
    if leverage is not None:
        if leverage > 0:
            parts.append(f"영업 레버리지 +{leverage:.1f}%p")
        else:
            parts.append(f"영업 레버리지 {leverage:.1f}%p")
    if quarter is not None:
        parts.append(f"최근 분기 매출 YoY {quarter:.1f}%")
    if fundamentals.quarterly_revenue_yoy_streak is not None:
        parts.append(f"분기 성장 지속 {fundamentals.quarterly_revenue_yoy_streak}회")
    if not parts:
        return "성장 품질: 다년 CAGR과 분기 YoY 데이터 부족, 공식 공시 추세 추가 확인 필요"
    tone = "성장 지속성과 영업 레버리지 확인"
    if _at_least(leverage, 0) and _at_least(quarter, 0):
        tone = "성장 지속성과 영업 레버리지가 함께 확인"
    elif leverage is not None and leverage < 0:
        tone = "매출 성장 대비 이익 증가 속도 확인 필요"
    return "성장 품질: " + ", ".join(parts) + f" - {tone}"


def _growth_quality_reason(fundamentals: Fundamentals) -> str:
    growth = fundamentals.revenue_growth_pct
    operating_growth = fundamentals.operating_income_growth_pct
    if growth is not None and operating_growth is not None:
        if operating_growth > growth:
            return f"영업이익 증가율 {operating_growth:.1f}%가 매출 성장률 {growth:.1f}%보다 높아 영업 레버리지 확인"
        return f"매출 성장률 {growth:.1f}% 대비 영업이익 증가율 {operating_growth:.1f}%로 이익 전환 속도 확인 필요"
    if fundamentals.quarterly_revenue_yoy_streak:
        return f"최근 {fundamentals.quarterly_revenue_yoy_streak}개 분기 매출 YoY 성장 지속"
    return "성장 품질 데이터는 제한적이어서 매출 성장, 마진, 현금흐름을 함께 확인"


def _profitability_check(fundamentals: Fundamentals) -> str:
    margin = fundamentals.operating_margin_pct
    roe = fundamentals.roe_pct
    margin_text = "N/A" if margin is None else f"{margin:.1f}%"
    roe_text = "N/A" if roe is None else f"{roe:.1f}%"
    ebitda_text = "" if fundamentals.ebitda is None else f", EBITDA {_compact_amount(fundamentals.ebitda)}"
    if _at_least(margin, 20) and _at_least(roe, 15):
        tone = "수익성과 자본효율이 모두 양호"
    elif margin is not None and margin < 0:
        tone = "영업 적자라 이익 개선 확인 필요"
    else:
        tone = "수익성의 지속성과 개선 속도 확인 필요"
    return f"이익의 질: 영업이익률 {margin_text}, ROE {roe_text}{ebitda_text} - {tone}"


def _cash_flow_check(fundamentals: Fundamentals) -> str:
    operating_cash_flow = fundamentals.operating_cash_flow
    free_cash_flow = fundamentals.free_cash_flow
    if operating_cash_flow is None and free_cash_flow is None:
        return "현금흐름: 영업현금흐름/FCF 데이터 부족, 현금창출력 추가 확인 필요"
    ocf_text = "N/A" if operating_cash_flow is None else _compact_amount(operating_cash_flow)
    fcf_text = "N/A" if free_cash_flow is None else _compact_amount(free_cash_flow)
    if free_cash_flow is not None and free_cash_flow < 0:
        tone = "투자 부담 또는 현금 유출 확인 필요"
    elif operating_cash_flow is not None and operating_cash_flow > 0:
        tone = "영업 현금창출은 양호한 편"
    else:
        tone = "현금창출력 개선 여부 확인 필요"
    return f"현금흐름: 영업현금흐름 {ocf_text}, FCF {fcf_text} - {tone}"


def _stability_check(fundamentals: Fundamentals) -> str:
    debt_to_equity = fundamentals.debt_to_equity_pct
    current_ratio = fundamentals.current_ratio_pct
    interest_coverage = fundamentals.interest_coverage
    if debt_to_equity is None or not math.isfinite(debt_to_equity):
        return "안정성: 부채비율 데이터 부족, 유동비율과 이자보상비율 추가 확인 필요"
    if debt_to_equity > 220:
        tone = "재무 부담이 큰 구간"
    elif debt_to_equity > 150:
        tone = "차입 부담 점검 필요"
    else:
        tone = "부채비율은 과도하지 않은 편"
    current_text = "N/A" if current_ratio is None else f"{current_ratio:.1f}%"
    interest_text = "N/A" if interest_coverage is None else f"{interest_coverage:.1f}배"
    return f"안정성: 부채비율 {debt_to_equity:.1f}%, 유동비율 {current_text}, 이자보상 {interest_text} - {tone}"


def _style_specific_second_order_check(analysis_style: str) -> str:
    if analysis_style == "경기민감 저PER 관찰":
        return "낮은 PER이 저평가가 아니라 이익 정점 신호일 가능성을 반대로 검토"
    if analysis_style == "사이클 회복 성장주":
        return "업황 회복이 일회성인지 구조적 이익 증가인지 분리해서 검토"
    if analysis_style in {"성장주", "고멀티플 검증"}:
        return "시장 기대보다 더 큰 이익 증가가 가능한지, 아니면 이미 선반영됐는지 검토"
    if analysis_style == "가치/퀄리티":
        return "낮은 멀티플의 이유가 일시적 소외인지 구조적 성장 둔화인지 검토"
    return "산업 전망, 이익 추정, 멀티플 가정 중 어느 하나라도 틀렸을 때의 하방 검토"


def _profit_base_for_valuation(
    fundamentals: Fundamentals, multiple: float | None
) -> tuple[str, float | None]:
    if fundamentals.net_income is not None and fundamentals.net_income > 0:
        return "순이익", fundamentals.net_income
    if fundamentals.operating_income is not None and fundamentals.operating_income > 0:
        return "영업이익 보정", fundamentals.operating_income * 0.75
    if fundamentals.ebitda is not None and fundamentals.ebitda > 0:
        return "EBITDA 보정", fundamentals.ebitda * 0.65
    if (
        fundamentals.market_cap is not None
        and fundamentals.market_cap > 0
        and multiple is not None
        and multiple > 0
    ):
        return "PER 역산 이익", fundamentals.market_cap / multiple
    return "이익 데이터 부족", None


def _multiple_range(multiple: float, analysis_style: str, role: str) -> tuple[float, float]:
    style_bands = {
        "성장주": (0.80, 1.15),
        "고멀티플 검증": (0.65, 0.95),
        "가치/퀄리티": (0.85, 1.10),
        "경기민감 저PER 관찰": (0.55, 0.85),
        "사이클 회복 성장주": (0.75, 1.05),
        "턴어라운드 관찰": (0.60, 1.00),
    }
    low_factor, high_factor = style_bands.get(analysis_style, (0.80, 1.05))
    if role == "core":
        high_factor += 0.05
    else:
        low_factor -= 0.05
        high_factor -= 0.05
    return max(multiple * low_factor, 1.0), max(multiple * high_factor, 1.0)


def _upside_pct(target_value: float | None, current_value: float | None) -> float | None:
    if target_value is None or current_value is None or current_value <= 0:
        return None
    return ((target_value / current_value) - 1) * 100


def _valuation_range_check(valuation_range: ValuationRange) -> str:
    if valuation_range.market_cap_low is None or valuation_range.market_cap_high is None:
        return f"밸류에이션 범위: {valuation_range.note}"
    upside = _range_text(valuation_range.upside_low_pct, valuation_range.upside_high_pct, suffix="%")
    return (
        "밸류에이션 범위: "
        f"{valuation_range.profit_metric} x {valuation_range.multiple_low:.1f}~{valuation_range.multiple_high:.1f}배, "
        f"현재 시총 대비 여력 {upside}"
    )


def _earnings_yield_proxy_score(fundamentals: Fundamentals) -> float:
    if (
        fundamentals.operating_income is not None
        and fundamentals.market_cap is not None
        and fundamentals.market_cap > 0
    ):
        return _scale((fundamentals.operating_income / fundamentals.market_cap) * 100, low=1, high=12)
    pe = fundamentals.forward_pe if fundamentals.forward_pe is not None else fundamentals.pe
    if pe is None or pe <= 0:
        return 45
    return _scale(100 / pe, low=1, high=10)


def _momentum_label(momentum: Momentum) -> str:
    if not _has_momentum_data(momentum):
        return "데이터 부족"
    parts: list[str] = []
    if _finite(momentum.one_month_pct):
        parts.append(f"1개월 {momentum.one_month_pct:.1f}%")
    if _finite(momentum.three_month_pct):
        parts.append(f"3개월 {momentum.three_month_pct:.1f}%")
    if _finite(momentum.range_position_pct):
        parts.append(f"6개월 위치 {momentum.range_position_pct:.1f}%")
    return ", ".join(parts) if parts else "중립"


def _is_cyclical_low_pe(stock: StockProfile) -> bool:
    pe = stock.fundamentals.forward_pe if stock.fundamentals.forward_pe is not None else stock.fundamentals.pe
    return pe is not None and pe <= 14 and _is_cyclical_industry(stock.industry)


def _is_cyclical_industry(industry: str) -> bool:
    cyclical_terms = ("반도체", "전력 인프라", "에너지 장비", "우주항공")
    return any(term in industry for term in cyclical_terms)


def _at_least(value: float | None, threshold: float) -> bool:
    return value is not None and math.isfinite(value) and value >= threshold


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _first_finite(*values: float | int | None) -> float | None:
    for value in values:
        if isinstance(value, (int, float)) and math.isfinite(value):
            return float(value)
    return None


def _average_finite(*values: float | int | None) -> float | None:
    valid_values = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and math.isfinite(value)
    ]
    if not valid_values:
        return None
    return sum(valid_values) / len(valid_values)


def _has_momentum_data(momentum: Momentum) -> bool:
    return any(
        _finite(value)
        for value in (
            momentum.one_month_pct,
            momentum.three_month_pct,
            momentum.six_month_pct,
            momentum.drawdown_from_high_pct,
            momentum.range_position_pct,
        )
    )


def _ratio_pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return (numerator / denominator) * 100


def _compact_amount(value: float) -> str:
    abs_value = abs(value)
    sign = "-" if value < 0 else ""
    if abs_value >= 1_000_000_000_000:
        return f"{sign}{abs_value / 1_000_000_000_000:.1f}조"
    if abs_value >= 100_000_000:
        return f"{sign}{abs_value / 100_000_000:.0f}억"
    if abs_value >= 1_000_000:
        return f"{sign}{abs_value / 1_000_000:.0f}백만"
    return f"{value:,.0f}"


def _compact_market_cap(value: float, currency: str) -> str:
    if currency.upper() == "KRW":
        if value >= 1_000_000_000_000:
            return f"{value / 1_000_000_000_000:.1f}조원"
        return f"{value / 100_000_000:.0f}억원"
    if value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"
    return f"${value / 1_000_000_000:.1f}B"


def _range_text(low: float | None, high: float | None, suffix: str = "") -> str:
    if low is None or high is None:
        return "N/A"
    return f"{low:.1f}{suffix}~{high:.1f}{suffix}"


def _term_score(counter: Counter[str], terms: Iterable[str], baseline: float, scale: float) -> float:
    return _clamp(baseline + _term_match_count(counter, terms) * scale, 0, 100)


def _term_match_count(counter: Counter[str], terms: Iterable[str]) -> float:
    count = 0.0
    for term in terms:
        tokens = _tokens(term)
        if len(tokens) == 1:
            count += counter[tokens[0]]
            continue
        phrase = " ".join(tokens)
        count += counter[phrase] * 2
        count += min(counter[token] for token in tokens) if tokens else 0
    return count


def _counter(text: str) -> Counter[str]:
    tokens = _tokens(text)
    counter = Counter(tokens)
    for size in (2, 3):
        for index in range(len(tokens) - size + 1):
            counter[" ".join(tokens[index : index + size])] += 1
    return counter


def _tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_PATTERN.finditer(text)]


def _scale(value: float | None, low: float, high: float) -> float:
    if value is None or not math.isfinite(value):
        return 50
    return _clamp(((value - low) / (high - low)) * 100, 0, 100)


def _inverse_scale(value: float | None, low: float, high: float) -> float:
    if value is None or not math.isfinite(value):
        return 50
    return 100 - _scale(value, low, high)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
