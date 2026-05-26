from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

from .config import AppConfig
from .data_sources import SOURCE_POLYGON, SOURCE_YAHOO, fetch_polygon_us_quotes, fetch_yahoo_quotes
from .models import Fundamentals, IndustryProfile, StockProfile
from .official_sources import KrxClient, OpenDartClient
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
    us_stocks, us_quote_ready = _quote_ready_us_stocks(us_probe, config, cache, warnings)
    kr_stocks, kr_quote_ready = _quote_ready_kr_stocks(kr_probe, config, cache, warnings)
    if us_candidates and not us_quote_ready:
        if config.polygon_api_key:
            warnings.append("미국 상장 후보의 Polygon/Yahoo 가격/시총 확인에 실패했습니다.")
        else:
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
    config: AppConfig,
    cache: CacheStore,
    warnings: list[str],
) -> tuple[tuple[StockProfile, ...], int]:
    quote_by_ticker: dict[str, dict] = {}
    if config.polygon_api_key:
        polygon_quotes = fetch_polygon_us_quotes(
            (item.ticker for item in candidates),
            api_key=config.polygon_api_key,
            fresh_limit=config.polygon_fresh_limit,
            cache=cache,
        )
        quote_by_ticker.update(polygon_quotes)
        if polygon_quotes:
            warnings.append(f"Polygon 가격/시총으로 미국 후보 {len(polygon_quotes)}개를 확인했습니다.")
        else:
            warnings.append("Polygon 가격/시총 확인 결과가 없어 Yahoo 확인으로 보완합니다.")
    missing_symbols = tuple(item.ticker for item in candidates if item.ticker.upper() not in quote_by_ticker)
    quote_by_ticker.update(_quotes_for_symbols(missing_symbols, cache))
    stocks = tuple(
        profile
        for item in candidates
        if (profile := _profile_from_quote(item, quote_by_ticker.get(item.ticker.upper())))
        is not None
    )
    return stocks, len(stocks)


def _quote_ready_kr_stocks(
    candidates: tuple[_RawCandidate, ...],
    config: AppConfig,
    cache: CacheStore,
    warnings: list[str],
) -> tuple[tuple[StockProfile, ...], int]:
    krx_by_code = _krx_quotes_for_candidates(candidates, config, cache, warnings)
    missing_for_yahoo = tuple(item for item in candidates if item.ticker.upper() not in krx_by_code)
    ks_by_code = _quotes_for_symbols((f"{item.ticker}.KS" for item in missing_for_yahoo), cache)
    missing_ks = tuple(item for item in missing_for_yahoo if f"{item.ticker}.KS" not in ks_by_code)
    kq_by_code = _quotes_for_symbols((f"{item.ticker}.KQ" for item in missing_ks), cache)

    stocks: list[StockProfile] = []
    for item in candidates:
        ks_symbol = f"{item.ticker}.KS"
        kq_symbol = f"{item.ticker}.KQ"
        quote = krx_by_code.get(item.ticker.upper()) or ks_by_code.get(ks_symbol) or kq_by_code.get(kq_symbol)
        if quote is None:
            continue
        symbol = str(quote.get("symbol") or ks_symbol).upper()
        profile = _profile_from_quote(item, quote, ticker=symbol)
        if profile is not None:
            stocks.append(profile)
    return tuple(stocks), len(stocks)


def _krx_quotes_for_candidates(
    candidates: tuple[_RawCandidate, ...],
    config: AppConfig,
    cache: CacheStore,
    warnings: list[str],
) -> dict[str, dict]:
    if not candidates or not config.krx_auth_key:
        return {}

    response = KrxClient(config, cache).fetch_latest_stock_daily_trades()
    if not response.ok or not isinstance(response.payload, dict):
        warnings.append(response.warning or "KRX 가격/시총 확인에 실패했습니다.")
        return {}

    rows = response.payload.get("OutBlock_1")
    if not isinstance(rows, list):
        warnings.append("KRX 일별매매정보 응답 형식이 예상과 다릅니다.")
        return {}
    warnings.extend(_krx_market_status_warnings(response.payload))

    wanted = {item.ticker.upper() for item in candidates}
    quotes: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = _krx_stock_code(row)
        if code not in wanted:
            continue
        quote = _krx_quote_from_row(row, code)
        if quote is not None:
            quotes[code] = quote
    if quotes:
        bas_dd = response.payload.get("basDd")
        suffix = f"({bas_dd})" if isinstance(bas_dd, str) and bas_dd else ""
        warnings.append(f"KRX 가격/시총으로 한국 후보 {len(quotes)}개를 확인했습니다{suffix}.")
    return quotes


def _krx_market_status_warnings(payload: dict) -> list[str]:
    market_status = payload.get("marketStatus")
    if not isinstance(market_status, dict):
        return []
    warnings: list[str] = []
    for market in ("KOSPI", "KOSDAQ"):
        status = market_status.get(market)
        if not isinstance(status, dict) or status.get("ok"):
            continue
        warning = status.get("warning")
        detail = f": {warning}" if isinstance(warning, str) and warning else ""
        warnings.append(f"KRX {market} 일별매매정보 확인이 일부 실패했습니다{detail}.")
    return warnings


def _krx_quote_from_row(row: dict, code: str) -> dict | None:
    price = _number_from_text(row.get("TDD_CLSPRC"))
    market_cap = _number_from_text(row.get("MKTCAP"))
    if not _positive(price) or not _positive(market_cap):
        return None
    market = str(row.get("_market") or row.get("MKT_NM") or "").upper()
    suffix = ".KQ" if "KOSDAQ" in market or "코스닥" in market else ".KS"
    name = _text(row.get("ISU_NM")) or _text(row.get("ISU_ABBRV")) or code
    quote = {
        "symbol": f"{code}{suffix}",
        "shortName": name,
        "longName": name,
        "regularMarketPrice": price,
        "marketCap": market_cap,
        "currency": "KRW",
        "financialCurrency": "KRW",
        "_source": "KRX",
        "_sourceUrl": "https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd",
    }
    previous_close = _krx_previous_close_from_row(row)
    if _positive(previous_close):
        quote["regularMarketPreviousClose"] = previous_close
    return quote


def _krx_previous_close_from_row(row: dict) -> float | None:
    for key in ("BFDD_CLSPRC", "PRVDD_CLSPRC", "PREV_CLSPRC"):
        value = _number_from_text(row.get(key))
        if _positive(value):
            return value
    return None


def _krx_stock_code(row: dict) -> str:
    for key in ("ISU_SRT_CD", "ISU_CD"):
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip().upper()
        if _valid_kr_stock_code(text):
            return text
    return ""


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
    quote_source = str(quote.get("_source") or SOURCE_YAHOO)
    fundamentals = _fundamentals_from_quote(quote, market_cap, currency, quote_source)
    market_cap_for_role = fundamentals.market_cap or 0
    role = _role_for_dynamic_stock(industry, market_cap_for_role, currency)
    source_name = f"OpenDART+{quote_source}" if candidate.country == "KR" else f"SEC EDGAR+{quote_source}"
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
            "가격/시총 확인 실패로 가격·규모 지표 없이 점수화됩니다.",
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
    source: str = SOURCE_YAHOO,
) -> Fundamentals:
    sources: dict[str, dict] = {}
    pe = _number(quote.get("trailingPE"))
    forward_pe = _number(quote.get("forwardPE"))
    if pe is not None:
        sources["pe"] = _field_source(source, quote.get("_sourceUrl"))
    if forward_pe is not None:
        sources["forwardPe"] = _field_source(source, quote.get("_sourceUrl"))
    if market_cap is not None:
        sources["marketCap"] = _field_source(source, quote.get("_sourceUrl"))
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
    tokens = set(re.findall(r"[a-z0-9가-힣]+", normalized))
    for alias, industry in _INDUSTRY_ALIASES:
        if _keyword_matches(alias, normalized, tokens):
            return industry
    for industry, keywords in _INDUSTRY_KEYWORDS:
        if any(_keyword_matches(keyword, normalized, tokens) for keyword in keywords):
            return industry
    return BROAD_MARKET_INDUSTRY.name


def _keyword_matches(keyword: str, normalized_text: str, tokens: set[str]) -> bool:
    normalized_keyword = keyword.lower()
    if re.fullmatch(r"[a-z0-9]{1,6}", normalized_keyword):
        return normalized_keyword in tokens
    return normalized_keyword in normalized_text


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
            f"{source_name} 확인 후보 {len(fallback_profiles)}개를 가격/시총 없이 보조 후보로 사용합니다."
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


def _field_source(source: str = SOURCE_YAHOO, url: object = None) -> dict:
    if isinstance(url, str) and url:
        return {"source": source, "url": url}
    if source == SOURCE_POLYGON:
        return {"source": source, "url": "https://polygon.io/docs/rest/stocks/tickers/ticker-overview"}
    if source == SOURCE_YAHOO:
        return {"source": SOURCE_YAHOO, "url": "https://finance.yahoo.com/quote/"}
    return {"source": source, "url": ""}


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


def _number_from_text(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _number(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "")
    if not text or text in {"-", "N/A"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


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


_INDUSTRY_ALIASES = (
    ("nvda", "AI 반도체 및 데이터센터"),
    ("nvidia", "AI 반도체 및 데이터센터"),
    ("amd", "AI 반도체 및 데이터센터"),
    ("advanced micro devices", "AI 반도체 및 데이터센터"),
    ("avgo", "AI 반도체 및 데이터센터"),
    ("broadcom", "AI 반도체 및 데이터센터"),
    ("tsm", "AI 반도체 및 데이터센터"),
    ("taiwan semiconductor", "AI 반도체 및 데이터센터"),
    ("asml", "AI 반도체 및 데이터센터"),
    ("amat", "AI 반도체 및 데이터센터"),
    ("applied materials", "AI 반도체 및 데이터센터"),
    ("lrcx", "AI 반도체 및 데이터센터"),
    ("lam research", "AI 반도체 및 데이터센터"),
    ("mu", "AI 반도체 및 데이터센터"),
    ("micron", "AI 반도체 및 데이터센터"),
    ("mrvl", "AI 반도체 및 데이터센터"),
    ("marvell", "AI 반도체 및 데이터센터"),
    ("smci", "AI 반도체 및 데이터센터"),
    ("super micro", "AI 반도체 및 데이터센터"),
    ("arm", "AI 반도체 및 데이터센터"),
    ("intc", "AI 반도체 및 데이터센터"),
    ("intel", "AI 반도체 및 데이터센터"),
    ("msft", "AI 반도체 및 데이터센터"),
    ("microsoft", "AI 반도체 및 데이터센터"),
    ("amzn", "AI 반도체 및 데이터센터"),
    ("amazon", "AI 반도체 및 데이터센터"),
    ("googl", "AI 반도체 및 데이터센터"),
    ("google", "AI 반도체 및 데이터센터"),
    ("alphabet", "AI 반도체 및 데이터센터"),
    ("000660", "AI 반도체 및 데이터센터"),
    ("sk하이닉스", "AI 반도체 및 데이터센터"),
    ("sk hynix", "AI 반도체 및 데이터센터"),
    ("005930", "AI 반도체 및 데이터센터"),
    ("삼성전자", "AI 반도체 및 데이터센터"),
    ("095340", "AI 반도체 및 데이터센터"),
    ("isc", "AI 반도체 및 데이터센터"),
    ("042700", "AI 반도체 및 데이터센터"),
    ("한미반도체", "AI 반도체 및 데이터센터"),
    ("039030", "AI 반도체 및 데이터센터"),
    ("이오테크닉스", "AI 반도체 및 데이터센터"),
    ("vRT", "전력 인프라 및 에너지 장비"),
    ("vertiv", "전력 인프라 및 에너지 장비"),
    ("etn", "전력 인프라 및 에너지 장비"),
    ("eaton", "전력 인프라 및 에너지 장비"),
    ("pwr", "전력 인프라 및 에너지 장비"),
    ("quanta services", "전력 인프라 및 에너지 장비"),
    ("ge", "전력 인프라 및 에너지 장비"),
    ("gev", "전력 인프라 및 에너지 장비"),
    ("ge vernova", "전력 인프라 및 에너지 장비"),
    ("nee", "전력 인프라 및 에너지 장비"),
    ("next era energy", "전력 인프라 및 에너지 장비"),
    ("010120", "전력 인프라 및 에너지 장비"),
    ("ls electric", "전력 인프라 및 에너지 장비"),
    ("267260", "전력 인프라 및 에너지 장비"),
    ("hd현대일렉트릭", "전력 인프라 및 에너지 장비"),
    ("llY", "비만 치료제 및 바이오 플랫폼"),
    ("eli lilly", "비만 치료제 및 바이오 플랫폼"),
    ("nvo", "비만 치료제 및 바이오 플랫폼"),
    ("novo nordisk", "비만 치료제 및 바이오 플랫폼"),
    ("amgn", "비만 치료제 및 바이오 플랫폼"),
    ("amgen", "비만 치료제 및 바이오 플랫폼"),
    ("regn", "비만 치료제 및 바이오 플랫폼"),
    ("regeneron", "비만 치료제 및 바이오 플랫폼"),
    ("vrtx", "비만 치료제 및 바이오 플랫폼"),
    ("vertex", "비만 치료제 및 바이오 플랫폼"),
    ("207940", "비만 치료제 및 바이오 플랫폼"),
    ("삼성바이오로직스", "비만 치료제 및 바이오 플랫폼"),
    ("068270", "비만 치료제 및 바이오 플랫폼"),
    ("셀트리온", "비만 치료제 및 바이오 플랫폼"),
    ("326030", "비만 치료제 및 바이오 플랫폼"),
    ("sk바이오팜", "비만 치료제 및 바이오 플랫폼"),
    ("lmt", "방산 및 우주항공"),
    ("lockheed", "방산 및 우주항공"),
    ("rtx", "방산 및 우주항공"),
    ("raytheon", "방산 및 우주항공"),
    ("noc", "방산 및 우주항공"),
    ("northrop", "방산 및 우주항공"),
    ("gd", "방산 및 우주항공"),
    ("general dynamics", "방산 및 우주항공"),
    ("ba", "방산 및 우주항공"),
    ("boeing", "방산 및 우주항공"),
    ("rklb", "방산 및 우주항공"),
    ("rocket lab", "방산 및 우주항공"),
    ("064350", "방산 및 우주항공"),
    ("현대로템", "방산 및 우주항공"),
    ("012450", "방산 및 우주항공"),
    ("한화에어로스페이스", "방산 및 우주항공"),
    ("047810", "방산 및 우주항공"),
    ("한국항공우주", "방산 및 우주항공"),
    ("crwd", "사이버보안"),
    ("crowdstrike", "사이버보안"),
    ("panw", "사이버보안"),
    ("palo alto", "사이버보안"),
    ("zs", "사이버보안"),
    ("zscaler", "사이버보안"),
    ("ftnt", "사이버보안"),
    ("fortinet", "사이버보안"),
    ("okta", "사이버보안"),
    ("cybr", "사이버보안"),
    ("cyberark", "사이버보안"),
)
