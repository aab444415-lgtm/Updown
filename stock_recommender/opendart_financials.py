from __future__ import annotations

import math
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
CASH_NAMES = ("현금및현금성자산", "현금및현금성자산및단기금융상품", "현금및예치금")
TOTAL_DEBT_NAMES = (
    "단기차입금",
    "장기차입금",
    "유동성장기부채",
    "유동성사채",
    "사채",
    "차입금",
)
PRETAX_INCOME_NAMES = ("법인세비용차감전순이익", "법인세비용차감전계속사업이익")
INCOME_TAX_EXPENSE_NAMES = ("법인세비용", "법인세비용수익")
RESEARCH_AND_DEVELOPMENT_NAMES = ("연구개발비", "경상연구개발비", "연구비", "개발비")
KOREA_DEFAULT_TAX_RATE = 0.24


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
    cash_row = _find_row(selected_rows, CASH_NAMES)
    debt_rows = _find_rows(selected_rows, TOTAL_DEBT_NAMES)
    pretax_income_row = _find_row(selected_rows, PRETAX_INCOME_NAMES)
    income_tax_expense_row = _find_row(selected_rows, INCOME_TAX_EXPENSE_NAMES)
    research_and_development_rows = _find_rows(selected_rows, RESEARCH_AND_DEVELOPMENT_NAMES)

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
    cash_and_equivalents, _ = _current_previous_from_row(cash_row)
    total_debt, _ = _sum_current_previous_from_rows(debt_rows)
    pretax_income, _ = _current_previous_from_row(pretax_income_row)
    income_tax_expense, _ = _current_previous_from_row(income_tax_expense_row)
    research_and_development, _ = _sum_current_previous_from_rows(research_and_development_rows)

    revenue_growth_pct = _growth_pct(revenue_current, revenue_previous)
    operating_margin_pct = _ratio_pct(operating_income, revenue_current)
    average_equity = _average(equity_current, equity_previous)
    roe_pct = _ratio_pct(net_income, average_equity)
    debt_to_equity_pct = _ratio_pct(_coalesce(total_debt, liabilities), equity_current)
    current_ratio_pct = _ratio_pct(current_assets, current_liabilities)
    depreciation_amortization = _sum_optional(depreciation, amortization)
    ebitda = _add(operating_income, depreciation_amortization)
    capex_outflow = _outflow(capital_expenditure)
    free_cash_flow = _subtract(operating_cash_flow, capex_outflow)
    interest_expense_outflow = _outflow(interest_expense)
    interest_coverage = _ratio(operating_income, interest_expense_outflow)
    market_cap = fallback.market_cap
    cash_value = _coalesce(cash_and_equivalents, fallback.cash_and_equivalents)
    total_debt_value = _coalesce(total_debt, fallback.total_debt)
    operating_income_value = _coalesce(operating_income, fallback.operating_income)
    pretax_income_value = _coalesce(pretax_income, fallback.pretax_income)
    income_tax_expense_value = _coalesce(income_tax_expense, fallback.income_tax_expense)
    revenue_value = _coalesce(revenue_current, fallback.revenue)
    research_and_development_value = _coalesce(
        research_and_development,
        fallback.research_and_development,
    )
    tax_rate, default_tax_rate_used = _effective_tax_rate(
        income_tax_expense_value,
        pretax_income_value,
        KOREA_DEFAULT_TAX_RATE,
    )
    enterprise_value = _enterprise_value(market_cap, total_debt_value, cash_value)
    roic_pct = _roic_pct(operating_income_value, tax_rate, total_debt_value, equity_current, cash_value)
    ev_to_ebit = _ratio(enterprise_value, operating_income_value)
    earnings_yield_pct = _ratio_pct(operating_income_value, enterprise_value)
    rd_to_revenue_pct = _ratio_pct(research_and_development_value, revenue_value)
    sources = dict(fallback.sources)
    _set_row_source(sources, "revenue", revenue_row, payload)
    _set_row_source(sources, "operatingIncome", operating_income_row, payload)
    _set_row_source(sources, "netIncome", net_income_row, payload)
    _set_row_source(sources, "operatingCashFlow", operating_cash_flow_row, payload)
    _set_row_source(sources, "capitalExpenditure", capital_expenditure_row, payload)
    _set_row_source(sources, "currentAssets", current_assets_row, payload)
    _set_row_source(sources, "currentLiabilities", current_liabilities_row, payload)
    _set_row_source(sources, "interestExpense", interest_expense_row, payload)
    _set_row_source(sources, "cashAndEquivalents", cash_row, payload)
    _set_row_source(sources, "totalDebt", debt_rows[0] if debt_rows else None, payload)
    _set_row_source(sources, "pretaxIncome", pretax_income_row, payload)
    _set_row_source(sources, "incomeTaxExpense", income_tax_expense_row, payload)
    _set_row_source(
        sources,
        "researchAndDevelopment",
        research_and_development_rows[0] if research_and_development_rows else None,
        payload,
    )
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
    if enterprise_value is not None:
        _set_row_source(
            sources,
            "enterpriseValue",
            (debt_rows[0] if debt_rows else None) or cash_row,
            payload,
            derived_from=("marketCap", "totalDebt", "cashAndEquivalents"),
        )
    if roic_pct is not None:
        _set_row_source(
            sources,
            "roic",
            operating_income_row or (debt_rows[0] if debt_rows else None) or cash_row or equity_row,
            payload,
            derived_from=("operatingIncome", "taxRate", "totalDebt", "equity", "cashAndEquivalents"),
            extra={
                "taxRate": tax_rate,
                "taxRateDefault": default_tax_rate_used,
                "defaultTaxRate": KOREA_DEFAULT_TAX_RATE if default_tax_rate_used else None,
            },
        )
    if ev_to_ebit is not None:
        _set_row_source(
            sources,
            "evToEbit",
            operating_income_row or (debt_rows[0] if debt_rows else None) or cash_row,
            payload,
            derived_from=("enterpriseValue", "operatingIncome"),
        )
    if earnings_yield_pct is not None:
        _set_row_source(
            sources,
            "earningsYield",
            operating_income_row or (debt_rows[0] if debt_rows else None) or cash_row,
            payload,
            derived_from=("operatingIncome", "enterpriseValue"),
        )
    if rd_to_revenue_pct is not None:
        _set_row_source(
            sources,
            "rdToRevenue",
            (research_and_development_rows[0] if research_and_development_rows else None) or revenue_row,
            payload,
            derived_from=("researchAndDevelopment", "revenue"),
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
        cash_and_equivalents=_coalesce(cash_and_equivalents, fallback.cash_and_equivalents),
        total_debt=_coalesce(total_debt, fallback.total_debt),
        pretax_income=_coalesce(pretax_income, fallback.pretax_income),
        income_tax_expense=_coalesce(income_tax_expense, fallback.income_tax_expense),
        research_and_development=_coalesce(
            research_and_development,
            fallback.research_and_development,
        ),
        enterprise_value=_coalesce(enterprise_value, fallback.enterprise_value),
        roic_pct=_coalesce(roic_pct, fallback.roic_pct),
        ev_to_ebit=_coalesce(ev_to_ebit, fallback.ev_to_ebit),
        earnings_yield_pct=_coalesce(earnings_yield_pct, fallback.earnings_yield_pct),
        rd_to_revenue_pct=_coalesce(rd_to_revenue_pct, fallback.rd_to_revenue_pct),
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
    normalized_names = tuple(name.replace(" ", "") for name in names)
    for row in rows:
        account_name = str(row.get("account_nm", "")).replace(" ", "")
        if account_name in normalized_names:
            return row
    for row in rows:
        account_name = str(row.get("account_nm", "")).replace(" ", "")
        if not any(name in account_name for name in normalized_names):
            continue
        return row
    return None


def _find_rows(rows: list[dict], names: tuple[str, ...]) -> list[dict]:
    matches: list[dict] = []
    seen: set[tuple[str, str]] = set()
    normalized_names = tuple(name.replace(" ", "") for name in names)
    for row in rows:
        account_name = str(row.get("account_nm", "")).replace(" ", "")
        if not any(name in account_name for name in normalized_names):
            continue
        key = (account_name, str(row.get("thstrm_amount") or ""))
        if key in seen:
            continue
        seen.add(key)
        matches.append(row)
    return matches


def _current_previous_from_row(row: dict | None) -> tuple[float | None, float | None]:
    if row is None:
        return None, None
    return _amount(row.get("thstrm_amount")), _amount(row.get("frmtrm_amount"))


def _sum_current_previous_from_rows(rows: list[dict]) -> tuple[float | None, float | None]:
    current_values: list[float] = []
    previous_values: list[float] = []
    for row in rows:
        current, previous = _current_previous_from_row(row)
        if current is not None:
            current_values.append(current)
        if previous is not None:
            previous_values.append(previous)
    current_total = sum(current_values) if current_values else None
    previous_total = sum(previous_values) if previous_values else None
    return current_total, previous_total


def _set_row_source(
    sources: dict[str, dict],
    field: str,
    row: dict | None,
    payload: dict,
    derived_from: tuple[str, ...] = (),
    extra: dict[str, object] | None = None,
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
    if extra:
        source.update({key: value for key, value in extra.items() if value is not None})
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


def _effective_tax_rate(
    income_tax_expense: float | None,
    pretax_income: float | None,
    default_rate: float,
) -> tuple[float, bool]:
    if (
        income_tax_expense is not None
        and pretax_income is not None
        and math.isfinite(income_tax_expense)
        and math.isfinite(pretax_income)
        and pretax_income > 0
        and income_tax_expense >= 0
    ):
        return _clamp_ratio(income_tax_expense / pretax_income, 0, 0.45), False
    return default_rate, True


def _enterprise_value(
    market_cap: float | None,
    total_debt: float | None,
    cash_and_equivalents: float | None,
) -> float | None:
    if (
        market_cap is None
        or total_debt is None
        or cash_and_equivalents is None
        or not math.isfinite(market_cap)
        or not math.isfinite(total_debt)
        or not math.isfinite(cash_and_equivalents)
    ):
        return None
    value = market_cap + total_debt - cash_and_equivalents
    return value if value > 0 else None


def _roic_pct(
    operating_income: float | None,
    tax_rate: float,
    total_debt: float | None,
    equity: float | None,
    cash_and_equivalents: float | None,
) -> float | None:
    if (
        operating_income is None
        or total_debt is None
        or equity is None
        or cash_and_equivalents is None
        or not math.isfinite(operating_income)
        or not math.isfinite(total_debt)
        or not math.isfinite(equity)
        or not math.isfinite(cash_and_equivalents)
    ):
        return None
    invested_capital = total_debt + equity - cash_and_equivalents
    if invested_capital <= 0:
        return None
    return (operating_income * (1 - tax_rate) / invested_capital) * 100


def _clamp_ratio(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _coalesce(value: float | None, fallback: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else fallback


@dataclass(frozen=True)
class OpenDartFinancialResult:
    stocks: tuple[StockProfile, ...]
    updated_count: int
    warnings: tuple[str, ...]
