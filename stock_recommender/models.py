from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


StockRole = Literal["core", "adjacent"]
MarketProxyRole = Literal["etf", "representative"]


@dataclass(frozen=True, init=False)
class Fundamentals:
    revenue_growth_pct: float | None = None
    operating_margin_pct: float | None = None
    roe_pct: float | None = None
    debt_to_equity_pct: float | None = None
    pe: float | None = None
    forward_pe: float | None = None
    market_cap: float | None = None
    market_cap_currency: str = "USD"
    revenue: float | None = None
    operating_income: float | None = None
    ebitda: float | None = None
    net_income: float | None = None
    operating_cash_flow: float | None = None
    capital_expenditure: float | None = None
    free_cash_flow: float | None = None
    current_assets: float | None = None
    current_liabilities: float | None = None
    current_ratio_pct: float | None = None
    interest_expense: float | None = None
    interest_coverage: float | None = None
    cash_and_equivalents: float | None = None
    total_debt: float | None = None
    pretax_income: float | None = None
    income_tax_expense: float | None = None
    research_and_development: float | None = None
    enterprise_value: float | None = None
    roic_pct: float | None = None
    ev_to_ebit: float | None = None
    earnings_yield_pct: float | None = None
    rd_to_revenue_pct: float | None = None
    revenue_cagr_3y_pct: float | None = None
    revenue_cagr_5y_pct: float | None = None
    operating_income_growth_pct: float | None = None
    operating_income_cagr_3y_pct: float | None = None
    operating_leverage_spread_pct: float | None = None
    latest_quarter_revenue_yoy_pct: float | None = None
    latest_quarter_operating_income_yoy_pct: float | None = None
    quarterly_revenue_yoy_streak: int | None = None
    quarterly_operating_leverage_streak: int | None = None
    annual_financials: tuple[dict, ...] = ()
    quarterly_financials: tuple[dict, ...] = ()
    sources: dict[str, dict] = field(default_factory=dict)

    def __init__(
        self,
        revenue_growth_pct: float | None = None,
        operating_margin_pct: float | None = None,
        roe_pct: float | None = None,
        debt_to_equity_pct: float | None = None,
        pe: float | None = None,
        forward_pe: float | None = None,
        market_cap: float | None = None,
        market_cap_currency: str = "USD",
        revenue: float | None = None,
        operating_income: float | None = None,
        ebitda: float | None = None,
        net_income: float | None = None,
        operating_cash_flow: float | None = None,
        capital_expenditure: float | None = None,
        free_cash_flow: float | None = None,
        current_assets: float | None = None,
        current_liabilities: float | None = None,
        current_ratio_pct: float | None = None,
        interest_expense: float | None = None,
        interest_coverage: float | None = None,
        cash_and_equivalents: float | None = None,
        total_debt: float | None = None,
        pretax_income: float | None = None,
        income_tax_expense: float | None = None,
        research_and_development: float | None = None,
        enterprise_value: float | None = None,
        roic_pct: float | None = None,
        ev_to_ebit: float | None = None,
        earnings_yield_pct: float | None = None,
        rd_to_revenue_pct: float | None = None,
        revenue_cagr_3y_pct: float | None = None,
        revenue_cagr_5y_pct: float | None = None,
        operating_income_growth_pct: float | None = None,
        operating_income_cagr_3y_pct: float | None = None,
        operating_leverage_spread_pct: float | None = None,
        latest_quarter_revenue_yoy_pct: float | None = None,
        latest_quarter_operating_income_yoy_pct: float | None = None,
        quarterly_revenue_yoy_streak: int | None = None,
        quarterly_operating_leverage_streak: int | None = None,
        annual_financials: tuple[dict, ...] = (),
        quarterly_financials: tuple[dict, ...] = (),
        market_cap_usd: float | None = None,
        sources: dict[str, dict] | None = None,
    ) -> None:
        if market_cap is None:
            market_cap = market_cap_usd
        values = {
            "revenue_growth_pct": revenue_growth_pct,
            "operating_margin_pct": operating_margin_pct,
            "roe_pct": roe_pct,
            "debt_to_equity_pct": debt_to_equity_pct,
            "pe": pe,
            "forward_pe": forward_pe,
            "market_cap": market_cap,
            "market_cap_currency": market_cap_currency,
            "revenue": revenue,
            "operating_income": operating_income,
            "ebitda": ebitda,
            "net_income": net_income,
            "operating_cash_flow": operating_cash_flow,
            "capital_expenditure": capital_expenditure,
            "free_cash_flow": free_cash_flow,
            "current_assets": current_assets,
            "current_liabilities": current_liabilities,
            "current_ratio_pct": current_ratio_pct,
            "interest_expense": interest_expense,
            "interest_coverage": interest_coverage,
            "cash_and_equivalents": cash_and_equivalents,
            "total_debt": total_debt,
            "pretax_income": pretax_income,
            "income_tax_expense": income_tax_expense,
            "research_and_development": research_and_development,
            "enterprise_value": enterprise_value,
            "roic_pct": roic_pct,
            "ev_to_ebit": ev_to_ebit,
            "earnings_yield_pct": earnings_yield_pct,
            "rd_to_revenue_pct": rd_to_revenue_pct,
            "revenue_cagr_3y_pct": revenue_cagr_3y_pct,
            "revenue_cagr_5y_pct": revenue_cagr_5y_pct,
            "operating_income_growth_pct": operating_income_growth_pct,
            "operating_income_cagr_3y_pct": operating_income_cagr_3y_pct,
            "operating_leverage_spread_pct": operating_leverage_spread_pct,
            "latest_quarter_revenue_yoy_pct": latest_quarter_revenue_yoy_pct,
            "latest_quarter_operating_income_yoy_pct": latest_quarter_operating_income_yoy_pct,
            "quarterly_revenue_yoy_streak": quarterly_revenue_yoy_streak,
            "quarterly_operating_leverage_streak": quarterly_operating_leverage_streak,
            "annual_financials": tuple(annual_financials or ()),
            "quarterly_financials": tuple(quarterly_financials or ()),
            "sources": dict(sources or {}),
        }
        for key, value in values.items():
            object.__setattr__(self, key, value)

    @property
    def market_cap_usd(self) -> float | None:
        return self.market_cap


FUNDAMENTAL_SOURCE_BY_ATTR = {
    "revenue_growth_pct": "revenueGrowth",
    "operating_margin_pct": "operatingMargin",
    "roe_pct": "roe",
    "debt_to_equity_pct": "debtToEquity",
    "pe": "pe",
    "forward_pe": "forwardPe",
    "market_cap": "marketCap",
    "revenue": "revenue",
    "operating_income": "operatingIncome",
    "ebitda": "ebitda",
    "net_income": "netIncome",
    "operating_cash_flow": "operatingCashFlow",
    "capital_expenditure": "capitalExpenditure",
    "free_cash_flow": "freeCashFlow",
    "current_assets": "currentAssets",
    "current_liabilities": "currentLiabilities",
    "current_ratio_pct": "currentRatio",
    "interest_expense": "interestExpense",
    "interest_coverage": "interestCoverage",
    "cash_and_equivalents": "cashAndEquivalents",
    "total_debt": "totalDebt",
    "pretax_income": "pretaxIncome",
    "income_tax_expense": "incomeTaxExpense",
    "research_and_development": "researchAndDevelopment",
    "enterprise_value": "enterpriseValue",
    "roic_pct": "roic",
    "ev_to_ebit": "evToEbit",
    "earnings_yield_pct": "earningsYield",
    "rd_to_revenue_pct": "rdToRevenue",
    "revenue_cagr_3y_pct": "revenueCagr3y",
    "revenue_cagr_5y_pct": "revenueCagr5y",
    "operating_income_growth_pct": "operatingIncomeGrowth",
    "operating_income_cagr_3y_pct": "operatingIncomeCagr3y",
    "operating_leverage_spread_pct": "operatingLeverageSpread",
    "latest_quarter_revenue_yoy_pct": "latestQuarterRevenueYoy",
    "latest_quarter_operating_income_yoy_pct": "latestQuarterOperatingIncomeYoy",
}


def fundamentals_with_real_sources_only(fundamentals: Fundamentals) -> Fundamentals:
    values = {
        attr: getattr(fundamentals, attr)
        if _has_real_source(fundamentals.sources.get(source_key))
        else None
        for attr, source_key in FUNDAMENTAL_SOURCE_BY_ATTR.items()
    }
    quarterly_financials = _real_financial_records(fundamentals.quarterly_financials)
    values["quarterly_revenue_yoy_streak"] = (
        fundamentals.quarterly_revenue_yoy_streak if quarterly_financials else None
    )
    values["quarterly_operating_leverage_streak"] = (
        fundamentals.quarterly_operating_leverage_streak if quarterly_financials else None
    )
    return Fundamentals(
        **values,
        market_cap_currency=fundamentals.market_cap_currency,
        annual_financials=_real_financial_records(fundamentals.annual_financials),
        quarterly_financials=quarterly_financials,
        sources={
            key: dict(value)
            for key, value in fundamentals.sources.items()
            if _has_real_source(value)
        },
    )


def real_fundamental_value_count(fundamentals: Fundamentals) -> int:
    return sum(
        1
        for attr in FUNDAMENTAL_SOURCE_BY_ATTR
        if getattr(fundamentals, attr) is not None
        and _has_real_source(fundamentals.sources.get(FUNDAMENTAL_SOURCE_BY_ATTR[attr]))
    )


def _real_financial_records(records: tuple[dict, ...]) -> tuple[dict, ...]:
    return tuple(record for record in records if _real_record_source(record))


def _real_record_source(record: dict) -> bool:
    source = record.get("source")
    return isinstance(source, str) and source and source != "universeFallback"


def _has_real_source(source: object) -> bool:
    if not isinstance(source, dict):
        return False
    name = source.get("source")
    return isinstance(name, str) and bool(name) and name != "universeFallback" and not source.get("fallback")


@dataclass(frozen=True)
class Momentum:
    one_month_pct: float | None = None
    three_month_pct: float | None = None
    six_month_pct: float | None = None
    drawdown_from_high_pct: float | None = None
    range_position_pct: float | None = None
    latest_open: float | None = None
    latest_high: float | None = None
    latest_low: float | None = None
    latest_close: float | None = None
    previous_close: float | None = None
    latest_close_date: str | None = None
    six_month_high: float | None = None
    six_month_low: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    ma120: float | None = None
    ma150: float | None = None
    ma200: float | None = None
    rsi14: float | None = None
    ma20_distance_pct: float | None = None
    ma60_distance_pct: float | None = None
    ma120_distance_pct: float | None = None
    ma150_distance_pct: float | None = None
    ma200_distance_pct: float | None = None
    ma20_slope_pct: float | None = None
    ma60_slope_pct: float | None = None
    ma150_slope_pct: float | None = None
    ma200_slope_pct: float | None = None
    latest_volume: float | None = None
    avg_volume_20: float | None = None
    volume_ratio: float | None = None
    twenty_day_breakout_pct: float | None = None
    sixty_day_breakout_pct: float | None = None
    bollinger_upper: float | None = None
    bollinger_middle: float | None = None
    bollinger_lower: float | None = None
    bollinger_bandwidth_pct: float | None = None
    bollinger_percent_b: float | None = None
    volume_zone_lower: float | None = None
    volume_zone_upper: float | None = None
    volume_zone_strength: float | None = None
    volume_zone_contains_latest: bool = False
    previous_swing_high: float | None = None
    previous_swing_high_distance_pct: float | None = None
    structure_zone_lower: float | None = None
    structure_zone_upper: float | None = None
    structure_zone_strength: float | None = None
    support_retest_lower: float | None = None
    support_retest_upper: float | None = None
    nearest_resistance: float | None = None
    major_resistance: float | None = None
    rejection_from_structure_zone: bool = False
    support_retest_active: bool = False
    ohlcv_coverage_pct: float | None = None
    source: str | None = None
    stale: bool = False


@dataclass(frozen=True)
class StockProfile:
    ticker: str
    name: str
    industry: str
    role: StockRole
    thesis: str
    risks: tuple[str, ...]
    fundamentals: Fundamentals
    recent_issues: tuple[str, ...] = ()
    country: str = "US"
    currency: str = "USD"
    dart_stock_code: str | None = None


@dataclass(frozen=True)
class IndustryProfile:
    name: str
    description: str
    news_terms: tuple[str, ...]
    macro_terms: tuple[str, ...]
    tailwinds: tuple[str, ...]
    risks: tuple[str, ...]


@dataclass(frozen=True)
class IndustryMarketProxy:
    ticker: str
    name: str
    role: MarketProxyRole
    weight: float = 1.0


@dataclass(frozen=True)
class BeneficiaryIndustryProfile:
    name: str
    description: str
    source_industry: str
    mechanism: str
    time_horizon: str
    keywords: tuple[str, ...]
    risks: tuple[str, ...]
    connection_strength: float
    market_proxies: tuple[IndustryMarketProxy, ...] = ()


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    published: str | None = None
    url: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class MacroIndicator:
    name: str
    value: float | None
    unit: str
    latest_date: str | None
    source: str
    note: str


@dataclass(frozen=True)
class MacroSnapshot:
    indicators: tuple[MacroIndicator, ...] = ()
    growth_score: float = 50
    defensive_score: float = 50
    infrastructure_score: float = 50
    korea_fx_score: float = 50
    summary: str = "거시지표가 아직 연결되지 않았습니다."
    investment_guidance: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DataQuality:
    live_news: bool = False
    live_market_data: bool = False
    live_fundamentals: bool = False
    live_macro: bool = False
    live_korea_fundamentals: bool = False
    universe_mode: str = "screened"
    universe_candidate_count: int = 0
    universe_quote_ready_count: int = 0
    universe_financial_target_count: int = 0
    universe_financial_ready_count: int = 0
    universe_final_count: int = 0
    universe_us_count: int = 0
    universe_kr_count: int = 0
    configured_sources: tuple[str, ...] = ()
    missing_sources: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndustryScore:
    industry: IndustryProfile
    score: float
    news_score: float
    macro_score: float
    market_score: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class BeneficiaryIndustryScore:
    profile: BeneficiaryIndustryProfile
    score: float
    source_industry_score: float
    connection_score: float
    macro_score: float
    news_score: float
    market_score: float
    evidence: tuple[str, ...]
    display_summary: str
    proxy_momentum_score: float = 50
    proxy_coverage_pct: float = 0
    news_recent_score: float = 35
    news_baseline_score: float = 35
    news_acceleration_score: float = 50
    news_coverage_label: str = "30일 데이터 부족"
    news_top_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValuationRange:
    profit_metric: str
    profit_value: float | None
    multiple_low: float | None
    multiple_high: float | None
    market_cap_low: float | None
    market_cap_high: float | None
    upside_low_pct: float | None
    upside_high_pct: float | None
    note: str


@dataclass(frozen=True)
class StockScore:
    stock: StockProfile
    score: float
    industry_score: float
    quality_score: float
    growth_quality_score: float
    valuation_score: float
    momentum_score: float
    role_score: float
    reasons: tuple[str, ...]
    cautions: tuple[str, ...]
    decision_grade: str
    risk_level: str
    risk_gate: str
    risk_gate_reasons: tuple[str, ...]
    valuation_label: str
    analysis_style: str
    weight_profile: str
    portfolio_signal: str
    target_weight_pct: float
    max_weight_pct: float
    sell_signals: tuple[str, ...]
    valuation_note: str
    valuation_range: ValuationRange
    analysis_checks: tuple[str, ...]
    second_order_checks: tuple[str, ...]


@dataclass(frozen=True)
class TradeTimingSignal:
    horizon: str
    action: str
    label: str
    score: float
    confidence: float
    setup: str
    reasons: tuple[str, ...]
    cautions: tuple[str, ...]
    reference_price: float | None = None
    ma150: float | None = None
    ma200: float | None = None
    bollinger_upper: float | None = None
    bollinger_middle: float | None = None
    bollinger_lower: float | None = None
    volume_zone_lower: float | None = None
    volume_zone_upper: float | None = None
    volume_zone_strength: float | None = None
    target_price: float | None = None
    target_type: str | None = None
    partial_take_profit_pct: float | None = None
    final_take_profit_pct: float | None = None
    entry_zone_lower: float | None = None
    entry_zone_upper: float | None = None
    target1_price: float | None = None
    target1_type: str | None = None
    target2_price: float | None = None
    target2_type: str | None = None
    position_plan: str = ""
    structure_setup: str = ""
    remaining_exit_rule: str = ""
    invalidation_rule: str = ""


@dataclass(frozen=True)
class EarlyGrowthScore:
    stock_score: StockScore
    score: float
    size_score: float
    growth_score: float
    pullback_score: float
    quality_anchor_score: float
    valuation_anchor_score: float
    entry_label: str
    reasons: tuple[str, ...]
    cautions: tuple[str, ...]


@dataclass(frozen=True)
class ShortTermScore:
    stock_score: StockScore
    score: float
    news_score: float
    market_score: float
    chart_score: float
    volume_score: float
    company_score: float
    confidence_score: float
    confidence_label: str
    signal_label: str
    setup_label: str
    time_horizon: str
    reasons: tuple[str, ...]
    cautions: tuple[str, ...]
    trade_signal: TradeTimingSignal | None = None
    theme_news_score: float = 0
    current_industry_score: float = 0
    beneficiary_theme_score: float = 0
    theme_label: str = ""
    matched_beneficiary_themes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediumTermScore:
    stock_score: StockScore
    score: float
    company_score: float
    market_score: float
    chart_score: float
    news_score: float
    confidence_score: float
    confidence_label: str
    signal_label: str
    time_horizon: str
    reasons: tuple[str, ...]
    cautions: tuple[str, ...]
    trade_signal: TradeTimingSignal | None = None


@dataclass(frozen=True)
class LongTermScore:
    stock_score: StockScore
    score: float
    company_score: float
    market_score: float
    chart_score: float
    news_score: float
    confidence_score: float
    confidence_label: str
    signal_label: str
    time_horizon: str
    reasons: tuple[str, ...]
    cautions: tuple[str, ...]


@dataclass(frozen=True)
class LegendStrategyScore:
    stock_score: StockScore
    lynch_score: float
    oneil_score: float
    greenblatt_score: float
    fisher_score: float
    composite_score: float
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RecommendationReport:
    created_at: datetime
    macro_context: str
    industry_scores: tuple[IndustryScore, ...]
    stock_scores: tuple[StockScore, ...]
    news_items: tuple[NewsItem, ...]
    early_growth_scores: tuple[EarlyGrowthScore, ...] = ()
    short_term_scores: tuple[ShortTermScore, ...] = ()
    medium_term_scores: tuple[MediumTermScore, ...] = ()
    long_term_scores: tuple[LongTermScore, ...] = ()
    legend_strategy_scores: tuple[LegendStrategyScore, ...] = ()
    beneficiary_industry_scores: tuple[BeneficiaryIndustryScore, ...] = ()
    macro_snapshot: MacroSnapshot | None = None
    data_quality: DataQuality = field(default_factory=DataQuality)
    momentums: dict[str, Momentum] = field(default_factory=dict)
    source_events: tuple[dict, ...] = ()
