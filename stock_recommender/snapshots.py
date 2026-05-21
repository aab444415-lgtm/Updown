from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from . import __version__
from .config import load_config
from .models import Momentum, RecommendationReport
from .snapshot_store import list_snapshot_rows, save_persistent_snapshot
from .storage import CacheStore


SNAPSHOT_PAYLOAD_VERSION = 11
BENCHMARK_TICKERS = ("SPY", "QQQ", "^KS11")
FUNDAMENTAL_SOURCE_FIELDS = (
    "revenue",
    "operatingIncome",
    "marketCap",
    "pe",
    "forwardPe",
    "freeCashFlow",
    "netIncome",
    "operatingCashFlow",
)
SECRET_QUERY_KEYS = {"api_key", "apikey", "crtfc_key", "key", "token", "secret", "access_token"}
LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
SECRET_PAIR_RE = re.compile(r"(?i)\b(api_key|apikey|crtfc_key|key|token|secret|access_token)=([^&\s]+)")


@dataclass(frozen=True)
class SavedSnapshot:
    id: int
    snapshot_date: str
    mode: str
    top_ticker: str | None
    top_name: str | None
    top_score: float | None


def _save_full_snapshot_artifact(path: Path | None, payload: dict, mode: str) -> None:
    if path is None:
        return
    path.mkdir(parents=True, exist_ok=True)
    digest = _payload_digest(payload).split(":", 1)[-1][:12]
    filename = f"{payload.get('snapshotDate', 'unknown')}-{mode}-{digest}.json"
    (path / filename).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_recommendation_snapshot(report: RecommendationReport, mode: str = "live") -> SavedSnapshot:
    config = load_config()
    cache = CacheStore(config.cache_db_path)
    payload = report_to_snapshot_payload(report, mode=mode)
    _save_full_snapshot_artifact(config.full_snapshot_dir, payload, mode=mode)
    top_stock = report.stock_scores[0] if report.stock_scores else None
    snapshot_id = cache.save_recommendation_snapshot(
        snapshot_date=payload["snapshotDate"],
        mode=mode,
        top_ticker=top_stock.stock.ticker if top_stock else None,
        top_name=top_stock.stock.name if top_stock else None,
        top_score=top_stock.score if top_stock else None,
        payload=payload,
    )
    persistent_id = 0
    if config.persist_repo_ledger:
        persistent_id = save_persistent_snapshot(
            config,
            snapshot_date=payload["snapshotDate"],
            mode=mode,
            top_ticker=top_stock.stock.ticker if top_stock else None,
            top_name=top_stock.stock.name if top_stock else None,
            top_score=top_stock.score if top_stock else None,
            payload=payload,
        )
    return SavedSnapshot(
        id=persistent_id or snapshot_id,
        snapshot_date=payload["snapshotDate"],
        mode=mode,
        top_ticker=top_stock.stock.ticker if top_stock else None,
        top_name=top_stock.stock.name if top_stock else None,
        top_score=top_stock.score if top_stock else None,
    )


def snapshot_history(limit: int = 30) -> dict:
    config = load_config()
    cache = CacheStore(config.cache_db_path)
    rows = list_snapshot_rows(config, cache, limit=limit, mode="live")
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


def report_to_snapshot_payload(report: RecommendationReport, mode: str = "live") -> dict:
    created_at = report.created_at
    source_events = _source_events_payload(report.source_events)
    legend_by_ticker = {
        item.stock_score.stock.ticker.upper(): item for item in report.legend_strategy_scores
    }
    payload = {
        "version": SNAPSHOT_PAYLOAD_VERSION,
        "mode": mode,
        "snapshotDate": created_at.date().isoformat(),
        "createdAt": created_at.isoformat(),
        "createdAtDisplay": created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "createdAtTimezone": _timezone_name(created_at),
        "audit": _audit_payload(created_at),
        "sourceEvents": source_events,
        "sourceEventSummary": _source_event_summary(source_events),
        "macroContext": report.macro_context,
        "dataQuality": {
            "liveNews": report.data_quality.live_news,
            "liveMarketData": report.data_quality.live_market_data,
            "liveFundamentals": report.data_quality.live_fundamentals,
            "liveMacro": report.data_quality.live_macro,
            "liveKoreaFundamentals": report.data_quality.live_korea_fundamentals,
            "configuredSources": list(report.data_quality.configured_sources),
            "missingSources": list(report.data_quality.missing_sources),
            "warnings": [_redact_text(item) for item in report.data_quality.warnings],
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
                },
                "fundamentalSources": _fundamental_sources(item.stock.fundamentals.sources),
                "momentumRaw": _momentum_payload(report.momentums.get(item.stock.ticker.upper())),
                "priceAnchor": _price_anchor_payload(
                    report.momentums.get(item.stock.ticker.upper()),
                    currency=item.stock.currency,
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
                "country": item.stock_score.stock.country,
                "industry": item.stock_score.stock.industry,
                "score": item.composite_score,
                "baseScore": item.stock_score.score,
                "decisionGrade": item.stock_score.decision_grade,
                "riskLevel": item.stock_score.risk_level,
                **_legend_strategy_to_dict(item),
            }
            for item in report.legend_strategy_scores
        ],
        "benchmarks": [
            {
                "ticker": ticker,
                "priceAnchor": _price_anchor_payload(report.momentums.get(ticker), currency=_benchmark_currency(ticker)),
            }
            for ticker in BENCHMARK_TICKERS
        ],
        "priceAnchors": _price_anchors_payload(report),
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
        "shortTermCandidates": [
            {
                "ticker": item.stock_score.stock.ticker,
                "name": item.stock_score.stock.name,
                "country": item.stock_score.stock.country,
                "industry": item.stock_score.stock.industry,
                "score": item.score,
                "baseScore": item.stock_score.score,
                "signalLabel": item.signal_label,
                "timeHorizon": item.time_horizon,
                "newsScore": item.news_score,
                "marketScore": item.market_score,
                "chartScore": item.chart_score,
                "volumeScore": item.volume_score,
                "companyScore": item.company_score,
                "confidenceScore": item.confidence_score,
                "confidenceLabel": item.confidence_label,
                "setupLabel": item.setup_label,
                "decisionGrade": item.stock_score.decision_grade,
                "riskLevel": item.stock_score.risk_level,
                "reasons": list(item.reasons),
                "cautions": list(item.cautions),
            }
            for item in report.short_term_scores
        ],
        "mediumTermCandidates": [
            {
                "ticker": item.stock_score.stock.ticker,
                "name": item.stock_score.stock.name,
                "country": item.stock_score.stock.country,
                "industry": item.stock_score.stock.industry,
                "score": item.score,
                "baseScore": item.stock_score.score,
                "signalLabel": item.signal_label,
                "timeHorizon": item.time_horizon,
                "companyScore": item.company_score,
                "marketScore": item.market_score,
                "chartScore": item.chart_score,
                "newsScore": item.news_score,
                "confidenceScore": item.confidence_score,
                "confidenceLabel": item.confidence_label,
                "decisionGrade": item.stock_score.decision_grade,
                "riskLevel": item.stock_score.risk_level,
                "reasons": list(item.reasons),
                "cautions": list(item.cautions),
            }
            for item in report.medium_term_scores
        ],
        "longTermCandidates": [
            {
                "ticker": item.stock_score.stock.ticker,
                "name": item.stock_score.stock.name,
                "country": item.stock_score.stock.country,
                "industry": item.stock_score.stock.industry,
                "score": item.score,
                "baseScore": item.stock_score.score,
                "signalLabel": item.signal_label,
                "timeHorizon": item.time_horizon,
                "companyScore": item.company_score,
                "marketScore": item.market_score,
                "chartScore": item.chart_score,
                "newsScore": item.news_score,
                "confidenceScore": item.confidence_score,
                "confidenceLabel": item.confidence_label,
                "decisionGrade": item.stock_score.decision_grade,
                "riskLevel": item.stock_score.risk_level,
                "reasons": list(item.reasons),
                "cautions": list(item.cautions),
            }
            for item in report.long_term_scores
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
    payload["snapshotQuality"] = _snapshot_quality(payload)
    return payload


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
        "legendWarnings": [_redact_text(text) for text in item.warnings],
    }


def _macro_snapshot_payload(report: RecommendationReport) -> dict | None:
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
        "warnings": [_redact_text(item) for item in snapshot.warnings],
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


def _audit_payload(created_at: datetime) -> dict:
    commit = os.environ.get("GITHUB_SHA") or _git_output(("rev-parse", "HEAD"))
    dirty = _git_dirty()
    if commit is None:
        dirty = True
    return {
        "modelVersion": f"stock-recommender/{__version__}",
        "gitCommit": commit,
        "gitDirty": dirty,
        "runId": os.environ.get("GITHUB_RUN_ID") or f"local-{created_at.strftime('%Y%m%dT%H%M%S')}",
        "createdAt": created_at.isoformat(),
        "createdAtTimezone": _timezone_name(created_at),
    }


def _git_output(args: tuple[str, ...]) -> str | None:
    try:
        result = subprocess.run(
            ("git", *args),
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _git_dirty() -> bool:
    status = _git_output(("status", "--porcelain"))
    if status is None:
        return True
    return bool(status.strip())


def _source_events_payload(events: tuple[dict, ...]) -> list[dict]:
    payload: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        payload.append(
            {
                "source": str(event.get("source") or "unknown"),
                "eventType": _event_type(event.get("eventType")),
                "message": _redact_text(str(event.get("message") or "")),
                "createdAt": str(event.get("createdAt") or ""),
                "metadata": _redact_metadata(event.get("metadata")),
            }
        )
    return payload


def _event_type(value: object) -> str:
    event_type = str(value or "warning").lower()
    return event_type if event_type in {"success", "warning", "error", "stale"} else "warning"


def _source_event_summary(events: list[dict]) -> dict:
    by_status = Counter(str(event.get("eventType") or "warning") for event in events)
    by_source: dict[str, dict[str, int]] = {}
    for event in events:
        source = str(event.get("source") or "unknown")
        event_type = str(event.get("eventType") or "warning")
        by_source.setdefault(source, {})
        by_source[source][event_type] = by_source[source].get(event_type, 0) + 1
    return {
        "total": len(events),
        "byStatus": dict(sorted(by_status.items())),
        "bySource": {source: dict(sorted(counts.items())) for source, counts in sorted(by_source.items())},
        "staleCount": by_status.get("stale", 0),
        "errorCount": by_status.get("error", 0),
    }


def _redact_text(value: str) -> str:
    value = SECRET_PAIR_RE.sub(lambda match: f"{match.group(1)}=***", value)
    value = LONG_TOKEN_RE.sub("***", value)
    words = value.split()
    redacted_words = [_redact_url(word) if word.startswith(("http://", "https://")) else word for word in words]
    return " ".join(redacted_words)


def _redact_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe_pairs = [
        (key, "***" if key.lower() in SECRET_QUERY_KEYS else _redact_scalar(text))
        for key, text in pairs
    ]
    path_parts = [
        "***" if len(part) >= 24 and part.replace("_", "").replace("-", "").isalnum() else part
        for part in parsed.path.split("/")
    ]
    return urllib.parse.urlunparse(
        parsed._replace(path="/".join(path_parts), query=urllib.parse.urlencode(safe_pairs))
    )


def _redact_scalar(value: str) -> str:
    return LONG_TOKEN_RE.sub("***", value)


def _redact_metadata(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    redacted: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, str):
            redacted[str(key)] = _redact_text(item)
        elif isinstance(item, dict):
            redacted[str(key)] = _redact_metadata(item)
        elif isinstance(item, list):
            redacted[str(key)] = [
                _redact_text(entry) if isinstance(entry, str) else entry for entry in item
            ]
        elif isinstance(item, (int, float, bool)) or item is None:
            redacted[str(key)] = item
    return redacted


def _payload_digest(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _fundamental_sources(sources: dict[str, dict]) -> dict:
    return {
        field: _source_payload(sources.get(field), field)
        for field in FUNDAMENTAL_SOURCE_FIELDS
    }


def _source_payload(source: object, field: str) -> dict:
    if not isinstance(source, dict):
        return _universe_fallback_source(field)
    name = source.get("source")
    if not isinstance(name, str) or not name:
        return _universe_fallback_source(field)
    return {
        "source": name,
        "periodEnd": _optional_scalar(source.get("periodEnd")),
        "fiscalYear": _optional_scalar(source.get("fiscalYear")),
        "filed": _optional_scalar(source.get("filed")),
        "form": _optional_scalar(source.get("form")),
        "reportCode": _optional_scalar(source.get("reportCode")),
        "fallback": bool(source.get("fallback", False)),
        **({"tag": source["tag"]} if isinstance(source.get("tag"), str) else {}),
        **({"derivedFrom": list(source["derivedFrom"])} if isinstance(source.get("derivedFrom"), list) else {}),
    }


def _universe_fallback_source(field: str) -> dict:
    return {
        "source": "universeFallback",
        "field": field,
        "periodEnd": None,
        "fiscalYear": None,
        "filed": None,
        "form": None,
        "reportCode": None,
        "fallback": True,
    }


def _optional_scalar(value: object) -> str | int | float | None:
    return value if isinstance(value, (str, int, float)) else None


def _momentum_payload(momentum: Momentum | None) -> dict:
    momentum = momentum or Momentum()
    return {
        "oneMonthPct": momentum.one_month_pct,
        "threeMonthPct": momentum.three_month_pct,
        "sixMonthPct": momentum.six_month_pct,
        "drawdownFromHighPct": momentum.drawdown_from_high_pct,
        "rangePositionPct": momentum.range_position_pct,
        "latestClose": momentum.latest_close,
        "latestCloseDate": momentum.latest_close_date,
        "sixMonthHigh": momentum.six_month_high,
        "sixMonthLow": momentum.six_month_low,
        "ma20": momentum.ma20,
        "ma60": momentum.ma60,
        "ma120": momentum.ma120,
        "rsi14": momentum.rsi14,
        "ma20DistancePct": momentum.ma20_distance_pct,
        "ma60DistancePct": momentum.ma60_distance_pct,
        "ma120DistancePct": momentum.ma120_distance_pct,
        "ma20SlopePct": momentum.ma20_slope_pct,
        "ma60SlopePct": momentum.ma60_slope_pct,
        "latestVolume": momentum.latest_volume,
        "avgVolume20": momentum.avg_volume_20,
        "volumeRatio": momentum.volume_ratio,
        "twentyDayBreakoutPct": momentum.twenty_day_breakout_pct,
        "sixtyDayBreakoutPct": momentum.sixty_day_breakout_pct,
        "source": momentum.source,
        "stale": momentum.stale,
    }


def _price_anchor_payload(momentum: Momentum | None, currency: str) -> dict:
    momentum = momentum or Momentum()
    return {
        "latestClose": momentum.latest_close,
        "latestCloseDate": momentum.latest_close_date,
        "currency": currency,
        "source": momentum.source,
        "stale": momentum.stale,
    }


def _price_anchors_payload(report: RecommendationReport) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for item in report.stock_scores:
        ticker = item.stock.ticker.upper()
        seen.add(ticker)
        rows.append(
            {
                "ticker": ticker,
                "priceAnchor": _price_anchor_payload(report.momentums.get(ticker), currency=item.stock.currency),
            }
        )
    for ticker in BENCHMARK_TICKERS:
        if ticker in seen:
            continue
        rows.append(
            {
                "ticker": ticker,
                "priceAnchor": _price_anchor_payload(report.momentums.get(ticker), currency=_benchmark_currency(ticker)),
            }
        )
    return rows


def _benchmark_currency(ticker: str) -> str:
    return "KRW" if ticker == "^KS11" else "USD"


def _snapshot_quality(payload: dict) -> dict:
    price_anchor_coverage = _price_anchor_coverage_pct(payload)
    benchmark_anchor_coverage = _benchmark_anchor_coverage_pct(payload)
    fundamental_source_coverage = _fundamental_source_coverage_pct(payload)
    source_summary = payload.get("sourceEventSummary")
    source_error_count = (
        int(source_summary.get("errorCount") or 0) if isinstance(source_summary, dict) else 0
    )
    source_stale_count = (
        int(source_summary.get("staleCount") or 0) if isinstance(source_summary, dict) else 0
    )
    exclusion_reasons: list[str] = []
    if price_anchor_coverage < 80:
        exclusion_reasons.append("priceAnchorCoverageBelow80")
    if benchmark_anchor_coverage < 100:
        exclusion_reasons.append("benchmarkAnchorCoverageBelow100")
    return {
        "priceAnchorCoveragePct": price_anchor_coverage,
        "benchmarkAnchorCoveragePct": benchmark_anchor_coverage,
        "fundamentalSourceCoveragePct": fundamental_source_coverage,
        "sourceErrorCount": source_error_count,
        "sourceStaleCount": source_stale_count,
        "backtestEligible": not exclusion_reasons,
        "exclusionReasons": exclusion_reasons,
    }


def _summary_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    payload = row.get("payload", {})
    payload_kind = str(row.get("payloadKind") or payload.get("payloadKind") or "full")
    payload_digest = row.get("payloadDigest") or payload.get("payloadDigest") or _payload_digest(payload)
    snapshot_quality = payload.get("snapshotQuality")
    if not isinstance(snapshot_quality, dict):
        snapshot_quality = _snapshot_quality(payload)
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
        "payloadKind": payload_kind,
        "payloadDigest": payload_digest,
        "snapshotQuality": snapshot_quality,
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
        "gitCommit": payload.get("audit", {}).get("gitCommit"),
        "sourceEventSummary": payload.get("sourceEventSummary") or _source_event_summary([]),
        "priceAnchorCoveragePct": snapshot_quality.get("priceAnchorCoveragePct", 0),
        "fundamentalSourceCoveragePct": snapshot_quality.get("fundamentalSourceCoveragePct", 0),
        "liveCoverage": {
            "news": payload.get("dataQuality", {}).get("liveNews", False),
            "market": payload.get("dataQuality", {}).get("liveMarketData", False),
            "fundamentals": payload.get("dataQuality", {}).get("liveFundamentals", False),
            "macro": payload.get("dataQuality", {}).get("liveMacro", False),
        },
    }


def _price_anchor_coverage_pct(payload: dict) -> float:
    stocks = payload.get("stocks")
    if not isinstance(stocks, list) or not stocks:
        return 0
    covered = 0
    for stock in stocks:
        if not isinstance(stock, dict):
            continue
        anchor = stock.get("priceAnchor")
        if isinstance(anchor, dict) and anchor.get("latestClose") is not None and anchor.get("latestCloseDate"):
            covered += 1
    return round(covered / len(stocks) * 100, 1)


def _benchmark_anchor_coverage_pct(payload: dict) -> float:
    benchmarks = payload.get("benchmarks")
    if not isinstance(benchmarks, list):
        return 0
    by_ticker = {
        str(item.get("ticker") or "").upper(): item
        for item in benchmarks
        if isinstance(item, dict)
    }
    covered = 0
    for ticker in BENCHMARK_TICKERS:
        item = by_ticker.get(ticker)
        anchor = item.get("priceAnchor") if isinstance(item, dict) else None
        if isinstance(anchor, dict) and anchor.get("latestClose") is not None and anchor.get("latestCloseDate"):
            covered += 1
    return round(covered / len(BENCHMARK_TICKERS) * 100, 1)


def _fundamental_source_coverage_pct(payload: dict) -> float:
    stocks = payload.get("stocks")
    if not isinstance(stocks, list) or not stocks:
        return 0
    total = 0
    covered = 0
    for stock in stocks:
        if not isinstance(stock, dict):
            continue
        sources = stock.get("fundamentalSources")
        if not isinstance(sources, dict):
            total += len(FUNDAMENTAL_SOURCE_FIELDS)
            continue
        for field in FUNDAMENTAL_SOURCE_FIELDS:
            total += 1
            source = sources.get(field)
            if isinstance(source, dict) and not source.get("fallback"):
                covered += 1
    return round(covered / total * 100, 1) if total else 0


def _display_created_at(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def _timezone_name(value: datetime) -> str:
    if value.tzinfo is None:
        return ""
    return getattr(value.tzinfo, "key", None) or value.tzname() or str(value.tzinfo)


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
