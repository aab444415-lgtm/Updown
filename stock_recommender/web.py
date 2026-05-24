from __future__ import annotations

import argparse
import math
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .backtest import BACKTEST_HORIZONS, BACKTEST_METHODS, BENCHMARKS, backtest_to_dict, create_backtest
from .http_utils import int_query as _int_query
from .http_utils import record_api_error as _record_api_error
from .http_utils import send_json_response
from .models import RecommendationReport
from .pipeline import create_recommendation_report
from .snapshots import snapshot_history
from .universe import DEFAULT_MACRO_CONTEXT


WEB_DIR = Path(__file__).resolve().parent.parent / "web"
REPORT_PAYLOAD_TTL_SECONDS = 60 * 10


def create_report(macro_context: str = DEFAULT_MACRO_CONTEXT) -> RecommendationReport:
    return create_recommendation_report(macro_context=macro_context)


def create_report_payload(
    macro_context: str = DEFAULT_MACRO_CONTEXT,
    force_refresh: bool = False,
) -> dict:
    cache = _report_payload_cache()
    cache_key = f"web-report-payload:{macro_context}"
    if not force_refresh:
        cached = cache.get_json(cache_key)
        if isinstance(cached, dict) and cached.get("stocks"):
            return cached
    payload = report_to_dict(create_report(macro_context=macro_context))
    if payload.get("stocks"):
        cache.set_json(cache_key, "local-report", "local://api/report", payload, REPORT_PAYLOAD_TTL_SECONDS)
    return payload


def report_to_dict(report: RecommendationReport) -> dict:
    technical_by_ticker = _technical_by_ticker(report)
    legend_coverage = _legend_metric_coverage(report)
    early_growth_by_ticker = {
        item.stock_score.stock.ticker.upper(): item for item in report.early_growth_scores
    }
    short_term_by_ticker = {
        item.stock_score.stock.ticker.upper(): item for item in report.short_term_scores
    }
    medium_term_by_ticker = {
        item.stock_score.stock.ticker.upper(): item for item in report.medium_term_scores
    }
    long_term_by_ticker = {
        item.stock_score.stock.ticker.upper(): item for item in report.long_term_scores
    }
    legend_by_ticker = {
        item.stock_score.stock.ticker.upper(): item for item in report.legend_strategy_scores
    }
    return {
        "createdAt": report.created_at.isoformat(),
        "createdAtDisplay": report.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "createdAtTimezone": _timezone_name(report.created_at),
        "snapshotDate": report.created_at.date().isoformat(),
        "macroContext": report.macro_context,
        "dataQuality": {
            "liveNews": report.data_quality.live_news,
            "liveMarketData": report.data_quality.live_market_data,
            "liveFundamentals": report.data_quality.live_fundamentals,
            "liveMacro": report.data_quality.live_macro,
            "liveKoreaFundamentals": report.data_quality.live_korea_fundamentals,
            "universeMode": report.data_quality.universe_mode,
            "universeCandidateCount": report.data_quality.universe_candidate_count,
            "universeQuoteReadyCount": report.data_quality.universe_quote_ready_count,
            "universeFinancialTargetCount": report.data_quality.universe_financial_target_count,
            "universeFinancialReadyCount": report.data_quality.universe_financial_ready_count,
            "universeFinalCount": report.data_quality.universe_final_count,
            "universeUsCount": report.data_quality.universe_us_count,
            "universeKrCount": report.data_quality.universe_kr_count,
            "configuredSources": list(report.data_quality.configured_sources),
            "missingSources": list(report.data_quality.missing_sources),
            "warnings": list(report.data_quality.warnings),
            **legend_coverage,
        },
        "macroSnapshot": _macro_snapshot_to_dict(report),
        "industries": [
            {
                "name": item.industry.name,
                "description": item.industry.description,
                "score": item.score,
                "newsScore": item.news_score,
                "macroScore": item.macro_score,
                "marketScore": item.market_score,
                "evidence": list(item.evidence),
                "tailwinds": list(item.industry.tailwinds),
                "risks": list(item.industry.risks),
            }
            for item in report.industry_scores
        ],
        "beneficiaryIndustries": [
            _beneficiary_industry_to_dict(item)
            for item in report.beneficiary_industry_scores
        ],
        "stocks": [
            {
                "ticker": item.stock.ticker,
                "name": item.stock.name,
                "industry": item.stock.industry,
                "role": "핵심 기업" if item.stock.role == "core" else "부가/연관 기업",
                "score": item.score,
                "industryScore": item.industry_score,
                "qualityScore": item.quality_score,
                "growthQualityScore": item.growth_quality_score,
                "valuationScore": item.valuation_score,
                "momentumScore": item.momentum_score,
                "reasons": list(item.reasons),
                "cautions": list(item.cautions),
                "recentIssues": list(item.stock.recent_issues),
                "decisionGrade": item.decision_grade,
                "riskLevel": item.risk_level,
                **_portfolio_fields(item),
                "valuationLabel": item.valuation_label,
                "analysisStyle": item.analysis_style,
                "valuationNote": item.valuation_note,
                "valuationRange": _valuation_range_to_dict(item),
                "analysisChecks": list(item.analysis_checks),
                "secondOrderChecks": list(item.second_order_checks),
                "fundamentals": {
                    "revenueGrowthPct": item.stock.fundamentals.revenue_growth_pct,
                    "operatingMarginPct": item.stock.fundamentals.operating_margin_pct,
                    "roePct": item.stock.fundamentals.roe_pct,
                    "debtToEquityPct": item.stock.fundamentals.debt_to_equity_pct,
                    "pe": item.stock.fundamentals.pe,
                    "forwardPe": item.stock.fundamentals.forward_pe,
                    "marketCap": item.stock.fundamentals.market_cap,
                    "marketCapUsd": item.stock.fundamentals.market_cap_usd,
                    "marketCapCurrency": item.stock.fundamentals.market_cap_currency,
                    "revenue": item.stock.fundamentals.revenue,
                    "operatingIncome": item.stock.fundamentals.operating_income,
                    "ebitda": item.stock.fundamentals.ebitda,
                    "netIncome": item.stock.fundamentals.net_income,
                    "operatingCashFlow": item.stock.fundamentals.operating_cash_flow,
                    "capitalExpenditure": item.stock.fundamentals.capital_expenditure,
                    "freeCashFlow": item.stock.fundamentals.free_cash_flow,
                    "currentAssets": item.stock.fundamentals.current_assets,
                    "currentLiabilities": item.stock.fundamentals.current_liabilities,
                    "currentRatioPct": item.stock.fundamentals.current_ratio_pct,
                    "interestExpense": item.stock.fundamentals.interest_expense,
                    "interestCoverage": item.stock.fundamentals.interest_coverage,
                    "cashAndEquivalents": item.stock.fundamentals.cash_and_equivalents,
                    "totalDebt": item.stock.fundamentals.total_debt,
                    "pretaxIncome": item.stock.fundamentals.pretax_income,
                    "incomeTaxExpense": item.stock.fundamentals.income_tax_expense,
                    "researchAndDevelopment": item.stock.fundamentals.research_and_development,
                    "enterpriseValue": item.stock.fundamentals.enterprise_value,
                    "roicPct": item.stock.fundamentals.roic_pct,
                    "evToEbit": item.stock.fundamentals.ev_to_ebit,
                    "earningsYieldPct": item.stock.fundamentals.earnings_yield_pct,
                    "rdToRevenuePct": item.stock.fundamentals.rd_to_revenue_pct,
                    "revenueCagr3yPct": item.stock.fundamentals.revenue_cagr_3y_pct,
                    "revenueCagr5yPct": item.stock.fundamentals.revenue_cagr_5y_pct,
                    "operatingIncomeGrowthPct": item.stock.fundamentals.operating_income_growth_pct,
                    "operatingIncomeCagr3yPct": item.stock.fundamentals.operating_income_cagr_3y_pct,
                    "operatingLeverageSpreadPct": item.stock.fundamentals.operating_leverage_spread_pct,
                    "latestQuarterRevenueYoyPct": item.stock.fundamentals.latest_quarter_revenue_yoy_pct,
                    "latestQuarterOperatingIncomeYoyPct": item.stock.fundamentals.latest_quarter_operating_income_yoy_pct,
                    "quarterlyRevenueYoyStreak": item.stock.fundamentals.quarterly_revenue_yoy_streak,
                    "quarterlyOperatingLeverageStreak": item.stock.fundamentals.quarterly_operating_leverage_streak,
                    "annualFinancials": list(item.stock.fundamentals.annual_financials),
                    "quarterlyFinancials": list(item.stock.fundamentals.quarterly_financials),
                },
                "technical": technical_by_ticker.get(item.stock.ticker.upper()),
                "country": item.stock.country,
                "currency": item.stock.currency,
                "earlyGrowth": _early_growth_to_dict(
                    early_growth_by_ticker.get(item.stock.ticker.upper())
                ),
                "shortTerm": _short_term_to_dict(
                    short_term_by_ticker.get(item.stock.ticker.upper())
                ),
                "mediumTerm": _medium_term_to_dict(
                    medium_term_by_ticker.get(item.stock.ticker.upper())
                ),
                "longTerm": _long_term_to_dict(
                    long_term_by_ticker.get(item.stock.ticker.upper())
                ),
                **_legend_strategy_stock_fields(
                    legend_by_ticker.get(item.stock.ticker.upper())
                ),
            }
            for item in report.stock_scores
        ],
        "legendCandidates": [
            {
                "ticker": item.stock_score.stock.ticker,
                "name": item.stock_score.stock.name,
                "industry": item.stock_score.stock.industry,
                "country": item.stock_score.stock.country,
                "currency": item.stock_score.stock.currency,
                "baseScore": item.stock_score.score,
                "decisionGrade": item.stock_score.decision_grade,
                "riskLevel": item.stock_score.risk_level,
                **_portfolio_fields(item.stock_score),
                **_legend_strategy_to_dict(item),
            }
            for item in report.legend_strategy_scores
        ],
        "shortTermCandidates": [
            {
                "ticker": item.stock_score.stock.ticker,
                "name": item.stock_score.stock.name,
                "industry": item.stock_score.stock.industry,
                "country": item.stock_score.stock.country,
                "currency": item.stock_score.stock.currency,
                "baseScore": item.stock_score.score,
                "decisionGrade": item.stock_score.decision_grade,
                "riskLevel": item.stock_score.risk_level,
                **_portfolio_fields(item.stock_score),
                **_short_term_to_dict(item),
            }
            for item in _short_term_entry_candidates(report.short_term_scores)
        ],
        "mediumTermCandidates": [
            {
                "ticker": item.stock_score.stock.ticker,
                "name": item.stock_score.stock.name,
                "industry": item.stock_score.stock.industry,
                "country": item.stock_score.stock.country,
                "currency": item.stock_score.stock.currency,
                "baseScore": item.stock_score.score,
                "decisionGrade": item.stock_score.decision_grade,
                "riskLevel": item.stock_score.risk_level,
                **_portfolio_fields(item.stock_score),
                **_medium_term_to_dict(item),
            }
            for item in report.medium_term_scores
        ],
        "longTermCandidates": [
            {
                "ticker": item.stock_score.stock.ticker,
                "name": item.stock_score.stock.name,
                "industry": item.stock_score.stock.industry,
                "country": item.stock_score.stock.country,
                "currency": item.stock_score.stock.currency,
                "baseScore": item.stock_score.score,
                "decisionGrade": item.stock_score.decision_grade,
                "riskLevel": item.stock_score.risk_level,
                **_portfolio_fields(item.stock_score),
                **_long_term_to_dict(item),
            }
            for item in report.long_term_scores
        ],
        "earlyGrowthCandidates": [
            {
                "ticker": item.stock_score.stock.ticker,
                "name": item.stock_score.stock.name,
                "industry": item.stock_score.stock.industry,
                "country": item.stock_score.stock.country,
                "currency": item.stock_score.stock.currency,
                "baseScore": item.stock_score.score,
                "decisionGrade": item.stock_score.decision_grade,
                "riskLevel": item.stock_score.risk_level,
                **_portfolio_fields(item.stock_score),
                **_early_growth_to_dict(item),
            }
            for item in report.early_growth_scores
        ],
        "news": [
            {
                "title": item.title,
                "source": item.source,
                "published": item.published,
                "url": item.url,
            }
            for item in report.news_items[:12]
        ],
    }


def _beneficiary_industry_to_dict(item) -> dict:
    profile = item.profile
    return {
        "name": profile.name,
        "description": profile.description,
        "sourceIndustry": profile.source_industry,
        "mechanism": profile.mechanism,
        "timeHorizon": profile.time_horizon,
        "keywords": list(profile.keywords),
        "risks": list(profile.risks),
        "marketProxies": [
            {
                "ticker": proxy.ticker,
                "name": proxy.name,
                "role": proxy.role,
                "weight": proxy.weight,
            }
            for proxy in profile.market_proxies
        ],
        "score": item.score,
        "sourceIndustryScore": item.source_industry_score,
        "connectionScore": item.connection_score,
        "macroScore": item.macro_score,
        "newsScore": item.news_score,
        "marketScore": item.market_score,
        "proxyMomentumScore": item.proxy_momentum_score,
        "proxyCoveragePct": item.proxy_coverage_pct,
        "newsRecentScore": item.news_recent_score,
        "newsBaselineScore": item.news_baseline_score,
        "newsAccelerationScore": item.news_acceleration_score,
        "newsCoverageLabel": item.news_coverage_label,
        "newsTopSources": list(item.news_top_sources),
        "evidence": list(item.evidence),
        "displaySummary": item.display_summary,
    }


def _technical_by_ticker(report: RecommendationReport) -> dict[str, dict]:
    results: dict[str, dict] = {}
    tickers = tuple(dict.fromkeys(item.stock.ticker.upper() for item in report.stock_scores))
    for ticker in tickers:
        results[ticker] = _momentum_technical_to_dict(report.momentums.get(ticker))
    return results


def _momentum_technical_to_dict(momentum) -> dict:
    return {
        "rsi14": _round_or_none(getattr(momentum, "rsi14", None)),
        "latestOpen": _round_or_none(getattr(momentum, "latest_open", None)),
        "latestHigh": _round_or_none(getattr(momentum, "latest_high", None)),
        "latestLow": _round_or_none(getattr(momentum, "latest_low", None)),
        "previousClose": _round_or_none(getattr(momentum, "previous_close", None)),
        "ma20DistancePct": _round_or_none(getattr(momentum, "ma20_distance_pct", None)),
        "ma60DistancePct": _round_or_none(getattr(momentum, "ma60_distance_pct", None)),
        "ma120DistancePct": _round_or_none(getattr(momentum, "ma120_distance_pct", None)),
        "ma150": _round_or_none(getattr(momentum, "ma150", None)),
        "ma200": _round_or_none(getattr(momentum, "ma200", None)),
        "ma150DistancePct": _round_or_none(getattr(momentum, "ma150_distance_pct", None)),
        "ma200DistancePct": _round_or_none(getattr(momentum, "ma200_distance_pct", None)),
        "volumeRatio": _round_or_none(getattr(momentum, "volume_ratio", None)),
        "twentyDayBreakoutPct": _round_or_none(getattr(momentum, "twenty_day_breakout_pct", None)),
        "sixtyDayBreakoutPct": _round_or_none(getattr(momentum, "sixty_day_breakout_pct", None)),
        "bollingerUpper": _round_or_none(getattr(momentum, "bollinger_upper", None)),
        "bollingerMiddle": _round_or_none(getattr(momentum, "bollinger_middle", None)),
        "bollingerLower": _round_or_none(getattr(momentum, "bollinger_lower", None)),
        "bollingerBandwidthPct": _round_or_none(getattr(momentum, "bollinger_bandwidth_pct", None)),
        "bollingerPercentB": _round_or_none(getattr(momentum, "bollinger_percent_b", None)),
        "volumeZoneLower": _round_or_none(getattr(momentum, "volume_zone_lower", None)),
        "volumeZoneUpper": _round_or_none(getattr(momentum, "volume_zone_upper", None)),
        "volumeZoneStrength": _round_or_none(getattr(momentum, "volume_zone_strength", None)),
        "volumeZoneContainsLatest": bool(getattr(momentum, "volume_zone_contains_latest", False)),
        "previousSwingHigh": _round_or_none(getattr(momentum, "previous_swing_high", None)),
        "previousSwingHighDistancePct": _round_or_none(getattr(momentum, "previous_swing_high_distance_pct", None)),
        "structureZoneLower": _round_or_none(getattr(momentum, "structure_zone_lower", None)),
        "structureZoneUpper": _round_or_none(getattr(momentum, "structure_zone_upper", None)),
        "structureZoneStrength": _round_or_none(getattr(momentum, "structure_zone_strength", None)),
        "supportRetestLower": _round_or_none(getattr(momentum, "support_retest_lower", None)),
        "supportRetestUpper": _round_or_none(getattr(momentum, "support_retest_upper", None)),
        "nearestResistance": _round_or_none(getattr(momentum, "nearest_resistance", None)),
        "majorResistance": _round_or_none(getattr(momentum, "major_resistance", None)),
        "rejectionFromStructureZone": bool(getattr(momentum, "rejection_from_structure_zone", False)),
        "supportRetestActive": bool(getattr(momentum, "support_retest_active", False)),
        "ohlcvCoveragePct": _round_or_none(getattr(momentum, "ohlcv_coverage_pct", None)),
        "trendLabel": _momentum_trend_label(momentum),
    }


def _momentum_trend_label(momentum) -> str:
    ma20_distance = getattr(momentum, "ma20_distance_pct", None)
    ma60_distance = getattr(momentum, "ma60_distance_pct", None)
    ma120_distance = getattr(momentum, "ma120_distance_pct", None)
    distances = (ma20_distance, ma60_distance, ma120_distance)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in distances):
        return "데이터 부족"
    if all(value > 0 for value in distances):
        return "상승 추세"
    if all(value < 0 for value in distances):
        return "하락 추세"
    return "중립"


def _round_or_none(value: object) -> float | None:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), 2)


def _report_payload_cache():
    from .config import load_config
    from .storage import CacheStore

    config = load_config()
    return CacheStore(config.cache_db_path)


def _legend_metric_coverage(report: RecommendationReport) -> dict[str, float]:
    candidates = report.legend_strategy_scores or report.stock_scores
    fundamentals = [
        item.stock_score.stock.fundamentals if hasattr(item, "stock_score") else item.stock.fundamentals
        for item in candidates[:20]
    ]
    return {
        "roicCoveragePct": _coverage_pct(fundamentals, "roic_pct"),
        "evEbitCoveragePct": _coverage_pct(fundamentals, "ev_to_ebit"),
        "rdCoveragePct": _coverage_pct(fundamentals, "rd_to_revenue_pct"),
        "growthQualityCoveragePct": _coverage_pct(fundamentals, "revenue_cagr_3y_pct"),
    }


def _coverage_pct(fundamentals: list, field: str) -> float:
    if not fundamentals:
        return 0.0
    covered = sum(1 for item in fundamentals if getattr(item, field, None) is not None)
    return round((covered / len(fundamentals)) * 100, 1)


def _valuation_range_to_dict(item) -> dict:
    valuation_range = item.valuation_range
    return {
        "profitMetric": valuation_range.profit_metric,
        "profitValue": valuation_range.profit_value,
        "multipleLow": valuation_range.multiple_low,
        "multipleHigh": valuation_range.multiple_high,
        "marketCapLow": valuation_range.market_cap_low,
        "marketCapHigh": valuation_range.market_cap_high,
        "upsideLowPct": valuation_range.upside_low_pct,
        "upsideHighPct": valuation_range.upside_high_pct,
        "note": valuation_range.note,
    }


def _portfolio_fields(item) -> dict:
    return {
        "riskGate": item.risk_gate,
        "riskGateReasons": list(item.risk_gate_reasons),
        "weightProfile": item.weight_profile,
        "portfolioSignal": item.portfolio_signal,
        "targetWeightPct": item.target_weight_pct,
        "maxWeightPct": item.max_weight_pct,
        "sellSignals": list(item.sell_signals),
    }


def _early_growth_to_dict(item) -> dict | None:
    if item is None:
        return None
    return {
        "score": item.score,
        "sizeScore": item.size_score,
        "growthScore": item.growth_score,
        "pullbackScore": item.pullback_score,
        "qualityAnchorScore": item.quality_anchor_score,
        "valuationAnchorScore": item.valuation_anchor_score,
        "entryLabel": item.entry_label,
        "reasons": list(item.reasons),
        "cautions": list(item.cautions),
    }


def _short_term_to_dict(item) -> dict | None:
    if item is None:
        return None
    return {
        "score": item.score,
        "newsScore": item.news_score,
        "marketScore": item.market_score,
        "chartScore": item.chart_score,
        "volumeScore": item.volume_score,
        "companyScore": item.company_score,
        "confidenceScore": item.confidence_score,
        "confidenceLabel": item.confidence_label,
        "signalLabel": item.signal_label,
        "setupLabel": item.setup_label,
        "timeHorizon": item.time_horizon,
        "reasons": list(item.reasons),
        "cautions": list(item.cautions),
        "tradeSignal": _trade_signal_to_dict(item.trade_signal),
    }


def _short_term_entry_candidates(items) -> tuple:
    return tuple(
        item
        for item in items
        if item.trade_signal is not None and item.trade_signal.action in {"buy", "scale_buy"}
    )


def _medium_term_to_dict(item) -> dict | None:
    if item is None:
        return None
    return {
        "score": item.score,
        "companyScore": item.company_score,
        "marketScore": item.market_score,
        "chartScore": item.chart_score,
        "newsScore": item.news_score,
        "confidenceScore": item.confidence_score,
        "confidenceLabel": item.confidence_label,
        "signalLabel": item.signal_label,
        "timeHorizon": item.time_horizon,
        "reasons": list(item.reasons),
        "cautions": list(item.cautions),
        "tradeSignal": _trade_signal_to_dict(item.trade_signal),
    }


def _trade_signal_to_dict(item) -> dict | None:
    if item is None:
        return None
    return {
        "horizon": item.horizon,
        "action": item.action,
        "label": item.label,
        "score": item.score,
        "confidence": item.confidence,
        "setup": item.setup,
        "reasons": list(item.reasons),
        "cautions": list(item.cautions),
        "referencePrice": item.reference_price,
        "ma150": item.ma150,
        "ma200": item.ma200,
        "bollingerUpper": item.bollinger_upper,
        "bollingerMiddle": item.bollinger_middle,
        "bollingerLower": item.bollinger_lower,
        "volumeZoneLower": item.volume_zone_lower,
        "volumeZoneUpper": item.volume_zone_upper,
        "volumeZoneStrength": item.volume_zone_strength,
        "targetPrice": item.target_price,
        "targetType": item.target_type,
        "partialTakeProfitPct": item.partial_take_profit_pct,
        "finalTakeProfitPct": item.final_take_profit_pct,
        "entryZoneLower": item.entry_zone_lower,
        "entryZoneUpper": item.entry_zone_upper,
        "target1Price": item.target1_price,
        "target1Type": item.target1_type,
        "target2Price": item.target2_price,
        "target2Type": item.target2_type,
        "positionPlan": item.position_plan,
        "structureSetup": item.structure_setup,
        "remainingExitRule": item.remaining_exit_rule,
        "invalidationRule": item.invalidation_rule,
    }


def _long_term_to_dict(item) -> dict | None:
    if item is None:
        return None
    return {
        "score": item.score,
        "companyScore": item.company_score,
        "marketScore": item.market_score,
        "chartScore": item.chart_score,
        "newsScore": item.news_score,
        "confidenceScore": item.confidence_score,
        "confidenceLabel": item.confidence_label,
        "signalLabel": item.signal_label,
        "timeHorizon": item.time_horizon,
        "reasons": list(item.reasons),
        "cautions": list(item.cautions),
    }


def _legend_strategy_stock_fields(item) -> dict:
    if item is None:
        return {
            "legendScores": None,
            "legendCompositeScore": None,
            "legendReasons": [],
            "legendWarnings": [],
        }
    payload = _legend_strategy_to_dict(item)
    return {
        "legendScores": payload["legendScores"],
        "legendCompositeScore": payload["legendCompositeScore"],
        "legendReasons": payload["legendReasons"],
        "legendWarnings": payload["legendWarnings"],
    }


def _legend_strategy_to_dict(item) -> dict:
    return {
        "legendCompositeScore": item.composite_score,
        "legendScores": {
            "lynch": item.lynch_score,
            "oneil": item.oneil_score,
            "greenblatt": item.greenblatt_score,
            "fisher": item.fisher_score,
        },
        "legendReasons": list(item.reasons),
        "legendWarnings": list(item.warnings),
    }


def _macro_snapshot_to_dict(report: RecommendationReport) -> dict | None:
    if report.macro_snapshot is None:
        return None
    snapshot = report.macro_snapshot
    return {
        "summary": snapshot.summary,
        "investmentGuidance": list(snapshot.investment_guidance),
        "growthScore": snapshot.growth_score,
        "defensiveScore": snapshot.defensive_score,
        "infrastructureScore": snapshot.infrastructure_score,
        "koreaFxScore": snapshot.korea_fx_score,
        "warnings": list(snapshot.warnings),
        "indicators": [
            {
                "name": item.name,
                "value": item.value,
                "unit": item.unit,
                "latestDate": item.latest_date,
                "source": item.source,
                "note": item.note,
            }
            for item in snapshot.indicators
        ],
    }


def _timezone_name(value) -> str:
    if value.tzinfo is None:
        return ""
    return getattr(value.tzinfo, "key", None) or value.tzname() or str(value.tzinfo)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "StockRecommenderWeb/0.1"

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8", include_body=False)
            return
        if parsed.path == "/favicon.svg":
            self._serve_file(WEB_DIR / "favicon.svg", "image/svg+xml", include_body=False)
            return
        if parsed.path.startswith("/assets/"):
            asset_path = _asset_path(parsed.path)
            if asset_path is None:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self._serve_file(asset_path, include_body=False)
            return
        if parsed.path == "/api/report":
            self._send_json({}, include_body=False)
            return
        if parsed.path == "/api/backtest":
            self._send_json({}, include_body=False)
            return
        if parsed.path == "/api/snapshots":
            self._send_json({}, include_body=False)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return

        if parsed.path == "/favicon.svg":
            self._serve_file(WEB_DIR / "favicon.svg", "image/svg+xml")
            return

        if parsed.path == "/api/report":
            query = parse_qs(parsed.query)
            macro_context = query.get("macro", [DEFAULT_MACRO_CONTEXT])[0] or DEFAULT_MACRO_CONTEXT
            force_refresh = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
            try:
                payload = create_report_payload(macro_context=macro_context, force_refresh=force_refresh)
            except Exception as exc:  # pragma: no cover - defensive server boundary
                _record_api_error("web/report", exc)
                self._send_json({"error": "추천 리포트를 생성하지 못했습니다."}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json(payload)
            return

        if parsed.path == "/api/backtest":
            query = parse_qs(parsed.query)
            months = _int_query(query, "months", 12)
            top_n = _int_query(query, "top", 5)
            benchmark = query.get("benchmark", ["SPY"])[0].upper()
            method = query.get("method", ["snapshot"])[0].lower()
            horizon = query.get("horizon", ["overall"])[0].lower()
            if benchmark not in BENCHMARKS:
                benchmark = "SPY"
            if method not in BACKTEST_METHODS:
                method = "snapshot"
            if horizon not in BACKTEST_HORIZONS:
                horizon = "overall"
            try:
                payload = backtest_to_dict(
                    create_backtest(
                        months=months,
                        top_n=top_n,
                        benchmark_ticker=benchmark,
                        method=method,
                        horizon=horizon,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive server boundary
                _record_api_error("web/backtest", exc)
                self._send_json({"error": "백테스트를 생성하지 못했습니다."}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json(payload)
            return

        if parsed.path == "/api/snapshots":
            query = parse_qs(parsed.query)
            limit = _int_query(query, "limit", 30)
            try:
                payload = snapshot_history(limit=limit)
            except Exception as exc:  # pragma: no cover - defensive server boundary
                _record_api_error("web/snapshots", exc)
                self._send_json({"error": "스냅샷 기록을 불러오지 못했습니다."}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json(payload)
            return

        if parsed.path.startswith("/assets/"):
            asset_path = _asset_path(parsed.path)
            if asset_path is None:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self._serve_file(asset_path)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _serve_file(
        self, path: Path, content_type: str | None = None, include_body: bool = True
    ) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        guessed_type = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", guessed_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if include_body:
            self.wfile.write(content)

    def _send_json(
        self, payload: dict, status: HTTPStatus = HTTPStatus.OK, include_body: bool = True
    ) -> None:
        send_json_response(self, payload, status=status, include_body=include_body)


def _asset_path(request_path: str) -> Path | None:
    requested = request_path.removeprefix("/assets/")
    assets_dir = (WEB_DIR / "assets").resolve()
    asset_path = (assets_dir / requested).resolve()
    if asset_path.is_relative_to(assets_dir):
        return asset_path
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="주식 추천 대시보드 웹 서버를 실행합니다.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{server.server_port}"
    print(f"대시보드 실행 중: {url}")
    print("종료하려면 Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
