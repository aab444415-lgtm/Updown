from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

from .data_sources import average_industry_momentum, momentum_to_score
from .macro_data import industry_macro_data_score
from .models import (
    DataQuality,
    Fundamentals,
    IndustryProfile,
    IndustryScore,
    MacroSnapshot,
    Momentum,
    NewsItem,
    RecommendationReport,
    StockProfile,
    StockScore,
)


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9가-힣][A-Za-z0-9가-힣+.-]*")


def build_report(
    macro_context: str,
    industries: Iterable[IndustryProfile],
    stocks: Iterable[StockProfile],
    news_items: Iterable[NewsItem],
    momentums: dict[str, Momentum] | None = None,
    macro_snapshot: MacroSnapshot | None = None,
    data_quality: DataQuality | None = None,
) -> RecommendationReport:
    from datetime import datetime

    industries_tuple = tuple(industries)
    stocks_tuple = tuple(stocks)
    news_tuple = tuple(news_items)
    momentums = momentums or {}

    industry_scores = score_industries(
        macro_context=macro_context,
        industries=industries_tuple,
        stocks=stocks_tuple,
        news_items=news_tuple,
        momentums=momentums,
        macro_snapshot=macro_snapshot,
    )
    stock_scores = score_stocks(stocks_tuple, industry_scores, momentums)
    return RecommendationReport(
        created_at=datetime.now(),
        macro_context=macro_context,
        industry_scores=tuple(sorted(industry_scores, key=lambda item: item.score, reverse=True)),
        stock_scores=tuple(sorted(stock_scores, key=lambda item: item.score, reverse=True)),
        news_items=news_tuple,
        macro_snapshot=macro_snapshot,
        data_quality=data_quality or DataQuality(),
    )


def score_industries(
    macro_context: str,
    industries: Iterable[IndustryProfile],
    stocks: Iterable[StockProfile],
    news_items: Iterable[NewsItem],
    momentums: dict[str, Momentum],
    macro_snapshot: MacroSnapshot | None = None,
) -> tuple[IndustryScore, ...]:
    news_text = " ".join(
        " ".join(part for part in (item.title, item.summary or "") if part) for item in news_items
    )
    macro_counter = _counter(macro_context)
    news_counter = _counter(news_text)
    stocks_tuple = tuple(stocks)

    scores: list[IndustryScore] = []
    for industry in industries:
        text_macro_score = _term_score(macro_counter, industry.macro_terms, baseline=40, scale=13)
        data_macro_score = industry_macro_data_score(industry.name, macro_snapshot)
        macro_score = text_macro_score * 0.60 + data_macro_score * 0.40
        news_score = _term_score(news_counter, industry.news_terms, baseline=35, scale=9)
        market_score = average_industry_momentum(industry.name, stocks_tuple, momentums)
        if market_score is None:
            market_score = 50

        total = macro_score * 0.35 + news_score * 0.30 + market_score * 0.35
        evidence = _industry_evidence(
            industry,
            macro_score,
            news_score,
            market_score,
            data_macro_score,
            macro_snapshot,
        )
        scores.append(
            IndustryScore(
                industry=industry,
                score=round(total, 1),
                news_score=round(news_score, 1),
                macro_score=round(macro_score, 1),
                market_score=round(market_score, 1),
                evidence=evidence,
            )
        )
    return tuple(scores)


def score_stocks(
    stocks: Iterable[StockProfile],
    industry_scores: Iterable[IndustryScore],
    momentums: dict[str, Momentum],
) -> tuple[StockScore, ...]:
    industry_score_by_name = {item.industry.name: item for item in industry_scores}
    results: list[StockScore] = []
    for stock in stocks:
        industry_score = industry_score_by_name[stock.industry]
        quality = quality_score(stock.fundamentals)
        valuation = valuation_score(stock.fundamentals)
        momentum = momentum_to_score(momentums.get(stock.ticker.upper(), Momentum()))
        if momentum is None:
            momentum = 50
        role = 65 if stock.role == "core" else 55
        total = (
            industry_score.score * 0.30
            + quality * 0.28
            + valuation * 0.16
            + momentum * 0.16
            + role * 0.10
        )
        reasons = _stock_reasons(stock, quality, valuation, momentum, industry_score)
        cautions = tuple(dict.fromkeys((*stock.risks, *industry_score.industry.risks[:1])))
        risk_level = risk_level_for_stock(stock, quality, valuation, momentum)
        decision_grade = decision_grade_for_stock(total, quality, valuation, momentum, risk_level)
        valuation_label = valuation_label_for_score(valuation)
        results.append(
            StockScore(
                stock=stock,
                score=round(total, 1),
                industry_score=industry_score.score,
                quality_score=round(quality, 1),
                valuation_score=round(valuation, 1),
                momentum_score=round(momentum, 1),
                role_score=role,
                reasons=reasons,
                cautions=cautions,
                decision_grade=decision_grade,
                risk_level=risk_level,
                valuation_label=valuation_label,
            )
        )
    return tuple(results)


def quality_score(fundamentals: Fundamentals) -> float:
    revenue_growth = _scale(fundamentals.revenue_growth_pct, low=-10, high=35)
    margin = _scale(fundamentals.operating_margin_pct, low=-5, high=45)
    roe = _scale(fundamentals.roe_pct, low=-10, high=45)
    leverage = _inverse_scale(fundamentals.debt_to_equity_pct, low=20, high=220)
    return revenue_growth * 0.30 + margin * 0.30 + roe * 0.25 + leverage * 0.15


def valuation_score(fundamentals: Fundamentals) -> float:
    pe = fundamentals.forward_pe if fundamentals.forward_pe is not None else fundamentals.pe
    if pe is None or pe <= 0:
        return 45
    if pe <= 12:
        return 86
    if pe <= 20:
        return 76
    if pe <= 35:
        return 63
    if pe <= 55:
        return 48
    if pe <= 85:
        return 34
    return 22


def decision_grade_for_stock(
    total_score: float, quality: float, valuation: float, momentum: float, risk_level: str
) -> str:
    adjusted = total_score
    if risk_level == "높음":
        adjusted -= 4
    if valuation < 40 and quality < 55:
        adjusted -= 3
    if momentum < 35:
        adjusted -= 3
    if adjusted >= 75:
        return "매수 후보"
    if adjusted >= 67:
        return "관심"
    if adjusted >= 57:
        return "관망"
    return "제외"


def risk_level_for_stock(
    stock: StockProfile, quality: float, valuation: float, momentum: float
) -> str:
    risk_points = 0
    if valuation <= 40:
        risk_points += 2
    elif valuation <= 50:
        risk_points += 1
    if momentum < 35:
        risk_points += 1
    if quality < 40:
        risk_points += 1
    debt_to_equity = stock.fundamentals.debt_to_equity_pct
    if debt_to_equity is not None and debt_to_equity > 220:
        risk_points += 2
    operating_margin = stock.fundamentals.operating_margin_pct
    if operating_margin is not None and operating_margin < 0:
        risk_points += 1
    if risk_points >= 3:
        return "높음"
    if risk_points >= 1:
        return "중간"
    return "낮음"


def valuation_label_for_score(score: float) -> str:
    if score >= 76:
        return "저평가/합리"
    if score >= 63:
        return "적정"
    if score >= 48:
        return "약간 고평가"
    return "고평가"


def _industry_evidence(
    industry: IndustryProfile,
    macro_score: float,
    news_score: float,
    market_score: float,
    data_macro_score: float,
    macro_snapshot: MacroSnapshot | None,
) -> tuple[str, ...]:
    evidence = [
        f"거시 테마 적합도 {macro_score:.1f}/100",
        f"실제 거시지표 반영 점수 {data_macro_score:.1f}/100",
        f"뉴스 언급 강도 {news_score:.1f}/100",
        f"산업 내 가격 모멘텀 {market_score:.1f}/100",
    ]
    if macro_snapshot is not None:
        evidence.append(macro_snapshot.summary)
    evidence.extend(industry.tailwinds[:2])
    return tuple(evidence)


def _stock_reasons(
    stock: StockProfile,
    quality: float,
    valuation: float,
    momentum: float,
    industry_score: IndustryScore,
) -> tuple[str, ...]:
    role_text = "핵심 기업" if stock.role == "core" else "부가/연관 기업"
    reasons = [
        f"{industry_score.industry.name} 산업의 {role_text}",
        stock.thesis,
        f"기본적 분석 점수 {quality:.1f}/100, 밸류에이션 점수 {valuation:.1f}/100",
        f"가격 모멘텀 점수 {momentum:.1f}/100",
    ]
    reasons.extend(stock.recent_issues[:1])
    return tuple(reasons)


def _term_score(counter: Counter[str], terms: Iterable[str], baseline: float, scale: float) -> float:
    count = 0
    for term in terms:
        tokens = _tokens(term)
        if len(tokens) == 1:
            count += counter[tokens[0]]
            continue
        phrase = " ".join(tokens)
        count += counter[phrase] * 2
        count += min(counter[token] for token in tokens) if tokens else 0
    return _clamp(baseline + count * scale, 0, 100)


def _counter(text: str) -> Counter[str]:
    tokens = _tokens(text)
    counter = Counter(tokens)
    for size in (2, 3):
        for index in range(len(tokens) - size + 1):
            counter[" ".join(tokens[index : index + size])] += 1
    return counter


def _tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_PATTERN.finditer(text)]


def _scale(value: float | None, low: float, high: float) -> float:
    if value is None or not math.isfinite(value):
        return 50
    return _clamp(((value - low) / (high - low)) * 100, 0, 100)


def _inverse_scale(value: float | None, low: float, high: float) -> float:
    if value is None or not math.isfinite(value):
        return 50
    return 100 - _scale(value, low, high)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
