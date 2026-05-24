from __future__ import annotations

import json
import math
import statistics
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha1
from typing import TYPE_CHECKING

from .models import Fundamentals, Momentum, NewsItem, StockProfile
from .storage import CacheStore
from .technical import (
    _last_finite,
    average_recent_volume,
    bollinger_bandwidth_pct,
    bollinger_bands,
    bollinger_percent_b,
    breakout_pct,
    distance_from_average,
    moving_average,
    moving_average_slope,
    ohlcv_coverage_pct,
    previous_swing_high,
    rsi,
    structure_zone,
    volume_profile_zone,
    volume_ratio,
)

if TYPE_CHECKING:
    from .config import AppConfig


USER_AGENT = "stock-recommender-mvp/0.1"
NEWS_TTL_SECONDS = 60 * 30
QUOTE_TTL_SECONDS = 60 * 15
MOMENTUM_TTL_SECONDS = 60 * 60 * 6
POLYGON_REFERENCE_TTL_SECONDS = 60 * 60 * 12
POLYGON_GROUPED_TTL_SECONDS = 60 * 60 * 6
SOURCE_YAHOO = "Yahoo Finance"
SOURCE_POLYGON = "Polygon"


@dataclass(frozen=True)
class _OhlcvPoint:
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: float | None


SOURCE_GOOGLE_NEWS = "Google News"
POLYGON_API_BASE_URL = "https://api.polygon.io"


def fetch_news(
    industry_terms: Iterable[str],
    limit: int = 30,
    timeout: float = 8.0,
    cache: CacheStore | None = None,
) -> tuple[NewsItem, ...]:
    query = " OR ".join(f'"{term}"' if " " in term else term for term in industry_terms)
    params = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    url = f"https://news.google.com/rss/search?{params}"
    cache_key = f"google-news:{limit}:{_digest(query)}"
    if cache is not None:
        cached = cache.get_json(cache_key)
        if isinstance(cached, list):
            return _news_items_from_payload(cached)

    try:
        with _open_url(url, timeout=timeout) as response:
            payload = response.read()
    except (OSError, urllib.error.URLError, TimeoutError):
        if cache is not None:
            stale = cache.get_json(cache_key, allow_expired=True)
            if isinstance(stale, list):
                _record_event(cache, SOURCE_GOOGLE_NEWS, "stale", "뉴스 RSS 호출 실패로 만료 캐시를 사용했습니다.")
                return _news_items_from_payload(stale)
            _record_event(cache, SOURCE_GOOGLE_NEWS, "error", "뉴스 RSS 호출에 실패했습니다.")
        return ()

    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        if cache is not None:
            stale = cache.get_json(cache_key, allow_expired=True)
            if isinstance(stale, list):
                _record_event(cache, SOURCE_GOOGLE_NEWS, "stale", "뉴스 RSS 파싱 실패로 만료 캐시를 사용했습니다.")
                return _news_items_from_payload(stale)
            _record_event(cache, SOURCE_GOOGLE_NEWS, "error", "뉴스 RSS 응답 파싱에 실패했습니다.")
        return ()

    items: list[NewsItem] = []
    for item in root.findall("./channel/item"):
        title = _xml_text(item, "title")
        if not title:
            continue
        source = _xml_text(item, "source") or "Google News"
        items.append(
            NewsItem(
                title=title,
                source=source,
                published=_xml_text(item, "pubDate"),
                url=_xml_text(item, "link"),
                summary=_xml_text(item, "description"),
            )
        )
        if len(items) >= limit:
            break
    result = tuple(items)
    if cache is not None:
        cache.set_json(cache_key, SOURCE_GOOGLE_NEWS, url, _news_items_to_payload(result), NEWS_TTL_SECONDS)
        _record_event(cache, SOURCE_GOOGLE_NEWS, "success", f"뉴스 RSS {len(result)}건을 수집했습니다.")
    return result


def enrich_with_live_market_data(
    stocks: Iterable[StockProfile],
    timeout: float = 8.0,
    cache: CacheStore | None = None,
    config: "AppConfig | None" = None,
) -> tuple[StockProfile, ...]:
    profiles = list(stocks)
    polygon_quotes: dict[str, dict] = {}
    if config is not None and config.polygon_api_key:
        polygon_quotes = fetch_polygon_us_quotes(
            (stock.ticker for stock in profiles if stock.country != "KR"),
            api_key=config.polygon_api_key,
            fresh_limit=config.polygon_fresh_limit,
            timeout=timeout,
            cache=cache,
        )
    yahoo_tickers = [stock.ticker for stock in profiles if stock.ticker.upper() not in polygon_quotes]
    quotes = {
        **fetch_yahoo_quotes(yahoo_tickers, timeout=timeout, cache=cache),
        **polygon_quotes,
    }
    enriched: list[StockProfile] = []
    for stock in profiles:
        quote = quotes.get(stock.ticker.upper(), {})
        fundamentals = stock.fundamentals
        if quote:
            source_name = str(quote.get("_source") or SOURCE_YAHOO)
            sources = dict(fundamentals.sources)
            if _is_number(quote.get("trailingPE")):
                sources["pe"] = _field_source(source_name, quote.get("_sourceUrl"))
            if _is_number(quote.get("forwardPE")):
                sources["forwardPe"] = _field_source(source_name, quote.get("_sourceUrl"))
            if _is_number(quote.get("marketCap")):
                sources["marketCap"] = _field_source(source_name, quote.get("_sourceUrl"))
            fundamentals = replace(
                fundamentals,
                pe=_number_or_existing(quote.get("trailingPE"), fundamentals.pe),
                forward_pe=_number_or_existing(quote.get("forwardPE"), fundamentals.forward_pe),
                market_cap=_number_or_existing(quote.get("marketCap"), fundamentals.market_cap),
                market_cap_currency=_text_or_existing(
                    quote.get("financialCurrency") or quote.get("currency"),
                    fundamentals.market_cap_currency,
                ),
                sources=sources,
            )
        enriched.append(replace(stock, fundamentals=fundamentals))
    return tuple(enriched)


def fetch_polygon_us_quotes(
    tickers: Iterable[str],
    api_key: str | None,
    fresh_limit: int = 4,
    timeout: float = 8.0,
    cache: CacheStore | None = None,
) -> dict[str, dict]:
    symbols = tuple(dict.fromkeys(ticker.upper() for ticker in tickers if ticker))
    if not symbols or not api_key:
        return {}

    closes = _fetch_polygon_grouped_closes(api_key, timeout=timeout, cache=cache)
    details_by_symbol = _fetch_polygon_ticker_details(
        symbols,
        api_key=api_key,
        fresh_limit=fresh_limit,
        timeout=timeout,
        cache=cache,
    )
    quotes: dict[str, dict] = {}
    for symbol in symbols:
        polygon_symbol = _polygon_symbol(symbol)
        details = details_by_symbol.get(symbol)
        close_payload = closes.get(polygon_symbol) or closes.get(symbol)
        quote = _polygon_quote_from_payload(symbol, details, close_payload)
        if quote is not None:
            quotes[symbol] = quote
    if cache is not None:
        _record_event(cache, SOURCE_POLYGON, "success", f"Polygon quote {len(quotes)}건을 준비했습니다.")
    return quotes


def _fetch_polygon_ticker_details(
    symbols: tuple[str, ...],
    api_key: str,
    fresh_limit: int,
    timeout: float,
    cache: CacheStore | None,
) -> dict[str, dict]:
    details_by_symbol: dict[str, dict] = {}
    fresh_remaining = max(0, fresh_limit)
    skipped = 0
    for symbol in symbols:
        polygon_symbol = _polygon_symbol(symbol)
        cache_key = f"polygon:ticker-details:{polygon_symbol}"
        cached = cache.get_json(cache_key) if cache is not None else None
        if isinstance(cached, dict):
            details_by_symbol[symbol] = cached
            continue
        if fresh_remaining <= 0:
            skipped += 1
            continue
        fresh_remaining -= 1
        url, public_url = _polygon_url(f"/v3/reference/tickers/{urllib.parse.quote(polygon_symbol, safe='')}", api_key)
        try:
            with _open_url(url, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            result = payload.get("results") if isinstance(payload, dict) else None
            if isinstance(result, dict):
                if cache is not None:
                    cache.set_json(
                        cache_key,
                        SOURCE_POLYGON,
                        public_url,
                        result,
                        POLYGON_REFERENCE_TTL_SECONDS,
                    )
                    _record_event(cache, SOURCE_POLYGON, "success", f"{polygon_symbol} ticker overview를 수집했습니다.")
                details_by_symbol[symbol] = result
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            stale = cache.get_json(cache_key, allow_expired=True) if cache is not None else None
            if isinstance(stale, dict):
                details_by_symbol[symbol] = stale
                if cache is not None:
                    _record_event(cache, SOURCE_POLYGON, "stale", f"{polygon_symbol} ticker overview 호출 실패로 만료 캐시를 사용했습니다.")
            elif cache is not None:
                _record_event(cache, SOURCE_POLYGON, "error", f"{polygon_symbol} ticker overview 호출 또는 파싱에 실패했습니다.")
    if skipped and cache is not None:
        _record_event(
            cache,
            SOURCE_POLYGON,
            "warning",
            f"Polygon fresh limit 때문에 ticker overview {skipped}건은 이번 실행에서 새로 호출하지 않았습니다.",
            metadata={"freshLimit": max(0, fresh_limit), "skipped": skipped},
        )
    return details_by_symbol


def _fetch_polygon_grouped_closes(
    api_key: str,
    timeout: float,
    cache: CacheStore | None,
) -> dict[str, dict]:
    for target_date in _recent_weekdays(date.today(), lookback_days=10):
        cache_key = f"polygon:grouped-daily:{target_date.isoformat()}"
        cached = cache.get_json(cache_key) if cache is not None else None
        if isinstance(cached, dict):
            return _polygon_grouped_close_map(cached, target_date)
        url, public_url = _polygon_url(
            f"/v2/aggs/grouped/locale/us/market/stocks/{target_date.isoformat()}",
            api_key,
            params={"adjusted": "true"},
        )
        try:
            with _open_url(url, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("results"), list) and payload["results"]:
                if cache is not None:
                    cache.set_json(
                        cache_key,
                        SOURCE_POLYGON,
                        public_url,
                        payload,
                        POLYGON_GROUPED_TTL_SECONDS,
                    )
                    _record_event(cache, SOURCE_POLYGON, "success", f"{target_date.isoformat()} grouped daily close를 수집했습니다.")
                return _polygon_grouped_close_map(payload, target_date)
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            stale = cache.get_json(cache_key, allow_expired=True) if cache is not None else None
            if isinstance(stale, dict):
                if cache is not None:
                    _record_event(cache, SOURCE_POLYGON, "stale", f"{target_date.isoformat()} grouped close 호출 실패로 만료 캐시를 사용했습니다.")
                return _polygon_grouped_close_map(stale, target_date)
            if cache is not None:
                _record_event(cache, SOURCE_POLYGON, "error", f"{target_date.isoformat()} grouped close 호출 또는 파싱에 실패했습니다.")
    return {}


def _polygon_grouped_close_map(payload: dict, target_date: date) -> dict[str, dict]:
    results = payload.get("results")
    if not isinstance(results, list):
        return {}
    close_by_symbol: dict[str, dict] = {}
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("T"), str):
            continue
        close = _number(item.get("c"))
        if close is None or close <= 0:
            continue
        close_by_symbol[item["T"].upper()] = {
            "close": close,
            "date": target_date.isoformat(),
            "volume": _number(item.get("v")),
        }
    return close_by_symbol


def _polygon_quote_from_payload(symbol: str, details: dict | None, close_payload: dict | None) -> dict | None:
    if not isinstance(details, dict) or not isinstance(close_payload, dict):
        return None
    market_cap = _number(details.get("market_cap"))
    close = _number(close_payload.get("close"))
    if market_cap is None or market_cap <= 0 or close is None or close <= 0:
        return None
    name = details.get("name")
    currency = str(details.get("currency_name") or "USD").upper()
    return {
        "symbol": symbol.upper(),
        "shortName": name if isinstance(name, str) else symbol.upper(),
        "longName": name if isinstance(name, str) else symbol.upper(),
        "regularMarketPreviousClose": close,
        "regularMarketPrice": close,
        "marketCap": market_cap,
        "currency": currency,
        "financialCurrency": currency,
        "quoteType": details.get("type"),
        "market": details.get("market"),
        "exchange": details.get("primary_exchange"),
        "_source": SOURCE_POLYGON,
        "_sourceUrl": f"{POLYGON_API_BASE_URL}/v3/reference/tickers/{urllib.parse.quote(_polygon_symbol(symbol), safe='')}",
        "_priceDate": close_payload.get("date"),
    }


def fetch_yahoo_quotes(
    tickers: Iterable[str], timeout: float = 8.0, cache: CacheStore | None = None
) -> dict[str, dict]:
    symbols = ",".join(sorted({ticker.upper() for ticker in tickers}))
    if not symbols:
        return {}

    params = urllib.parse.urlencode({"symbols": symbols})
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?{params}"
    cache_key = f"yahoo-quotes:{_digest(symbols)}"
    if cache is not None:
        cached = cache.get_json(cache_key)
        if isinstance(cached, dict):
            return cached
    try:
        with _open_url(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        if cache is not None:
            stale = cache.get_json(cache_key, allow_expired=True)
            if isinstance(stale, dict):
                _record_event(cache, SOURCE_YAHOO, "stale", "quote 호출 실패로 만료 캐시를 사용했습니다.")
                return stale
            _record_event(cache, SOURCE_YAHOO, "error", "quote 호출 또는 파싱에 실패했습니다.")
        return {}

    result = payload.get("quoteResponse", {}).get("result", [])
    quotes = {item.get("symbol", "").upper(): item for item in result if item.get("symbol")}
    if cache is not None:
        cache.set_json(cache_key, SOURCE_YAHOO, url, quotes, QUOTE_TTL_SECONDS)
        _record_event(cache, SOURCE_YAHOO, "success", f"quote {len(quotes)}건을 수집했습니다.")
    return quotes


def fetch_momentum(ticker: str, timeout: float = 8.0, cache: CacheStore | None = None) -> Momentum:
    symbol = ticker.upper()
    params = urllib.parse.urlencode({"range": "1y", "interval": "1d"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol, safe='')}?{params}"
    cache_key = f"yahoo-momentum:{symbol}:1y:1d"
    payload: dict | None = None
    stale = False
    if cache is not None:
        cached = cache.get_json(cache_key)
        if isinstance(cached, dict):
            payload = cached
    if payload is None:
        try:
            with _open_url(url, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if cache is not None:
                cache.set_json(cache_key, SOURCE_YAHOO, url, payload, MOMENTUM_TTL_SECONDS)
                _record_event(cache, SOURCE_YAHOO, "success", f"{symbol} momentum을 수집했습니다.")
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if cache is not None:
                stale_payload = cache.get_json(cache_key, allow_expired=True)
                if isinstance(stale_payload, dict):
                    _record_event(cache, SOURCE_YAHOO, "stale", f"{symbol} momentum 호출 실패로 만료 캐시를 사용했습니다.")
                    payload = stale_payload
                    stale = True
                else:
                    _record_event(cache, SOURCE_YAHOO, "error", f"{symbol} momentum 호출 또는 파싱에 실패했습니다.")
    if payload is None:
        return Momentum()

    try:
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        opens_raw = quote.get("open") or []
        highs_raw = quote.get("high") or []
        lows_raw = quote.get("low") or []
        closes_raw = quote.get("close") or []
        volumes_raw = quote.get("volume") or []
        adjcloses = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        if cache is not None:
            _record_event(cache, SOURCE_YAHOO, "warning", f"{symbol} momentum 응답 형식이 올바르지 않습니다.")
        return Momentum(source=SOURCE_YAHOO, stale=stale)

    points: list[tuple[str, float | None, float | None, float | None, float, float | None]] = []
    for index, timestamp in enumerate(timestamps):
        close = _list_number(adjcloses, index)
        if close is None:
            close = _list_number(closes_raw, index)
        if not isinstance(timestamp, (int, float)) or close is None or close <= 0:
            continue
        points.append(
            (
                datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(),
                _list_number(opens_raw, index),
                _list_number(highs_raw, index),
                _list_number(lows_raw, index),
                close,
                _list_number(volumes_raw, index),
            )
        )

    if not points:
        if cache is not None:
            _record_event(cache, SOURCE_YAHOO, "warning", f"{symbol} momentum 가격 앵커가 비어 있습니다.")
        return Momentum(source=SOURCE_YAHOO, stale=stale)

    opens = [_price_or_close(open_price, close) for _, open_price, _, _, close, _ in points]
    highs = [_price_or_close(high, close) for _, _, high, _, close, _ in points]
    lows = [_price_or_close(low, close) for _, _, _, low, close, _ in points]
    closes = [close for _, _, _, _, close, _ in points]
    volumes = [volume for _, _, _, _, _, volume in points]
    latest = closes[-1]
    latest_open = opens[-1]
    latest_high = highs[-1]
    latest_low = lows[-1]
    recent = closes[-126:] if len(closes) > 126 else closes
    recent_highs = highs[-126:] if len(highs) > 126 else highs
    recent_lows = lows[-126:] if len(lows) > 126 else lows
    high = max(recent_highs)
    low = min(recent_lows)
    ma20_values = moving_average(closes, 20)
    ma60_values = moving_average(closes, 60)
    ma120_values = moving_average(closes, 120)
    ma150_values = moving_average(closes, 150)
    ma200_values = moving_average(closes, 200)
    bollinger_upper_values, bollinger_middle_values, bollinger_lower_values = bollinger_bands(
        closes, 20, 2.0
    )
    latest_bollinger_upper = _last_finite(bollinger_upper_values)
    latest_bollinger_middle = _last_finite(bollinger_middle_values)
    latest_bollinger_lower = _last_finite(bollinger_lower_values)
    latest_volume = volumes[-1] if volumes else None
    avg_volume_20 = average_recent_volume(volumes, 20)
    volume_zone = volume_profile_zone(highs, lows, closes, volumes)
    structure = structure_zone(highs, lows, closes, volumes, volume_zone=volume_zone)
    swing_high = previous_swing_high(highs)
    return Momentum(
        one_month_pct=_pct_change(closes, 21) if len(closes) >= 22 else None,
        three_month_pct=_pct_change(closes, 63) if len(closes) >= 64 else None,
        six_month_pct=_pct_change(closes, min(126, len(closes) - 1)) if len(closes) >= 2 else None,
        drawdown_from_high_pct=_pct_from_high(latest, high),
        range_position_pct=_range_position(latest, low, high),
        latest_open=latest_open,
        latest_high=latest_high,
        latest_low=latest_low,
        latest_close=latest,
        previous_close=closes[-2] if len(closes) >= 2 else None,
        latest_close_date=points[-1][0],
        six_month_high=high,
        six_month_low=low,
        ma20=_last_finite(ma20_values),
        ma60=_last_finite(ma60_values),
        ma120=_last_finite(ma120_values),
        ma150=_last_finite(ma150_values),
        ma200=_last_finite(ma200_values),
        rsi14=rsi(closes, 14),
        ma20_distance_pct=distance_from_average(latest, _last_finite(ma20_values)),
        ma60_distance_pct=distance_from_average(latest, _last_finite(ma60_values)),
        ma120_distance_pct=distance_from_average(latest, _last_finite(ma120_values)),
        ma150_distance_pct=distance_from_average(latest, _last_finite(ma150_values)),
        ma200_distance_pct=distance_from_average(latest, _last_finite(ma200_values)),
        ma20_slope_pct=moving_average_slope(ma20_values),
        ma60_slope_pct=moving_average_slope(ma60_values),
        ma150_slope_pct=moving_average_slope(ma150_values),
        ma200_slope_pct=moving_average_slope(ma200_values),
        latest_volume=latest_volume,
        avg_volume_20=avg_volume_20,
        volume_ratio=volume_ratio(latest_volume, avg_volume_20),
        twenty_day_breakout_pct=breakout_pct(closes, 20),
        sixty_day_breakout_pct=breakout_pct(closes, 60),
        bollinger_upper=latest_bollinger_upper,
        bollinger_middle=latest_bollinger_middle,
        bollinger_lower=latest_bollinger_lower,
        bollinger_bandwidth_pct=bollinger_bandwidth_pct(
            latest_bollinger_upper, latest_bollinger_middle, latest_bollinger_lower
        ),
        bollinger_percent_b=bollinger_percent_b(
            latest, latest_bollinger_upper, latest_bollinger_lower
        ),
        volume_zone_lower=volume_zone.lower if volume_zone else None,
        volume_zone_upper=volume_zone.upper if volume_zone else None,
        volume_zone_strength=volume_zone.strength if volume_zone else None,
        volume_zone_contains_latest=volume_zone.contains_latest if volume_zone else False,
        previous_swing_high=swing_high,
        previous_swing_high_distance_pct=distance_from_average(latest, swing_high),
        structure_zone_lower=structure.lower if structure else None,
        structure_zone_upper=structure.upper if structure else None,
        structure_zone_strength=structure.strength if structure else None,
        support_retest_lower=structure.support_lower if structure else None,
        support_retest_upper=structure.support_upper if structure else None,
        nearest_resistance=structure.nearest_resistance if structure else None,
        major_resistance=structure.major_resistance if structure else None,
        rejection_from_structure_zone=structure.rejection_from_zone if structure else False,
        support_retest_active=structure.support_retest_active if structure else False,
        ohlcv_coverage_pct=ohlcv_coverage_pct(
            _OhlcvPoint(open_price, high_price, low_price, close, volume)
            for _, open_price, high_price, low_price, close, volume in points
        ),
        source=SOURCE_YAHOO,
        stale=stale,
    )


def fetch_many_momentums(
    tickers: Iterable[str],
    timeout: float = 8.0,
    cache: CacheStore | None = None,
    max_workers: int = 6,
) -> dict[str, Momentum]:
    symbols = tuple(dict.fromkeys(ticker.upper() for ticker in tickers))
    if not symbols:
        return {}
    results: dict[str, Momentum] = {}
    workers = max(1, min(max_workers, len(symbols)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_momentum, ticker, timeout=timeout, cache=cache): ticker
            for ticker in symbols
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results[ticker] = future.result()
            except Exception as exc:  # pragma: no cover - defensive worker boundary
                if cache is not None:
                    _record_event(cache, SOURCE_YAHOO, "error", f"{ticker} momentum 처리 실패: {exc}")
                results[ticker] = Momentum()
    return results


def average_industry_momentum(
    industry: str, stocks: Iterable[StockProfile], momentums: dict[str, Momentum]
) -> float | None:
    values: list[float] = []
    for stock in stocks:
        if stock.industry != industry:
            continue
        momentum = momentums.get(stock.ticker.upper(), Momentum())
        score = momentum_to_score(momentum)
        if score is not None:
            values.append(score)
    if not values:
        return None
    return statistics.fmean(values)


def momentum_to_score(momentum: Momentum) -> float | None:
    values = [
        value
        for value in (momentum.one_month_pct, momentum.three_month_pct, momentum.six_month_pct)
        if value is not None and math.isfinite(value)
    ]
    if not values:
        return None
    weighted = (
        (momentum.one_month_pct or 0) * 0.45
        + (momentum.three_month_pct or 0) * 0.35
        + (momentum.six_month_pct or 0) * 0.20
    )
    return _clamp(50 + weighted, 0, 100)


def _pct_change(values: list[float], lookback: int) -> float | None:
    if len(values) <= lookback:
        return None
    start = values[-lookback - 1]
    end = values[-1]
    if start <= 0:
        return None
    return ((end / start) - 1) * 100


def _pct_from_high(latest: float, high: float) -> float | None:
    if high <= 0:
        return None
    return ((latest / high) - 1) * 100


def _range_position(latest: float, low: float, high: float) -> float | None:
    if high <= low:
        return 50.0
    return _clamp((latest - low) / (high - low) * 100, 0, 100)


def _polygon_symbol(symbol: str) -> str:
    return symbol.upper().replace("-", ".")


def _polygon_url(path: str, api_key: str, params: dict[str, str] | None = None) -> tuple[str, str]:
    query = dict(params or {})
    query["apiKey"] = api_key
    public_query = dict(params or {})
    url = f"{POLYGON_API_BASE_URL}{path}?{urllib.parse.urlencode(query)}"
    public_url = f"{POLYGON_API_BASE_URL}{path}"
    if public_query:
        public_url = f"{public_url}?{urllib.parse.urlencode(public_query)}"
    return url, public_url


def _recent_weekdays(today: date, lookback_days: int) -> Iterable[date]:
    for offset in range(0, lookback_days):
        candidate = today - timedelta(days=offset)
        if candidate.weekday() < 5:
            yield candidate


def _open_url(url: str, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)


def _xml_text(node: ET.Element, child_name: str) -> str | None:
    child = node.find(child_name)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _number_or_existing(value: object, existing: float | None) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return existing


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _list_number(values: list, index: int) -> float | None:
    if index >= len(values):
        return None
    value = values[index]
    return float(value) if _is_number(value) else None


def _price_or_close(value: float | None, close: float) -> float:
    return float(value) if value is not None and math.isfinite(value) and value > 0 else float(close)


def _field_source(source: str, url: object = None) -> dict:
    payload = {
        "source": source,
        "periodEnd": None,
        "fiscalYear": None,
        "filed": None,
        "form": None,
        "reportCode": None,
        "fallback": False,
    }
    if isinstance(url, str) and url:
        payload["url"] = url
    return payload


def _text_or_existing(value: object, existing: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return existing


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _news_items_to_payload(items: Iterable[NewsItem]) -> list[dict]:
    return [
        {
            "title": item.title,
            "source": item.source,
            "published": item.published,
            "url": item.url,
            "summary": item.summary,
        }
        for item in items
    ]


def _news_items_from_payload(payload: list) -> tuple[NewsItem, ...]:
    items: list[NewsItem] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str):
            continue
        items.append(
            NewsItem(
                title=item["title"],
                source=str(item.get("source") or SOURCE_GOOGLE_NEWS),
                published=item.get("published") if isinstance(item.get("published"), str) else None,
                url=item.get("url") if isinstance(item.get("url"), str) else None,
                summary=item.get("summary") if isinstance(item.get("summary"), str) else None,
            )
        )
    return tuple(items)


def _digest(value: str) -> str:
    return sha1(value.encode("utf-8")).hexdigest()


def _record_event(
    cache: CacheStore,
    source: str,
    event_type: str,
    message: str,
    metadata: dict | None = None,
) -> None:
    try:
        cache.record_source_event(source, event_type, message, metadata=metadata)
    except Exception:
        return
