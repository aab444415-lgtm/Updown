from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
import gzip
from dataclasses import dataclass, replace
from typing import Iterable

from .config import AppConfig
from .models import Fundamentals, StockProfile
from .storage import CacheStore


TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ANNUAL_FORMS = {"10-K", "20-F", "40-F"}
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
            warnings.append("SEC EDGAR에서 갱신 가능한 재무지표를 찾지 못해 내장 지표를 유지했습니다.")
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

    latest_revenue, previous_revenue = _fact_values(revenue)
    latest_operating_income, _ = _fact_values(operating_income)
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


def _is_annual_fact(fact: dict) -> bool:
    form = str(fact.get("form", ""))
    if form not in ANNUAL_FORMS:
        return False
    if fact.get("fp") not in {None, "FY"}:
        return False
    if fact.get("frame") and not str(fact.get("frame")).startswith("CY"):
        return False
    return True


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
