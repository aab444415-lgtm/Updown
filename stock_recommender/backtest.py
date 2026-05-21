from __future__ import annotations

import json
import math
import statistics
import urllib.error
import urllib.parse
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .config import AppConfig, load_config
from .data_sources import _open_url
from .models import IndustryProfile, Momentum, StockProfile
from .scoring import (
    score_industries,
    score_long_term_candidates,
    score_medium_term_candidates,
    score_short_term_candidates,
    score_stocks,
)
from .snapshot_store import list_snapshot_rows
from .storage import CacheStore
from .technical import (
    _last_finite,
    average_recent_volume,
    breakout_pct,
    distance_from_average,
    moving_average,
    moving_average_slope,
    rsi,
    volume_ratio,
)
from .time_utils import now_in_app_timezone
from .universe import DEFAULT_MACRO_CONTEXT, INDUSTRIES, STOCKS


BENCHMARKS = ("SPY", "QQQ", "^KS11")
BACKTEST_METHODS = ("snapshot", "legacy")
BACKTEST_HORIZONS = ("overall", "short", "medium", "long")


@dataclass(frozen=True)
class PricePoint:
    date: date
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class BacktestPeriod:
    start_date: date
    end_date: date
    tickers: tuple[str, ...]
    names: tuple[str, ...]
    return_pct: float
    benchmark_return_pct: float
    alpha_pct: float
    snapshot_date: str | None = None
    price_source: str = "liveHistory"
    anchor_coverage_pct: float = 0
    period_status: str = "included"
    excluded_reason: str | None = None


@dataclass(frozen=True)
class BenchmarkResult:
    ticker: str
    return_pct: float | None


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_date: date
    payload: dict


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
    method: str = "legacy"
    point_in_time: bool = False
    snapshot_days: int = 0
    snapshot_coverage_pct: float = 0
    required_snapshot_days: int = 0
    created_at_timezone: str = ""
    price_source: str = "liveHistory"
    horizon: str = "overall"

    @property
    def period_count(self) -> int:
        return len(self.periods)


def create_backtest(
    months: int = 12,
    top_n: int = 5,
    benchmark_ticker: str = "SPY",
    timeout: float = 8.0,
    method: str = "snapshot",
    horizon: str = "overall",
) -> BacktestResult:
    months = _clamp_int(months, 3, 36)
    top_n = _clamp_int(top_n, 3, 10)
    benchmark_ticker = benchmark_ticker.upper()
    if benchmark_ticker not in BENCHMARKS:
        benchmark_ticker = "SPY"
    method = _normalize_method(method)
    horizon = _normalize_horizon(horizon)

    config = load_config()
    cache = CacheStore(config.cache_db_path)
    created_at = now_in_app_timezone(config)
    if method == "snapshot":
        return create_snapshot_backtest(
            config=config,
            cache=cache,
            months=months,
            top_n=top_n,
            benchmark_ticker=benchmark_ticker,
            timeout=timeout,
            created_at=created_at,
            timezone_name=config.timezone_name,
            horizon=horizon,
        )

    tickers = tuple(dict.fromkeys(stock.ticker.upper() for stock in STOCKS))
    benchmark_tickers = tuple(dict.fromkeys((benchmark_ticker, *BENCHMARKS)))
    range_value = _history_range_for_months(months)
    histories = fetch_price_histories(
        (*tickers, *benchmark_tickers),
        cache=cache,
        range_value=range_value,
        timeout=timeout,
    )
    return run_backtest(
        stocks=STOCKS,
        industries=INDUSTRIES,
        histories=histories,
        months=months,
        top_n=top_n,
        benchmark_ticker=benchmark_ticker,
        created_at=created_at,
        timezone_name=config.timezone_name,
        method="legacy",
        horizon=horizon,
    )


def create_snapshot_backtest(
    config: AppConfig,
    cache: CacheStore,
    months: int,
    top_n: int,
    benchmark_ticker: str,
    timeout: float,
    created_at: datetime,
    timezone_name: str,
    horizon: str = "overall",
) -> BacktestResult:
    rows = list_snapshot_rows(config, cache, limit=max(365, months * 40), mode="live")
    snapshots = _snapshot_records(rows)
    if len({snapshot.snapshot_date for snapshot in snapshots}) < 2:
        return run_snapshot_backtest(
            snapshots=snapshots,
            histories={},
            months=months,
            top_n=top_n,
            benchmark_ticker=benchmark_ticker,
            created_at=created_at,
            timezone_name=timezone_name,
            horizon=horizon,
        )

    snapshot_tickers = tuple(
        sorted(
            {
                ticker
                for snapshot in snapshots
                for ticker in _snapshot_tickers(snapshot.payload, top_n=top_n, horizon=horizon)
            }
        )
    )
    benchmark_tickers = tuple(dict.fromkeys((benchmark_ticker, *BENCHMARKS)))
    range_value = _history_range_for_months(months)
    histories = fetch_price_histories(
        (*snapshot_tickers, *benchmark_tickers),
        cache=cache,
        range_value=range_value,
        timeout=timeout,
    )
    return run_snapshot_backtest(
        snapshots=snapshots,
        histories=histories,
        months=months,
        top_n=top_n,
        benchmark_ticker=benchmark_ticker,
        created_at=created_at,
        timezone_name=timezone_name,
        horizon=horizon,
    )


def run_backtest(
    stocks: Iterable[StockProfile],
    industries: Iterable[IndustryProfile],
    histories: dict[str, tuple[PricePoint, ...]],
    months: int = 12,
    top_n: int = 5,
    benchmark_ticker: str = "SPY",
    created_at: datetime | None = None,
    timezone_name: str = "",
    method: str = "legacy",
    horizon: str = "overall",
) -> BacktestResult:
    stocks_tuple = tuple(stocks)
    industries_tuple = tuple(industries)
    benchmark_ticker = benchmark_ticker.upper()
    horizon = _normalize_horizon(horizon)
    warnings: list[str] = []
    benchmark_history = histories.get(benchmark_ticker, ())
    month_ends = _month_end_dates(benchmark_history)
    if len(month_ends) < 2:
        return _empty_result(
            months,
            top_n,
            benchmark_ticker,
            "벤치마크 가격 데이터를 충분히 가져오지 못했습니다.",
            created_at=created_at,
            timezone_name=timezone_name,
            method=method,
            horizon=horizon,
        )

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
        selected = _legacy_selected_stocks(
            horizon=horizon,
            scores=scores,
            industry_scores=industry_scores,
            momentums=momentums,
            top_n=top_n,
        )
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
        return _empty_result(
            months,
            top_n,
            benchmark_ticker,
            "검증 가능한 월별 구간을 만들지 못했습니다.",
            data_coverage,
            created_at=created_at,
            timezone_name=timezone_name,
            method=method,
            horizon=horizon,
        )

    strategy_returns = [period.return_pct for period in periods]
    benchmark_returns = [period.benchmark_return_pct for period in periods]
    strategy_total = _compound_return(strategy_returns)
    benchmark_total = _compound_return(benchmark_returns)
    benchmark_results = tuple(_benchmark_result(ticker, histories, periods) for ticker in BENCHMARKS)

    return BacktestResult(
        created_at=created_at or now_in_app_timezone(),
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
        volatility_pct=_round_optional(_annualized_volatility(strategy_returns), 2),
        data_coverage_pct=round(data_coverage, 1),
        benchmark_results=benchmark_results,
        warnings=tuple(dict.fromkeys(warnings)),
        method=method,
        point_in_time=False,
        created_at_timezone=timezone_name,
        horizon=horizon,
    )


def run_snapshot_backtest(
    snapshots: tuple[SnapshotRecord, ...],
    histories: dict[str, tuple[PricePoint, ...]],
    months: int = 12,
    top_n: int = 5,
    benchmark_ticker: str = "SPY",
    created_at: datetime | None = None,
    timezone_name: str = "",
    horizon: str = "overall",
) -> BacktestResult:
    benchmark_ticker = benchmark_ticker.upper()
    horizon = _normalize_horizon(horizon)
    benchmark_history = histories.get(benchmark_ticker, ())
    required_snapshot_days = months + 1
    snapshot_days = len({snapshot.snapshot_date for snapshot in snapshots})
    if snapshot_days < 2:
        return _empty_result(
            months,
            top_n,
            benchmark_ticker,
            "저장된 추천 스냅샷이 부족해 포인트인타임 백테스트를 만들지 못했습니다. 먼저 2일 이상 스냅샷을 저장하세요.",
            created_at=created_at,
            timezone_name=timezone_name,
            method="snapshot",
            point_in_time=True,
            snapshot_days=snapshot_days,
            snapshot_coverage_pct=0,
            required_snapshot_days=required_snapshot_days,
            price_source="snapshotAnchors",
            horizon=horizon,
        )

    period_dates = _snapshot_period_dates(benchmark_history, snapshots, months)
    if len(period_dates) < 2:
        return _empty_result(
            months,
            top_n,
            benchmark_ticker,
            "벤치마크 가격 데이터 또는 스냅샷 가격 앵커가 부족합니다.",
            created_at=created_at,
            timezone_name=timezone_name,
            method="snapshot",
            point_in_time=True,
            snapshot_days=snapshot_days,
            required_snapshot_days=required_snapshot_days,
            price_source="unknown",
            horizon=horizon,
        )

    possible_periods = max(0, len(period_dates) - 1)

    warnings: list[str] = []
    periods: list[BacktestPeriod] = []
    periods_with_snapshot = 0
    unique_selected_tickers: set[str] = set()
    price_ready_tickers: set[str] = set()
    skipped_snapshot_warnings: set[str] = set()
    for start_date, end_date in zip(period_dates, period_dates[1:]):
        snapshot = _latest_eligible_snapshot_on_or_before(
            snapshots, start_date, warnings, skipped_snapshot_warnings
        )
        if snapshot is None:
            continue
        end_snapshot = _latest_eligible_snapshot_on_or_before(
            snapshots, end_date, warnings, skipped_snapshot_warnings
        )
        periods_with_snapshot += 1
        selected = _snapshot_top_stocks(snapshot.payload, top_n, horizon)
        if len(selected) < top_n:
            warnings.append(f"{snapshot.snapshot_date.isoformat()} 스냅샷의 종목 수가 Top {top_n}보다 부족합니다.")
            continue

        selected_returns: list[float] = []
        selected_tickers: list[str] = []
        selected_names: list[str] = []
        missing_tickers: list[str] = []
        fallback_used = False
        anchor_hits = 0
        anchor_possible = top_n + 1
        for item in selected:
            ticker = str(item.get("ticker", "")).upper()
            name = str(item.get("name") or ticker)
            if not ticker:
                continue
            unique_selected_tickers.add(ticker)
            anchor_return = (
                _anchor_return(snapshot.payload, end_snapshot.payload, ticker)
                if end_snapshot is not None
                else None
            )
            if anchor_return is None:
                fallback_used = True
                history = histories.get(ticker, ())
                start_price = _price_on_or_before(history, start_date)
                end_price = _price_on_or_before(history, end_date)
                if start_price is None or end_price is None or start_price <= 0:
                    missing_tickers.append(ticker)
                    continue
                selected_returns.append(((end_price / start_price) - 1) * 100)
            else:
                selected_returns.append(anchor_return)
                anchor_hits += 1
            price_ready_tickers.add(ticker)
            selected_tickers.append(ticker)
            selected_names.append(name)

        benchmark_return = (
            _anchor_return(snapshot.payload, end_snapshot.payload, benchmark_ticker)
            if end_snapshot is not None
            else None
        )
        if benchmark_return is None:
            fallback_used = True
            benchmark_return = _period_return(benchmark_history, start_date, end_date)
        else:
            anchor_hits += 1
        if len(selected_returns) < top_n or benchmark_return is None:
            if missing_tickers:
                warnings.append("가격 데이터 부족으로 스냅샷 구간 제외: " + ", ".join(missing_tickers[:8]))
            continue
        price_source = "liveHistoryFallback" if fallback_used else "snapshotAnchors"
        anchor_coverage_pct = round(anchor_hits / anchor_possible * 100, 1) if anchor_possible else 0
        if fallback_used:
            warnings.append(
                f"{start_date.isoformat()}~{end_date.isoformat()} 구간은 스냅샷 가격 앵커 부족으로 Yahoo history fallback을 사용했습니다."
            )

        period_return = statistics.fmean(selected_returns)
        periods.append(
            BacktestPeriod(
                start_date=start_date,
                end_date=end_date,
                tickers=tuple(selected_tickers),
                names=tuple(selected_names),
                return_pct=round(period_return, 2),
                benchmark_return_pct=round(benchmark_return, 2),
                alpha_pct=round(period_return - benchmark_return, 2),
                snapshot_date=snapshot.snapshot_date.isoformat(),
                price_source=price_source,
                anchor_coverage_pct=anchor_coverage_pct,
                period_status="included",
                excluded_reason=None,
            )
        )

    snapshot_coverage = (periods_with_snapshot / possible_periods * 100) if possible_periods else 0
    data_coverage = (
        len(price_ready_tickers) / len(unique_selected_tickers) * 100
        if unique_selected_tickers
        else 0
    )
    if not periods:
        return _empty_result(
            months,
            top_n,
            benchmark_ticker,
            "스냅샷은 있으나 가격 데이터와 리밸런싱 구간을 연결하지 못했습니다.",
            data_coverage,
            created_at=created_at,
            timezone_name=timezone_name,
            method="snapshot",
            point_in_time=True,
            snapshot_days=snapshot_days,
            snapshot_coverage_pct=snapshot_coverage,
            required_snapshot_days=required_snapshot_days,
            price_source="liveHistoryFallback",
            extra_warnings=tuple(warnings),
            horizon=horizon,
        )

    strategy_returns = [period.return_pct for period in periods]
    benchmark_returns = [period.benchmark_return_pct for period in periods]
    strategy_total = _compound_return(strategy_returns)
    benchmark_total = _compound_return(benchmark_returns)
    benchmark_results = tuple(_benchmark_result(ticker, histories, periods) for ticker in BENCHMARKS)
    price_source = (
        "snapshotAnchors"
        if all(period.price_source == "snapshotAnchors" for period in periods)
        else "liveHistoryFallback"
    )

    return BacktestResult(
        created_at=created_at or now_in_app_timezone(),
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
        volatility_pct=_round_optional(_annualized_volatility(strategy_returns), 2),
        data_coverage_pct=round(data_coverage, 1),
        benchmark_results=benchmark_results,
        warnings=tuple(dict.fromkeys(warnings)),
        method="snapshot",
        point_in_time=True,
        snapshot_days=snapshot_days,
        snapshot_coverage_pct=round(snapshot_coverage, 1),
        required_snapshot_days=required_snapshot_days,
        created_at_timezone=timezone_name,
        price_source=price_source,
        horizon=horizon,
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
                _record_event(cache, "Yahoo Finance", "success", f"{symbol} 가격 히스토리를 수집했습니다.")
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if cache is not None:
                cached = cache.get_json(cache_key, allow_expired=True)
                if isinstance(cached, dict):
                    _record_event(cache, "Yahoo Finance", "stale", f"{symbol} 가격 히스토리 호출 실패로 만료 캐시를 사용했습니다.")
                    payload = cached
                else:
                    _record_event(cache, "Yahoo Finance", "error", f"{symbol} 가격 히스토리 호출 또는 파싱에 실패했습니다.")
    if payload is None:
        return ()
    return parse_yahoo_history(payload)


def fetch_price_histories(
    tickers: Iterable[str],
    cache: CacheStore | None = None,
    range_value: str = "3y",
    interval: str = "1d",
    timeout: float = 8.0,
    max_workers: int = 6,
) -> dict[str, tuple[PricePoint, ...]]:
    symbols = tuple(dict.fromkeys(ticker.upper() for ticker in tickers if ticker))
    if not symbols:
        return {}
    histories: dict[str, tuple[PricePoint, ...]] = {}
    workers = max(1, min(max_workers, len(symbols)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                fetch_price_history,
                ticker,
                cache=cache,
                range_value=range_value,
                interval=interval,
                timeout=timeout,
            ): ticker
            for ticker in symbols
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                histories[ticker] = future.result()
            except Exception as exc:  # pragma: no cover - defensive worker boundary
                if cache is not None:
                    _record_event(cache, "Yahoo Finance", "error", f"{ticker} 가격 히스토리 처리 실패: {exc}")
                histories[ticker] = ()
    return histories


def parse_yahoo_history(payload: dict) -> tuple[PricePoint, ...]:
    try:
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
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
                volume=_list_value(volumes, index),
            )
        )
    return tuple(sorted(points, key=lambda item: item.date))


def backtest_to_dict(result: BacktestResult) -> dict:
    return {
        "createdAt": result.created_at.isoformat(),
        "createdAtDisplay": result.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "createdAtTimezone": result.created_at_timezone,
        "snapshotDate": result.created_at.date().isoformat(),
        "method": result.method,
        "horizon": result.horizon,
        "pointInTime": result.point_in_time,
        "priceSource": result.price_source,
        "snapshotDays": result.snapshot_days,
        "snapshotCoveragePct": result.snapshot_coverage_pct,
        "requiredSnapshotDays": result.required_snapshot_days,
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
                "snapshotDate": period.snapshot_date,
                "priceSource": period.price_source,
                "anchorCoveragePct": period.anchor_coverage_pct,
                "periodStatus": period.period_status,
                "excludedReason": period.excluded_reason,
            }
            for period in result.periods
        ],
        "assumptions": _backtest_assumptions(result),
    }


def render_backtest_markdown(result: BacktestResult) -> str:
    lines = [
        "# 추천 모델 백테스트",
        "",
        f"- 생성 시각: {result.created_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 검증 기간: 최근 {result.period_count}개월",
        f"- 규칙: 월말 리밸런싱, Top {result.top_n} 동일비중",
        f"- 검증 대상: {_horizon_label(result.horizon)}",
        f"- 검증 방식: {'포인트인타임 스냅샷' if result.point_in_time else '현재 유니버스 기반 legacy'}",
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
        f"- 스냅샷 커버리지: {_pct_text(result.snapshot_coverage_pct)}",
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
        snapshot_text = f" / 스냅샷 {period.snapshot_date}" if period.snapshot_date else ""
        lines.append(
            f"- {period.start_date.isoformat()} -> {period.end_date.isoformat()}: "
            f"전략 {_pct_text(period.return_pct)}, 벤치마크 {_pct_text(period.benchmark_return_pct)}, "
            f"초과 {_pct_text(period.alpha_pct)} / {', '.join(period.tickers)}{snapshot_text}"
        )
    source_limitation = (
        "- 각 구간은 리밸런싱일 이전에 저장된 최신 추천 스냅샷의 순위를 사용합니다."
        if result.point_in_time
        else "- legacy 방식은 현재 기본 지표와 과거 가격 모멘텀을 함께 사용합니다."
    )
    lines.extend(
        [
            "",
            "## 해석상 한계",
            "",
            source_limitation,
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


def _snapshot_period_dates(
    benchmark_history: tuple[PricePoint, ...],
    snapshots: tuple[SnapshotRecord, ...],
    months: int,
) -> list[date]:
    month_ends = _month_end_dates(benchmark_history)
    if len(month_ends) >= 2:
        return month_ends[-(months + 1) :]
    by_month: dict[tuple[int, int], date] = {}
    for snapshot in snapshots:
        key = (snapshot.snapshot_date.year, snapshot.snapshot_date.month)
        by_month[key] = max(by_month.get(key, snapshot.snapshot_date), snapshot.snapshot_date)
    return [by_month[key] for key in sorted(by_month)][-(months + 1) :]


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
    selected_points = [point for point in points if point.date <= target]
    closes = [point.close for point in selected_points]
    volumes = [point.volume for point in selected_points]
    recent = closes[-126:] if len(closes) > 126 else closes
    latest = recent[-1] if recent else None
    high = max(recent) if recent else None
    low = min(recent) if recent else None
    ma20_values = moving_average(closes, 20)
    ma60_values = moving_average(closes, 60)
    ma120_values = moving_average(closes, 120)
    latest_volume = volumes[-1] if volumes else None
    avg_volume_20 = average_recent_volume(volumes, 20)
    return Momentum(
        one_month_pct=_lookback_return(closes, 21),
        three_month_pct=_lookback_return(closes, 63),
        six_month_pct=_lookback_return(closes, 126),
        drawdown_from_high_pct=_pct_from_high(latest, high),
        range_position_pct=_range_position(latest, low, high),
        latest_close=latest,
        latest_close_date=selected_points[-1].date.isoformat() if selected_points else None,
        six_month_high=high,
        six_month_low=low,
        ma20=_last_finite(ma20_values),
        ma60=_last_finite(ma60_values),
        ma120=_last_finite(ma120_values),
        rsi14=rsi(closes, 14),
        ma20_distance_pct=distance_from_average(latest, _last_finite(ma20_values)),
        ma60_distance_pct=distance_from_average(latest, _last_finite(ma60_values)),
        ma120_distance_pct=distance_from_average(latest, _last_finite(ma120_values)),
        ma20_slope_pct=moving_average_slope(ma20_values),
        ma60_slope_pct=moving_average_slope(ma60_values),
        latest_volume=latest_volume,
        avg_volume_20=avg_volume_20,
        volume_ratio=volume_ratio(latest_volume, avg_volume_20),
        twenty_day_breakout_pct=breakout_pct(closes, 20),
        sixty_day_breakout_pct=breakout_pct(closes, 60),
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


def _anchor_return(start_payload: dict, end_payload: dict, ticker: str) -> float | None:
    start_close = _anchor_close(start_payload, ticker)
    end_close = _anchor_close(end_payload, ticker)
    if start_close is None or end_close is None or start_close <= 0:
        return None
    return ((end_close / start_close) - 1) * 100


def _latest_eligible_snapshot_on_or_before(
    snapshots: tuple[SnapshotRecord, ...],
    target: date,
    warnings: list[str],
    skipped_warnings: set[str],
) -> SnapshotRecord | None:
    for snapshot in reversed(snapshots):
        if snapshot.snapshot_date > target:
            continue
        eligible, reason, fallback_allowed = _snapshot_backtest_eligibility(snapshot.payload)
        if eligible:
            if fallback_allowed:
                _append_once(
                    warnings,
                    skipped_warnings,
                    f"{snapshot.snapshot_date.isoformat()} 구형 스냅샷은 가격 히스토리 fallback으로만 검증합니다.",
                )
            return snapshot
        _append_once(
            warnings,
            skipped_warnings,
            f"{snapshot.snapshot_date.isoformat()} 스냅샷은 품질 기준 미달로 백테스트에서 제외했습니다: {reason}",
        )
    return None


def _snapshot_backtest_eligibility(payload: dict) -> tuple[bool, str | None, bool]:
    version = _payload_version(payload)
    if version is None or version < 10:
        return True, None, True
    quality = payload.get("snapshotQuality")
    if not isinstance(quality, dict):
        quality = _computed_snapshot_quality(payload)
    if bool(quality.get("backtestEligible")):
        return True, None, False
    reasons = quality.get("exclusionReasons")
    if isinstance(reasons, list) and reasons:
        return False, ", ".join(str(item) for item in reasons), False
    return False, "snapshotQuality.backtestEligible=false", False


def _payload_version(payload: dict) -> int | None:
    value = payload.get("version")
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _computed_snapshot_quality(payload: dict) -> dict:
    price_coverage = _anchor_collection_coverage(payload.get("stocks"))
    benchmark_coverage = _benchmark_anchor_coverage(payload)
    reasons: list[str] = []
    if price_coverage < 80:
        reasons.append("priceAnchorCoverageBelow80")
    if benchmark_coverage < 100:
        reasons.append("benchmarkAnchorCoverageBelow100")
    return {
        "priceAnchorCoveragePct": price_coverage,
        "benchmarkAnchorCoveragePct": benchmark_coverage,
        "backtestEligible": not reasons,
        "exclusionReasons": reasons,
    }


def _anchor_collection_coverage(collection: object) -> float:
    if not isinstance(collection, list) or not collection:
        return 0
    covered = 0
    for item in collection:
        if not isinstance(item, dict):
            continue
        if _anchor_close({"stocks": [item]}, str(item.get("ticker") or "")) is not None:
            covered += 1
    return round(covered / len(collection) * 100, 1)


def _benchmark_anchor_coverage(payload: dict) -> float:
    benchmarks = payload.get("benchmarks")
    if not isinstance(benchmarks, list):
        return 0
    by_ticker = {
        str(item.get("ticker") or "").upper(): item
        for item in benchmarks
        if isinstance(item, dict)
    }
    covered = 0
    for ticker in BENCHMARKS:
        item = by_ticker.get(ticker)
        if item is not None and _anchor_close({"benchmarks": [item]}, ticker) is not None:
            covered += 1
    return round(covered / len(BENCHMARKS) * 100, 1)


def _append_once(warnings: list[str], seen: set[str], message: str) -> None:
    if message in seen:
        return
    seen.add(message)
    warnings.append(message)


def _anchor_close(payload: dict, ticker: str) -> float | None:
    normalized = ticker.upper()
    for collection_name in ("stocks", "benchmarks", "priceAnchors"):
        collection = payload.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict) or str(item.get("ticker") or "").upper() != normalized:
                continue
            anchor = item.get("priceAnchor")
            if isinstance(anchor, dict):
                close = anchor.get("latestClose")
                if isinstance(close, (int, float)) and math.isfinite(close) and close > 0:
                    return float(close)
            momentum = item.get("momentumRaw")
            if isinstance(momentum, dict):
                close = momentum.get("latestClose")
                if isinstance(close, (int, float)) and math.isfinite(close) and close > 0:
                    return float(close)
    return None


def _pct_from_high(latest: float | None, high: float | None) -> float | None:
    if latest is None or high is None or high <= 0:
        return None
    return ((latest / high) - 1) * 100


def _range_position(latest: float | None, low: float | None, high: float | None) -> float | None:
    if latest is None or low is None or high is None:
        return None
    if high <= low:
        return 50.0
    return max(0.0, min(100.0, (latest - low) / (high - low) * 100))


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


def _round_optional(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


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
    created_at: datetime | None = None,
    timezone_name: str = "",
    method: str = "legacy",
    point_in_time: bool = False,
    snapshot_days: int = 0,
    snapshot_coverage_pct: float = 0,
    required_snapshot_days: int = 0,
    price_source: str = "liveHistory",
    extra_warnings: tuple[str, ...] = (),
    horizon: str = "overall",
) -> BacktestResult:
    return BacktestResult(
        created_at=created_at or now_in_app_timezone(),
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
        warnings=tuple(dict.fromkeys((warning, *extra_warnings))),
        method=method,
        point_in_time=point_in_time,
        snapshot_days=snapshot_days,
        snapshot_coverage_pct=round(snapshot_coverage_pct, 1),
        required_snapshot_days=required_snapshot_days,
        created_at_timezone=timezone_name,
        price_source=price_source,
        horizon=horizon,
    )


def _history_range_for_months(months: int) -> str:
    if months <= 12:
        return "2y"
    if months <= 24:
        return "3y"
    return "5y"


def _normalize_method(method: str) -> str:
    normalized = str(method or "snapshot").lower()
    return normalized if normalized in BACKTEST_METHODS else "snapshot"


def _normalize_horizon(horizon: str) -> str:
    normalized = str(horizon or "overall").lower()
    return normalized if normalized in BACKTEST_HORIZONS else "overall"


def _horizon_label(horizon: str) -> str:
    return {
        "overall": "종합 추천",
        "short": "단기 후보",
        "medium": "중기 후보",
        "long": "장기 후보",
    }.get(horizon, "종합 추천")


def _legacy_selected_stocks(
    horizon: str,
    scores,
    industry_scores,
    momentums: dict[str, Momentum],
    top_n: int,
) -> tuple[StockProfile, ...]:
    if horizon == "short":
        return tuple(
            item.stock_score.stock
            for item in score_short_term_candidates(scores, industry_scores, (), momentums)[:top_n]
        )
    if horizon == "medium":
        return tuple(
            item.stock_score.stock
            for item in score_medium_term_candidates(scores, industry_scores, (), momentums)[:top_n]
        )
    if horizon == "long":
        return tuple(
            item.stock_score.stock
            for item in score_long_term_candidates(scores, industry_scores, (), momentums)[:top_n]
        )
    return tuple(score.stock for score in scores[:top_n])


def _snapshot_records(rows: list[dict]) -> tuple[SnapshotRecord, ...]:
    records: list[SnapshotRecord] = []
    for row in rows:
        payload = row.get("payload")
        raw_date = row.get("snapshotDate")
        if isinstance(payload, dict) and isinstance(raw_date, str):
            try:
                records.append(SnapshotRecord(date.fromisoformat(raw_date), payload))
            except ValueError:
                continue
    return tuple(sorted(records, key=lambda item: item.snapshot_date))


def _snapshot_tickers(payload: dict, top_n: int, horizon: str = "overall") -> tuple[str, ...]:
    return tuple(
        ticker
        for item in _snapshot_top_stocks(payload, top_n, horizon)
        if (ticker := str(item.get("ticker", "")).upper())
    )


def _snapshot_top_stocks(payload: dict, top_n: int, horizon: str = "overall") -> tuple[dict, ...]:
    stocks = payload.get(_snapshot_collection_name(horizon))
    if not isinstance(stocks, list):
        return ()
    valid = [item for item in stocks if isinstance(item, dict) and item.get("ticker")]
    return tuple(valid[:top_n])


def _snapshot_collection_name(horizon: str) -> str:
    return {
        "short": "shortTermCandidates",
        "medium": "mediumTermCandidates",
        "long": "longTermCandidates",
    }.get(horizon, "stocks")


def _latest_snapshot_on_or_before(
    snapshots: tuple[SnapshotRecord, ...], target: date
) -> SnapshotRecord | None:
    selected: SnapshotRecord | None = None
    for snapshot in snapshots:
        if snapshot.snapshot_date > target:
            break
        selected = snapshot
    return selected


def _record_event(cache: CacheStore, source: str, event_type: str, message: str) -> None:
    try:
        cache.record_source_event(source, event_type, message)
    except Exception:
        return


def _list_value(values: list, index: int) -> float | None:
    if index >= len(values):
        return None
    value = values[index]
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _pct_text(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}%"


def _backtest_assumptions(result: BacktestResult) -> list[str]:
    assumptions = ["월말 리밸런싱, 동일비중 Top N 보유로 계산합니다."]
    if result.point_in_time:
        assumptions.append("각 리밸런싱일 이전에 저장된 최신 추천 스냅샷의 순위를 사용합니다.")
    else:
        assumptions.append("legacy 방식은 현재 유니버스/재무 지표와 과거 가격 모멘텀을 함께 사용합니다.")
    assumptions.append("거래비용, 세금, 환율 환산, 슬리피지는 아직 반영하지 않았습니다.")
    return assumptions
