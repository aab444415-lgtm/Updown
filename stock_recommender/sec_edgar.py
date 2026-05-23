from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
import gzip
from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable

from .config import AppConfig
from .models import Fundamentals, StockProfile
from .storage import CacheStore


TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
QUARTERLY_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A"}
USD = "USD"

REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
NET_INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")
ASSET_TAGS = ("Assets",)
LIABILITY_TAGS = ("Liabilities",)
EQUITY_TAGS = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
CURRENT_ASSET_TAGS = ("AssetsCurrent",)
CURRENT_LIABILITY_TAGS = ("LiabilitiesCurrent",)
DEPRECIATION_AMORTIZATION_TAGS = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAndAmortization",
    "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
)
OPERATING_CASH_FLOW_TAGS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
CAPEX_TAGS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
)
INTEREST_EXPENSE_TAGS = (
    "InterestExpenseNonOperating",
    "InterestExpense",
    "InterestAndDebtExpense",
)
CASH_TAGS = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "CashAndDueFromBanks",
)
TOTAL_DEBT_TAGS = (
    "DebtAndFinanceLeaseObligations",
    "ShortTermBorrowingsAndCurrentPortionOfLongTermDebt",
)
DEBT_COMPONENT_TAGS = (
    "ShortTermBorrowings",
    "ShortTermDebt",
    "CurrentDebt",
    "LongTermDebtCurrent",
    "LongTermDebtNoncurrent",
    "LongTermDebt",
    "LongTermDebtAndFinanceLeaseObligationsCurrent",
    "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
)
PRETAX_INCOME_TAGS = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "IncomeLossBeforeIncomeTaxes",
)
INCOME_TAX_EXPENSE_TAGS = ("IncomeTaxExpenseBenefit",)
RESEARCH_AND_DEVELOPMENT_TAGS = (
    "ResearchAndDevelopmentExpense",
    "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
)
US_DEFAULT_TAX_RATE = 0.21


class SecEdgarClient:
    def __init__(
        self,
        config: AppConfig,
        cache: CacheStore,
        timeout: float = 10.0,
        request_interval_seconds: float = 0.2,
    ):
        self.config = config
        self.cache = cache
        self.timeout = timeout
        self.request_interval_seconds = request_interval_seconds

    def enrich_stocks(self, stocks: Iterable[StockProfile]) -> SecFundamentalResult:
        warnings: list[str] = []
        stock_tuple = tuple(stocks)
        try:
            ticker_map = self.fetch_ticker_map()
        except DataSourceError as exc:
            return SecFundamentalResult(stock_tuple, 0, (f"SEC EDGAR 티커 목록 수집 실패: {exc}",))

        enriched: list[StockProfile] = []
        updated_count = 0
        stale_count = 0
        for stock in stock_tuple:
            cik = ticker_map.get(stock.ticker.upper())
            if cik is None:
                enriched.append(stock)
                continue
            try:
                company_facts = self.fetch_company_facts(cik)
                facts = company_facts.payload
                if company_facts.stale:
                    stale_count += 1
                fundamentals = extract_fundamentals(facts, fallback=stock.fundamentals)
            except DataSourceError as exc:
                warnings.append(f"{stock.ticker} SEC 재무 수집 실패: {exc}")
                enriched.append(stock)
                continue

            if fundamentals != stock.fundamentals:
                updated_count += 1
            enriched.append(replace(stock, fundamentals=fundamentals))

        if updated_count == 0:
            warnings.append("SEC EDGAR에서 갱신 가능한 공식 재무지표를 찾지 못했습니다.")
        else:
            warnings.append(f"SEC EDGAR 재무지표로 {updated_count}개 종목을 갱신했습니다.")
        if stale_count:
            warnings.append(f"SEC EDGAR 실시간 호출이 일부 실패해 캐시된 공식 재무 데이터를 사용했습니다({stale_count}개).")
        return SecFundamentalResult(tuple(enriched), updated_count, tuple(warnings))

    def fetch_ticker_map(self) -> dict[str, str]:
        cache_key = "sec:ticker-map"
        cached = self.cache.get_json(cache_key)
        if isinstance(cached, dict):
            return {str(item["ticker"]).upper(): str(item["cik_str"]).zfill(10) for item in cached.values()}

        try:
            payload = self._fetch_json(TICKER_MAP_URL)
        except DataSourceError:
            stale = self.cache.get_json(cache_key, allow_expired=True)
            if isinstance(stale, dict):
                self._record_event("stale", "SEC 티커 목록 호출 실패로 만료 캐시를 사용했습니다.")
                return {str(item["ticker"]).upper(): str(item["cik_str"]).zfill(10) for item in stale.values()}
            raise
        if not isinstance(payload, dict):
            raise DataSourceError("티커 목록 응답 형식이 올바르지 않습니다.")
        self.cache.set_json(cache_key, "SEC EDGAR", TICKER_MAP_URL, payload, ttl_seconds=60 * 60 * 24)
        return {str(item["ticker"]).upper(): str(item["cik_str"]).zfill(10) for item in payload.values()}

    def fetch_company_facts(self, cik: str) -> CompanyFactsResult:
        normalized_cik = cik.zfill(10)
        url = COMPANY_FACTS_URL.format(cik=normalized_cik)
        cache_key = f"sec:companyfacts:{normalized_cik}"
        cached = self.cache.get_json(cache_key)
        if isinstance(cached, dict):
            return CompanyFactsResult(cached, stale=False)

        try:
            payload = self._fetch_json(url)
        except DataSourceError:
            stale = self.cache.get_json(cache_key, allow_expired=True)
            if isinstance(stale, dict):
                self._record_event("stale", f"CIK {normalized_cik} companyfacts 호출 실패로 만료 캐시를 사용했습니다.")
                return CompanyFactsResult(stale, stale=True)
            raise
        if not isinstance(payload, dict):
            raise DataSourceError("companyfacts 응답 형식이 올바르지 않습니다.")
        self.cache.set_json(cache_key, "SEC EDGAR", url, payload, ttl_seconds=60 * 60 * 12)
        return CompanyFactsResult(payload, stale=False)

    def _fetch_json(self, url: str) -> dict | list:
        time.sleep(self.request_interval_seconds)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.config.sec_user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            self._record_event("error", f"SEC EDGAR 호출 실패: {exc}")
            raise DataSourceError(str(exc)) from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
            self._record_event("success", "SEC EDGAR 응답을 수집했습니다.")
            return payload
        except json.JSONDecodeError as exc:
            self._record_event("error", "SEC EDGAR JSON 파싱 실패")
            raise DataSourceError("JSON 파싱 실패") from exc

    def _record_event(self, event_type: str, message: str) -> None:
        try:
            self.cache.record_source_event("SEC EDGAR", event_type, message)
        except Exception:
            return


def extract_fundamentals(facts: dict, fallback: Fundamentals | None = None) -> Fundamentals:
    fallback = fallback or Fundamentals()
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    if not isinstance(us_gaap, dict):
        return fallback

    revenue = _latest_and_previous_facts(us_gaap, REVENUE_TAGS)
    operating_income = _latest_and_previous_facts(us_gaap, OPERATING_INCOME_TAGS)
    net_income = _latest_and_previous_facts(us_gaap, NET_INCOME_TAGS)
    assets = _latest_and_previous_facts(us_gaap, ASSET_TAGS)
    liabilities = _latest_and_previous_facts(us_gaap, LIABILITY_TAGS)
    equity = _latest_and_previous_facts(us_gaap, EQUITY_TAGS)
    current_assets = _latest_and_previous_facts(us_gaap, CURRENT_ASSET_TAGS)
    current_liabilities = _latest_and_previous_facts(us_gaap, CURRENT_LIABILITY_TAGS)
    depreciation_amortization = _latest_and_previous_facts(us_gaap, DEPRECIATION_AMORTIZATION_TAGS)
    operating_cash_flow = _latest_and_previous_facts(us_gaap, OPERATING_CASH_FLOW_TAGS)
    capital_expenditure = _latest_and_previous_facts(us_gaap, CAPEX_TAGS)
    interest_expense = _latest_and_previous_facts(us_gaap, INTEREST_EXPENSE_TAGS)
    cash = _latest_and_previous_facts(us_gaap, CASH_TAGS)
    direct_debt = _latest_and_previous_facts(us_gaap, TOTAL_DEBT_TAGS)
    debt_components = _latest_and_previous_summed_facts(us_gaap, DEBT_COMPONENT_TAGS)
    pretax_income = _latest_and_previous_facts(us_gaap, PRETAX_INCOME_TAGS)
    income_tax_expense = _latest_and_previous_facts(us_gaap, INCOME_TAX_EXPENSE_TAGS)
    research_and_development = _latest_and_previous_facts(us_gaap, RESEARCH_AND_DEVELOPMENT_TAGS)
    annual_financials_for_calc = _annual_financial_series(us_gaap, limit=6)
    annual_financials = tuple(annual_financials_for_calc[:5])
    quarterly_financials = _quarterly_financial_series(us_gaap, limit=8)

    latest_revenue, previous_revenue = _fact_values(revenue)
    latest_operating_income, _ = _fact_values(operating_income)
    _, previous_operating_income = _fact_values(operating_income)
    latest_net_income, _ = _fact_values(net_income)
    latest_liabilities, _ = _fact_values(liabilities)
    latest_equity, previous_equity = _fact_values(equity)
    latest_current_assets, _ = _fact_values(current_assets)
    latest_current_liabilities, _ = _fact_values(current_liabilities)
    latest_depreciation_amortization, _ = _fact_values(depreciation_amortization)
    latest_operating_cash_flow, _ = _fact_values(operating_cash_flow)
    latest_capex, _ = _fact_values(capital_expenditure)
    latest_interest_expense, _ = _fact_values(interest_expense)
    latest_cash, _ = _fact_values(cash)
    latest_direct_debt, _ = _fact_values(direct_debt)
    latest_component_debt, _ = _fact_values(debt_components)
    latest_total_debt = _coalesce(latest_direct_debt, latest_component_debt)
    latest_pretax_income, _ = _fact_values(pretax_income)
    latest_income_tax_expense, _ = _fact_values(income_tax_expense)
    latest_research_and_development, _ = _fact_values(research_and_development)

    revenue_growth_pct = _growth_pct(latest_revenue, previous_revenue)
    operating_income_growth_pct = _growth_pct_positive_base(latest_operating_income, previous_operating_income)
    revenue_cagr_3y_pct = _cagr_from_financials(annual_financials_for_calc, "revenue", 3)
    revenue_cagr_5y_pct = _cagr_from_financials(annual_financials_for_calc, "revenue", 5)
    operating_income_cagr_3y_pct = _cagr_from_financials(annual_financials_for_calc, "operatingIncome", 3)
    operating_leverage_spread_pct = _subtract(operating_income_growth_pct, revenue_growth_pct)
    latest_quarter_revenue_yoy_pct = _latest_quarter_metric(quarterly_financials, "revenueYoYPct")
    latest_quarter_operating_income_yoy_pct = _latest_quarter_metric(
        quarterly_financials, "operatingIncomeYoYPct"
    )
    quarterly_revenue_yoy_streak = _quarterly_positive_streak(quarterly_financials, "revenueYoYPct")
    quarterly_operating_leverage_streak = _quarterly_operating_leverage_streak(quarterly_financials)
    operating_margin_pct = _ratio_pct(latest_operating_income, latest_revenue)
    average_equity = _average(latest_equity, previous_equity)
    roe_pct = _ratio_pct(latest_net_income, average_equity)
    debt_to_equity_pct = _ratio_pct(_coalesce(latest_total_debt, latest_liabilities), latest_equity)
    current_ratio_pct = _ratio_pct(latest_current_assets, latest_current_liabilities)
    ebitda = _add(latest_operating_income, latest_depreciation_amortization)
    capex_outflow = _outflow(latest_capex)
    free_cash_flow = _subtract(latest_operating_cash_flow, capex_outflow)
    interest_expense_outflow = _outflow(latest_interest_expense)
    interest_coverage = _ratio(latest_operating_income, interest_expense_outflow)
    market_cap = fallback.market_cap
    cash_value = _coalesce(latest_cash, fallback.cash_and_equivalents)
    total_debt_value = _coalesce(latest_total_debt, fallback.total_debt)
    equity_value = _coalesce(latest_equity, None)
    operating_income_value = _coalesce(latest_operating_income, fallback.operating_income)
    pretax_income_value = _coalesce(latest_pretax_income, fallback.pretax_income)
    income_tax_expense_value = _coalesce(latest_income_tax_expense, fallback.income_tax_expense)
    revenue_value = _coalesce(latest_revenue, fallback.revenue)
    research_and_development_value = _coalesce(
        latest_research_and_development, fallback.research_and_development
    )
    tax_rate, default_tax_rate_used = _effective_tax_rate(
        income_tax_expense_value,
        pretax_income_value,
        US_DEFAULT_TAX_RATE,
    )
    enterprise_value = _enterprise_value(market_cap, total_debt_value, cash_value)
    roic_pct = _roic_pct(operating_income_value, tax_rate, total_debt_value, equity_value, cash_value)
    ev_to_ebit = _ratio(enterprise_value, operating_income_value)
    earnings_yield_pct = _ratio_pct(operating_income_value, enterprise_value)
    rd_to_revenue_pct = _ratio_pct(research_and_development_value, revenue_value)
    sources = dict(fallback.sources)
    _set_source(sources, "revenue", revenue[0])
    _set_source(sources, "operatingIncome", operating_income[0])
    _set_source(sources, "netIncome", net_income[0])
    _set_source(sources, "operatingCashFlow", operating_cash_flow[0])
    _set_source(sources, "capitalExpenditure", capital_expenditure[0])
    _set_source(sources, "currentAssets", current_assets[0])
    _set_source(sources, "currentLiabilities", current_liabilities[0])
    _set_source(sources, "interestExpense", interest_expense[0])
    _set_source(sources, "cashAndEquivalents", cash[0])
    _set_source(sources, "totalDebt", direct_debt[0] or debt_components[0])
    _set_source(sources, "pretaxIncome", pretax_income[0])
    _set_source(sources, "incomeTaxExpense", income_tax_expense[0])
    _set_source(sources, "researchAndDevelopment", research_and_development[0])
    if revenue_growth_pct is not None:
        _set_source(sources, "revenueGrowth", revenue[0], derived_from=("revenue",))
    if operating_margin_pct is not None:
        _set_source(sources, "operatingMargin", operating_income[0] or revenue[0], derived_from=("operatingIncome", "revenue"))
    if roe_pct is not None:
        _set_source(sources, "roe", net_income[0] or equity[0], derived_from=("netIncome", "equity"))
    if debt_to_equity_pct is not None:
        _set_source(
            sources,
            "debtToEquity",
            direct_debt[0] or debt_components[0] or liabilities[0] or equity[0],
            derived_from=("totalDebt", "liabilities", "equity"),
        )
    if current_ratio_pct is not None:
        _set_source(
            sources,
            "currentRatio",
            current_assets[0] or current_liabilities[0],
            derived_from=("currentAssets", "currentLiabilities"),
        )
    if interest_coverage is not None:
        _set_source(
            sources,
            "interestCoverage",
            operating_income[0] or interest_expense[0],
            derived_from=("operatingIncome", "interestExpense"),
        )
    if ebitda is not None:
        _set_source(sources, "ebitda", operating_income[0] or depreciation_amortization[0])
    if free_cash_flow is not None:
        _set_source(
            sources,
            "freeCashFlow",
            operating_cash_flow[0] or capital_expenditure[0],
            derived_from=("operatingCashFlow", "capitalExpenditure"),
        )
    if enterprise_value is not None:
        _set_source(
            sources,
            "enterpriseValue",
            direct_debt[0] or debt_components[0] or cash[0],
            derived_from=("marketCap", "totalDebt", "cashAndEquivalents"),
        )
    if roic_pct is not None:
        _set_source(
            sources,
            "roic",
            operating_income[0] or direct_debt[0] or debt_components[0] or cash[0] or equity[0],
            derived_from=("operatingIncome", "taxRate", "totalDebt", "equity", "cashAndEquivalents"),
            extra={
                "taxRate": tax_rate,
                "taxRateDefault": default_tax_rate_used,
                "defaultTaxRate": US_DEFAULT_TAX_RATE if default_tax_rate_used else None,
            },
        )
    if ev_to_ebit is not None:
        _set_source(
            sources,
            "evToEbit",
            operating_income[0] or direct_debt[0] or debt_components[0] or cash[0],
            derived_from=("enterpriseValue", "operatingIncome"),
        )
    if earnings_yield_pct is not None:
        _set_source(
            sources,
            "earningsYield",
            operating_income[0] or direct_debt[0] or debt_components[0] or cash[0],
            derived_from=("operatingIncome", "enterpriseValue"),
        )
    if rd_to_revenue_pct is not None:
        _set_source(
            sources,
            "rdToRevenue",
            research_and_development[0] or revenue[0],
            derived_from=("researchAndDevelopment", "revenue"),
        )
    _set_derived_growth_sources(
        sources,
        source_name="SEC EDGAR",
        annual_financials=annual_financials,
        quarterly_financials=quarterly_financials,
        fields=(
            ("revenueCagr3y", revenue_cagr_3y_pct, ("annualFinancials",)),
            ("revenueCagr5y", revenue_cagr_5y_pct, ("annualFinancials",)),
            ("operatingIncomeGrowth", operating_income_growth_pct, ("operatingIncome",)),
            ("operatingIncomeCagr3y", operating_income_cagr_3y_pct, ("annualFinancials",)),
            ("operatingLeverageSpread", operating_leverage_spread_pct, ("revenueGrowth", "operatingIncomeGrowth")),
            ("latestQuarterRevenueYoy", latest_quarter_revenue_yoy_pct, ("quarterlyFinancials",)),
            (
                "latestQuarterOperatingIncomeYoy",
                latest_quarter_operating_income_yoy_pct,
                ("quarterlyFinancials",),
            ),
        ),
    )

    return Fundamentals(
        revenue_growth_pct=_coalesce(revenue_growth_pct, fallback.revenue_growth_pct),
        operating_margin_pct=_coalesce(operating_margin_pct, fallback.operating_margin_pct),
        roe_pct=_coalesce(roe_pct, fallback.roe_pct),
        debt_to_equity_pct=_coalesce(debt_to_equity_pct, fallback.debt_to_equity_pct),
        pe=fallback.pe,
        forward_pe=fallback.forward_pe,
        market_cap=fallback.market_cap,
        market_cap_currency=fallback.market_cap_currency,
        revenue=_coalesce(latest_revenue, fallback.revenue),
        operating_income=_coalesce(latest_operating_income, fallback.operating_income),
        ebitda=_coalesce(ebitda, fallback.ebitda),
        net_income=_coalesce(latest_net_income, fallback.net_income),
        operating_cash_flow=_coalesce(latest_operating_cash_flow, fallback.operating_cash_flow),
        capital_expenditure=_coalesce(capex_outflow, fallback.capital_expenditure),
        free_cash_flow=_coalesce(free_cash_flow, fallback.free_cash_flow),
        current_assets=_coalesce(latest_current_assets, fallback.current_assets),
        current_liabilities=_coalesce(latest_current_liabilities, fallback.current_liabilities),
        current_ratio_pct=_coalesce(current_ratio_pct, fallback.current_ratio_pct),
        interest_expense=_coalesce(interest_expense_outflow, fallback.interest_expense),
        interest_coverage=_coalesce(interest_coverage, fallback.interest_coverage),
        cash_and_equivalents=_coalesce(latest_cash, fallback.cash_and_equivalents),
        total_debt=_coalesce(latest_total_debt, fallback.total_debt),
        pretax_income=_coalesce(latest_pretax_income, fallback.pretax_income),
        income_tax_expense=_coalesce(latest_income_tax_expense, fallback.income_tax_expense),
        research_and_development=_coalesce(
            latest_research_and_development,
            fallback.research_and_development,
        ),
        enterprise_value=_coalesce(enterprise_value, fallback.enterprise_value),
        roic_pct=_coalesce(roic_pct, fallback.roic_pct),
        ev_to_ebit=_coalesce(ev_to_ebit, fallback.ev_to_ebit),
        earnings_yield_pct=_coalesce(earnings_yield_pct, fallback.earnings_yield_pct),
        rd_to_revenue_pct=_coalesce(rd_to_revenue_pct, fallback.rd_to_revenue_pct),
        revenue_cagr_3y_pct=_coalesce(revenue_cagr_3y_pct, fallback.revenue_cagr_3y_pct),
        revenue_cagr_5y_pct=_coalesce(revenue_cagr_5y_pct, fallback.revenue_cagr_5y_pct),
        operating_income_growth_pct=_coalesce(
            operating_income_growth_pct,
            fallback.operating_income_growth_pct,
        ),
        operating_income_cagr_3y_pct=_coalesce(
            operating_income_cagr_3y_pct,
            fallback.operating_income_cagr_3y_pct,
        ),
        operating_leverage_spread_pct=_coalesce(
            operating_leverage_spread_pct,
            fallback.operating_leverage_spread_pct,
        ),
        latest_quarter_revenue_yoy_pct=_coalesce(
            latest_quarter_revenue_yoy_pct,
            fallback.latest_quarter_revenue_yoy_pct,
        ),
        latest_quarter_operating_income_yoy_pct=_coalesce(
            latest_quarter_operating_income_yoy_pct,
            fallback.latest_quarter_operating_income_yoy_pct,
        ),
        quarterly_revenue_yoy_streak=_coalesce_int(
            quarterly_revenue_yoy_streak,
            fallback.quarterly_revenue_yoy_streak,
        ),
        quarterly_operating_leverage_streak=_coalesce_int(
            quarterly_operating_leverage_streak,
            fallback.quarterly_operating_leverage_streak,
        ),
        annual_financials=annual_financials or fallback.annual_financials,
        quarterly_financials=quarterly_financials or fallback.quarterly_financials,
        sources=sources,
    )


def _latest_and_previous(us_gaap: dict, tags: tuple[str, ...]) -> tuple[float | None, float | None]:
    latest, previous = _latest_and_previous_facts(us_gaap, tags)
    return _fact_value(latest), _fact_value(previous)


def _latest_and_previous_facts(
    us_gaap: dict, tags: tuple[str, ...]
) -> tuple[SelectedFact | None, SelectedFact | None]:
    candidates: dict[str, tuple[str, SelectedFact]] = {}
    for tag in tags:
        concept = us_gaap.get(tag)
        if not isinstance(concept, dict):
            continue
        units = concept.get("units", {})
        facts = units.get(USD)
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if not _is_annual_fact(fact):
                continue
            value = fact.get("val")
            period = _annual_period_key(fact)
            filed = str(fact.get("filed") or "")
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not period:
                continue
            existing = candidates.get(period)
            if existing is None or filed >= existing[0]:
                candidates[period] = (filed, SelectedFact(float(value), _fact_source(fact, tag)))

    unique = sorted(
        ((period, selected) for period, (_, selected) in candidates.items()),
        key=lambda item: item[0],
        reverse=True,
    )[:2]
    latest = unique[0][1] if unique else None
    previous = unique[1][1] if len(unique) > 1 else None
    return latest, previous


def _latest_and_previous_summed_facts(
    us_gaap: dict, tags: tuple[str, ...]
) -> tuple[SelectedFact | None, SelectedFact | None]:
    candidates: dict[str, dict[str, tuple[str, SelectedFact]]] = {}
    for tag in tags:
        concept = us_gaap.get(tag)
        if not isinstance(concept, dict):
            continue
        units = concept.get("units", {})
        facts = units.get(USD)
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if not _is_annual_fact(fact):
                continue
            value = fact.get("val")
            period = _annual_period_key(fact)
            filed = str(fact.get("filed") or "")
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not period:
                continue
            by_tag = candidates.setdefault(period, {})
            existing = by_tag.get(tag)
            if existing is None or filed >= existing[0]:
                by_tag[tag] = (filed, SelectedFact(float(value), _fact_source(fact, tag)))

    summed: list[tuple[str, SelectedFact]] = []
    for period, by_tag in candidates.items():
        facts = [item[1] for item in by_tag.values()]
        if not facts:
            continue
        source_fact = max(facts, key=lambda item: str(item.source.get("filed") or ""))
        source = dict(source_fact.source)
        source["tag"] = "+".join(sorted(by_tag))
        source["derivedFrom"] = sorted(by_tag)
        summed.append((period, SelectedFact(sum(item.value for item in facts), source)))
    summed.sort(key=lambda item: item[0], reverse=True)
    latest = summed[0][1] if summed else None
    previous = summed[1][1] if len(summed) > 1 else None
    return latest, previous


def _annual_financial_series(us_gaap: dict, limit: int = 6) -> tuple[dict, ...]:
    revenue_by_period = _selected_fact_series(us_gaap, REVENUE_TAGS, _is_annual_fact, _annual_period_key)
    operating_by_period = _selected_fact_series(
        us_gaap, OPERATING_INCOME_TAGS, _is_annual_fact, _annual_period_key
    )
    return _financial_records(revenue_by_period, operating_by_period, limit=limit)


def _quarterly_financial_series(us_gaap: dict, limit: int = 8) -> tuple[dict, ...]:
    revenue_by_period = _selected_fact_series(us_gaap, REVENUE_TAGS, _is_quarterly_fact, _quarter_period_key)
    operating_by_period = _selected_fact_series(
        us_gaap, OPERATING_INCOME_TAGS, _is_quarterly_fact, _quarter_period_key
    )
    records = _financial_records(revenue_by_period, operating_by_period, limit=None)
    return _annotate_quarterly_yoy(records)[:limit]


def _selected_fact_series(
    us_gaap: dict,
    tags: tuple[str, ...],
    fact_filter,
    period_key_fn,
) -> dict[str, SelectedFact]:
    candidates: dict[str, tuple[str, SelectedFact]] = {}
    for tag in tags:
        concept = us_gaap.get(tag)
        if not isinstance(concept, dict):
            continue
        units = concept.get("units", {})
        facts = units.get(USD)
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if not fact_filter(fact):
                continue
            value = fact.get("val")
            period = period_key_fn(fact)
            filed = str(fact.get("filed") or "")
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not period:
                continue
            existing = candidates.get(period)
            if existing is None or filed >= existing[0]:
                candidates[period] = (filed, SelectedFact(float(value), _fact_source(fact, tag)))
    return {period: selected for period, (_, selected) in candidates.items()}


def _financial_records(
    revenue_by_period: dict[str, SelectedFact],
    operating_by_period: dict[str, SelectedFact],
    limit: int | None,
) -> tuple[dict, ...]:
    records: list[dict] = []
    for period in sorted(set(revenue_by_period) | set(operating_by_period), reverse=True):
        revenue = revenue_by_period.get(period)
        operating_income = operating_by_period.get(period)
        source = revenue.source if revenue is not None else operating_income.source if operating_income else {}
        record = {
            "source": source.get("source") or "SEC EDGAR",
            "periodEnd": period,
            "fiscalYear": source.get("fiscalYear"),
            "fiscalPeriod": source.get("fiscalPeriod"),
            "filed": source.get("filed"),
            "revenue": _fact_value(revenue),
            "operatingIncome": _fact_value(operating_income),
        }
        records.append(record)
    if limit is None:
        return tuple(records)
    return tuple(records[:limit])


def _annotate_quarterly_yoy(records: tuple[dict, ...]) -> tuple[dict, ...]:
    by_identity: dict[tuple[int, str], dict] = {}
    normalized: list[dict] = []
    for record in records:
        next_record = dict(record)
        year = _record_year(next_record)
        period = _record_quarter(next_record)
        next_record["_year"] = year
        next_record["_quarter"] = period
        if year is not None and period:
            by_identity[(year, period)] = next_record
        normalized.append(next_record)

    annotated: list[dict] = []
    for record in normalized:
        prior = None
        year = record.get("_year")
        period = record.get("_quarter")
        if isinstance(year, int) and isinstance(period, str):
            prior = by_identity.get((year - 1, period))
        next_record = {key: value for key, value in record.items() if not key.startswith("_")}
        if prior is not None:
            revenue_yoy = _growth_pct_positive_base(
                _number(next_record.get("revenue")),
                _number(prior.get("revenue")),
            )
            operating_yoy = _growth_pct_positive_base(
                _number(next_record.get("operatingIncome")),
                _number(prior.get("operatingIncome")),
            )
            leverage = _subtract(operating_yoy, revenue_yoy)
            next_record["revenueYoYPct"] = revenue_yoy
            next_record["operatingIncomeYoYPct"] = operating_yoy
            next_record["operatingLeverageSpreadPct"] = leverage
        annotated.append(next_record)
    return tuple(annotated)


def _fact_values(facts: tuple[SelectedFact | None, SelectedFact | None]) -> tuple[float | None, float | None]:
    return _fact_value(facts[0]), _fact_value(facts[1])


def _fact_value(fact: SelectedFact | None) -> float | None:
    return fact.value if fact is not None else None


def _fact_source(fact: dict, tag: str) -> dict:
    return {
        "source": "SEC EDGAR",
        "tag": tag,
        "periodEnd": fact.get("end") if isinstance(fact.get("end"), str) else None,
        "fiscalYear": fact.get("fy") if isinstance(fact.get("fy"), (int, str)) else None,
        "fiscalPeriod": fact.get("fp") if isinstance(fact.get("fp"), str) else None,
        "filed": fact.get("filed") if isinstance(fact.get("filed"), str) else None,
        "form": fact.get("form") if isinstance(fact.get("form"), str) else None,
        "reportCode": None,
        "fallback": False,
    }


def _set_source(
    sources: dict[str, dict],
    field: str,
    fact: SelectedFact | None,
    derived_from: tuple[str, ...] = (),
    extra: dict[str, object] | None = None,
) -> None:
    if fact is None:
        return
    source = dict(fact.source)
    if derived_from:
        source["derivedFrom"] = list(derived_from)
    if extra:
        source.update({key: value for key, value in extra.items() if value is not None})
    sources[field] = source


def _set_derived_growth_sources(
    sources: dict[str, dict],
    source_name: str,
    annual_financials: tuple[dict, ...],
    quarterly_financials: tuple[dict, ...],
    fields: tuple[tuple[str, float | None, tuple[str, ...]], ...],
) -> None:
    reference = annual_financials[0] if annual_financials else quarterly_financials[0] if quarterly_financials else {}
    for field, value, derived_from in fields:
        if value is None:
            continue
        sources[field] = {
            "source": source_name,
            "periodEnd": reference.get("periodEnd"),
            "fiscalYear": reference.get("fiscalYear"),
            "filed": reference.get("filed"),
            "form": None,
            "reportCode": None,
            "fallback": False,
            "derivedFrom": list(derived_from),
        }


def _annual_period_key(fact: dict) -> str | None:
    end = fact.get("end")
    if isinstance(end, str) and end:
        return end
    fy = fact.get("fy")
    if isinstance(fy, (int, str)) and str(fy):
        return str(fy)
    frame = fact.get("frame")
    if isinstance(frame, str) and frame.startswith("CY"):
        return frame[:6]
    return None


def _quarter_period_key(fact: dict) -> str | None:
    end = fact.get("end")
    return end if isinstance(end, str) and end else None


def _is_annual_fact(fact: dict) -> bool:
    form = str(fact.get("form", ""))
    if form not in ANNUAL_FORMS:
        return False
    if fact.get("fp") not in {None, "FY"}:
        return False
    if fact.get("frame") and not str(fact.get("frame")).startswith("CY"):
        return False
    return True


def _is_quarterly_fact(fact: dict) -> bool:
    form = str(fact.get("form", ""))
    if form not in QUARTERLY_FORMS:
        return False
    if _duration_days(fact) is None or not 45 <= _duration_days(fact) <= 130:
        return False
    fp = fact.get("fp")
    frame = str(fact.get("frame") or "")
    if fp in {"Q1", "Q2", "Q3", "Q4"}:
        return True
    return "Q" in frame


def _duration_days(fact: dict) -> int | None:
    start = _parse_date(fact.get("start"))
    end = _parse_date(fact.get("end"))
    if start is None or end is None:
        return None
    return (end - start).days + 1


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _record_year(record: dict) -> int | None:
    fiscal_year = record.get("fiscalYear")
    if isinstance(fiscal_year, int):
        return fiscal_year
    if isinstance(fiscal_year, str) and fiscal_year.isdigit():
        return int(fiscal_year)
    period_end = record.get("periodEnd")
    if isinstance(period_end, str) and len(period_end) >= 4 and period_end[:4].isdigit():
        return int(period_end[:4])
    return None


def _record_quarter(record: dict) -> str | None:
    fiscal_period = record.get("fiscalPeriod")
    if fiscal_period in {"Q1", "Q2", "Q3", "Q4"}:
        return str(fiscal_period)
    period_end = _parse_date(record.get("periodEnd"))
    if period_end is None:
        return None
    return f"Q{((period_end.month - 1) // 3) + 1}"


def _growth_pct(latest: float | None, previous: float | None) -> float | None:
    if latest is None or previous is None or previous == 0:
        return None
    return ((latest / previous) - 1) * 100


def _growth_pct_positive_base(latest: float | None, previous: float | None) -> float | None:
    if latest is None or previous is None or previous <= 0:
        return None
    return ((latest / previous) - 1) * 100


def _cagr_from_financials(records: tuple[dict, ...], field: str, years: int) -> float | None:
    latest = next((record for record in records if _number(record.get(field)) is not None), None)
    if latest is None:
        return None
    latest_year = _record_year(latest)
    older = None
    if latest_year is not None:
        older = next((record for record in records if _record_year(record) == latest_year - years), None)
    if older is None and len(records) > years:
        older = records[years]
    if older is None:
        return None
    return _cagr_pct(_number(latest.get(field)), _number(older.get(field)), years)


def _cagr_pct(latest: float | None, previous: float | None, years: int) -> float | None:
    if latest is None or previous is None or latest <= 0 or previous <= 0 or years <= 0:
        return None
    return ((latest / previous) ** (1 / years) - 1) * 100


def _latest_quarter_metric(records: tuple[dict, ...], field: str) -> float | None:
    for record in records:
        value = _number(record.get(field))
        if value is not None:
            return value
    return None


def _quarterly_positive_streak(records: tuple[dict, ...], field: str) -> int | None:
    if not records:
        return None
    count = 0
    saw_metric = False
    for record in records:
        value = _number(record.get(field))
        if value is None:
            if not saw_metric:
                continue
            break
        saw_metric = True
        if value <= 0:
            break
        count += 1
    return count if saw_metric else None


def _quarterly_operating_leverage_streak(records: tuple[dict, ...]) -> int | None:
    if not records:
        return None
    count = 0
    saw_metric = False
    for record in records:
        revenue_yoy = _number(record.get("revenueYoYPct"))
        operating_yoy = _number(record.get("operatingIncomeYoYPct"))
        if revenue_yoy is None or operating_yoy is None:
            if not saw_metric:
                continue
            break
        saw_metric = True
        if revenue_yoy <= 0 or operating_yoy <= revenue_yoy:
            break
        count += 1
    return count if saw_metric else None


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


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


def _coalesce_int(value: int | None, fallback: int | None) -> int | None:
    return value if value is not None else fallback


class DataSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SecFundamentalResult:
    stocks: tuple[StockProfile, ...]
    updated_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CompanyFactsResult:
    payload: dict
    stale: bool


@dataclass(frozen=True)
class SelectedFact:
    value: float
    source: dict
