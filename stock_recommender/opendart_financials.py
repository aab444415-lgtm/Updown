from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable

from .config import AppConfig
from .models import Fundamentals, StockProfile
from .official_sources import OpenDartClient
from .storage import CacheStore


REVENUE_NAMES = ("매출액", "수익(매출액)", "영업수익")
OPERATING_INCOME_NAMES = ("영업이익",)
NET_INCOME_NAMES = ("당기순이익", "분기순이익")
ASSET_NAMES = ("자산총계",)
LIABILITY_NAMES = ("부채총계",)
EQUITY_NAMES = ("자본총계",)


class OpenDartFinancialClient:
    def __init__(self, config: AppConfig, cache: CacheStore, timeout: float = 10.0):
        self.dart = OpenDartClient(config, cache, timeout=timeout)

    def enrich_stocks(self, stocks: Iterable[StockProfile]) -> OpenDartFinancialResult:
        stock_tuple = tuple(stocks)
        korean_stocks = [stock for stock in stock_tuple if stock.dart_stock_code or stock.country == "KR"]
        if not korean_stocks:
            return OpenDartFinancialResult(stock_tuple, 0, ())

        corp_codes = self.dart.fetch_corp_code_map()
        if not corp_codes.ok or not isinstance(corp_codes.payload, dict):
            return OpenDartFinancialResult(
                stock_tuple,
                0,
                (corp_codes.warning or "OpenDART 고유번호 목록을 읽지 못했습니다.",),
            )

        warnings: list[str] = []
        updated_count = 0
        enriched: list[StockProfile] = []
        for stock in stock_tuple:
            if stock not in korean_stocks:
                enriched.append(stock)
                continue
            stock_code = stock.dart_stock_code or stock.ticker.split(".")[0]
            corp_info = corp_codes.payload.get(stock_code)
            if not isinstance(corp_info, dict) or not corp_info.get("corp_code"):
                warnings.append(f"{stock.ticker} OpenDART 고유번호를 찾지 못했습니다.")
                enriched.append(stock)
                continue

            fundamentals = None
            for business_year in _candidate_years():
                response = self.dart.fetch_single_company_accounts(
                    corp_code=str(corp_info["corp_code"]),
                    business_year=str(business_year),
                    report_code="11011",
                )
                if not response.ok or not isinstance(response.payload, dict):
                    continue
                if response.payload.get("status") != "000":
                    continue
                fundamentals = extract_opendart_fundamentals(response.payload, stock.fundamentals)
                break

            if fundamentals is None:
                warnings.append(f"{stock.ticker} OpenDART 연간 재무제표를 찾지 못해 샘플 지표를 유지했습니다.")
                enriched.append(stock)
                continue

            if fundamentals != stock.fundamentals:
                updated_count += 1
            enriched.append(replace(stock, fundamentals=fundamentals))

        if updated_count:
            warnings.append(f"OpenDART 재무지표로 한국 종목 {updated_count}개를 갱신했습니다.")
        return OpenDartFinancialResult(tuple(enriched), updated_count, tuple(warnings))


def extract_opendart_fundamentals(payload: dict, fallback: Fundamentals | None = None) -> Fundamentals:
    fallback = fallback or Fundamentals(market_cap_currency="KRW")
    rows = payload.get("list")
    if not isinstance(rows, list):
        return fallback
    selected_rows = _preferred_statement_rows(rows)

    revenue_current, revenue_previous = _find_current_previous(selected_rows, REVENUE_NAMES)
    operating_income, _ = _find_current_previous(selected_rows, OPERATING_INCOME_NAMES)
    net_income, _ = _find_current_previous(selected_rows, NET_INCOME_NAMES)
    liabilities, _ = _find_current_previous(selected_rows, LIABILITY_NAMES)
    equity_current, equity_previous = _find_current_previous(selected_rows, EQUITY_NAMES)

    revenue_growth_pct = _growth_pct(revenue_current, revenue_previous)
    operating_margin_pct = _ratio_pct(operating_income, revenue_current)
    average_equity = _average(equity_current, equity_previous)
    roe_pct = _ratio_pct(net_income, average_equity)
    debt_to_equity_pct = _ratio_pct(liabilities, equity_current)

    return Fundamentals(
        revenue_growth_pct=_coalesce(revenue_growth_pct, fallback.revenue_growth_pct),
        operating_margin_pct=_coalesce(operating_margin_pct, fallback.operating_margin_pct),
        roe_pct=_coalesce(roe_pct, fallback.roe_pct),
        debt_to_equity_pct=_coalesce(debt_to_equity_pct, fallback.debt_to_equity_pct),
        pe=fallback.pe,
        forward_pe=fallback.forward_pe,
        market_cap_usd=fallback.market_cap_usd,
        market_cap_currency=fallback.market_cap_currency or "KRW",
    )


def _candidate_years() -> tuple[int, ...]:
    current_year = date.today().year
    return (current_year - 1, current_year - 2, current_year - 3)


def _preferred_statement_rows(rows: list[dict]) -> list[dict]:
    consolidated = [row for row in rows if row.get("fs_div") == "CFS"]
    return consolidated or rows


def _find_current_previous(rows: list[dict], names: tuple[str, ...]) -> tuple[float | None, float | None]:
    for row in rows:
        account_name = str(row.get("account_nm", "")).replace(" ", "")
        if not any(name.replace(" ", "") in account_name for name in names):
            continue
        return _amount(row.get("thstrm_amount")), _amount(row.get("frmtrm_amount"))
    return None, None


def _amount(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").replace(" ", "")
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _growth_pct(latest: float | None, previous: float | None) -> float | None:
    if latest is None or previous is None or previous == 0:
        return None
    return ((latest / previous) - 1) * 100


def _ratio_pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return (numerator / denominator) * 100


def _average(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return first
    return (first + second) / 2


def _coalesce(value: float | None, fallback: float | None) -> float | None:
    return value if value is not None else fallback


@dataclass(frozen=True)
class OpenDartFinancialResult:
    stocks: tuple[StockProfile, ...]
    updated_count: int
    warnings: tuple[str, ...]
