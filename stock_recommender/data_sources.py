from __future__ import annotations

import json
import math
import statistics
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha1

from .models import Fundamentals, Momentum, NewsItem, StockProfile
from .storage import CacheStore


USER_AGENT = "stock-recommender-mvp/0.1"
NEWS_TTL_SECONDS = 60 * 30
QUOTE_TTL_SECONDS = 60 * 15
MOMENTUM_TTL_SECONDS = 60 * 60 * 6
SOURCE_YAHOO = "Yahoo Finance"
SOURCE_GOOGLE_NEWS = "Google News"


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
    stocks: Iterable[StockProfile], timeout: float = 8.0, cache: CacheStore | None = None
) -> tuple[StockProfile, ...]:
    profiles = list(stocks)
    quotes = fetch_yahoo_quotes([stock.ticker for stock in profiles], timeout=timeout, cache=cache)
    enriched: list[StockProfile] = []
    for stock in profiles:
        quote = quotes.get(stock.ticker.upper(), {})
        fundamentals = stock.fundamentals
        if quote:
            sources = dict(fundamentals.sources)
            if _is_number(quote.get("trailingPE")):
                sources["pe"] = _field_source(SOURCE_YAHOO)
            if _is_number(quote.get("forwardPE")):
                sources["forwardPe"] = _field_source(SOURCE_YAHOO)
            if _is_number(quote.get("marketCap")):
                sources["marketCap"] = _field_source(SOURCE_YAHOO)
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
    params = urllib.parse.urlencode({"range": "6mo", "interval": "1d"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol, safe='')}?{params}"
    cache_key = f"yahoo-momentum:{symbol}:6mo:1d"
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
        closes_raw = quote.get("close") or []
        adjcloses = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        if cache is not None:
            _record_event(cache, SOURCE_YAHOO, "warning", f"{symbol} momentum 응답 형식이 올바르지 않습니다.")
        return Momentum(source=SOURCE_YAHOO, stale=stale)

    points: list[tuple[str, float]] = []
    for index, timestamp in enumerate(timestamps):
        close = _list_number(adjcloses, index)
        if close is None:
            close = _list_number(closes_raw, index)
        if not isinstance(timestamp, (int, float)) or close is None or close <= 0:
            continue
        points.append((datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(), close))

    if not points:
        if cache is not None:
            _record_event(cache, SOURCE_YAHOO, "warning", f"{symbol} momentum 가격 앵커가 비어 있습니다.")
        return Momentum(source=SOURCE_YAHOO, stale=stale)

    closes = [close for _, close in points]
    latest = closes[-1]
    high = max(closes)
    low = min(closes)
    return Momentum(
        one_month_pct=_pct_change(closes, 21) if len(closes) >= 22 else None,
        three_month_pct=_pct_change(closes, 63) if len(closes) >= 64 else None,
        six_month_pct=_pct_change(closes, min(126, len(closes) - 1)) if len(closes) >= 2 else None,
        drawdown_from_high_pct=_pct_from_high(latest, high),
        range_position_pct=_range_position(latest, low, high),
        latest_close=latest,
        latest_close_date=points[-1][0],
        six_month_high=high,
        six_month_low=low,
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


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _list_number(values: list, index: int) -> float | None:
    if index >= len(values):
        return None
    value = values[index]
    return float(value) if _is_number(value) else None


def _field_source(source: str) -> dict:
    return {
        "source": source,
        "periodEnd": None,
        "fiscalYear": None,
        "filed": None,
        "form": None,
        "reportCode": None,
        "fallback": False,
    }


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


def _record_event(cache: CacheStore, source: str, event_type: str, message: str) -> None:
    try:
        cache.record_source_event(source, event_type, message)
    except Exception:
        return
