from __future__ import annotations

import json
import zipfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from io import BytesIO
from dataclasses import dataclass
from datetime import date, timedelta

from .config import AppConfig
from .storage import CacheStore


@dataclass(frozen=True)
class SourceResponse:
    ok: bool
    source: str
    payload: dict | list | None = None
    warning: str | None = None


class OpenDartClient:
    source = "OpenDART"

    def __init__(self, config: AppConfig, cache: CacheStore, timeout: float = 10.0):
        self.config = config
        self.cache = cache
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.config.opendart_api_key)

    def fetch_single_company_accounts(
        self, corp_code: str, business_year: str, report_code: str = "11011"
    ) -> SourceResponse:
        if not self.config.opendart_api_key:
            return SourceResponse(False, self.source, warning="OpenDART API 키가 없습니다.")
        params = {
            "crtfc_key": self.config.opendart_api_key,
            "corp_code": corp_code,
            "bsns_year": business_year,
            "reprt_code": report_code,
        }
        url = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json?" + urllib.parse.urlencode(params)
        return self._fetch_cached(url, f"opendart:single-accounts:{corp_code}:{business_year}:{report_code}")

    def fetch_recent_filings(self) -> SourceResponse:
        if not self.config.opendart_api_key:
            return SourceResponse(False, self.source, warning="OpenDART API 키가 없습니다.")
        start_date = (date.today() - timedelta(days=60)).strftime("%Y%m%d")
        params = {
            "crtfc_key": self.config.opendart_api_key,
            "bgn_de": start_date,
            "page_no": "1",
            "page_count": "1",
        }
        url = "https://opendart.fss.or.kr/api/list.json?" + urllib.parse.urlencode(params)
        return _fetch_json(
            url,
            f"opendart:health:recent-filings:{start_date}",
            self.source,
            self.cache,
            self.timeout,
        )

    def fetch_corp_code_map(self) -> SourceResponse:
        if not self.config.opendart_api_key:
            return SourceResponse(False, self.source, warning="OpenDART API 키가 없습니다.")
        cache_key = "opendart:corp-code-map"
        cached = self.cache.get_json(cache_key)
        if isinstance(cached, dict):
            return SourceResponse(True, self.source, payload=cached)

        params = {"crtfc_key": self.config.opendart_api_key}
        url = "https://opendart.fss.or.kr/api/corpCode.xml?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": "stock-recommender/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
            payload = _parse_corp_code_zip(raw)
        except (OSError, urllib.error.URLError, TimeoutError, zipfile.BadZipFile, ET.ParseError) as exc:
            return SourceResponse(False, self.source, warning=f"OpenDART 고유번호 목록 호출 실패: {exc}")

        self.cache.set_json(cache_key, self.source, _redact_url(url), payload, ttl_seconds=60 * 60 * 24 * 7)
        return SourceResponse(True, self.source, payload=payload)

    def _fetch_cached(self, url: str, cache_key: str) -> SourceResponse:
        cached = self.cache.get_json(cache_key)
        if cached is not None:
            return SourceResponse(True, self.source, payload=cached)
        return _fetch_json(url, cache_key, self.source, self.cache, self.timeout)


class FredClient:
    source = "FRED"

    def __init__(self, config: AppConfig, cache: CacheStore, timeout: float = 10.0):
        self.config = config
        self.cache = cache
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.config.fred_api_key)

    def fetch_series_observations(
        self, series_id: str, observation_start: str | None = None
    ) -> SourceResponse:
        if not self.config.fred_api_key:
            return SourceResponse(False, self.source, warning="FRED API 키가 없습니다.")
        params = {
            "series_id": series_id,
            "api_key": self.config.fred_api_key,
            "file_type": "json",
        }
        if observation_start:
            params["observation_start"] = observation_start
        url = "https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode(params)
        return _fetch_json(url, f"fred:series:{series_id}:{observation_start or 'all'}", self.source, self.cache, self.timeout)


class EcosClient:
    source = "ECOS"

    def __init__(self, config: AppConfig, cache: CacheStore, timeout: float = 10.0):
        self.config = config
        self.cache = cache
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.config.ecos_api_key)

    def fetch_statistic(
        self,
        table_code: str,
        cycle: str,
        start: str,
        end: str,
        item_code1: str = "",
    ) -> SourceResponse:
        if not self.config.ecos_api_key:
            return SourceResponse(False, self.source, warning="ECOS API 키가 없습니다.")
        parts = [
            "https://ecos.bok.or.kr/api/StatisticSearch",
            self.config.ecos_api_key,
            "json",
            "kr",
            "1",
            "1000",
            table_code,
            cycle,
            start,
            end,
        ]
        if item_code1:
            parts.append(item_code1)
        url = "/".join(parts)
        cache_key = f"ecos:stat:{table_code}:{cycle}:{start}:{end}:{item_code1 or 'all'}"
        return _fetch_json(url, cache_key, self.source, self.cache, self.timeout)

    def fetch_statistic_table_list(self) -> SourceResponse:
        if not self.config.ecos_api_key:
            return SourceResponse(False, self.source, warning="ECOS API 키가 없습니다.")
        url = f"https://ecos.bok.or.kr/api/StatisticTableList/{self.config.ecos_api_key}/json/kr/1/1"
        return _fetch_json(url, "ecos:health:table-list", self.source, self.cache, self.timeout)


def _fetch_json(
    url: str, cache_key: str, source: str, cache: CacheStore, timeout: float
) -> SourceResponse:
    cached = cache.get_json(cache_key)
    if cached is not None:
        return SourceResponse(True, source, payload=cached)

    request = urllib.request.Request(url, headers={"User-Agent": "stock-recommender/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return SourceResponse(False, source, warning=f"{source} 호출 실패: {exc}")

    cache.set_json(cache_key, source, _redact_url(url), payload, ttl_seconds=60 * 60 * 6)
    return SourceResponse(True, source, payload=payload)


def _parse_corp_code_zip(raw: bytes) -> dict[str, dict[str, str]]:
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        with archive.open("CORPCODE.xml") as file:
            root = ET.fromstring(file.read())
    result: dict[str, dict[str, str]] = {}
    for item in root.findall("list"):
        corp_code = _xml_text(item, "corp_code")
        corp_name = _xml_text(item, "corp_name")
        stock_code = _xml_text(item, "stock_code")
        if corp_code and stock_code:
            result[stock_code] = {"corp_code": corp_code, "corp_name": corp_name or ""}
    return result


def _xml_text(node: ET.Element, child_name: str) -> str | None:
    child = node.find(child_name)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _redact_url(url: str) -> str:
    redacted = url
    parsed = urllib.parse.urlparse(url)
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if query_pairs:
        safe_pairs = [
            (key, "***" if key.lower() in {"crtfc_key", "api_key"} else value)
            for key, value in query_pairs
        ]
        redacted = urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(safe_pairs))
        )
    parts = redacted.split("/")
    for index, part in enumerate(parts):
        if len(part) >= 20 and part.isalnum():
            parts[index] = "***"
    return "/".join(parts)
