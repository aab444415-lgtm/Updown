from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from stock_recommender.backtest import BACKTEST_HORIZONS, BACKTEST_METHODS, BENCHMARKS, backtest_to_dict, create_backtest
from stock_recommender.http_utils import int_query as _int_query
from stock_recommender.http_utils import record_api_error as _record_api_error
from stock_recommender.http_utils import send_json_response


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
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
        except Exception as exc:  # pragma: no cover - serverless boundary
            _record_api_error("api/backtest", exc)
            self._send_json({"error": "백테스트를 생성하지 못했습니다."}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(payload)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        send_json_response(self, payload, status=status)
