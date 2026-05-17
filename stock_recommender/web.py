from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .backtest import BENCHMARKS, backtest_to_dict, create_backtest
from .models import RecommendationReport
from .pipeline import create_recommendation_report
from .snapshots import snapshot_history
from .universe import DEFAULT_MACRO_CONTEXT


WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_report(live: bool = False, macro_context: str = DEFAULT_MACRO_CONTEXT) -> RecommendationReport:
    return create_recommendation_report(live=live, macro_context=macro_context)


def report_to_dict(report: RecommendationReport) -> dict:
    return {
        "createdAt": report.created_at.strftime("%Y-%m-%d %H:%M:%S"),
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
                "decisionGrade": item.decision_grade,
                "riskLevel": item.risk_level,
                "valuationLabel": item.valuation_label,
                "fundamentals": {
                    "revenueGrowthPct": item.stock.fundamentals.revenue_growth_pct,
                    "operatingMarginPct": item.stock.fundamentals.operating_margin_pct,
                    "roePct": item.stock.fundamentals.roe_pct,
                    "debtToEquityPct": item.stock.fundamentals.debt_to_equity_pct,
                    "pe": item.stock.fundamentals.pe,
                    "forwardPe": item.stock.fundamentals.forward_pe,
                    "marketCapUsd": item.stock.fundamentals.market_cap_usd,
                    "marketCapCurrency": item.stock.fundamentals.market_cap_currency,
                },
                "country": item.stock.country,
                "currency": item.stock.currency,
            }
            for item in report.stock_scores
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


def _macro_snapshot_to_dict(report: RecommendationReport) -> dict | None:
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
            if not str(asset_path).startswith(str(WEB_DIR.resolve())):
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
            live = query.get("live", ["0"])[0] in {"1", "true", "yes"}
            macro_context = query.get("macro", [DEFAULT_MACRO_CONTEXT])[0] or DEFAULT_MACRO_CONTEXT
            try:
                payload = report_to_dict(create_report(live=live, macro_context=macro_context))
            except Exception as exc:  # pragma: no cover - defensive server boundary
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json(payload)
            return

        if parsed.path == "/api/backtest":
            query = parse_qs(parsed.query)
            months = _int_query(query, "months", 12)
            top_n = _int_query(query, "top", 5)
            benchmark = query.get("benchmark", ["SPY"])[0].upper()
            if benchmark not in BENCHMARKS:
                benchmark = "SPY"
            try:
                payload = backtest_to_dict(
                    create_backtest(months=months, top_n=top_n, benchmark_ticker=benchmark)
                )
            except Exception as exc:  # pragma: no cover - defensive server boundary
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json(payload)
            return

        if parsed.path == "/api/snapshots":
            query = parse_qs(parsed.query)
            limit = _int_query(query, "limit", 30)
            try:
                payload = snapshot_history(limit=limit)
            except Exception as exc:  # pragma: no cover - defensive server boundary
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json(payload)
            return

        if parsed.path.startswith("/assets/"):
            requested = parsed.path.removeprefix("/assets/")
            asset_path = (WEB_DIR / requested).resolve()
            if not str(asset_path).startswith(str(WEB_DIR.resolve())):
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
