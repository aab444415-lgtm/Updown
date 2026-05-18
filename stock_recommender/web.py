from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .backtest import BACKTEST_METHODS, BENCHMARKS, backtest_to_dict, create_backtest, fetch_price_history
from .config import load_config
from .models import RecommendationReport
from .pipeline import create_recommendation_report
from .snapshots import snapshot_history
from .storage import CacheStore
from .technical import build_technical_snapshot, technical_snapshot_to_dict
from .universe import DEFAULT_MACRO_CONTEXT


WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_report(macro_context: str = DEFAULT_MACRO_CONTEXT) -> RecommendationReport:
    return create_recommendation_report(macro_context=macro_context)


def report_to_dict(report: RecommendationReport) -> dict:
    technical_by_ticker = _technical_by_ticker(report)
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
            "configuredSources": list(report.data_quality.configured_sources),
            "missingSources": list(report.data_quality.missing_sources),
            "warnings": list(report.data_quality.warnings),
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
        "stocks": [
            {
                "ticker": item.stock.ticker,
                "name": item.stock.name,
                "industry": item.stock.industry,
                "role": "핵심 기업" if item.stock.role == "core" else "부가/연관 기업",
                "score": item.score,
                "industryScore": item.industry_score,
                "qualityScore": item.quality_score,
                "valuationScore": item.valuation_score,
                "momentumScore": item.momentum_score,
                "reasons": list(item.reasons),
                "cautions": list(item.cautions),
                "recentIssues": list(item.stock.recent_issues),
                "decisionGrade": item.decision_grade,
                "riskLevel": item.risk_level,
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
            }
            for item in report.stock_scores
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
                **_short_term_to_dict(item),
            }
            for item in report.short_term_scores
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


def _technical_by_ticker(report: RecommendationReport) -> dict[str, dict]:
    config = load_config()
    cache = CacheStore(config.cache_db_path)
    results: dict[str, dict] = {}
    tickers = tuple(dict.fromkeys(item.stock.ticker.upper() for item in report.stock_scores))
    for ticker in tickers:
        points = fetch_price_history(ticker, cache=cache, range_value="1y", timeout=5.0)
        snapshot = build_technical_snapshot(points)
        results[ticker] = technical_snapshot_to_dict(snapshot)
    return results


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
        "companyScore": item.company_score,
        "signalLabel": item.signal_label,
        "timeHorizon": item.time_horizon,
        "reasons": list(item.reasons),
        "cautions": list(item.cautions),
    }


def _medium_term_to_dict(item) -> dict | None:
    if item is None:
        return None
    return {
        "score": item.score,
        "companyScore": item.company_score,
        "marketScore": item.market_score,
        "chartScore": item.chart_score,
        "newsScore": item.news_score,
        "signalLabel": item.signal_label,
        "timeHorizon": item.time_horizon,
        "reasons": list(item.reasons),
        "cautions": list(item.cautions),
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
        "signalLabel": item.signal_label,
        "timeHorizon": item.time_horizon,
        "reasons": list(item.reasons),
        "cautions": list(item.cautions),
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
        if parsed.path.startswith("/assets/"):
            requested = parsed.path.removeprefix("/assets/")
            asset_path = (WEB_DIR / requested).resolve()
            if not asset_path.is_relative_to(WEB_DIR.resolve()):
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

        if parsed.path == "/api/report":
            query = parse_qs(parsed.query)
            macro_context = query.get("macro", [DEFAULT_MACRO_CONTEXT])[0] or DEFAULT_MACRO_CONTEXT
            try:
                payload = report_to_dict(create_report(macro_context=macro_context))
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
            if benchmark not in BENCHMARKS:
                benchmark = "SPY"
            if method not in BACKTEST_METHODS:
                method = "snapshot"
            try:
                payload = backtest_to_dict(
                    create_backtest(
                        months=months,
                        top_n=top_n,
                        benchmark_ticker=benchmark,
                        method=method,
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
            requested = parsed.path.removeprefix("/assets/")
            asset_path = (WEB_DIR / requested).resolve()
            if not asset_path.is_relative_to(WEB_DIR.resolve()):
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
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if include_body:
            self.wfile.write(content)


def _int_query(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(query.get(name, [str(default)])[0])
    except (TypeError, ValueError):
        return default


def _record_api_error(source: str, exc: Exception) -> None:
    try:
        config = load_config()
        CacheStore(config.cache_db_path).record_source_event(source, "error", str(exc))
    except Exception:
        return


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
