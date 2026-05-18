from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import load_config
from .models import RecommendationReport
from .storage import CacheStore


@dataclass(frozen=True)
class SavedSnapshot:
    id: int
    snapshot_date: str
    mode: str
    top_ticker: str | None
    top_name: str | None
    top_score: float | None


def save_recommendation_snapshot(report: RecommendationReport, mode: str) -> SavedSnapshot:
    config = load_config()
    cache = CacheStore(config.cache_db_path)
    payload = report_to_snapshot_payload(report, mode=mode)
    top_stock = report.stock_scores[0] if report.stock_scores else None
    snapshot_id = cache.save_recommendation_snapshot(
        snapshot_date=payload["snapshotDate"],
        mode=mode,
        top_ticker=top_stock.stock.ticker if top_stock else None,
        top_name=top_stock.stock.name if top_stock else None,
        top_score=top_stock.score if top_stock else None,
        payload=payload,
    )
    return SavedSnapshot(
        id=snapshot_id,
        snapshot_date=payload["snapshotDate"],
        mode=mode,
        top_ticker=top_stock.stock.ticker if top_stock else None,
        top_name=top_stock.stock.name if top_stock else None,
        top_score=top_stock.score if top_stock else None,
    )


def snapshot_history(limit: int = 30) -> dict:
    config = load_config()
    cache = CacheStore(config.cache_db_path)
    rows = cache.list_recommendation_snapshots(limit=limit)
    unique_dates = sorted({row["snapshotDate"] for row in rows})
    latest = rows[0] if rows else None
    return {
        "snapshotCount": len(rows),
        "uniqueDays": len(unique_dates),
        "latest": _summary_row(latest) if latest else None,
        "coverageLabel": _coverage_label(len(unique_dates)),
        "readinessScore": _readiness_score(len(unique_dates)),
        "minimumDaysForPointInTimeBacktest": 30,
        "rows": [_summary_row(row) for row in rows],
    }


def report_to_snapshot_payload(report: RecommendationReport, mode: str) -> dict:
    created_at = report.created_at
    return {
        "version": 5,
        "mode": mode,
        "snapshotDate": created_at.date().isoformat(),
        "createdAt": created_at.isoformat(),
        "createdAtDisplay": created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "macroContext": report.macro_context,
        "dataQuality": {
            "liveNews": report.data_quality.live_news,
            "liveMarketData": report.data_quality.live_market_data,
            "liveFundamentals": report.data_quality.live_fundamentals,
            "liveMacro": report.data_quality.live_macro,
            "liveKoreaFundamentals": report.data_quality.live_korea_fundamentals,
            "configuredSources": list(report.data_quality.configured_sources),
            "missingSources": list(report.data_quality.missing_sources),
            "warnings": list(report.data_quality.warnings),
        },
        "macroSnapshot": _macro_snapshot_payload(report),
        "industries": [
            {
                "name": item.industry.name,
                "score": item.score,
                "newsScore": item.news_score,
                "macroScore": item.macro_score,
                "marketScore": item.market_score,
                "evidence": list(item.evidence),
            }
            for item in report.industry_scores
        ],
        "stocks": [
            {
                "ticker": item.stock.ticker,
                "name": item.stock.name,
                "country": item.stock.country,
                "currency": item.stock.currency,
                "industry": item.stock.industry,
                "role": item.stock.role,
                "score": item.score,
                "industryScore": item.industry_score,
                "qualityScore": item.quality_score,
                "valuationScore": item.valuation_score,
                "momentumScore": item.momentum_score,
                "roleScore": item.role_score,
                "decisionGrade": item.decision_grade,
                "riskLevel": item.risk_level,
                "valuationLabel": item.valuation_label,
                "analysisStyle": item.analysis_style,
                "valuationNote": item.valuation_note,
                "valuationRange": _valuation_range_payload(item),
                "analysisChecks": list(item.analysis_checks),
                "secondOrderChecks": list(item.second_order_checks),
                "reasons": list(item.reasons),
                "cautions": list(item.cautions),
                "recentIssues": list(item.stock.recent_issues),
                "fundamentals": {
                    "revenueGrowthPct": item.stock.fundamentals.revenue_growth_pct,
                    "operatingMarginPct": item.stock.fundamentals.operating_margin_pct,
                    "roePct": item.stock.fundamentals.roe_pct,
                    "debtToEquityPct": item.stock.fundamentals.debt_to_equity_pct,
                    "pe": item.stock.fundamentals.pe,
                    "forwardPe": item.stock.fundamentals.forward_pe,
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
                },
            }
            for item in report.stock_scores
        ],
        "earlyGrowthCandidates": [
            {
                "ticker": item.stock_score.stock.ticker,
                "name": item.stock_score.stock.name,
                "country": item.stock_score.stock.country,
                "industry": item.stock_score.stock.industry,
                "score": item.score,
                "baseScore": item.stock_score.score,
                "entryLabel": item.entry_label,
                "sizeScore": item.size_score,
                "growthScore": item.growth_score,
                "pullbackScore": item.pullback_score,
                "qualityAnchorScore": item.quality_anchor_score,
                "valuationAnchorScore": item.valuation_anchor_score,
                "decisionGrade": item.stock_score.decision_grade,
                "riskLevel": item.stock_score.risk_level,
                "reasons": list(item.reasons),
                "cautions": list(item.cautions),
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
            for item in report.news_items[:20]
        ],
    }


def _macro_snapshot_payload(report: RecommendationReport) -> dict | None:
    if report.macro_snapshot is None:
        return None
    snapshot = report.macro_snapshot
    return {
        "summary": snapshot.summary,
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


def _valuation_range_payload(item) -> dict:
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


def _summary_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    payload = row.get("payload", {})
    stocks = payload.get("stocks", [])
    top_stocks = stocks[:5] if isinstance(stocks, list) else []
    created_at = payload.get("createdAtDisplay") or _display_created_at(payload.get("createdAt")) or _display_created_at(row.get("createdAt"))
    return {
        "id": row.get("id"),
        "snapshotDate": row.get("snapshotDate"),
        "createdAt": created_at,
        "mode": row.get("mode"),
        "topTicker": row.get("topTicker"),
        "topName": row.get("topName"),
        "topScore": row.get("topScore"),
        "topStocks": [
            {
                "ticker": item.get("ticker"),
                "name": item.get("name"),
                "score": item.get("score"),
                "decisionGrade": item.get("decisionGrade"),
                "riskLevel": item.get("riskLevel"),
                "analysisStyle": item.get("analysisStyle"),
            }
            for item in top_stocks
            if isinstance(item, dict)
        ],
        "configuredSources": payload.get("dataQuality", {}).get("configuredSources", []),
        "liveCoverage": {
            "news": payload.get("dataQuality", {}).get("liveNews", False),
            "market": payload.get("dataQuality", {}).get("liveMarketData", False),
            "fundamentals": payload.get("dataQuality", {}).get("liveFundamentals", False),
            "macro": payload.get("dataQuality", {}).get("liveMacro", False),
        },
    }


def _display_created_at(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def _readiness_score(unique_days: int) -> float:
    return round(min(unique_days / 30 * 100, 100), 1)


def _coverage_label(unique_days: int) -> str:
    if unique_days >= 60:
        return "검증 데이터 충분"
    if unique_days >= 30:
        return "기초 백테스트 가능"
    if unique_days >= 7:
        return "초기 추적 중"
    if unique_days >= 1:
        return "기록 시작"
    return "기록 없음"
