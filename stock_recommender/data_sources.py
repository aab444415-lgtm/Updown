from __future__ import annotations

import json
import math
import statistics
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import replace

from .models import Fundamentals, Momentum, NewsItem, StockProfile


USER_AGENT = "stock-recommender-mvp/0.1"


def fetch_news(industry_terms: Iterable[str], limit: int = 30, timeout: float = 8.0) -> tuple[NewsItem, ...]:
    query = " OR ".join(f'"{term}"' if " " in term else term for term in industry_terms)
    params = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    url = f"https://news.google.com/rss/search?{params}"
    try:
        with _open_url(url, timeout=timeout) as response:
            payload = response.read()
    except (OSError, urllib.error.URLError, TimeoutError):
        return ()

    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
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
    return tuple(items)


def enrich_with_live_market_data(
    stocks: Iterable[StockProfile], timeout: float = 8.0
) -> tuple[StockProfile, ...]:
    profiles = list(stocks)
    quotes = fetch_yahoo_quotes([stock.ticker for stock in profiles], timeout=timeout)
    enriched: list[StockProfile] = []
    for stock in profiles:
        quote = quotes.get(stock.ticker.upper(), {})
        fundamentals = stock.fundamentals
        if quote:
            fundamentals = replace(
                fundamentals,
                pe=_number_or_existing(quote.get("trailingPE"), fundamentals.pe),
                forward_pe=_number_or_existing(quote.get("forwardPE"), fundamentals.forward_pe),
                market_cap_usd=_number_or_existing(quote.get("marketCap"), fundamentals.market_cap_usd),
                market_cap_currency=_text_or_existing(
                    quote.get("financialCurrency") or quote.get("currency"),
                    fundamentals.market_cap_currency,
                ),
            )
        enriched.append(replace(stock, fundamentals=fundamentals))
    return tuple(enriched)


def fetch_yahoo_quotes(tickers: Iterable[str], timeout: float = 8.0) -> dict[str, dict]:
    symbols = ",".join(sorted({ticker.upper() for ticker in tickers}))
    if not symbols:
        return {}

    params = urllib.parse.urlencode({"symbols": symbols})
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?{params}"
    try:
        with _open_url(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {}

    result = payload.get("quoteResponse", {}).get("result", [])
    return {item.get("symbol", "").upper(): item for item in result if item.get("symbol")}


def fetch_momentum(ticker: str, timeout: float = 8.0) -> Momentum:
    params = urllib.parse.urlencode({"range": "6mo", "interval": "1d"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?{params}"
    try:
        with _open_url(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return Momentum()

    try:
        quote = payload["chart"]["result"][0]["indicators"]["quote"][0]
        closes = [value for value in quote.get("close", []) if isinstance(value, (int, float)) and value > 0]
    except (KeyError, IndexError, TypeError):
        return Momentum()

    if len(closes) < 22:
        return Momentum()

    return Momentum(
        one_month_pct=_pct_change(closes, 21),
        three_month_pct=_pct_change(closes, 63),
        six_month_pct=_pct_change(closes, min(126, len(closes) - 1)),
    )


def fetch_many_momentums(tickers: Iterable[str], timeout: float = 8.0) -> dict[str, Momentum]:
    return {ticker.upper(): fetch_momentum(ticker, timeout=timeout) for ticker in tickers}


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


def _text_or_existing(value: object, existing: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return existing


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
