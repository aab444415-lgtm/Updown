from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
import gzip
from dataclasses import dataclass, replace
from typing import Iterable

from .config import AppConfig
from .models import Fundamentals, StockProfile
from .storage import CacheStore


TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ANNUAL_FORMS = {"10-K", "20-F", "40-F"}
USD = "USD"

REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
NET_INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")
ASSET_TAGS = ("Assets",)
LIABILITY_TAGS = ("Liabilities",)
EQUITY_TAGS = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)


class SecEdgarClient:
    def __init__(
        self,
        config: AppConfig,
        cache: CacheStore,
        timeout: float = 10.0,
        request_interval_seconds: float = 0.2,
    ):
        self.config = config
        self.cache = cache
        self.timeout = timeout
        self.request_interval_seconds = request_interval_seconds

    def enrich_stocks(self, stocks: Iterable[StockProfile]) -> SecFundamentalResult:
        warnings: list[str] = []
        stock_tuple = tuple(stocks)
        try:
            ticker_map = self.fetch_ticker_map()
        except DataSourceError as exc:
            return SecFundamentalResult(stock_tuple, 0, (f"SEC EDGAR 티커 목록 수집 실패: {exc}",))

        enriched: list[StockProfile] = []
        updated_count = 0
        stale_count = 0
        for stock in stock_tuple:
            cik = ticker_map.get(stock.ticker.upper())
            if cik is None:
                enriched.append(stock)
                continue
            try:
                company_facts = self.fetch_company_facts(cik)
                facts = company_facts.payload
                if company_facts.stale:
                    stale_count += 1
                fundamentals = extract_fundamentals(facts, fallback=stock.fundamentals)
            except DataSourceError as exc:
                warnings.append(f"{stock.ticker} SEC 재무 수집 실패: {exc}")
                enriched.append(stock)
                continue

            if fundamentals != stock.fundamentals:
                updated_count += 1
            enriched.append(replace(stock, fundamentals=fundamentals))

        if updated_count == 0:
            warnings.append("SEC EDGAR에서 갱신 가능한 재무지표를 찾지 못해 내장 지표를 유지했습니다.")
        else:
            warnings.append(f"SEC EDGAR 재무지표로 {updated_count}개 종목을 갱신했습니다.")
        if stale_count:
            warnings.append(f"SEC EDGAR 실시간 호출이 일부 실패해 캐시된 공식 재무 데이터를 사용했습니다({stale_count}개).")
        return SecFundamentalResult(tuple(enriched), updated_count, tuple(warnings))

    def fetch_ticker_map(self) -> dict[str, str]:
        cache_key = "sec:ticker-map"
        cached = self.cache.get_json(cache_key)
        if isinstance(cached, dict):
            return {str(item["ticker"]).upper(): str(item["cik_str"]).zfill(10) for item in cached.values()}

        try:
            payload = self._fetch_json(TICKER_MAP_URL)
        except DataSourceError:
            stale = self.cache.get_json(cache_key, allow_expired=True)
            if isinstance(stale, dict):
                return {str(item["ticker"]).upper(): str(item["cik_str"]).zfill(10) for item in stale.values()}
            raise
        if not isinstance(payload, dict):
            raise DataSourceError("티커 목록 응답 형식이 올바르지 않습니다.")
        self.cache.set_json(cache_key, "SEC EDGAR", TICKER_MAP_URL, payload, ttl_seconds=60 * 60 * 24)
        return {str(item["ticker"]).upper(): str(item["cik_str"]).zfill(10) for item in payload.values()}

    def fetch_company_facts(self, cik: str) -> CompanyFactsResult:
        normalized_cik = cik.zfill(10)
        url = COMPANY_FACTS_URL.format(cik=normalized_cik)
        cache_key = f"sec:companyfacts:{normalized_cik}"
        cached = self.cache.get_json(cache_key)
        if isinstance(cached, dict):
            return CompanyFactsResult(cached, stale=False)

        try:
            payload = self._fetch_json(url)
        except DataSourceError:
            stale = self.cache.get_json(cache_key, allow_expired=True)
            if isinstance(stale, dict):
                return CompanyFactsResult(stale, stale=True)
            raise
        if not isinstance(payload, dict):
            raise DataSourceError("companyfacts 응답 형식이 올바르지 않습니다.")
        self.cache.set_json(cache_key, "SEC EDGAR", url, payload, ttl_seconds=60 * 60 * 12)
        return CompanyFactsResult(payload, stale=False)

    def _fetch_json(self, url: str) -> dict | list:
        time.sleep(self.request_interval_seconds)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.config.sec_user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise DataSourceError(str(exc)) from exc

        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DataSourceError("JSON 파싱 실패") from exc


def extract_fundamentals(facts: dict, fallback: Fundamentals | None = None) -> Fundamentals:
    fallback = fallback or Fundamentals()
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    if not isinstance(us_gaap, dict):
        return fallback

    revenue = _latest_and_previous(us_gaap, REVENUE_TAGS)
    operating_income = _latest_and_previous(us_gaap, OPERATING_INCOME_TAGS)
    net_income = _latest_and_previous(us_gaap, NET_INCOME_TAGS)
    assets = _latest_and_previous(us_gaap, ASSET_TAGS)
    liabilities = _latest_and_previous(us_gaap, LIABILITY_TAGS)
    equity = _latest_and_previous(us_gaap, EQUITY_TAGS)

    latest_revenue, previous_revenue = revenue
    latest_operating_income, _ = operating_income
    latest_net_income, _ = net_income
    latest_liabilities, _ = liabilities
    latest_equity, previous_equity = equity

    revenue_growth_pct = _growth_pct(latest_revenue, previous_revenue)
    operating_margin_pct = _ratio_pct(latest_operating_income, latest_revenue)
    average_equity = _average(latest_equity, previous_equity)
    roe_pct = _ratio_pct(latest_net_income, average_equity)
    debt_to_equity_pct = _ratio_pct(latest_liabilities, latest_equity)

    return Fundamentals(
        revenue_growth_pct=_coalesce(revenue_growth_pct, fallback.revenue_growth_pct),
        operating_margin_pct=_coalesce(operating_margin_pct, fallback.operating_margin_pct),
        roe_pct=_coalesce(roe_pct, fallback.roe_pct),
        debt_to_equity_pct=_coalesce(debt_to_equity_pct, fallback.debt_to_equity_pct),
        pe=fallback.pe,
        forward_pe=fallback.forward_pe,
        market_cap_usd=fallback.market_cap_usd,
    )


def _latest_and_previous(us_gaap: dict, tags: tuple[str, ...]) -> tuple[float | None, float | None]:
    candidates: list[tuple[str, float]] = []
    for tag in tags:
        concept = us_gaap.get(tag)
        if not isinstance(concept, dict):
            continue
        units = concept.get("units", {})
        facts = units.get(USD)
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if not _is_annual_fact(fact):
                continue
            value = fact.get("val")
            filed = fact.get("filed") or fact.get("end")
            if isinstance(value, (int, float)) and math.isfinite(value) and filed:
                candidates.append((str(filed), float(value)))

    candidates.sort(key=lambda item: item[0])
    unique: list[tuple[str, float]] = []
    seen_periods: set[str] = set()
    for period, value in reversed(candidates):
        if period in seen_periods:
            continue
        seen_periods.add(period)
        unique.append((period, value))
        if len(unique) == 2:
            break
    latest = unique[0][1] if unique else None
    previous = unique[1][1] if len(unique) > 1 else None
    return latest, previous


def _is_annual_fact(fact: dict) -> bool:
    form = str(fact.get("form", ""))
    if form not in ANNUAL_FORMS:
        return False
    if fact.get("fp") not in {None, "FY"}:
        return False
    if fact.get("frame") and not str(fact.get("frame")).startswith("CY"):
        return False
    return True


def _growth_pct(latest: float | None, previous: float | None) -> float | None:
    if latest is None or previous is None or previous == 0:
        return None
    return ((latest / previous) - 1) * 100


def _ratio_pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return (numerator / denominator) * 100


def _average(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return first
    return (first + second) / 2


def _coalesce(value: float | None, fallback: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else fallback


class DataSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SecFundamentalResult:
    stocks: tuple[StockProfile, ...]
    updated_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CompanyFactsResult:
    payload: dict
    stale: bool
