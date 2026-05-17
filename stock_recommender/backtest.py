from __future__ import annotations

import json
import math
import statistics
import urllib.error
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .config import load_config
from .data_sources import _open_url
from .models import IndustryProfile, Momentum, StockProfile
from .scoring import score_industries, score_stocks
from .storage import CacheStore
from .universe import DEFAULT_MACRO_CONTEXT, INDUSTRIES, STOCKS


BENCHMARKS = ("SPY", "QQQ", "^KS11")


@dataclass(frozen=True)
class PricePoint:
    date: date
    close: float


@dataclass(frozen=True)
class BacktestPeriod:
    start_date: date
    end_date: date
    tickers: tuple[str, ...]
    names: tuple[str, ...]
    return_pct: float
    benchmark_return_pct: float
    alpha_pct: float


@dataclass(frozen=True)
class BenchmarkResult:
    ticker: str
    return_pct: float | None


@dataclass(frozen=True)
class BacktestResult:
    created_at: datetime
    months: int
    top_n: int
    benchmark_ticker: str
    periods: tuple[BacktestPeriod, ...]
    strategy_return_pct: float | None
    benchmark_return_pct: float | None
    alpha_pct: float | None
    average_monthly_return_pct: float | None
    win_rate_pct: float | None
    hit_rate_pct: float | None
    max_drawdown_pct: float | None
    volatility_pct: float | None
    data_coverage_pct: float
    benchmark_results: tuple[BenchmarkResult, ...]
    warnings: tuple[str, ...]

    @property
    def period_count(self) -> int:
        return len(self.periods)


def create_backtest(
    months: int = 12,
    top_n: int = 5,
    benchmark_ticker: str = "SPY",
    timeout: float = 8.0,
) -> BacktestResult:
    months = _clamp_int(months, 3, 36)
    top_n = _clamp_int(top_n, 3, 10)
    benchmark_ticker = benchmark_ticker.upper()
    if benchmark_ticker not in BENCHMARKS:
        benchmark_ticker = "SPY"

    config = load_config()
    cache = CacheStore(config.cache_db_path)
    tickers = tuple(dict.fromkeys(stock.ticker.upper() for stock in STOCKS))
    benchmark_tickers = tuple(dict.fromkeys((benchmark_ticker, *BENCHMARKS)))
    range_value = _history_range_for_months(months)
    histories = {
        ticker: fetch_price_history(ticker, cache=cache, range_value=range_value, timeout=timeout)
        for ticker in (*tickers, *benchmark_tickers)
    }
    return run_backtest(
        stocks=STOCKS,
        industries=INDUSTRIES,
        histories=histories,
        months=months,
        top_n=top_n,
        benchmark_ticker=benchmark_ticker,
    )


def run_backtest(
    stocks: Iterable[StockProfile],
    industries: Iterable[IndustryProfile],
    histories: dict[str, tuple[PricePoint, ...]],
    months: int = 12,
    top_n: int = 5,
    benchmark_ticker: str = "SPY",
) -> BacktestResult:
    stocks_tuple = tuple(stocks)
    industries_tuple = tuple(industries)
    benchmark_ticker = benchmark_ticker.upper()
    warnings: list[str] = []
    benchmark_history = histories.get(benchmark_ticker, ())
    month_ends = _month_end_dates(benchmark_history)
    if len(month_ends) < 2:
        return _empty_result(months, top_n, benchmark_ticker, "벤치마크 가격 데이터를 충분히 가져오지 못했습니다.")

    period_dates = month_ends[-(months + 1) :]
    if len(period_dates) < months + 1:
        warnings.append(f"요청한 {months}개월보다 짧은 {len(period_dates) - 1}개월만 검증했습니다.")

    available_histories = {
        ticker: points for ticker, points in histories.items() if _valid_history(points)
    }
    stock_history_count = sum(1 for stock in stocks_tuple if stock.ticker.upper() in available_histories)
    data_coverage = (stock_history_count / len(stocks_tuple) * 100) if stocks_tuple else 0
    missing = [stock.ticker for stock in stocks_tuple if stock.ticker.upper() not in available_histories]
    if missing:
        warnings.append("가격 데이터 부족으로 제외된 종목: " + ", ".join(missing[:8]))

    industry_scores = score_industries(
        macro_context=DEFAULT_MACRO_CONTEXT,
        industries=industries_tuple,
        stocks=stocks_tuple,
        news_items=(),
        momentums={},
    )

    periods: list[BacktestPeriod] = []
    for start_date, end_date in zip(period_dates, period_dates[1:]):
        eligible_stocks: list[StockProfile] = []
        momentums: dict[str, Momentum] = {}
        returns: dict[str, float] = {}
        for stock in stocks_tuple:
            ticker = stock.ticker.upper()
            history = available_histories.get(ticker, ())
            start_price = _price_on_or_before(history, start_date)
            end_price = _price_on_or_before(history, end_date)
            if start_price is None or end_price is None or start_price <= 0:
                continue
            eligible_stocks.append(stock)
            momentums[ticker] = _momentum_until(history, start_date)
            returns[ticker] = ((end_price / start_price) - 1) * 100

        if len(eligible_stocks) < top_n:
            continue

        scores = score_stocks(eligible_stocks, industry_scores, momentums)
        selected = tuple(score.stock for score in scores[:top_n])
        selected_returns = [returns[stock.ticker.upper()] for stock in selected if stock.ticker.upper() in returns]
        benchmark_return = _period_return(benchmark_history, start_date, end_date)
        if not selected_returns or benchmark_return is None:
            continue

        period_return = statistics.fmean(selected_returns)
        periods.append(
            BacktestPeriod(
                start_date=start_date,
                end_date=end_date,
                tickers=tuple(stock.ticker for stock in selected),
                names=tuple(stock.name for stock in selected),
                return_pct=round(period_return, 2),
                benchmark_return_pct=round(benchmark_return, 2),
                alpha_pct=round(period_return - benchmark_return, 2),
            )
        )

    if not periods:
        return _empty_result(months, top_n, benchmark_ticker, "검증 가능한 월별 구간을 만들지 못했습니다.", data_coverage)

    strategy_returns = [period.return_pct for period in periods]
    benchmark_returns = [period.benchmark_return_pct for period in periods]
    strategy_total = _compound_return(strategy_returns)
    benchmark_total = _compound_return(benchmark_returns)
    benchmark_results = tuple(_benchmark_result(ticker, histories, periods) for ticker in BENCHMARKS)

    return BacktestResult(
        created_at=datetime.now(),
        months=months,
        top_n=top_n,
        benchmark_ticker=benchmark_ticker,
        periods=tuple(periods),
        strategy_return_pct=round(strategy_total, 2),
        benchmark_return_pct=round(benchmark_total, 2),
        alpha_pct=round(strategy_total - benchmark_total, 2),
        average_monthly_return_pct=round(statistics.fmean(strategy_returns), 2),
        win_rate_pct=round(_ratio(value > 0 for value in strategy_returns), 1),
        hit_rate_pct=round(_ratio(period.return_pct > period.benchmark_return_pct for period in periods), 1),
        max_drawdown_pct=round(_max_drawdown(strategy_returns), 2),
        volatility_pct=round(_annualized_volatility(strategy_returns), 2),
        data_coverage_pct=round(data_coverage, 1),
        benchmark_results=benchmark_results,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def fetch_price_history(
    ticker: str,
    cache: CacheStore | None = None,
    range_value: str = "3y",
    interval: str = "1d",
    timeout: float = 8.0,
) -> tuple[PricePoint, ...]:
    symbol = ticker.upper()
    encoded = urllib.parse.quote(symbol, safe="")
    params = urllib.parse.urlencode({"range": range_value, "interval": interval})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?{params}"
    cache_key = f"yahoo-history:{symbol}:{range_value}:{interval}"
    payload: dict | None = None
    if cache is not None:
        cached = cache.get_json(cache_key)
        if isinstance(cached, dict):
            payload = cached
    if payload is None:
        try:
            with _open_url(url, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if cache is not None:
                cache.set_json(cache_key, "Yahoo Finance", url, payload, ttl_seconds=60 * 60 * 12)
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if cache is not None:
                cached = cache.get_json(cache_key, allow_expired=True)
                if isinstance(cached, dict):
                    payload = cached
    if payload is None:
        return ()
    return parse_yahoo_history(payload)


def parse_yahoo_history(payload: dict) -> tuple[PricePoint, ...]:
    try:
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        closes = quote.get("close") or []
        adjcloses = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        return ()

    points: list[PricePoint] = []
    for index, timestamp in enumerate(timestamps):
        close = _list_value(adjcloses, index)
        if close is None:
            close = _list_value(closes, index)
        if not isinstance(timestamp, (int, float)) or not isinstance(close, (int, float)):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        points.append(
            PricePoint(
                date=datetime.fromtimestamp(timestamp, tz=timezone.utc).date(),
                close=float(close),
            )
        )
    return tuple(sorted(points, key=lambda item: item.date))


def backtest_to_dict(result: BacktestResult) -> dict:
    return {
        "createdAt": result.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "months": result.months,
        "topN": result.top_n,
        "benchmarkTicker": result.benchmark_ticker,
        "periodCount": result.period_count,
        "strategyReturnPct": result.strategy_return_pct,
        "benchmarkReturnPct": result.benchmark_return_pct,
        "alphaPct": result.alpha_pct,
        "averageMonthlyReturnPct": result.average_monthly_return_pct,
        "winRatePct": result.win_rate_pct,
        "hitRatePct": result.hit_rate_pct,
        "maxDrawdownPct": result.max_drawdown_pct,
        "volatilityPct": result.volatility_pct,
        "dataCoveragePct": result.data_coverage_pct,
        "benchmarks": [
            {"ticker": item.ticker, "returnPct": item.return_pct} for item in result.benchmark_results
        ],
        "warnings": list(result.warnings),
        "periods": [
            {
                "startDate": period.start_date.isoformat(),
                "endDate": period.end_date.isoformat(),
                "tickers": list(period.tickers),
                "names": list(period.names),
                "returnPct": period.return_pct,
                "benchmarkReturnPct": period.benchmark_return_pct,
                "alphaPct": period.alpha_pct,
            }
            for period in result.periods
        ],
        "assumptions": [
            "월말 리밸런싱, 동일비중 Top N 보유로 계산합니다.",
            "과거 시점의 뉴스/재무 스냅샷이 없어 현재 기본 지표와 과거 가격 모멘텀을 함께 사용합니다.",
            "거래비용, 세금, 환율 환산, 슬리피지는 아직 반영하지 않았습니다.",
        ],
    }


def render_backtest_markdown(result: BacktestResult) -> str:
    lines = [
        "# 추천 모델 백테스트",
        "",
        f"- 생성 시각: {result.created_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 검증 기간: 최근 {result.period_count}개월",
        f"- 규칙: 월말 리밸런싱, Top {result.top_n} 동일비중",
        f"- 벤치마크: {result.benchmark_ticker}",
        "",
        "## 요약",
        "",
        f"- 전략 누적수익률: {_pct_text(result.strategy_return_pct)}",
        f"- 벤치마크 누적수익률: {_pct_text(result.benchmark_return_pct)}",
        f"- 초과수익: {_pct_text(result.alpha_pct)}",
        f"- 월 승률: {_pct_text(result.win_rate_pct)}",
        f"- 벤치마크 이긴 달: {_pct_text(result.hit_rate_pct)}",
        f"- 최대낙폭: {_pct_text(result.max_drawdown_pct)}",
        f"- 연율화 변동성: {_pct_text(result.volatility_pct)}",
        f"- 가격 데이터 커버리지: {_pct_text(result.data_coverage_pct)}",
        "",
    ]
    if result.benchmark_results:
        lines.extend(["## 벤치마크 비교", ""])
        for item in result.benchmark_results:
            lines.append(f"- {item.ticker}: {_pct_text(item.return_pct)}")
        lines.append("")
    if result.warnings:
        lines.extend(["## 주의", ""])
        for warning in result.warnings:
            lines.append(f"- {warning}")
        lines.append("")
    lines.extend(["## 월별 결과", ""])
    for period in result.periods:
        lines.append(
            f"- {period.start_date.isoformat()} -> {period.end_date.isoformat()}: "
            f"전략 {_pct_text(period.return_pct)}, 벤치마크 {_pct_text(period.benchmark_return_pct)}, "
            f"초과 {_pct_text(period.alpha_pct)} / {', '.join(period.tickers)}"
        )
    lines.extend(
        [
            "",
            "## 해석상 한계",
            "",
            "- 과거 시점의 뉴스/재무 스냅샷이 없어 현재 기본 지표와 과거 가격 모멘텀을 함께 사용합니다.",
            "- 거래비용, 세금, 환율 환산, 슬리피지는 아직 반영하지 않았습니다.",
            "- 실제 투자 판단용으로 쓰려면 점수 스냅샷을 매일 저장한 뒤 그 기록으로 다시 검증해야 합니다.",
        ]
    )
    return "\n".join(lines)


def _month_end_dates(points: Iterable[PricePoint]) -> list[date]:
    by_month: dict[tuple[int, int], date] = {}
    for point in points:
        by_month[(point.date.year, point.date.month)] = point.date
    return [by_month[key] for key in sorted(by_month)]


def _valid_history(points: tuple[PricePoint, ...]) -> bool:
    return len(points) >= 80


def _price_on_or_before(points: tuple[PricePoint, ...], target: date) -> float | None:
    selected: float | None = None
    for point in points:
        if point.date > target:
            break
        selected = point.close
    return selected


def _momentum_until(points: tuple[PricePoint, ...], target: date) -> Momentum:
    closes = [point.close for point in points if point.date <= target]
    return Momentum(
        one_month_pct=_lookback_return(closes, 21),
        three_month_pct=_lookback_return(closes, 63),
        six_month_pct=_lookback_return(closes, 126),
    )


def _lookback_return(closes: list[float], lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    start = closes[-lookback - 1]
    end = closes[-1]
    if start <= 0:
        return None
    return ((end / start) - 1) * 100


def _period_return(points: tuple[PricePoint, ...], start: date, end: date) -> float | None:
    start_price = _price_on_or_before(points, start)
    end_price = _price_on_or_before(points, end)
    if start_price is None or end_price is None or start_price <= 0:
        return None
    return ((end_price / start_price) - 1) * 100


def _compound_return(returns: Iterable[float]) -> float:
    value = 1.0
    for item in returns:
        value *= 1 + item / 100
    return (value - 1) * 100


def _max_drawdown(returns: Iterable[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for item in returns:
        equity *= 1 + item / 100
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, (equity / peak - 1) * 100)
    return max_drawdown


def _annualized_volatility(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(12)


def _ratio(items: Iterable[bool]) -> float:
    values = list(items)
    if not values:
        return 0
    return sum(1 for item in values if item) / len(values) * 100


def _benchmark_result(
    ticker: str,
    histories: dict[str, tuple[PricePoint, ...]],
    periods: tuple[BacktestPeriod, ...] | list[BacktestPeriod],
) -> BenchmarkResult:
    return BenchmarkResult(ticker=ticker, return_pct=_total_benchmark_return(histories.get(ticker, ()), periods))


def _total_benchmark_return(
    points: tuple[PricePoint, ...], periods: tuple[BacktestPeriod, ...] | list[BacktestPeriod]
) -> float | None:
    if not periods:
        return None
    returns: list[float] = []
    for period in periods:
        value = _period_return(points, period.start_date, period.end_date)
        if value is None:
            return None
        returns.append(value)
    return round(_compound_return(returns), 2)


def _empty_result(
    months: int,
    top_n: int,
    benchmark_ticker: str,
    warning: str,
    data_coverage_pct: float = 0,
) -> BacktestResult:
    return BacktestResult(
        created_at=datetime.now(),
        months=months,
        top_n=top_n,
        benchmark_ticker=benchmark_ticker,
        periods=(),
        strategy_return_pct=None,
        benchmark_return_pct=None,
        alpha_pct=None,
        average_monthly_return_pct=None,
        win_rate_pct=None,
        hit_rate_pct=None,
        max_drawdown_pct=None,
        volatility_pct=None,
        data_coverage_pct=round(data_coverage_pct, 1),
        benchmark_results=(),
        warnings=(warning,),
    )


def _history_range_for_months(months: int) -> str:
    if months <= 12:
        return "2y"
    if months <= 24:
        return "3y"
    return "5y"


def _list_value(values: list, index: int) -> float | None:
    if index >= len(values):
        return None
    value = values[index]
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _pct_text(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}%"
