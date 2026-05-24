from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

from .config import AppConfig
from .data_sources import SOURCE_YAHOO, fetch_yahoo_quotes
from .models import Fundamentals, IndustryProfile, StockProfile
from .official_sources import OpenDartClient
from .sec_edgar import SecEdgarClient
from .storage import CacheStore
from .universe import INDUSTRIES, STOCKS


QUOTE_BATCH_SIZE = 80
US_QUOTE_PROBE_MULTIPLIER = 2
KR_QUOTE_PROBE_MULTIPLIER = 4
BROAD_MARKET_INDUSTRY = IndustryProfile(
    name="광범위 시장 후보",
    description="특정 테마 키워드에 바로 묶이지 않는 실제 상장 후보군입니다.",
    news_terms=("earnings", "guidance", "market", "stocks"),
    macro_terms=("market", "earnings", "growth", "rates", "liquidity"),
    tailwinds=("실적 성장과 시장 유동성이 확인되는 종목은 후보로 남깁니다.",),
    risks=("테마 분류가 약하므로 사업 내용과 공식 재무 데이터 확인이 필요합니다.",),
)
SCREENED_INDUSTRIES: tuple[IndustryProfile, ...] = (*INDUSTRIES, BROAD_MARKET_INDUSTRY)


@dataclass(frozen=True)
class UniverseLoadResult:
    stocks: tuple[StockProfile, ...]
    candidate_count: int
    quote_ready_count: int
    financial_target_count: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RawCandidate:
    ticker: str
    name: str
    country: str
    currency: str
    dart_stock_code: str | None = None


def load_stock_universe(config: AppConfig, cache: CacheStore) -> UniverseLoadResult:
    if config.universe_mode == "curated":
        stocks = tuple(STOCKS[: config.universe_limit])
        return UniverseLoadResult(
            stocks=stocks,
            candidate_count=len(STOCKS),
            quote_ready_count=len(stocks),
            financial_target_count=len(stocks),
        )

    warnings: list[str] = []
    us_candidates = _us_candidates(config, cache, warnings)
    kr_candidates = _kr_candidates(config, cache, warnings)
    candidate_count = len(us_candidates) + len(kr_candidates)

    us_probe = us_candidates[: _quote_probe_limit(config.us_universe_limit, US_QUOTE_PROBE_MULTIPLIER)]
    kr_probe = kr_candidates[: _quote_probe_limit(config.kr_universe_limit, KR_QUOTE_PROBE_MULTIPLIER)]
    us_stocks, us_quote_ready = _quote_ready_us_stocks(us_probe, cache)
    kr_stocks, kr_quote_ready = _quote_ready_kr_stocks(kr_probe, cache)
    if us_candidates and not us_quote_ready:
        warnings.append("미국 상장 후보의 Yahoo 가격/시총 확인에 실패했습니다.")
    if kr_candidates and not kr_quote_ready:
        warnings.append("한국 상장 후보의 Yahoo 가격/시총 확인에 실패했습니다.")
    if len(us_probe) < len(us_candidates):
        warnings.append(f"미국 후보 {len(us_candidates)}개 중 SEC 우선순위 상위 {len(us_probe)}개만 가격 확인했습니다.")
    if len(kr_probe) < len(kr_candidates):
        warnings.append(f"한국 후보 {len(kr_candidates)}개 중 stock_code 순서 상위 {len(kr_probe)}개만 가격 확인했습니다.")
    if us_candidates and not us_quote_ready:
        us_stocks = _fill_with_official_only_profiles(
            us_stocks,
            us_probe,
            target_limit=config.us_universe_limit,
            source_name="SEC EDGAR",
            warnings=warnings,
        )
    if kr_candidates and not kr_quote_ready:
        kr_stocks = _fill_with_official_only_profiles(
            kr_stocks,
            kr_probe,
            target_limit=config.kr_universe_limit,
            source_name="OpenDART",
            warnings=warnings,
        )

    selected = (
        *_limit_by_market_cap(us_stocks, config.us_universe_limit),
        *_limit_by_market_cap(kr_stocks, config.kr_universe_limit),
    )
    stocks = tuple(selected[: config.universe_limit])
    warnings.append(
        f"동적 유니버스: 후보 {candidate_count}개 중 가격/시총 확인 {us_quote_ready + kr_quote_ready}개, "
        f"최종 {len(stocks)}개를 사용합니다."
    )
    return UniverseLoadResult(
        stocks=stocks,
        candidate_count=candidate_count,
        quote_ready_count=us_quote_ready + kr_quote_ready,
        financial_target_count=len(select_financial_targets(stocks, config)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def select_financial_targets(
    stocks: Iterable[StockProfile],
    config: AppConfig,
) -> tuple[StockProfile, ...]:
    stock_tuple = tuple(stocks)
    if config.universe_mode == "curated":
        return stock_tuple
    us_limit = max(0, config.us_fundamental_limit)
    kr_limit = max(0, config.kr_fundamental_limit)
    us_targets = [stock for stock in stock_tuple if stock.country != "KR"][:us_limit]
    kr_targets = [stock for stock in stock_tuple if stock.country == "KR"][:kr_limit]
    return tuple((*us_targets, *kr_targets))


def _us_candidates(config: AppConfig, cache: CacheStore, warnings: list[str]) -> tuple[_RawCandidate, ...]:
    try:
        records = SecEdgarClient(config, cache).fetch_ticker_records()
    except Exception as exc:
        warnings.append(f"SEC EDGAR 상장 후보 목록 수집 실패: {exc}")
        return ()
    return tuple(
        _RawCandidate(
            ticker=str(record["ticker"]).upper(),
            name=str(record.get("title") or record["ticker"]),
            country="US",
            currency="USD",
        )
        for record in records
        if _valid_us_symbol(str(record.get("ticker", "")).upper())
    )


def _kr_candidates(config: AppConfig, cache: CacheStore, warnings: list[str]) -> tuple[_RawCandidate, ...]:
    if not config.opendart_api_key:
        warnings.append("OpenDART API 키가 없어 한국 상장 후보 스크리닝은 건너뜁니다.")
        return ()
    response = OpenDartClient(config, cache).fetch_corp_code_map()
    if not response.ok or not isinstance(response.payload, dict):
        warnings.append(response.warning or "OpenDART 상장 후보 목록 수집 실패")
        return ()
    candidates: list[_RawCandidate] = []
    for stock_code, info in sorted(response.payload.items()):
        if not _valid_kr_stock_code(stock_code):
            continue
        name = str(info.get("corp_name") or stock_code) if isinstance(info, dict) else stock_code
        candidates.append(
            _RawCandidate(
                ticker=stock_code,
                name=name,
                country="KR",
                currency="KRW",
                dart_stock_code=stock_code,
            )
        )
    return tuple(candidates)


def _quote_ready_us_stocks(
    candidates: tuple[_RawCandidate, ...],
    cache: CacheStore,
) -> tuple[tuple[StockProfile, ...], int]:
    quote_by_ticker = _quotes_for_symbols((item.ticker for item in candidates), cache)
    stocks = tuple(
        profile
        for item in candidates
        if (profile := _profile_from_quote(item, quote_by_ticker.get(item.ticker.upper())))
        is not None
    )
    return stocks, len(stocks)


def _quote_ready_kr_stocks(
    candidates: tuple[_RawCandidate, ...],
    cache: CacheStore,
) -> tuple[tuple[StockProfile, ...], int]:
    ks_by_code = _quotes_for_symbols((f"{item.ticker}.KS" for item in candidates), cache)
    missing = tuple(item for item in candidates if f"{item.ticker}.KS" not in ks_by_code)
    kq_by_code = _quotes_for_symbols((f"{item.ticker}.KQ" for item in missing), cache)

    stocks: list[StockProfile] = []
    for item in candidates:
        ks_symbol = f"{item.ticker}.KS"
        kq_symbol = f"{item.ticker}.KQ"
        quote = ks_by_code.get(ks_symbol) or kq_by_code.get(kq_symbol)
        if quote is None:
            continue
        symbol = str(quote.get("symbol") or ks_symbol).upper()
        profile = _profile_from_quote(item, quote, ticker=symbol)
        if profile is not None:
            stocks.append(profile)
    return tuple(stocks), len(stocks)


def _quotes_for_symbols(symbols: Iterable[str], cache: CacheStore) -> dict[str, dict]:
    result: dict[str, dict] = {}
    symbol_tuple = tuple(dict.fromkeys(symbol.upper() for symbol in symbols if symbol))
    for chunk in _chunks(symbol_tuple, QUOTE_BATCH_SIZE):
        result.update(fetch_yahoo_quotes(chunk, cache=cache))
    return result


def _profile_from_quote(
    candidate: _RawCandidate,
    quote: dict | None,
    ticker: str | None = None,
) -> StockProfile | None:
    if not isinstance(quote, dict) or not _quote_has_price_and_market_cap(quote):
        return None
    market_cap = _number(quote.get("marketCap"))
    currency = str(quote.get("financialCurrency") or quote.get("currency") or candidate.currency)
    symbol = (ticker or str(quote.get("symbol") or candidate.ticker)).upper()
    name = _quote_name(quote) or candidate.name
    text = " ".join(
        str(value)
        for value in (
            symbol,
            name,
            quote.get("quoteType"),
            quote.get("market"),
            quote.get("exchange"),
        )
        if value
    )
    industry = _classify_industry(text)
    fundamentals = _fundamentals_from_quote(quote, market_cap, currency)
    market_cap_for_role = fundamentals.market_cap or 0
    role = _role_for_dynamic_stock(industry, market_cap_for_role, currency)
    source_name = "OpenDART+Yahoo" if candidate.country == "KR" else "SEC EDGAR+Yahoo"
    return StockProfile(
        ticker=symbol,
        name=name,
        industry=industry,
        role=role,
        thesis=f"{source_name}에서 상장 여부와 가격/시총이 확인된 동적 스크리너 후보입니다.",
        risks=(
            "공식 재무 데이터가 부족하면 점수 상한이 적용됩니다.",
            "동적 산업 분류는 사업 내용 확인 전까지 보조 신호로만 사용해야 합니다.",
        ),
        fundamentals=fundamentals,
        country=candidate.country,
        currency=currency,
        dart_stock_code=candidate.dart_stock_code,
    )


def _profile_from_official_candidate(candidate: _RawCandidate, source_name: str) -> StockProfile:
    industry = _classify_industry(f"{candidate.ticker} {candidate.name}")
    return StockProfile(
        ticker=candidate.ticker,
        name=candidate.name,
        industry=industry,
        role="adjacent",
        thesis=f"{source_name}에서 상장 여부가 확인된 동적 스크리너 후보입니다.",
        risks=(
            "Yahoo 가격/시총 확인 실패로 가격·규모 지표 없이 점수화됩니다.",
            "공식 재무 데이터가 부족하면 점수 상한이 적용됩니다.",
        ),
        fundamentals=Fundamentals(market_cap_currency=candidate.currency),
        country=candidate.country,
        currency=candidate.currency,
        dart_stock_code=candidate.dart_stock_code,
    )


def _fundamentals_from_quote(
    quote: dict,
    market_cap: float | None,
    currency: str,
) -> Fundamentals:
    sources: dict[str, dict] = {}
    pe = _number(quote.get("trailingPE"))
    forward_pe = _number(quote.get("forwardPE"))
    if pe is not None:
        sources["pe"] = _field_source()
    if forward_pe is not None:
        sources["forwardPe"] = _field_source()
    if market_cap is not None:
        sources["marketCap"] = _field_source()
    return Fundamentals(
        pe=pe,
        forward_pe=forward_pe,
        market_cap=market_cap,
        market_cap_currency=currency,
        sources=sources,
    )


def _quote_has_price_and_market_cap(quote: dict) -> bool:
    return _positive(_number(quote.get("marketCap"))) and _positive(
        _first_number(
            quote.get("regularMarketPrice"),
            quote.get("postMarketPrice"),
            quote.get("preMarketPrice"),
            quote.get("regularMarketPreviousClose"),
        )
    )


def _classify_industry(text: str) -> str:
    normalized = text.lower()
    for industry, keywords in _INDUSTRY_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return industry
    return BROAD_MARKET_INDUSTRY.name


def _role_for_dynamic_stock(industry: str, market_cap: float, currency: str) -> str:
    if industry == BROAD_MARKET_INDUSTRY.name:
        return "adjacent"
    if currency.upper() == "KRW":
        return "core" if market_cap >= 5_000_000_000_000 else "adjacent"
    return "core" if market_cap >= 25_000_000_000 else "adjacent"


def _limit_by_market_cap(stocks: tuple[StockProfile, ...], limit: int) -> tuple[StockProfile, ...]:
    ranked = sorted(
        stocks,
        key=lambda stock: stock.fundamentals.market_cap or 0,
        reverse=True,
    )
    return tuple(ranked[: max(0, limit)])


def _quote_probe_limit(target_limit: int, multiplier: int) -> int:
    return max(target_limit, target_limit * multiplier)


def _fill_with_official_only_profiles(
    stocks: tuple[StockProfile, ...],
    candidates: tuple[_RawCandidate, ...],
    target_limit: int,
    source_name: str,
    warnings: list[str],
) -> tuple[StockProfile, ...]:
    missing_count = max(0, target_limit - len(stocks))
    if missing_count == 0:
        return stocks
    existing: set[str] = set()
    for stock in stocks:
        existing.update(_stock_identity_keys(stock))
    fallback_profiles: list[StockProfile] = []
    for candidate in candidates:
        if existing.intersection(_candidate_identity_keys(candidate)):
            continue
        fallback_profiles.append(_profile_from_official_candidate(candidate, source_name))
        if len(fallback_profiles) >= missing_count:
            break
    if fallback_profiles:
        warnings.append(
            f"{source_name} 확인 후보 {len(fallback_profiles)}개를 Yahoo 가격/시총 없이 보조 후보로 사용합니다."
        )
    return tuple((*stocks, *fallback_profiles))


def _stock_identity_keys(stock: StockProfile) -> set[str]:
    keys = {stock.ticker.upper()}
    if "." in stock.ticker:
        keys.add(stock.ticker.split(".", 1)[0].upper())
    if stock.dart_stock_code:
        keys.add(stock.dart_stock_code.upper())
    return keys


def _candidate_identity_keys(candidate: _RawCandidate) -> set[str]:
    keys = {candidate.ticker.upper()}
    if candidate.dart_stock_code:
        keys.add(candidate.dart_stock_code.upper())
    return keys


def _field_source() -> dict:
    return {"source": SOURCE_YAHOO, "url": "https://finance.yahoo.com/quote/"}


def _quote_name(quote: dict) -> str | None:
    for key in ("longName", "shortName", "displayName", "symbol"):
        value = quote.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _valid_us_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9-]{1,10}", symbol.upper()))


def _valid_kr_stock_code(stock_code: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", stock_code))


def _chunks(values: tuple[str, ...], size: int) -> Iterable[tuple[str, ...]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _first_number(*values: object) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive(value: float | None) -> bool:
    return value is not None and value > 0


_INDUSTRY_KEYWORDS = (
    (
        "AI 반도체 및 데이터센터",
        (
            "semiconductor",
            "semi",
            "chip",
            "memory",
            "foundry",
            "data center",
            "datacenter",
            "server",
            "cloud",
            "networking",
            "optical",
            "electronics",
            "software",
        ),
    ),
    (
        "전력 인프라 및 에너지 장비",
        (
            "electric",
            "electrical",
            "power",
            "grid",
            "energy",
            "utility",
            "utilities",
            "transformer",
            "solar",
            "renewable",
            "infrastructure",
        ),
    ),
    (
        "비만 치료제 및 바이오 플랫폼",
        (
            "pharma",
            "pharmaceutical",
            "therapeutics",
            "biotech",
            "biotechnology",
            "bio",
            "health",
            "medical",
            "diabetes",
            "obesity",
            "drug",
        ),
    ),
    (
        "방산 및 우주항공",
        (
            "aerospace",
            "defense",
            "defence",
            "missile",
            "rocket",
            "space",
            "satellite",
            "aviation",
            "aircraft",
        ),
    ),
    (
        "사이버보안",
        (
            "cyber",
            "security",
            "identity",
            "authentication",
            "zero trust",
            "endpoint",
            "firewall",
        ),
    ),
)
