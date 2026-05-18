from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from stock_recommender.backtest import BACKTEST_METHODS, BENCHMARKS, backtest_to_dict, create_backtest
from stock_recommender.config import load_config
from stock_recommender.storage import CacheStore


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
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
        except Exception as exc:  # pragma: no cover - serverless boundary
            _record_api_error("api/backtest", exc)
            self._send_json({"error": "백테스트를 생성하지 못했습니다."}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(payload)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
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
