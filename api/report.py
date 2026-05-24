from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from stock_recommender.http_utils import record_api_error as _record_api_error
from stock_recommender.http_utils import send_json_response
from stock_recommender.universe import DEFAULT_MACRO_CONTEXT
from stock_recommender.web import create_report_payload


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        macro_context = query.get("macro", [DEFAULT_MACRO_CONTEXT])[0] or DEFAULT_MACRO_CONTEXT
        force_refresh = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
        try:
            payload = create_report_payload(macro_context=macro_context, force_refresh=force_refresh)
        except Exception as exc:  # pragma: no cover - serverless boundary
            _record_api_error("api/report", exc)
            self._send_json({"error": "추천 리포트를 생성하지 못했습니다."}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(payload)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        send_json_response(self, payload, status=status)
