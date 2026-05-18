from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from stock_recommender.pipeline import create_recommendation_report
from stock_recommender.config import load_config
from stock_recommender.storage import CacheStore
from stock_recommender.universe import DEFAULT_MACRO_CONTEXT
from stock_recommender.web import report_to_dict


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        macro_context = query.get("macro", [DEFAULT_MACRO_CONTEXT])[0] or DEFAULT_MACRO_CONTEXT
        try:
            payload = report_to_dict(create_recommendation_report(macro_context=macro_context))
        except Exception as exc:  # pragma: no cover - serverless boundary
            _record_api_error("api/report", exc)
            self._send_json({"error": "추천 리포트를 생성하지 못했습니다."}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(payload)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def _record_api_error(source: str, exc: Exception) -> None:
    try:
        config = load_config()
        CacheStore(config.cache_db_path).record_source_event(source, "error", str(exc))
    except Exception:
        return
