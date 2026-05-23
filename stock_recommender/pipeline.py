from __future__ import annotations

from dataclasses import replace

from .config import configured_source_names, load_config, missing_optional_source_names
from .data_sources import enrich_with_live_market_data, fetch_many_momentums, fetch_news
from .macro_data import fetch_macro_snapshot
from .models import (
    FUNDAMENTAL_SOURCE_BY_ATTR,
    DataQuality,
    RecommendationReport,
    StockProfile,
    fundamentals_with_real_sources_only,
    real_fundamental_value_count,
)
from .opendart_financials import OpenDartFinancialClient
from .scoring import build_report
from .sec_edgar import SecEdgarClient
from .storage import CacheStore
from .time_utils import now_in_app_timezone
from .universe import BENEFICIARY_INDUSTRIES, DEFAULT_MACRO_CONTEXT, INDUSTRIES, STOCKS


SNAPSHOT_BENCHMARK_TICKERS = ("SPY", "QQQ", "^KS11")


def beneficiary_market_proxy_tickers(
    beneficiary_industries=BENEFICIARY_INDUSTRIES,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            proxy.ticker.upper()
            for profile in beneficiary_industries
            for proxy in profile.market_proxies
            if proxy.ticker
        )
    )


def beneficiary_news_terms(
    beneficiary_industries=BENEFICIARY_INDUSTRIES,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            term
            for profile in beneficiary_industries
            for term in profile.keywords
            if term
        )
    )


def create_recommendation_report(
    macro_context: str = DEFAULT_MACRO_CONTEXT,
    use_sec_fundamentals: bool = True,
) -> RecommendationReport:
    config = load_config()
    cache = CacheStore(config.cache_db_path)
    run_started_at = now_in_app_timezone(config)
    warnings: list[str] = []
    stocks = STOCKS
    news_items = ()
    momentums = {}
    live_market_data = False
    live_fundamentals = False
    live_korea_fundamentals = False
    macro_snapshot = None

    macro_snapshot = fetch_macro_snapshot(config, cache)
    warnings.extend(macro_snapshot.warnings)

    all_terms = tuple(
        dict.fromkeys(
            (
                *(term for industry in INDUSTRIES for term in industry.news_terms),
                *beneficiary_news_terms(),
            )
        )
    )
    news_items = fetch_news(all_terms, cache=cache)
    if not news_items:
        warnings.append("뉴스 RSS 수집에 실패해 산업 테마 키워드만 사용했습니다.")

    stocks = enrich_with_live_market_data(stocks, cache=cache)

    if use_sec_fundamentals:
        sec_result = SecEdgarClient(config, cache).enrich_stocks(stocks)
        stocks = sec_result.stocks
        live_fundamentals = sec_result.updated_count > 0
        warnings.extend(sec_result.warnings)

    dart_result = OpenDartFinancialClient(config, cache).enrich_stocks(stocks)
    stocks = dart_result.stocks
    live_korea_fundamentals = dart_result.updated_count > 0
    warnings.extend(dart_result.warnings)
    live_fundamentals = live_fundamentals or live_korea_fundamentals
    stocks, removed_fundamental_values = _keep_real_fundamentals_only(stocks)
    if removed_fundamental_values:
        warnings.append(
            f"출처가 확인되지 않은 내장 재무 데이터 {removed_fundamental_values}개를 추천 계산과 표시에서 제외했습니다."
        )

    momentum_tickers = tuple(
        dict.fromkeys(
            (
                *(stock.ticker for stock in stocks),
                *beneficiary_market_proxy_tickers(),
                *SNAPSHOT_BENCHMARK_TICKERS,
            )
        )
    )
    momentums = fetch_many_momentums(momentum_tickers, cache=cache)
    live_market_data = any(
        any(
            value is not None
            for value in (
                momentum.one_month_pct,
                momentum.three_month_pct,
                momentum.six_month_pct,
                momentum.latest_close,
            )
        )
        for momentum in momentums.values()
    )
    if not live_market_data:
        warnings.append("시장 가격 모멘텀 수집에 실패해 중립 점수로 계산했습니다.")

    if "your-email@example.com" in config.sec_user_agent:
        warnings.append(".env의 SEC_USER_AGENT를 본인 이메일이 포함된 값으로 바꾸면 SEC 접근 정책에 더 잘 맞습니다.")

    source_events = cache.list_source_events_since(run_started_at, limit=300)

    return build_report(
        macro_context=macro_context,
        industries=INDUSTRIES,
        stocks=stocks,
        news_items=news_items,
        momentums=momentums,
        macro_snapshot=macro_snapshot,
        created_at=run_started_at,
        source_events=source_events,
        beneficiary_industries=BENEFICIARY_INDUSTRIES,
        data_quality=DataQuality(
            live_news=bool(news_items),
            live_market_data=live_market_data,
            live_fundamentals=live_fundamentals,
            live_macro=bool(macro_snapshot and macro_snapshot.indicators),
            live_korea_fundamentals=live_korea_fundamentals,
            configured_sources=configured_source_names(config),
            missing_sources=missing_optional_source_names(config),
            warnings=tuple(dict.fromkeys(warnings)),
        ),
    )


def _keep_real_fundamentals_only(
    stocks: tuple[StockProfile, ...],
) -> tuple[tuple[StockProfile, ...], int]:
    cleaned: list[StockProfile] = []
    removed_values = 0
    for stock in stocks:
        before = _present_fundamental_value_count(stock)
        fundamentals = fundamentals_with_real_sources_only(stock.fundamentals)
        after = real_fundamental_value_count(fundamentals)
        removed_values += max(0, before - after)
        cleaned.append(replace(stock, fundamentals=fundamentals))
    return tuple(cleaned), removed_values


def _present_fundamental_value_count(stock: StockProfile) -> int:
    return sum(
        1
        for attr in FUNDAMENTAL_SOURCE_BY_ATTR
        if getattr(stock.fundamentals, attr) is not None
    )
