from __future__ import annotations

from .config import configured_source_names, load_config, missing_optional_source_names
from .data_sources import enrich_with_live_market_data, fetch_many_momentums, fetch_news
from .macro_data import fetch_macro_snapshot
from .models import DataQuality, RecommendationReport
from .opendart_financials import OpenDartFinancialClient
from .scoring import build_report
from .sec_edgar import SecEdgarClient
from .storage import CacheStore
from .universe import DEFAULT_MACRO_CONTEXT, INDUSTRIES, STOCKS


def create_recommendation_report(
    live: bool = False,
    macro_context: str = DEFAULT_MACRO_CONTEXT,
    use_sec_fundamentals: bool = True,
) -> RecommendationReport:
    config = load_config()
    cache = CacheStore(config.cache_db_path)
    warnings: list[str] = []
    stocks = STOCKS
    news_items = ()
    momentums = {}
    live_market_data = False
    live_fundamentals = False
    live_korea_fundamentals = False
    macro_snapshot = None

    if live:
        macro_snapshot = fetch_macro_snapshot(config, cache)
        warnings.extend(macro_snapshot.warnings)

        all_terms = tuple(term for industry in INDUSTRIES for term in industry.news_terms)
        news_items = fetch_news(all_terms)
        if not news_items:
            warnings.append("뉴스 RSS 수집에 실패해 내장 산업 테마만 사용했습니다.")

        stocks = enrich_with_live_market_data(stocks)

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

        momentums = fetch_many_momentums(stock.ticker for stock in stocks)
        live_market_data = any(
            any(value is not None for value in vars(momentum).values()) for momentum in momentums.values()
        )
        if not live_market_data:
            warnings.append("시장 가격 모멘텀 수집에 실패해 중립 점수로 계산했습니다.")
    else:
        warnings.append("샘플 모드입니다. 내장 지표와 중립 모멘텀으로 계산했습니다.")

    if "your-email@example.com" in config.sec_user_agent:
        warnings.append(".env의 SEC_USER_AGENT를 본인 이메일이 포함된 값으로 바꾸면 SEC 접근 정책에 더 잘 맞습니다.")

    return build_report(
        macro_context=macro_context,
        industries=INDUSTRIES,
        stocks=stocks,
        news_items=news_items,
        momentums=momentums,
        macro_snapshot=macro_snapshot,
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
