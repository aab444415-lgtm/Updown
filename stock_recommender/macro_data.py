from __future__ import annotations

from datetime import date, timedelta
from statistics import fmean

from .config import AppConfig
from .models import MacroIndicator, MacroSnapshot
from .official_sources import EcosClient, FredClient, SourceResponse
from .storage import CacheStore
from .time_utils import now_in_app_timezone


FRED_SERIES = {
    "fed_funds": ("FEDFUNDS", "미국 기준금리", "%"),
    "ten_year": ("DGS10", "미국 10년 국채금리", "%"),
    "two_year": ("DGS2", "미국 2년 국채금리", "%"),
    "cpi": ("CPIAUCSL", "미국 CPI", "index"),
    "unemployment": ("UNRATE", "미국 실업률", "%"),
    "dollar": ("DTWEXBGS", "미국 달러지수", "index"),
}


def fetch_macro_snapshot(config: AppConfig, cache: CacheStore) -> MacroSnapshot:
    warnings: list[str] = []
    indicators: list[MacroIndicator] = []
    today = now_in_app_timezone(config).date()
    start_2y = (today - timedelta(days=760)).isoformat()
    start_6m = (today - timedelta(days=220)).isoformat()

    fred_values: dict[str, tuple[float | None, str | None, list[tuple[str, float]]]] = {}
    if config.fred_api_key:
        fred = FredClient(config, cache)
        for key, (series_id, name, unit) in FRED_SERIES.items():
            response = fred.fetch_series_observations(series_id, observation_start=start_2y)
            series = _fred_series(response)
            latest_value, latest_date = _latest(series)
            fred_values[key] = (latest_value, latest_date, series)
            if latest_value is None:
                warnings.append(f"FRED {series_id} 지표를 읽지 못했습니다.")
                continue
            indicators.append(
                MacroIndicator(
                    name=name,
                    value=latest_value,
                    unit=unit,
                    latest_date=latest_date,
                    source="FRED",
                    note=_fred_note(key, latest_value, series),
                )
            )
    else:
        warnings.append("FRED API 키가 없어 미국 거시지표를 중립값으로 계산했습니다.")

    ecos_usdkrw = None
    ecos_usdkrw_date = None
    if config.ecos_api_key:
        ecos = EcosClient(config, cache)
        start = (today - timedelta(days=140)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        response = ecos.fetch_statistic("731Y001", "D", start, end, "0000001")
        ecos_series = _ecos_series(response)
        ecos_usdkrw, ecos_usdkrw_date = _latest(ecos_series)
        if ecos_usdkrw is not None:
            indicators.append(
                MacroIndicator(
                    name="원/달러 환율",
                    value=ecos_usdkrw,
                    unit="KRW",
                    latest_date=ecos_usdkrw_date,
                    source="ECOS",
                    note=_fx_note(ecos_series),
                )
            )
        else:
            warnings.append("ECOS 원/달러 환율 지표를 읽지 못했습니다.")
    else:
        warnings.append("ECOS API 키가 없어 한국 환율 지표를 중립값으로 계산했습니다.")

    fed_funds = fred_values.get("fed_funds", (None, None, []))[0]
    ten_year = fred_values.get("ten_year", (None, None, []))[0]
    two_year = fred_values.get("two_year", (None, None, []))[0]
    cpi_series = fred_values.get("cpi", (None, None, []))[2]
    unemployment_series = fred_values.get("unemployment", (None, None, []))[2]
    dollar_series = fred_values.get("dollar", (None, None, []))[2]

    yield_spread = _subtract(ten_year, two_year)
    cpi_yoy = _pct_change(cpi_series, months=12)
    unemployment_delta = _delta_from_months(unemployment_series, months=6)
    dollar_change = _pct_change(dollar_series, periods=126)
    fx_change = _pct_change(ecos_series if config.ecos_api_key else [], periods=60)

    growth_score = _growth_score(fed_funds, yield_spread, cpi_yoy, unemployment_delta, dollar_change)
    defensive_score = _defensive_score(growth_score, unemployment_delta, cpi_yoy)
    infrastructure_score = _infrastructure_score(fed_funds, cpi_yoy, growth_score)
    korea_fx_score = _korea_fx_score(ecos_usdkrw, fx_change)

    summary = _summary(growth_score, defensive_score, infrastructure_score, korea_fx_score)
    investment_guidance = _investment_guidance(
        growth_score,
        defensive_score,
        infrastructure_score,
        korea_fx_score,
        fed_funds,
        yield_spread,
        cpi_yoy,
        unemployment_delta,
        dollar_change,
    )
    return MacroSnapshot(
        indicators=tuple(indicators),
        growth_score=round(growth_score, 1),
        defensive_score=round(defensive_score, 1),
        infrastructure_score=round(infrastructure_score, 1),
        korea_fx_score=round(korea_fx_score, 1),
        summary=summary,
        investment_guidance=investment_guidance,
        warnings=tuple(warnings),
    )


def industry_macro_data_score(industry_name: str, snapshot: MacroSnapshot | None) -> float:
    if snapshot is None:
        return 50
    if "AI 반도체" in industry_name:
        return fmean((snapshot.growth_score, snapshot.infrastructure_score, snapshot.korea_fx_score))
    if "전력 인프라" in industry_name:
        return fmean((snapshot.infrastructure_score, snapshot.defensive_score))
    if "비만 치료제" in industry_name:
        return fmean((snapshot.defensive_score, snapshot.growth_score))
    if "방산" in industry_name:
        return fmean((snapshot.defensive_score, snapshot.infrastructure_score))
    if "사이버보안" in industry_name:
        return fmean((snapshot.growth_score, snapshot.defensive_score))
    return 50


def _fred_series(response: SourceResponse) -> list[tuple[str, float]]:
    payload = response.payload if response.ok else None
    if not isinstance(payload, dict):
        return []
    observations = payload.get("observations")
    if not isinstance(observations, list):
        return []
    series: list[tuple[str, float]] = []
    for item in observations:
        value = _to_float(item.get("value"))
        date_value = item.get("date")
        if value is not None and isinstance(date_value, str):
            series.append((date_value, value))
    return series


def _ecos_series(response: SourceResponse) -> list[tuple[str, float]]:
    payload = response.payload if response.ok else None
    if not isinstance(payload, dict):
        return []
    rows = payload.get("StatisticSearch", {}).get("row")
    if not isinstance(rows, list):
        return []
    series: list[tuple[str, float]] = []
    for item in rows:
        value = _to_float(item.get("DATA_VALUE"))
        time_value = item.get("TIME")
        if value is not None and isinstance(time_value, str):
            series.append((time_value, value))
    return series


def _latest(series: list[tuple[str, float]]) -> tuple[float | None, str | None]:
    if not series:
        return None, None
    date_value, value = sorted(series, key=lambda item: item[0])[-1]
    return value, date_value


def _subtract(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def _pct_change(
    series: list[tuple[str, float]], months: int | None = None, periods: int | None = None
) -> float | None:
    if len(series) < 2:
        return None
    ordered = sorted(series, key=lambda item: item[0])
    latest = ordered[-1][1]
    lookback = periods if periods is not None else months or 1
    lookback_index = max(0, len(ordered) - lookback - 1)
    previous = ordered[lookback_index][1]
    if previous == 0:
        return None
    return ((latest / previous) - 1) * 100


def _delta_from_months(series: list[tuple[str, float]], months: int) -> float | None:
    if len(series) < 2:
        return None
    ordered = sorted(series, key=lambda item: item[0])
    latest = ordered[-1][1]
    lookback_index = max(0, len(ordered) - months - 1)
    return latest - ordered[lookback_index][1]


def _growth_score(
    fed_funds: float | None,
    yield_spread: float | None,
    cpi_yoy: float | None,
    unemployment_delta: float | None,
    dollar_change: float | None,
) -> float:
    score = 55.0
    if fed_funds is not None:
        score += _clamp((3.0 - fed_funds) * 6, -18, 14)
    if yield_spread is not None:
        score += _clamp(yield_spread * 12, -18, 14)
    if cpi_yoy is not None:
        score += _clamp((3.0 - cpi_yoy) * 4, -14, 10)
    if unemployment_delta is not None:
        score += _clamp(-unemployment_delta * 18, -14, 10)
    if dollar_change is not None:
        score += _clamp(-dollar_change * 0.7, -8, 8)
    return _clamp(score, 0, 100)


def _defensive_score(growth_score: float, unemployment_delta: float | None, cpi_yoy: float | None) -> float:
    score = 100 - growth_score * 0.55
    if unemployment_delta is not None and unemployment_delta > 0.2:
        score += 8
    if cpi_yoy is not None and cpi_yoy > 3.0:
        score += 6
    return _clamp(score, 0, 100)


def _infrastructure_score(fed_funds: float | None, cpi_yoy: float | None, growth_score: float) -> float:
    score = 52 + (growth_score - 50) * 0.25
    if fed_funds is not None:
        score += _clamp((4.5 - fed_funds) * 4, -10, 8)
    if cpi_yoy is not None and cpi_yoy > 3.2:
        score -= 4
    return _clamp(score, 0, 100)


def _korea_fx_score(value: float | None, change_pct: float | None) -> float:
    if value is None:
        return 50
    score = 58
    if value >= 1450:
        score -= 8
    elif value >= 1350:
        score -= 3
    elif value <= 1250:
        score += 5
    if change_pct is not None:
        score += _clamp(-change_pct * 0.8, -8, 8)
    return _clamp(score, 0, 100)


def _summary(growth: float, defensive: float, infrastructure: float, korea_fx: float) -> str:
    if growth >= 62:
        regime = "성장주와 경기민감 업종에 비교적 우호적인 거시 환경"
    elif defensive >= 62:
        regime = "방어주와 현금흐름 안정성이 더 중요한 거시 환경"
    else:
        regime = "성장성과 방어성을 함께 확인해야 하는 중립적 거시 환경"
    return (
        f"{regime}입니다. 성장 {growth:.1f}, 방어 {defensive:.1f}, "
        f"인프라 {infrastructure:.1f}, 한국 환율 {korea_fx:.1f}점으로 계산했습니다."
    )


def _investment_guidance(
    growth: float,
    defensive: float,
    infrastructure: float,
    korea_fx: float,
    fed_funds: float | None,
    yield_spread: float | None,
    cpi_yoy: float | None,
    unemployment_delta: float | None,
    dollar_change: float | None,
) -> tuple[str, ...]:
    guidance: list[str] = []
    if defensive >= growth + 8:
        guidance.append(
            "기본 전략: 공격적인 추격 매수보다 현금흐름이 안정적인 기업, 방어 업종, 배당/필수 수요 종목을 우선 검토합니다."
        )
    elif growth >= defensive + 8:
        guidance.append(
            "기본 전략: 성장주와 경기민감 업종 비중을 늘릴 수 있지만, 실적 성장과 가격 모멘텀이 동시에 확인되는 종목으로 압축합니다."
        )
    else:
        guidance.append(
            "기본 전략: 한쪽으로 크게 베팅하기보다 우량 성장주와 방어주를 함께 담는 균형형 포트폴리오가 유리합니다."
        )

    if infrastructure >= 60:
        guidance.append(
            "우선 업종: 전력 인프라, AI 데이터센터, 방산처럼 정책 지출이나 설비투자 사이클이 뒷받침되는 분야를 우선순위에 둡니다."
        )
    elif infrastructure <= 44:
        guidance.append(
            "우선 업종: 대규모 설비투자 수혜주를 무리하게 쫓기보다 실적 가시성이 높은 소프트웨어, 헬스케어, 필수 소비 쪽을 함께 봅니다."
        )

    if cpi_yoy is not None and cpi_yoy >= 3.2:
        guidance.append(
            f"물가 대응: CPI 전년 대비 상승률이 {cpi_yoy:.1f}%로 높아 고PER 장기 성장주는 할인율 부담을 받기 쉽습니다. FCF, 마진, 가격 전가력이 있는 기업을 우선합니다."
        )
    elif cpi_yoy is not None and cpi_yoy <= 2.4:
        guidance.append(
            f"물가 대응: CPI 전년 대비 상승률이 {cpi_yoy:.1f}%로 완화적이면 금리 부담이 줄 수 있어 성장주의 진입 구간을 넓게 볼 수 있습니다."
        )

    if fed_funds is not None and fed_funds >= 4.5:
        guidance.append(
            f"금리 대응: 기준금리 {fed_funds:.2f}% 구간에서는 레버리지 높은 기업과 먼 미래 이익에 의존하는 종목의 비중을 보수적으로 관리합니다."
        )
    if yield_spread is not None and yield_spread < -0.25:
        guidance.append(
            f"경기 신호: 10년-2년 금리차가 {yield_spread:.2f}%p로 역전되어 있어 경기 둔화 가능성을 감안해 분할 매수와 손절 기준을 명확히 둡니다."
        )
    if unemployment_delta is not None and unemployment_delta > 0.2:
        guidance.append(
            f"고용 신호: 실업률이 6개월 전보다 {unemployment_delta:+.1f}%p 올라 경기 방어력이 약한 소비/경기민감주는 선별 접근이 필요합니다."
        )
    if dollar_change is not None and dollar_change >= 4:
        guidance.append(
            f"달러 신호: 달러지수가 6개월간 {dollar_change:+.1f}% 움직여 해외 매출, 원가, 환율 민감도를 종목별로 확인해야 합니다."
        )
    if korea_fx <= 45:
        guidance.append(
            "한국 주식 대응: 환율 점수가 낮아 원화 약세 부담이 있습니다. 수입 원가가 큰 내수주는 보수적으로 보고, 달러 매출 비중이 큰 기업은 상대적으로 유리한지 확인합니다."
        )

    execution_rule = "실행 원칙: 추천 점수만 보고 일괄 매수하지 말고, 데이터 신뢰도와 최근 실적 확인 후 2~3회 분할 진입하며 종목별 비중을 제한합니다."
    guidance.append(execution_rule)
    if len(guidance) > 6:
        return tuple((*guidance[:5], execution_rule))
    return tuple(guidance)


def _fred_note(key: str, value: float, series: list[tuple[str, float]]) -> str:
    if key == "cpi":
        yoy = _pct_change(series, months=12)
        return "전년 대비 물가 압력 " + ("N/A" if yoy is None else f"{yoy:.1f}%")
    if key == "unemployment":
        delta = _delta_from_months(series, months=6)
        return "6개월 변화 " + ("N/A" if delta is None else f"{delta:+.1f}%p")
    if key == "dollar":
        change = _pct_change(series, periods=126)
        return "6개월 변화 " + ("N/A" if change is None else f"{change:+.1f}%")
    return f"최근값 {value:.2f}"


def _fx_note(series: list[tuple[str, float]]) -> str:
    change = _pct_change(series, periods=60)
    return "약 3개월 변화 " + ("N/A" if change is None else f"{change:+.1f}%")


def _to_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
