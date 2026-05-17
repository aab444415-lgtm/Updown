from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


StockRole = Literal["core", "adjacent"]


@dataclass(frozen=True)
class Fundamentals:
    revenue_growth_pct: float | None = None
    operating_margin_pct: float | None = None
    roe_pct: float | None = None
    debt_to_equity_pct: float | None = None
    pe: float | None = None
    forward_pe: float | None = None
    market_cap_usd: float | None = None
    market_cap_currency: str = "USD"


@dataclass(frozen=True)
class Momentum:
    one_month_pct: float | None = None
    three_month_pct: float | None = None
    six_month_pct: float | None = None


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
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DataQuality:
    live_news: bool = False
    live_market_data: bool = False
    live_fundamentals: bool = False
    live_macro: bool = False
    live_korea_fundamentals: bool = False
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
class StockScore:
    stock: StockProfile
    score: float
    industry_score: float
    quality_score: float
    valuation_score: float
    momentum_score: float
    role_score: float
    reasons: tuple[str, ...]
    cautions: tuple[str, ...]
    decision_grade: str
    risk_level: str
    valuation_label: str
    analysis_style: str
    valuation_note: str
    analysis_checks: tuple[str, ...]
    second_order_checks: tuple[str, ...]


@dataclass(frozen=True)
class RecommendationReport:
    created_at: datetime
    macro_context: str
    industry_scores: tuple[IndustryScore, ...]
    stock_scores: tuple[StockScore, ...]
    news_items: tuple[NewsItem, ...]
    macro_snapshot: MacroSnapshot | None = None
    data_quality: DataQuality = field(default_factory=DataQuality)
