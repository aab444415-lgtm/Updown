from __future__ import annotations

from .models import (
    BeneficiaryIndustryScore,
    EarlyGrowthScore,
    Fundamentals,
    LegendStrategyScore,
    LongTermScore,
    MediumTermScore,
    RecommendationReport,
    ShortTermScore,
    StockScore,
)


def render_markdown(
    report: RecommendationReport, top_industries: int = 3, top_stocks: int = 5
) -> str:
    lines: list[str] = []
    lines.append("# 주식 추천 리서치 리포트")
    lines.append("")
    lines.append(f"- 생성 시각: {report.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("- 용도: 투자 후보 발굴용 리서치 보조 자료")
    lines.append("- 주의: 매수/매도 지시가 아니며, 실제 투자 전 최신 공시와 가격을 직접 확인해야 합니다.")
    lines.append("")
    lines.append("## 거시경제/뉴스 맥락")
    lines.append("")
    lines.append(report.macro_context)
    lines.append("")

    lines.append("## 데이터 품질")
    lines.append("")
    lines.append(f"- 실시간 뉴스 사용: {'예' if report.data_quality.live_news else '아니오'}")
    lines.append(f"- 실시간 시장 데이터 사용: {'예' if report.data_quality.live_market_data else '아니오'}")
    lines.append(f"- 공식 재무 데이터 사용: {'예' if report.data_quality.live_fundamentals else '아니오'}")
    lines.append(f"- 한국 OpenDART 재무 사용: {'예' if report.data_quality.live_korea_fundamentals else '아니오'}")
    lines.append(f"- 거시경제 지표 사용: {'예' if report.data_quality.live_macro else '아니오'}")
    if report.data_quality.configured_sources:
        lines.append("- 설정된 데이터 소스: " + ", ".join(report.data_quality.configured_sources))
    if report.data_quality.missing_sources:
        lines.append("- 아직 키가 없는 선택 소스: " + ", ".join(report.data_quality.missing_sources))
    for warning in report.data_quality.warnings:
        lines.append(f"- {warning}")
    lines.append("")

    if report.macro_snapshot is not None:
        lines.append("## 거시경제 지표")
        lines.append("")
        lines.append(report.macro_snapshot.summary)
        lines.append("")
        if report.macro_snapshot.investment_guidance:
            lines.append("### 투자 방향")
            lines.append("")
            for guidance in report.macro_snapshot.investment_guidance:
                lines.append(f"- {guidance}")
            lines.append("")
        for indicator in report.macro_snapshot.indicators:
            value = "N/A" if indicator.value is None else f"{indicator.value:,.2f}{indicator.unit}"
            date = indicator.latest_date or "날짜 없음"
            lines.append(f"- {indicator.name}: {value} ({indicator.source}, {date}) - {indicator.note}")
        lines.append("")

    lines.append("## 현재 활발한 산업")
    lines.append("")
    for rank, item in enumerate(report.industry_scores[:top_industries], start=1):
        lines.append(f"### {rank}. {item.industry.name} - {item.score:.1f}점")
        lines.append("")
        lines.append(item.industry.description)
        lines.append("")
        lines.append(
            f"- 세부 점수: 거시 {item.macro_score:.1f}, 뉴스 {item.news_score:.1f}, 시장 {item.market_score:.1f}"
        )
        for evidence in item.evidence:
            lines.append(f"- {evidence}")
        lines.append("- 주요 리스크: " + "; ".join(item.industry.risks[:2]))
        lines.append("")

    if report.beneficiary_industry_scores:
        lines.append("## 미래 수혜 산업")
        lines.append("")
        lines.append("현재 활발한 산업이 만든 수요 병목과 투자 흐름이 후행 산업으로 번질 가능성을 정리합니다.")
        lines.append("")
        for rank, item in enumerate(report.beneficiary_industry_scores[:top_industries], start=1):
            lines.extend(_render_beneficiary_industry(rank, item))
            lines.append("")

    lines.append("## 추천 종목 후보")
    lines.append("")
    for rank, item in enumerate(report.stock_scores[:top_stocks], start=1):
        lines.extend(_render_stock(rank, item))
        lines.append("")

    lines.append("## 투자 전설 전략 후보")
    lines.append("")
    lines.append("린치, 오닐, 그린블라트, 피셔 기준에 맞춰 실제 출처가 있는 정량 지표만 결합해 후보를 따로 정렬합니다.")
    lines.append("")
    for rank, item in enumerate(report.legend_strategy_scores[:top_stocks], start=1):
        lines.extend(_render_legend_strategy_stock(rank, item))
        lines.append("")

    lines.append("## 단기 매매 후보")
    lines.append("")
    lines.append("차트 구조, 거래량, 시장 모멘텀, 뉴스/이슈, 기업 데이터를 당일~2주 관점으로 따로 점수화합니다.")
    lines.append("")
    for rank, item in enumerate(report.short_term_scores[:top_stocks], start=1):
        lines.extend(_render_short_term_stock(rank, item))
        lines.append("")

    lines.append("## 중기 매매 후보")
    lines.append("")
    lines.append("기업 데이터, 3개월 시장 흐름, 차트 위치, 산업 이슈로 2주~3개월 관점의 후보를 따로 점수화합니다.")
    lines.append("")
    for rank, item in enumerate(report.medium_term_scores[:top_stocks], start=1):
        lines.extend(_render_medium_term_stock(rank, item))
        lines.append("")

    lines.append("## 장기 투자 후보")
    lines.append("")
    lines.append("기업 가치, 산업/거시 환경, 장기 차트, 구조적 이슈로 3개월~1년 이상 관점의 후보를 따로 점수화합니다.")
    lines.append("")
    for rank, item in enumerate(report.long_term_scores[:top_stocks], start=1):
        lines.extend(_render_long_term_stock(rank, item))
        lines.append("")

    lines.append("## 저점 성장주 후보")
    lines.append("")
    lines.append("소형/중소형 규모, 매출 성장, 재무 버팀목, 가격 조정 후 반등 가능성을 따로 점수화한 후보입니다.")
    lines.append("")
    for rank, item in enumerate(report.early_growth_scores[:top_stocks], start=1):
        lines.extend(_render_early_growth_stock(rank, item))
        lines.append("")

    if report.news_items:
        lines.append("## 참고 뉴스")
        lines.append("")
        for item in report.news_items[:8]:
            url = f" ({item.url})" if item.url else ""
            lines.append(f"- {item.title} - {item.source}{url}")
        lines.append("")

    lines.append("## 점수 해석")
    lines.append("")
    lines.append("- 산업 점수: 거시 테마, 뉴스 언급 강도, 산업 내 가격 모멘텀을 합산합니다.")
    lines.append(
        "- 종목 점수: 산업 점수, 기본적 분석, 성장성까지 반영한 밸류에이션, 가격 모멘텀, 핵심/부가 기업 역할을 합산합니다."
    )
    lines.append("- 단기 점수: 차트 45%, 거래량 20%, 시장/섹터 모멘텀 20%, 뉴스 10%, 기업 데이터 5%를 반영합니다.")
    lines.append("- 중기 점수: 기업 데이터 30%, 차트 30%, 시장 데이터 25%, 뉴스 15%를 반영합니다.")
    lines.append("- 장기 점수: 기업 데이터 50%, 시장/산업/거시 25%, 구조적 이슈 15%, 장기 차트 10%를 반영합니다.")
    lines.append("- 신뢰도 점수는 가격, 거래량, 재무, 뉴스 데이터의 커버리지를 반영하며 데이터가 부족하면 상위 신호를 제한합니다.")
    lines.append("- 저점 성장주 점수: 작은 시가총액, 매출 성장, 재무 버팀목, 고점 대비 조정과 단기 반등 여부를 따로 봅니다.")
    lines.append("- 투자 전설 전략 점수: 린치 25%, 오닐 35%, 그린블라트 25%, 피셔 15% 기본 가중치를 사용합니다.")
    lines.append("- 낮은 PER은 단독 매수 근거가 아니며, 이익 정점과 성장 둔화 가능성을 함께 봅니다.")
    lines.append("- 점수는 정답이 아니라 후보 압축용 랭킹입니다.")
    return "\n".join(lines)


def _render_beneficiary_industry(rank: int, item: BeneficiaryIndustryScore) -> list[str]:
    profile = item.profile
    lines = [f"### {rank}. {profile.name} - {item.score:.1f}점", ""]
    lines.append(profile.description)
    lines.append("")
    lines.append(f"- 원인 산업: {profile.source_industry}")
    lines.append(f"- 예상 시차: {profile.time_horizon}")
    lines.append(f"- 수혜 논리: {profile.mechanism}")
    if profile.market_proxies:
        proxy_text = ", ".join(
            f"{proxy.ticker}({proxy.role})" for proxy in profile.market_proxies
        )
        lines.append(f"- 대표 ETF/종목: {proxy_text}")
    lines.append(
        f"- 세부 점수: 원인 산업 {item.source_industry_score:.1f}, 연결도 {item.connection_score:.1f}, "
        f"거시 {item.macro_score:.1f}, 뉴스 {item.news_score:.1f}, 시장 {item.market_score:.1f}"
    )
    lines.append(
        f"- Proxy 모멘텀: {item.proxy_momentum_score:.1f}/100, 커버리지 {item.proxy_coverage_pct:.1f}%"
    )
    lines.append(
        f"- 뉴스 흐름: 7일 {item.news_recent_score:.1f}, 30일 {item.news_baseline_score:.1f}, "
        f"증가율 {item.news_acceleration_score:.1f} ({item.news_coverage_label})"
    )
    if item.news_top_sources:
        lines.append("- 주요 출처: " + ", ".join(item.news_top_sources[:3]))
    lines.append("- 근거:")
    for evidence in item.evidence[:4]:
        lines.append(f"  - {evidence}")
    lines.append("- 주요 리스크: " + "; ".join(profile.risks[:2]))
    return lines


def _render_stock(rank: int, item: StockScore) -> list[str]:
    stock = item.stock
    role = "핵심 기업" if stock.role == "core" else "부가/연관 기업"
    lines = [f"### {rank}. {stock.name} ({stock.ticker}) - {item.score:.1f}점 / {item.decision_grade}", ""]
    lines.append(f"- 산업/역할: {stock.industry} / {role}")
    lines.append(
        f"- 투자 판단: {item.decision_grade}, 리스크 {item.risk_level}, 밸류에이션 {item.valuation_label}"
    )
    lines.append(
        f"- 리스크 게이트: {item.risk_gate} / 권장 액션: {item.portfolio_signal} "
        f"(목표 {item.target_weight_pct:.1f}%, 최대 {item.max_weight_pct:.1f}%)"
    )
    lines.append(f"- 분석 스타일: {item.analysis_style}, 가중치 프로필: {item.weight_profile}")
    lines.append(
        f"- 세부 점수: 산업 {item.industry_score:.1f}, 기본적 분석 {item.quality_score:.1f}, "
        f"성장 품질 {item.growth_quality_score:.1f}, "
        f"밸류에이션 {item.valuation_score:.1f}, 모멘텀 {item.momentum_score:.1f}"
    )
    lines.append(f"- 약식 적정 시총 범위: {_format_valuation_range(item)}")
    lines.append(f"- 주요 지표: {_format_fundamentals(stock.fundamentals)}")
    lines.append("- 추천 근거:")
    for reason in item.reasons:
        lines.append(f"  - {reason}")
    lines.append("- 체크할 리스크:")
    for caution in item.cautions[:3]:
        lines.append(f"  - {caution}")
    lines.append("- 매도/축소 신호:")
    for signal in item.sell_signals[:3]:
        lines.append(f"  - {signal}")
    lines.append("- 분석 체크:")
    for check in item.analysis_checks:
        lines.append(f"  - {check}")
    lines.append("- 2차적 사고 체크:")
    for check in item.second_order_checks:
        lines.append(f"  - {check}")
    return lines


def _render_early_growth_stock(rank: int, item: EarlyGrowthScore) -> list[str]:
    stock_score = item.stock_score
    stock = stock_score.stock
    lines = [f"### {rank}. {stock.name} ({stock.ticker}) - {item.score:.1f}점 / {item.entry_label}", ""]
    lines.append(f"- 기존 종합 판단: {stock_score.decision_grade}, 리스크 {stock_score.risk_level}")
    lines.append(
        f"- 세부 점수: 규모 {item.size_score:.1f}, 성장 {item.growth_score:.1f}, "
        f"저점 진입 {item.pullback_score:.1f}, 재무 {item.quality_anchor_score:.1f}, "
        f"밸류 {item.valuation_anchor_score:.1f}"
    )
    lines.append(f"- 주요 지표: {_format_fundamentals(stock.fundamentals)}")
    lines.append("- 후보 근거:")
    for reason in item.reasons:
        lines.append(f"  - {reason}")
    if item.cautions:
        lines.append("- 확인할 점:")
        for caution in item.cautions[:4]:
            lines.append(f"  - {caution}")
    return lines


def _render_legend_strategy_stock(rank: int, item: LegendStrategyScore) -> list[str]:
    stock = item.stock_score.stock
    lines = [f"### {rank}. {stock.name} ({stock.ticker}) - {item.composite_score:.1f}점", ""]
    lines.append(
        f"- 전략별 점수: 린치 {item.lynch_score:.1f}, 오닐 {item.oneil_score:.1f}, "
        f"그린블라트 {item.greenblatt_score:.1f}, 피셔 {item.fisher_score:.1f}"
    )
    lines.append(f"- 종합 판단 참고: {item.stock_score.decision_grade}, 리스크 {item.stock_score.risk_level}")
    lines.append("- 전략 근거:")
    for reason in item.reasons:
        lines.append(f"  - {reason}")
    if item.warnings:
        lines.append("- 데이터 한계:")
        for warning in item.warnings[:4]:
            lines.append(f"  - {warning}")
    return lines


def _render_short_term_stock(rank: int, item: ShortTermScore) -> list[str]:
    stock_score = item.stock_score
    stock = stock_score.stock
    lines = [f"### {rank}. {stock.name} ({stock.ticker}) - {item.score:.1f}점 / {item.signal_label}", ""]
    lines.append(f"- 기간: {item.time_horizon}")
    lines.append(f"- 셋업/신뢰도: {item.setup_label}, {item.confidence_label} ({item.confidence_score:.1f}/100)")
    lines.append(
        f"- 세부 점수: 차트 {item.chart_score:.1f}, 거래량 {item.volume_score:.1f}, "
        f"시장 {item.market_score:.1f}, 뉴스 {item.news_score:.1f}, 기업 {item.company_score:.1f}"
    )
    lines.append(f"- 종합 판단 참고: {stock_score.decision_grade}, 리스크 {stock_score.risk_level}")
    lines.append("- 단기 근거:")
    for reason in item.reasons:
        lines.append(f"  - {reason}")
    if item.cautions:
        lines.append("- 확인할 점:")
        for caution in item.cautions[:4]:
            lines.append(f"  - {caution}")
    return lines


def _render_medium_term_stock(rank: int, item: MediumTermScore) -> list[str]:
    stock_score = item.stock_score
    stock = stock_score.stock
    lines = [f"### {rank}. {stock.name} ({stock.ticker}) - {item.score:.1f}점 / {item.signal_label}", ""]
    lines.append(f"- 기간: {item.time_horizon}")
    lines.append(f"- 신뢰도: {item.confidence_label} ({item.confidence_score:.1f}/100)")
    lines.append(
        f"- 세부 점수: 기업 {item.company_score:.1f}, 차트 {item.chart_score:.1f}, "
        f"시장 {item.market_score:.1f}, 뉴스 {item.news_score:.1f}"
    )
    lines.append(f"- 종합 판단 참고: {stock_score.decision_grade}, 리스크 {stock_score.risk_level}")
    lines.append("- 중기 근거:")
    for reason in item.reasons:
        lines.append(f"  - {reason}")
    if item.cautions:
        lines.append("- 확인할 점:")
        for caution in item.cautions[:4]:
            lines.append(f"  - {caution}")
    return lines


def _render_long_term_stock(rank: int, item: LongTermScore) -> list[str]:
    stock_score = item.stock_score
    stock = stock_score.stock
    lines = [f"### {rank}. {stock.name} ({stock.ticker}) - {item.score:.1f}점 / {item.signal_label}", ""]
    lines.append(f"- 기간: {item.time_horizon}")
    lines.append(f"- 신뢰도: {item.confidence_label} ({item.confidence_score:.1f}/100)")
    lines.append(
        f"- 세부 점수: 기업 {item.company_score:.1f}, 시장/산업/거시 {item.market_score:.1f}, "
        f"구조적 이슈 {item.news_score:.1f}, 장기 차트 {item.chart_score:.1f}"
    )
    lines.append(f"- 종합 판단 참고: {stock_score.decision_grade}, 리스크 {stock_score.risk_level}")
    lines.append("- 장기 근거:")
    for reason in item.reasons:
        lines.append(f"  - {reason}")
    if item.cautions:
        lines.append("- 확인할 점:")
        for caution in item.cautions[:4]:
            lines.append(f"  - {caution}")
    return lines


def _format_fundamentals(fundamentals: Fundamentals) -> str:
    currency = fundamentals.market_cap_currency
    parts = [
        ("매출성장", _pct(fundamentals.revenue_growth_pct)),
        ("3년 CAGR", _pct(fundamentals.revenue_cagr_3y_pct)),
        ("5년 CAGR", _pct(fundamentals.revenue_cagr_5y_pct)),
        ("영업익성장", _pct(fundamentals.operating_income_growth_pct)),
        ("영업레버리지", _pct(fundamentals.operating_leverage_spread_pct)),
        ("분기매출 YoY", _pct(fundamentals.latest_quarter_revenue_yoy_pct)),
        ("분기성장지속", _count(fundamentals.quarterly_revenue_yoy_streak)),
        ("영업이익률", _pct(fundamentals.operating_margin_pct)),
        ("ROE", _pct(fundamentals.roe_pct)),
        ("부채비율", _pct(fundamentals.debt_to_equity_pct)),
        ("유동비율", _pct(fundamentals.current_ratio_pct)),
        ("이자보상", _multiple(fundamentals.interest_coverage)),
        ("매출액", _amount(fundamentals.revenue, currency)),
        ("영업이익", _amount(fundamentals.operating_income, currency)),
        ("EBITDA", _amount(fundamentals.ebitda, currency)),
        ("순이익", _amount(fundamentals.net_income, currency)),
        ("영업현금흐름", _amount(fundamentals.operating_cash_flow, currency)),
        ("FCF", _amount(fundamentals.free_cash_flow, currency)),
        ("PER", _multiple(fundamentals.pe)),
        ("Forward PER", _multiple(fundamentals.forward_pe)),
        ("시가총액", _market_cap(fundamentals.market_cap, fundamentals.market_cap_currency)),
    ]
    formatted = ", ".join(f"{name} {value}" for name, value in parts if value != "N/A")
    return formatted or "실제 출처 확인 지표 없음"


def _format_valuation_range(item: StockScore) -> str:
    valuation_range = item.valuation_range
    currency = item.stock.fundamentals.market_cap_currency
    if valuation_range.market_cap_low is None or valuation_range.market_cap_high is None:
        return valuation_range.note
    upside = _pct_range(valuation_range.upside_low_pct, valuation_range.upside_high_pct)
    return (
        f"{_amount(valuation_range.market_cap_low, currency)}~{_amount(valuation_range.market_cap_high, currency)} "
        f"({valuation_range.profit_metric}, {valuation_range.multiple_low:.1f}~{valuation_range.multiple_high:.1f}배, "
        f"여력 {upside})"
    )


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1f}%"


def _count(value: int | None) -> str:
    return "N/A" if value is None else f"{value}회"


def _pct_range(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "N/A"
    return f"{low:.1f}%~{high:.1f}%"


def _multiple(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1f}배"


def _market_cap(value: float | None, currency: str) -> str:
    if value is None:
        return "N/A"
    if currency == "KRW":
        if value >= 1_000_000_000_000:
            return f"{value / 1_000_000_000_000:.1f}조원"
        return f"{value / 100_000_000:.0f}억원"
    if value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"
    return f"${value / 1_000_000_000:.1f}B"


def _amount(value: float | None, currency: str) -> str:
    if value is None:
        return "N/A"
    sign = "-" if value < 0 else ""
    abs_value = abs(value)
    if currency == "KRW":
        if abs_value >= 1_000_000_000_000:
            return f"{sign}{abs_value / 1_000_000_000_000:.1f}조원"
        if abs_value >= 100_000_000:
            return f"{sign}{abs_value / 100_000_000:.0f}억원"
        return f"{value:,.0f}원"
    if abs_value >= 1_000_000_000_000:
        return f"{sign}${abs_value / 1_000_000_000_000:.2f}T"
    if abs_value >= 1_000_000_000:
        return f"{sign}${abs_value / 1_000_000_000:.1f}B"
    if abs_value >= 1_000_000:
        return f"{sign}${abs_value / 1_000_000:.1f}M"
    return f"{sign}${abs_value:,.0f}"
