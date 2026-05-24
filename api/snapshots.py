from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from stock_recommender.http_utils import int_query as _int_query
from stock_recommender.http_utils import record_api_error as _record_api_error
from stock_recommender.http_utils import send_json_response
from stock_recommender.snapshots import snapshot_history


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        limit = _int_query(query, "limit", 30)
        try:
            payload = snapshot_history(limit=limit)
        except Exception as exc:  # pragma: no cover - serverless boundary
            _record_api_error("api/snapshots", exc)
            self._send_json({"error": "스냅샷 기록을 불러오지 못했습니다."}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(payload)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        send_json_response(self, payload, status=status)
