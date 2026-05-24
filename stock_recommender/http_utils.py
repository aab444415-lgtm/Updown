from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler

from .config import load_config
from .storage import CacheStore


def int_query(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(query.get(name, [str(default)])[0])
    except (TypeError, ValueError):
        return default


def send_json_response(
    handler: BaseHTTPRequestHandler,
    payload: dict,
    status: HTTPStatus = HTTPStatus.OK,
    include_body: bool = True,
) -> None:
    content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    if include_body:
        handler.wfile.write(content)


def record_api_error(source: str, exc: Exception) -> None:
    try:
        config = load_config()
        CacheStore(config.cache_db_path).record_source_event(source, "error", str(exc))
    except Exception:
        return
