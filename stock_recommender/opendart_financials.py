from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable

from .config import AppConfig
from .models import Fundamentals, StockProfile
from .official_sources import OpenDartClient
from .storage import CacheStore
from .time_utils import now_in_app_timezone


REVENUE_NAMES = ("매출액", "수익(매출액)", "영업수익")
OPERATING_INCOME_NAMES = ("영업이익",)
NET_INCOME_NAMES = ("당기순이익", "분기순이익")
ASSET_NAMES = ("자산총계",)
LIABILITY_NAMES = ("부채총계",)
EQUITY_NAMES = ("자본총계",)
CURRENT_ASSET_NAMES = ("유동자산",)
CURRENT_LIABILITY_NAMES = ("유동부채",)
DEPRECIATION_NAMES = ("감가상각비", "감가상각비및무형자산상각비", "감가상각비와무형자산상각비")
AMORTIZATION_NAMES = ("무형자산상각비",)
OPERATING_CASH_FLOW_NAMES = (
    "영업활동현금흐름",
    "영업활동으로인한현금흐름",
    "영업활동으로부터의현금흐름",
)
CAPEX_NAMES = ("유형자산의취득", "유형자산취득", "유형자산의증가")
INTEREST_EXPENSE_NAMES = ("이자비용", "금융비용")


class OpenDartFinancialClient:
    def __init__(self, config: AppConfig, cache: CacheStore, timeout: float = 10.0):
        self.config = config
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
            for business_year in _candidate_years(self.config):
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
                warnings.append(f"{stock.ticker} OpenDART 연간 재무제표를 찾지 못해 기본 유니버스 지표를 유지했습니다.")
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

    revenue_row = _find_row(selected_rows, REVENUE_NAMES)
    operating_income_row = _find_row(selected_rows, OPERATING_INCOME_NAMES)
    net_income_row = _find_row(selected_rows, NET_INCOME_NAMES)
    liabilities_row = _find_row(selected_rows, LIABILITY_NAMES)
    equity_row = _find_row(selected_rows, EQUITY_NAMES)
    current_assets_row = _find_row(selected_rows, CURRENT_ASSET_NAMES)
    current_liabilities_row = _find_row(selected_rows, CURRENT_LIABILITY_NAMES)
    depreciation_row = _find_row(selected_rows, DEPRECIATION_NAMES)
    amortization_row = _find_row(selected_rows, AMORTIZATION_NAMES)
    operating_cash_flow_row = _find_row(selected_rows, OPERATING_CASH_FLOW_NAMES)
    capital_expenditure_row = _find_row(selected_rows, CAPEX_NAMES)
    interest_expense_row = _find_row(selected_rows, INTEREST_EXPENSE_NAMES)

    revenue_current, revenue_previous = _current_previous_from_row(revenue_row)
    operating_income, _ = _current_previous_from_row(operating_income_row)
    net_income, _ = _current_previous_from_row(net_income_row)
    liabilities, _ = _current_previous_from_row(liabilities_row)
    equity_current, equity_previous = _current_previous_from_row(equity_row)
    current_assets, _ = _current_previous_from_row(current_assets_row)
    current_liabilities, _ = _current_previous_from_row(current_liabilities_row)
    depreciation, _ = _current_previous_from_row(depreciation_row)
    amortization, _ = _current_previous_from_row(amortization_row)
    operating_cash_flow, _ = _current_previous_from_row(operating_cash_flow_row)
    capital_expenditure, _ = _current_previous_from_row(capital_expenditure_row)
    interest_expense, _ = _current_previous_from_row(interest_expense_row)

    revenue_growth_pct = _growth_pct(revenue_current, revenue_previous)
    operating_margin_pct = _ratio_pct(operating_income, revenue_current)
    average_equity = _average(equity_current, equity_previous)
    roe_pct = _ratio_pct(net_income, average_equity)
    debt_to_equity_pct = _ratio_pct(liabilities, equity_current)
    current_ratio_pct = _ratio_pct(current_assets, current_liabilities)
    depreciation_amortization = _sum_optional(depreciation, amortization)
    ebitda = _add(operating_income, depreciation_amortization)
    capex_outflow = _outflow(capital_expenditure)
    free_cash_flow = _subtract(operating_cash_flow, capex_outflow)
    interest_expense_outflow = _outflow(interest_expense)
    interest_coverage = _ratio(operating_income, interest_expense_outflow)
    sources = dict(fallback.sources)
    _set_row_source(sources, "revenue", revenue_row, payload)
    _set_row_source(sources, "operatingIncome", operating_income_row, payload)
    _set_row_source(sources, "netIncome", net_income_row, payload)
    _set_row_source(sources, "operatingCashFlow", operating_cash_flow_row, payload)
    _set_row_source(sources, "capitalExpenditure", capital_expenditure_row, payload)
    _set_row_source(sources, "currentAssets", current_assets_row, payload)
    _set_row_source(sources, "currentLiabilities", current_liabilities_row, payload)
    _set_row_source(sources, "interestExpense", interest_expense_row, payload)
    if ebitda is not None:
        _set_row_source(sources, "ebitda", operating_income_row or depreciation_row or amortization_row, payload)
    if free_cash_flow is not None:
        _set_row_source(
            sources,
            "freeCashFlow",
            operating_cash_flow_row or capital_expenditure_row,
            payload,
            derived_from=("operatingCashFlow", "capitalExpenditure"),
        )

    return Fundamentals(
        revenue_growth_pct=_coalesce(revenue_growth_pct, fallback.revenue_growth_pct),
        operating_margin_pct=_coalesce(operating_margin_pct, fallback.operating_margin_pct),
        roe_pct=_coalesce(roe_pct, fallback.roe_pct),
        debt_to_equity_pct=_coalesce(debt_to_equity_pct, fallback.debt_to_equity_pct),
        pe=fallback.pe,
        forward_pe=fallback.forward_pe,
        market_cap=fallback.market_cap,
        market_cap_currency=fallback.market_cap_currency or "KRW",
        revenue=_coalesce(revenue_current, fallback.revenue),
        operating_income=_coalesce(operating_income, fallback.operating_income),
        ebitda=_coalesce(ebitda, fallback.ebitda),
        net_income=_coalesce(net_income, fallback.net_income),
        operating_cash_flow=_coalesce(operating_cash_flow, fallback.operating_cash_flow),
        capital_expenditure=_coalesce(capex_outflow, fallback.capital_expenditure),
        free_cash_flow=_coalesce(free_cash_flow, fallback.free_cash_flow),
        current_assets=_coalesce(current_assets, fallback.current_assets),
        current_liabilities=_coalesce(current_liabilities, fallback.current_liabilities),
        current_ratio_pct=_coalesce(current_ratio_pct, fallback.current_ratio_pct),
        interest_expense=_coalesce(interest_expense_outflow, fallback.interest_expense),
        interest_coverage=_coalesce(interest_coverage, fallback.interest_coverage),
        sources=sources,
    )


def _candidate_years(config: AppConfig | None = None) -> tuple[int, ...]:
    current_year = now_in_app_timezone(config).year
    return (current_year - 1, current_year - 2, current_year - 3)


def _preferred_statement_rows(rows: list[dict]) -> list[dict]:
    consolidated = [row for row in rows if row.get("fs_div") == "CFS"]
    return consolidated or rows


def _find_current_previous(rows: list[dict], names: tuple[str, ...]) -> tuple[float | None, float | None]:
    return _current_previous_from_row(_find_row(rows, names))


def _find_row(rows: list[dict], names: tuple[str, ...]) -> dict | None:
    for row in rows:
        account_name = str(row.get("account_nm", "")).replace(" ", "")
        if not any(name.replace(" ", "") in account_name for name in names):
            continue
        return row
    return None


def _current_previous_from_row(row: dict | None) -> tuple[float | None, float | None]:
    if row is None:
        return None, None
    return _amount(row.get("thstrm_amount")), _amount(row.get("frmtrm_amount"))


def _set_row_source(
    sources: dict[str, dict],
    field: str,
    row: dict | None,
    payload: dict,
    derived_from: tuple[str, ...] = (),
) -> None:
    if row is None or _amount(row.get("thstrm_amount")) is None:
        return
    source = {
        "source": "OpenDART",
        "periodEnd": row.get("thstrm_dt") if isinstance(row.get("thstrm_dt"), str) else None,
        "fiscalYear": row.get("bsns_year") or payload.get("bsns_year"),
        "filed": None,
        "form": None,
        "reportCode": row.get("reprt_code") or payload.get("reprt_code") or "11011",
        "fallback": False,
    }
    if derived_from:
        source["derivedFrom"] = list(derived_from)
    sources[field] = source


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


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _add(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return None
    return first + second


def _subtract(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return None
    return first - second


def _sum_optional(*values: float | None) -> float | None:
    valid_values = [value for value in values if value is not None]
    if not valid_values:
        return None
    return sum(valid_values)


def _outflow(value: float | None) -> float | None:
    if value is None:
        return None
    return abs(value)


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
