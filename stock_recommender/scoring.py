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
    ValuationRange,
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
        analysis_style = analysis_style_for_stock(stock)
        valuation_note = valuation_note_for_stock(stock, valuation, analysis_style)
        valuation_range = valuation_range_for_stock(stock, analysis_style)
        total = (
            industry_score.score * 0.30
            + quality * 0.28
            + valuation * 0.16
            + momentum * 0.16
            + role * 0.10
        )
        reasons = _stock_reasons(
            stock, quality, valuation, momentum, industry_score, analysis_style, valuation_note
        )
        analysis_checks = analysis_checks_for_stock(stock, valuation_note, valuation_range)
        second_order_checks = second_order_checks_for_stock(stock, industry_score, analysis_style)
        cautions = tuple(
            dict.fromkeys(
                (
                    *stock.risks,
                    *industry_score.industry.risks[:1],
                    *risk_cautions_for_stock(stock, analysis_style),
                )
            )
        )
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
                analysis_style=analysis_style,
                valuation_note=valuation_note,
                valuation_range=valuation_range,
                analysis_checks=analysis_checks,
                second_order_checks=second_order_checks,
            )
        )
    return tuple(results)


def quality_score(fundamentals: Fundamentals) -> float:
    revenue_growth = _scale(fundamentals.revenue_growth_pct, low=-10, high=35)
    margin = _scale(fundamentals.operating_margin_pct, low=-5, high=45)
    roe = _scale(fundamentals.roe_pct, low=-10, high=45)
    leverage = _inverse_scale(fundamentals.debt_to_equity_pct, low=20, high=220)
    fcf_margin = _ratio_pct(fundamentals.free_cash_flow, fundamentals.revenue)
    cash_flow = _scale(fcf_margin, low=-10, high=25)
    liquidity = _scale(fundamentals.current_ratio_pct, low=80, high=220)
    interest_safety = _scale(fundamentals.interest_coverage, low=1, high=10)
    return (
        revenue_growth * 0.26
        + margin * 0.25
        + roe * 0.22
        + leverage * 0.12
        + cash_flow * 0.08
        + liquidity * 0.04
        + interest_safety * 0.03
    )


def valuation_score(fundamentals: Fundamentals) -> float:
    pe = fundamentals.forward_pe if fundamentals.forward_pe is not None else fundamentals.pe
    if pe is None or pe <= 0:
        return 45
    if pe <= 12:
        base = 86
    elif pe <= 20:
        base = 76
    elif pe <= 35:
        base = 63
    elif pe <= 55:
        base = 48
    elif pe <= 85:
        base = 34
    else:
        base = 22

    growth = fundamentals.revenue_growth_pct
    margin = fundamentals.operating_margin_pct
    roe = fundamentals.roe_pct
    debt_to_equity = fundamentals.debt_to_equity_pct

    if growth is not None and math.isfinite(growth):
        if pe > 35 and growth >= 25 and _at_least(margin, 10):
            base += min(14, (growth - 20) * 0.35)
        elif pe > 35 and growth < 12:
            base -= 10

        if pe <= 14 and growth < 5:
            base -= 12
        elif pe <= 22 and growth >= 20:
            base += 6

    if pe > 25 and margin is not None and math.isfinite(margin) and margin < 8:
        base -= 8
    if margin is not None and math.isfinite(margin) and margin < 0:
        base -= 12
    if debt_to_equity is not None and math.isfinite(debt_to_equity) and debt_to_equity > 220:
        base -= 8
    if roe is not None and math.isfinite(roe) and roe > 25 and pe <= 30:
        base += 6
    if fundamentals.free_cash_flow is not None and fundamentals.free_cash_flow < 0 and pe > 35:
        base -= 6
    if fundamentals.interest_coverage is not None and fundamentals.interest_coverage < 3:
        base -= 5

    return _clamp(base, 0, 100)


def analysis_style_for_stock(stock: StockProfile) -> str:
    fundamentals = stock.fundamentals
    pe = fundamentals.forward_pe if fundamentals.forward_pe is not None else fundamentals.pe
    growth = fundamentals.revenue_growth_pct
    margin = fundamentals.operating_margin_pct

    if _is_cyclical_industry(stock.industry) and pe is not None and pe <= 14:
        if _at_least(growth, 20):
            return "사이클 회복 성장주"
        return "경기민감 저PER 관찰"
    if _at_least(growth, 25):
        return "성장주"
    if pe is not None and pe >= 45:
        return "고멀티플 검증"
    if pe is not None and pe <= 20 and quality_score(fundamentals) >= 60:
        return "가치/퀄리티"
    if margin is not None and margin < 0 and _at_least(growth, 15):
        return "턴어라운드 관찰"
    return "균형형"


def valuation_note_for_stock(stock: StockProfile, valuation: float, analysis_style: str) -> str:
    fundamentals = stock.fundamentals
    pe = fundamentals.forward_pe if fundamentals.forward_pe is not None else fundamentals.pe
    if pe is None or pe <= 0:
        return "이익 멀티플 데이터가 부족해 보수적으로 중립 이하로 봅니다."

    if analysis_style == "경기민감 저PER 관찰":
        return "낮은 PER은 매력보다 이익 정점 신호일 수 있어 업황 둔화 여부를 먼저 확인합니다."
    if analysis_style == "사이클 회복 성장주":
        return "낮은 멀티플과 강한 성장률이 함께 보이지만 사이클 회복 지속성을 확인해야 합니다."
    if pe >= 45 and valuation < 45:
        return "높은 멀티플은 미래 이익 개선이 계속될 때만 정당화됩니다."
    if pe <= 20 and valuation >= 70:
        return "낮은 멀티플이 긍정적이나 성장 둔화나 재무 부담이 숨어 있는지 확인합니다."
    if valuation >= 63:
        return "현재 멀티플은 성장성과 수익성 대비 무리하지 않은 구간으로 봅니다."
    return "멀티플 부담이 있어 실적 상향이나 산업 전망 개선의 근거가 필요합니다."


def valuation_range_for_stock(stock: StockProfile, analysis_style: str) -> ValuationRange:
    fundamentals = stock.fundamentals
    multiple = fundamentals.forward_pe if fundamentals.forward_pe is not None else fundamentals.pe
    profit_metric, profit_value = _profit_base_for_valuation(fundamentals, multiple)
    if profit_value is None or profit_value <= 0 or multiple is None or multiple <= 0:
        return ValuationRange(
            profit_metric=profit_metric,
            profit_value=profit_value,
            multiple_low=None,
            multiple_high=None,
            market_cap_low=None,
            market_cap_high=None,
            upside_low_pct=None,
            upside_high_pct=None,
            note="이익 규모 또는 멀티플 데이터가 부족해 적정 시가총액 범위를 계산하지 않았습니다.",
        )

    multiple_low, multiple_high = _multiple_range(multiple, analysis_style, stock.role)
    market_cap_low = profit_value * multiple_low
    market_cap_high = profit_value * multiple_high
    upside_low = _upside_pct(market_cap_low, fundamentals.market_cap_usd)
    upside_high = _upside_pct(market_cap_high, fundamentals.market_cap_usd)
    note = (
        f"{profit_metric}을 기준 이익으로 두고 {multiple_low:.1f}~{multiple_high:.1f}배 멀티플을 적용한 약식 범위입니다."
    )
    return ValuationRange(
        profit_metric=profit_metric,
        profit_value=profit_value,
        multiple_low=multiple_low,
        multiple_high=multiple_high,
        market_cap_low=market_cap_low,
        market_cap_high=market_cap_high,
        upside_low_pct=upside_low,
        upside_high_pct=upside_high,
        note=note,
    )


def analysis_checks_for_stock(
    stock: StockProfile, valuation_note: str, valuation_range: ValuationRange
) -> tuple[str, ...]:
    fundamentals = stock.fundamentals
    return (
        _growth_check(fundamentals),
        _profitability_check(fundamentals),
        _cash_flow_check(fundamentals),
        _stability_check(fundamentals),
        f"멀티플 해석: {valuation_note}",
        _valuation_range_check(valuation_range),
    )


def second_order_checks_for_stock(
    stock: StockProfile, industry_score: IndustryScore, analysis_style: str
) -> tuple[str, ...]:
    leadership_check = (
        f"{stock.name}의 선두 프리미엄이 경쟁사 대비 타당한지 확인"
        if stock.role == "core"
        else f"{stock.name}이 선두 기업과의 격차를 줄일 수 있는 구체적 이유 확인"
    )
    return (
        f"{stock.industry} 성장률이 몇 년 지속될지와 현재 산업 점수 {industry_score.score:.1f}점의 지속성 확인",
        "미래 이익 규모와 적용할 멀티플을 각각 범위로 잡아 상승 여력을 재계산",
        leadership_check,
        _style_specific_second_order_check(analysis_style),
    )


def risk_cautions_for_stock(stock: StockProfile, analysis_style: str) -> tuple[str, ...]:
    cautions: list[str] = []
    if analysis_style == "경기민감 저PER 관찰":
        cautions.append("낮은 PER이 이익 정점 구간에서 나타난 착시인지 확인 필요")
    if analysis_style in {"고멀티플 검증", "성장주"}:
        cautions.append("성장 기대가 이미 가격에 반영된 정도 확인 필요")
    if stock.fundamentals.debt_to_equity_pct is not None and stock.fundamentals.debt_to_equity_pct > 200:
        cautions.append("부채비율이 높아 이자보상비율과 현금흐름 추가 확인 필요")
    if stock.fundamentals.current_ratio_pct is not None and stock.fundamentals.current_ratio_pct < 100:
        cautions.append("유동비율이 낮아 단기 지급능력 확인 필요")
    if stock.fundamentals.free_cash_flow is not None and stock.fundamentals.free_cash_flow < 0:
        cautions.append("잉여현금흐름이 음수라 투자/운전자본 부담 확인 필요")
    return tuple(cautions)


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
    current_ratio = stock.fundamentals.current_ratio_pct
    if current_ratio is not None and current_ratio < 100:
        risk_points += 1
    interest_coverage = stock.fundamentals.interest_coverage
    if interest_coverage is not None and interest_coverage < 3:
        risk_points += 1
    free_cash_flow = stock.fundamentals.free_cash_flow
    if free_cash_flow is not None and free_cash_flow < 0:
        risk_points += 1
    if _is_cyclical_low_pe(stock):
        risk_points += 1
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
    analysis_style: str,
    valuation_note: str,
) -> tuple[str, ...]:
    role_text = "핵심 기업" if stock.role == "core" else "부가/연관 기업"
    reasons = [
        f"{industry_score.industry.name} 산업의 {role_text}",
        stock.thesis,
        f"분석 스타일: {analysis_style}",
        f"기본적 분석 점수 {quality:.1f}/100, 밸류에이션 점수 {valuation:.1f}/100",
        valuation_note,
        f"가격 모멘텀 점수 {momentum:.1f}/100",
    ]
    reasons.extend(stock.recent_issues[:1])
    return tuple(reasons)


def _growth_check(fundamentals: Fundamentals) -> str:
    growth = fundamentals.revenue_growth_pct
    if growth is None or not math.isfinite(growth):
        return "매출 성장: 데이터 부족으로 산업 성장성과 공시 확인 필요"
    if growth >= 25:
        tone = "강한 확장"
    elif growth >= 8:
        tone = "완만한 성장"
    elif growth >= 0:
        tone = "성장 둔화"
    else:
        tone = "매출 감소"
    return f"매출 성장: {growth:.1f}%로 {tone} 흐름"


def _profitability_check(fundamentals: Fundamentals) -> str:
    margin = fundamentals.operating_margin_pct
    roe = fundamentals.roe_pct
    margin_text = "N/A" if margin is None else f"{margin:.1f}%"
    roe_text = "N/A" if roe is None else f"{roe:.1f}%"
    ebitda_text = "" if fundamentals.ebitda is None else f", EBITDA {_compact_amount(fundamentals.ebitda)}"
    if _at_least(margin, 20) and _at_least(roe, 15):
        tone = "수익성과 자본효율이 모두 양호"
    elif margin is not None and margin < 0:
        tone = "영업 적자라 이익 개선 확인 필요"
    else:
        tone = "수익성의 지속성과 개선 속도 확인 필요"
    return f"이익의 질: 영업이익률 {margin_text}, ROE {roe_text}{ebitda_text} - {tone}"


def _cash_flow_check(fundamentals: Fundamentals) -> str:
    operating_cash_flow = fundamentals.operating_cash_flow
    free_cash_flow = fundamentals.free_cash_flow
    if operating_cash_flow is None and free_cash_flow is None:
        return "현금흐름: 영업현금흐름/FCF 데이터 부족, 현금창출력 추가 확인 필요"
    ocf_text = "N/A" if operating_cash_flow is None else _compact_amount(operating_cash_flow)
    fcf_text = "N/A" if free_cash_flow is None else _compact_amount(free_cash_flow)
    if free_cash_flow is not None and free_cash_flow < 0:
        tone = "투자 부담 또는 현금 유출 확인 필요"
    elif operating_cash_flow is not None and operating_cash_flow > 0:
        tone = "영업 현금창출은 양호한 편"
    else:
        tone = "현금창출력 개선 여부 확인 필요"
    return f"현금흐름: 영업현금흐름 {ocf_text}, FCF {fcf_text} - {tone}"


def _stability_check(fundamentals: Fundamentals) -> str:
    debt_to_equity = fundamentals.debt_to_equity_pct
    current_ratio = fundamentals.current_ratio_pct
    interest_coverage = fundamentals.interest_coverage
    if debt_to_equity is None or not math.isfinite(debt_to_equity):
        return "안정성: 부채비율 데이터 부족, 유동비율과 이자보상비율 추가 확인 필요"
    if debt_to_equity > 220:
        tone = "재무 부담이 큰 구간"
    elif debt_to_equity > 150:
        tone = "차입 부담 점검 필요"
    else:
        tone = "부채비율은 과도하지 않은 편"
    current_text = "N/A" if current_ratio is None else f"{current_ratio:.1f}%"
    interest_text = "N/A" if interest_coverage is None else f"{interest_coverage:.1f}배"
    return f"안정성: 부채비율 {debt_to_equity:.1f}%, 유동비율 {current_text}, 이자보상 {interest_text} - {tone}"


def _style_specific_second_order_check(analysis_style: str) -> str:
    if analysis_style == "경기민감 저PER 관찰":
        return "낮은 PER이 저평가가 아니라 이익 정점 신호일 가능성을 반대로 검토"
    if analysis_style == "사이클 회복 성장주":
        return "업황 회복이 일회성인지 구조적 이익 증가인지 분리해서 검토"
    if analysis_style in {"성장주", "고멀티플 검증"}:
        return "시장 기대보다 더 큰 이익 증가가 가능한지, 아니면 이미 선반영됐는지 검토"
    if analysis_style == "가치/퀄리티":
        return "낮은 멀티플의 이유가 일시적 소외인지 구조적 성장 둔화인지 검토"
    return "산업 전망, 이익 추정, 멀티플 가정 중 어느 하나라도 틀렸을 때의 하방 검토"


def _profit_base_for_valuation(
    fundamentals: Fundamentals, multiple: float | None
) -> tuple[str, float | None]:
    if fundamentals.net_income is not None and fundamentals.net_income > 0:
        return "순이익", fundamentals.net_income
    if fundamentals.operating_income is not None and fundamentals.operating_income > 0:
        return "영업이익 보정", fundamentals.operating_income * 0.75
    if fundamentals.ebitda is not None and fundamentals.ebitda > 0:
        return "EBITDA 보정", fundamentals.ebitda * 0.65
    if (
        fundamentals.market_cap_usd is not None
        and fundamentals.market_cap_usd > 0
        and multiple is not None
        and multiple > 0
    ):
        return "PER 역산 이익", fundamentals.market_cap_usd / multiple
    return "이익 데이터 부족", None


def _multiple_range(multiple: float, analysis_style: str, role: str) -> tuple[float, float]:
    style_bands = {
        "성장주": (0.80, 1.15),
        "고멀티플 검증": (0.65, 0.95),
        "가치/퀄리티": (0.85, 1.10),
        "경기민감 저PER 관찰": (0.55, 0.85),
        "사이클 회복 성장주": (0.75, 1.05),
        "턴어라운드 관찰": (0.60, 1.00),
    }
    low_factor, high_factor = style_bands.get(analysis_style, (0.80, 1.05))
    if role == "core":
        high_factor += 0.05
    else:
        low_factor -= 0.05
        high_factor -= 0.05
    return max(multiple * low_factor, 1.0), max(multiple * high_factor, 1.0)


def _upside_pct(target_value: float | None, current_value: float | None) -> float | None:
    if target_value is None or current_value is None or current_value <= 0:
        return None
    return ((target_value / current_value) - 1) * 100


def _valuation_range_check(valuation_range: ValuationRange) -> str:
    if valuation_range.market_cap_low is None or valuation_range.market_cap_high is None:
        return f"밸류에이션 범위: {valuation_range.note}"
    upside = _range_text(valuation_range.upside_low_pct, valuation_range.upside_high_pct, suffix="%")
    return (
        "밸류에이션 범위: "
        f"{valuation_range.profit_metric} x {valuation_range.multiple_low:.1f}~{valuation_range.multiple_high:.1f}배, "
        f"현재 시총 대비 여력 {upside}"
    )


def _is_cyclical_low_pe(stock: StockProfile) -> bool:
    pe = stock.fundamentals.forward_pe if stock.fundamentals.forward_pe is not None else stock.fundamentals.pe
    return pe is not None and pe <= 14 and _is_cyclical_industry(stock.industry)


def _is_cyclical_industry(industry: str) -> bool:
    cyclical_terms = ("반도체", "전력 인프라", "에너지 장비", "우주항공")
    return any(term in industry for term in cyclical_terms)


def _at_least(value: float | None, threshold: float) -> bool:
    return value is not None and math.isfinite(value) and value >= threshold


def _ratio_pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return (numerator / denominator) * 100


def _compact_amount(value: float) -> str:
    abs_value = abs(value)
    sign = "-" if value < 0 else ""
    if abs_value >= 1_000_000_000_000:
        return f"{sign}{abs_value / 1_000_000_000_000:.1f}조"
    if abs_value >= 100_000_000:
        return f"{sign}{abs_value / 100_000_000:.0f}억"
    if abs_value >= 1_000_000:
        return f"{sign}{abs_value / 1_000_000:.0f}백만"
    return f"{value:,.0f}"


def _range_text(low: float | None, high: float | None, suffix: str = "") -> str:
    if low is None or high is None:
        return "N/A"
    return f"{low:.1f}{suffix}~{high:.1f}{suffix}"


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
