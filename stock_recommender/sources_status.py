from __future__ import annotations

import argparse
import sys

from .config import load_config
from .data_sources import fetch_polygon_us_quotes
from .official_sources import EcosClient, FredClient, OpenDartClient, SourceResponse
from .storage import CacheStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="설정된 데이터 소스 API 키와 응답 상태를 확인합니다.")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    config = load_config()
    cache = CacheStore(config.cache_db_path)

    checks = [
        ("OpenDART", config.opendart_api_key, lambda: OpenDartClient(config, cache, args.timeout).fetch_recent_filings()),
        ("FRED", config.fred_api_key, lambda: FredClient(config, cache, args.timeout).fetch_series_observations("FEDFUNDS", "2025-01-01")),
        ("ECOS", config.ecos_api_key, lambda: EcosClient(config, cache, args.timeout).fetch_statistic_table_list()),
        ("Polygon", config.polygon_api_key, lambda: _check_polygon(config.polygon_api_key, cache, args.timeout)),
        ("NewsAPI", config.news_api_key, None),
        ("Adanos", config.adanos_api_key if config.enable_external_research else None, None),
        ("Funda", config.funda_api_key if config.enable_external_research else None, None),
    ]

    failed = 0
    for name, key, runner in checks:
        if not key:
            print(f"{name}: not configured")
            continue
        if runner is None:
            print(f"{name}: configured, connector not implemented yet")
            continue
        response = runner()
        if _response_ok(response):
            print(f"{name}: ok")
        else:
            failed += 1
            print(f"{name}: failed - {response.warning or _extract_api_message(response)}")

    return 1 if failed else 0


def _response_ok(response: SourceResponse) -> bool:
    if not response.ok or response.payload is None:
        return False
    if response.source == "OpenDART" and isinstance(response.payload, dict):
        return response.payload.get("status") in {"000", "013"}
    if response.source == "FRED" and isinstance(response.payload, dict):
        return isinstance(response.payload.get("observations"), list)
    if response.source == "ECOS" and isinstance(response.payload, dict):
        return "StatisticTableList" in response.payload
    return True


def _check_polygon(api_key: str | None, cache: CacheStore, timeout: float) -> SourceResponse:
    quotes = fetch_polygon_us_quotes(("AAPL",), api_key=api_key, fresh_limit=1, timeout=timeout, cache=cache)
    if quotes:
        return SourceResponse(True, "Polygon", payload=quotes)
    return SourceResponse(False, "Polygon", warning="AAPL 가격/시총 확인에 실패했습니다.")


def _extract_api_message(response: SourceResponse) -> str:
    payload = response.payload
    if isinstance(payload, dict):
        for key in ("message", "MESSAGE", "msg", "error_message"):
            value = payload.get(key)
            if value:
                return str(value)
    return "응답 형식 확인 필요"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
