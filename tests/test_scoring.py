import json
import unittest
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from stock_recommender.backtest import PricePoint, SnapshotRecord, backtest_to_dict, run_backtest, run_snapshot_backtest
from stock_recommender.config import AppConfig, configured_source_names, load_config, missing_optional_source_names
import stock_recommender.data_sources as data_sources
import stock_recommender.universe_loader as universe_loader
from stock_recommender.macro_data import industry_macro_data_score
from stock_recommender.models import (
    BeneficiaryIndustryProfile,
    DataQuality,
    Fundamentals,
    IndustryMarketProxy,
    IndustryProfile,
    MacroIndicator,
    MacroSnapshot,
    Momentum,
    NewsItem,
    StockProfile,
)
from stock_recommender.opendart_financials import extract_opendart_fundamentals
from stock_recommender.pipeline import beneficiary_market_proxy_tickers, create_recommendation_report
from stock_recommender.report import render_markdown
from stock_recommender.scoring import (
    build_report,
    data_coverage_gate_for_stock,
    decision_grade_for_stock,
    growth_quality_score,
    quality_score,
    valuation_score,
)
from stock_recommender.sec_edgar import extract_fundamentals
from stock_recommender.snapshot_store import SnapshotFileStore, SnapshotStoreError
from stock_recommender.snapshots import report_to_snapshot_payload, save_recommendation_snapshot, snapshot_history
from stock_recommender.storage import CacheStore
from stock_recommender.universe import BENEFICIARY_INDUSTRIES, DEFAULT_MACRO_CONTEXT, INDUSTRIES, STOCKS
from stock_recommender.web import report_to_dict


class ScoringTests(unittest.TestCase):
    def test_quality_score_rewards_profitable_growth(self):
        nvidia = Fundamentals(
            revenue_growth_pct=65.0,
            operating_margin_pct=60.0,
            roe_pct=80.0,
            debt_to_equity_pct=12.0,
            free_cash_flow=90_000_000_000,
            revenue=200_000_000_000,
        )
        cloudflare = Fundamentals(
            revenue_growth_pct=30.0,
            operating_margin_pct=-3.0,
            roe_pct=-7.0,
            debt_to_equity_pct=160.0,
            free_cash_flow=-100_000_000,
            revenue=1_000_000_000,
        )

        self.assertGreater(quality_score(nvidia), quality_score(cloudflare))

    def test_valuation_score_penalizes_high_multiple(self):
        amd = Fundamentals(
            revenue_growth_pct=12.0,
            operating_margin_pct=5.0,
            roe_pct=3.0,
            debt_to_equity_pct=4.0,
            pe=120.0,
            forward_pe=75.0,
        )
        lockheed = Fundamentals(
            revenue_growth_pct=2.0,
            operating_margin_pct=12.0,
            roe_pct=35.0,
            debt_to_equity_pct=160.0,
            pe=18.0,
            forward_pe=17.0,
        )

        self.assertGreater(valuation_score(lockheed), valuation_score(amd))

    def test_valuation_score_does_not_blindly_reward_low_pe(self):
        cheap_but_stagnant = Fundamentals(
            revenue_growth_pct=-2.0,
            operating_margin_pct=4.0,
            roe_pct=3.0,
            debt_to_equity_pct=170.0,
            pe=8.0,
            forward_pe=8.0,
        )
        reasonable_growth = Fundamentals(
            revenue_growth_pct=24.0,
            operating_margin_pct=25.0,
            roe_pct=28.0,
            debt_to_equity_pct=45.0,
            pe=30.0,
            forward_pe=18.0,
        )

        self.assertGreater(valuation_score(reasonable_growth), valuation_score(cheap_but_stagnant))

    def test_growth_quality_rewards_operating_leverage_and_quarterly_streak(self):
        strong = Fundamentals(
            revenue_growth_pct=22.0,
            operating_margin_pct=24.0,
            roe_pct=20.0,
            debt_to_equity_pct=35.0,
            revenue_cagr_3y_pct=18.0,
            operating_income_growth_pct=42.0,
            operating_income_cagr_3y_pct=28.0,
            operating_leverage_spread_pct=20.0,
            latest_quarter_revenue_yoy_pct=26.0,
            latest_quarter_operating_income_yoy_pct=48.0,
            quarterly_revenue_yoy_streak=4,
            quarterly_operating_leverage_streak=3,
        )
        hollow = Fundamentals(
            revenue_growth_pct=22.0,
            operating_margin_pct=12.0,
            roe_pct=10.0,
            debt_to_equity_pct=35.0,
            revenue_cagr_3y_pct=7.0,
            operating_income_growth_pct=6.0,
            operating_income_cagr_3y_pct=5.0,
            operating_leverage_spread_pct=-16.0,
            latest_quarter_revenue_yoy_pct=-2.0,
            latest_quarter_operating_income_yoy_pct=-12.0,
            quarterly_revenue_yoy_streak=0,
            quarterly_operating_leverage_streak=0,
        )

        self.assertGreater(growth_quality_score(strong), growth_quality_score(hollow) + 35)
        self.assertGreater(quality_score(strong), quality_score(hollow))

    def test_stock_scores_include_analysis_checks(self):
        industry = INDUSTRIES[0]
        nvidia_profile = StockProfile(
            ticker="NVDA",
            name="NVIDIA",
            industry=industry.name,
            role="core",
            thesis="AI GPU leader",
            risks=(),
            fundamentals=Fundamentals(
                revenue_growth_pct=65.0,
                operating_margin_pct=60.0,
                roe_pct=80.0,
                debt_to_equity_pct=12.0,
                pe=52.0,
                forward_pe=35.0,
                market_cap=3_000_000_000_000,
            ),
        )
        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=(industry,),
            stocks=(nvidia_profile,),
            news_items=(),
        )
        nvidia = next(item for item in report.stock_scores if item.stock.ticker == "NVDA")

        self.assertEqual(nvidia.analysis_style, "성장주")
        self.assertTrue(any("멀티플" in check for check in nvidia.analysis_checks))
        self.assertGreaterEqual(len(nvidia.second_order_checks), 4)
        self.assertEqual(nvidia.valuation_range.profit_metric, "PER 역산 이익")
        self.assertIsNotNone(nvidia.valuation_range.market_cap_low)
        self.assertTrue(any("밸류에이션 범위" in check for check in nvidia.analysis_checks))

    def test_risk_gate_hard_fail_excludes_stock(self):
        industry = IndustryProfile("테스트 성장 산업", "", (), (), (), ())
        stock = StockProfile(
            ticker="FAIL",
            name="Fail Corp",
            industry=industry.name,
            role="core",
            thesis="위험 재무 구조 테스트",
            risks=(),
            fundamentals=Fundamentals(
                revenue_growth_pct=45.0,
                operating_margin_pct=-8.0,
                roe_pct=-12.0,
                debt_to_equity_pct=450.0,
                pe=25.0,
                forward_pe=22.0,
                free_cash_flow=-100.0,
                current_ratio_pct=55.0,
                interest_coverage=0.8,
            ),
        )

        report = build_report(DEFAULT_MACRO_CONTEXT, (industry,), (stock,), ())
        item = report.stock_scores[0]

        self.assertEqual(item.risk_gate, "Hard Fail")
        self.assertEqual(item.decision_grade, "제외")
        self.assertEqual(item.target_weight_pct, 0)
        self.assertTrue(any("즉시 제외" in signal for signal in item.sell_signals))

    def test_aggressive_allow_keeps_high_growth_as_limited_candidate(self):
        industry = IndustryProfile("테스트 AI 산업", "", (), (), (), ())
        stock = StockProfile(
            ticker="AGGR",
            name="Aggressive Growth",
            industry=industry.name,
            role="core",
            thesis="고성장 리스크 허용 테스트",
            risks=(),
            fundamentals=Fundamentals(
                revenue_growth_pct=48.0,
                operating_margin_pct=18.0,
                roe_pct=22.0,
                debt_to_equity_pct=260.0,
                pe=38.0,
                forward_pe=32.0,
                operating_cash_flow=300.0,
                free_cash_flow=120.0,
                current_ratio_pct=150.0,
                interest_coverage=5.0,
            ),
        )

        report = build_report(DEFAULT_MACRO_CONTEXT, (industry,), (stock,), ())
        item = report.stock_scores[0]

        self.assertEqual(item.risk_gate, "Aggressive Allow")
        self.assertEqual(item.risk_level, "높음")
        self.assertLessEqual(item.max_weight_pct, 4.0)
        self.assertNotEqual(item.decision_grade, "제외")

    def test_style_weight_profile_and_portfolio_fields_are_serialized(self):
        industry = IndustryProfile("테스트 금융", "", (), (), (), ())
        stock = StockProfile(
            ticker="BANK",
            name="Bank Quality",
            industry=industry.name,
            role="core",
            thesis="가치 금융 테스트",
            risks=(),
            fundamentals=Fundamentals(
                revenue_growth_pct=8.0,
                operating_margin_pct=32.0,
                roe_pct=18.0,
                debt_to_equity_pct=35.0,
                pe=12.0,
                forward_pe=10.0,
                operating_cash_flow=500.0,
                free_cash_flow=320.0,
                current_ratio_pct=180.0,
                interest_coverage=9.0,
            ),
        )

        report = build_report(DEFAULT_MACRO_CONTEXT, (industry,), (stock,), ())
        item = report.stock_scores[0]
        api_payload = report_to_dict(report)
        snapshot_payload = report_to_snapshot_payload(report)

        self.assertEqual(item.weight_profile, "가치/금융주")
        self.assertIn(item.portfolio_signal, {"편입 후보", "분할 관찰", "보유 점검"})
        self.assertEqual(api_payload["stocks"][0]["riskGate"], item.risk_gate)
        self.assertEqual(snapshot_payload["stocks"][0]["targetWeightPct"], item.target_weight_pct)

    def test_report_contains_data_quality(self):
        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=INDUSTRIES,
            stocks=STOCKS,
            news_items=(),
            data_quality=DataQuality(warnings=("테스트 경고",)),
        )

        markdown = render_markdown(report, top_industries=1, top_stocks=1)

        self.assertIn("## 데이터 품질", markdown)
        self.assertIn("테스트 경고", markdown)
        self.assertIn("## 추천 종목 후보", markdown)
        self.assertIn("## 단기 매매 후보", markdown)
        self.assertIn("## 중기 매매 후보", markdown)
        self.assertIn("## 장기 투자 후보", markdown)
        self.assertIn("## 저점 성장주 후보", markdown)
        self.assertIn("## 투자 전설 전략 후보", markdown)

    def test_report_contains_macro_snapshot(self):
        snapshot = MacroSnapshot(
            indicators=(
                MacroIndicator("미국 기준금리", 4.5, "%", "2026-01-01", "FRED", "테스트"),
            ),
            growth_score=40,
            defensive_score=70,
            infrastructure_score=55,
            korea_fx_score=45,
            summary="테스트 거시 환경",
            investment_guidance=("방어 업종과 현금흐름 안정성을 우선 확인합니다.",),
        )
        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=INDUSTRIES,
            stocks=STOCKS,
            news_items=(),
            macro_snapshot=snapshot,
            data_quality=DataQuality(live_macro=True),
        )

        markdown = render_markdown(report, top_industries=1, top_stocks=1)

        self.assertIn("## 거시경제 지표", markdown)
        self.assertIn("테스트 거시 환경", markdown)
        self.assertIn("### 투자 방향", markdown)
        self.assertIn("방어 업종과 현금흐름 안정성", markdown)
        self.assertIn("미국 기준금리", markdown)

    def test_industry_macro_data_score_uses_industry_sensitivity(self):
        snapshot = MacroSnapshot(growth_score=30, defensive_score=80, infrastructure_score=70, korea_fx_score=45)

        power_score = industry_macro_data_score("전력 인프라 및 에너지 장비", snapshot)
        ai_score = industry_macro_data_score("AI 반도체 및 데이터센터", snapshot)

        self.assertGreater(power_score, ai_score)

    def test_beneficiary_industry_score_follows_source_industry_strength(self):
        hot = IndustryProfile(
            name="Hot Source",
            description="강한 원인 산업",
            news_terms=("hotsource",),
            macro_terms=("hotmacro",),
            tailwinds=("수요 확대",),
            risks=("과열",),
        )
        cold = IndustryProfile(
            name="Cold Source",
            description="약한 원인 산업",
            news_terms=("coldsource",),
            macro_terms=("coldmacro",),
            tailwinds=("회복 가능성",),
            risks=("둔화",),
        )
        hot_stock = StockProfile(
            ticker="HOT",
            name="Hot Co",
            industry=hot.name,
            role="core",
            thesis="원인 산업 대표 기업입니다.",
            risks=("변동성",),
            fundamentals=Fundamentals(20.0, 20.0, 18.0, 30.0, 25.0, 20.0, 10_000_000_000),
        )
        cold_stock = StockProfile(
            ticker="COLD",
            name="Cold Co",
            industry=cold.name,
            role="core",
            thesis="약한 산업 대표 기업입니다.",
            risks=("둔화",),
            fundamentals=Fundamentals(2.0, 8.0, 7.0, 40.0, 18.0, 16.0, 10_000_000_000),
        )
        hot_beneficiary = BeneficiaryIndustryProfile(
            name="Hot Beneficiary",
            description="강한 원인 산업의 수혜 산업",
            source_industry=hot.name,
            mechanism="원인 산업 투자 확대가 후행 수요로 이어집니다.",
            time_horizon="6~18개월",
            keywords=("sharedbenefit",),
            risks=("수요 지연",),
            connection_strength=75,
        )
        cold_beneficiary = BeneficiaryIndustryProfile(
            name="Cold Beneficiary",
            description="약한 원인 산업의 수혜 산업",
            source_industry=cold.name,
            mechanism="원인 산업 투자 확대가 후행 수요로 이어집니다.",
            time_horizon="6~18개월",
            keywords=("sharedbenefit",),
            risks=("수요 지연",),
            connection_strength=75,
        )

        report = build_report(
            macro_context="hotmacro investment cycle",
            industries=(hot, cold),
            stocks=(hot_stock, cold_stock),
            news_items=(NewsItem("hotsource sharedbenefit demand", "test"),),
            beneficiary_industries=(hot_beneficiary, cold_beneficiary),
        )
        scores = {item.profile.name: item for item in report.beneficiary_industry_scores}

        self.assertGreater(scores["Hot Beneficiary"].score, scores["Cold Beneficiary"].score)
        self.assertGreater(
            scores["Hot Beneficiary"].source_industry_score,
            scores["Cold Beneficiary"].source_industry_score,
        )

    def test_beneficiary_industry_news_keywords_raise_score(self):
        source = IndustryProfile(
            name="Source",
            description="원인 산업",
            news_terms=("source",),
            macro_terms=("sourcecapex",),
            tailwinds=("투자 확대",),
            risks=("과열",),
        )
        source_stock = StockProfile(
            ticker="SRC",
            name="Source Co",
            industry=source.name,
            role="core",
            thesis="원인 산업 대표 기업입니다.",
            risks=("변동성",),
            fundamentals=Fundamentals(20.0, 20.0, 18.0, 30.0, 25.0, 20.0, 10_000_000_000),
        )
        matched = BeneficiaryIndustryProfile(
            name="Matched Benefit",
            description="뉴스 키워드가 잡힌 수혜 산업",
            source_industry=source.name,
            mechanism="수요 병목이 후행 투자로 이어집니다.",
            time_horizon="3~12개월",
            keywords=("cooling", "thermal management"),
            risks=("발주 지연",),
            connection_strength=80,
        )
        unmatched = BeneficiaryIndustryProfile(
            name="Unmatched Benefit",
            description="뉴스 키워드가 없는 수혜 산업",
            source_industry=source.name,
            mechanism="수요 병목이 후행 투자로 이어집니다.",
            time_horizon="3~12개월",
            keywords=("unseenbenefit",),
            risks=("발주 지연",),
            connection_strength=80,
        )

        report = build_report(
            macro_context="sourcecapex",
            industries=(source,),
            stocks=(source_stock,),
            news_items=(NewsItem("data center cooling thermal management demand", "test"),),
            beneficiary_industries=(matched, unmatched),
        )
        scores = {item.profile.name: item for item in report.beneficiary_industry_scores}

        self.assertGreater(scores["Matched Benefit"].news_score, scores["Unmatched Benefit"].news_score)
        self.assertGreater(scores["Matched Benefit"].score, scores["Unmatched Benefit"].score)

    def test_beneficiary_market_proxy_tickers_are_pipeline_targets(self):
        profile = BeneficiaryIndustryProfile(
            name="Proxy Benefit",
            description="명시 proxy를 가진 수혜 산업",
            source_industry="Source",
            mechanism="원인 산업 수요가 후행합니다.",
            time_horizon="3~12개월",
            keywords=("proxybenefit",),
            risks=("지연",),
            connection_strength=80,
            market_proxies=(
                IndustryMarketProxy("pxa", "Proxy A", "etf", 1.2),
                IndustryMarketProxy("PXB", "Proxy B", "representative", 1.0),
            ),
        )

        self.assertEqual(beneficiary_market_proxy_tickers((profile,)), ("PXA", "PXB"))

    def test_beneficiary_proxy_momentum_uses_explicit_market_proxies(self):
        source = IndustryProfile(
            name="Proxy Source",
            description="원인 산업",
            news_terms=("proxysource",),
            macro_terms=("proxycapex",),
            tailwinds=("투자 확대",),
            risks=("과열",),
        )
        source_stock = StockProfile(
            ticker="SRC",
            name="Source Co",
            industry=source.name,
            role="core",
            thesis="원인 산업 대표 기업입니다.",
            risks=("변동성",),
            fundamentals=Fundamentals(20.0, 20.0, 18.0, 30.0, 25.0, 20.0, 10_000_000_000),
        )
        proxied = BeneficiaryIndustryProfile(
            name="Proxied Benefit",
            description="proxy 모멘텀이 있는 수혜 산업",
            source_industry=source.name,
            mechanism="후행 투자로 연결됩니다.",
            time_horizon="3~12개월",
            keywords=("proxied",),
            risks=("발주 지연",),
            connection_strength=80,
            market_proxies=(IndustryMarketProxy("PXHIGH", "High Proxy", "representative", 1.0),),
        )
        no_proxy = BeneficiaryIndustryProfile(
            name="No Proxy Benefit",
            description="proxy가 없는 수혜 산업",
            source_industry=source.name,
            mechanism="후행 투자로 연결됩니다.",
            time_horizon="3~12개월",
            keywords=("noproxy",),
            risks=("발주 지연",),
            connection_strength=80,
        )

        report = build_report(
            macro_context="proxycapex",
            industries=(source,),
            stocks=(source_stock,),
            news_items=(),
            momentums={"PXHIGH": Momentum(24.0, 30.0, 36.0)},
            beneficiary_industries=(proxied, no_proxy),
        )
        scores = {item.profile.name: item for item in report.beneficiary_industry_scores}

        self.assertGreater(scores["Proxied Benefit"].market_score, scores["No Proxy Benefit"].market_score)
        self.assertEqual(scores["Proxied Benefit"].proxy_coverage_pct, 100)
        self.assertEqual(scores["No Proxy Benefit"].proxy_coverage_pct, 0)

    def test_beneficiary_news_trend_rewards_recent_acceleration(self):
        source = IndustryProfile(
            name="Trend Source",
            description="원인 산업",
            news_terms=("trendsource",),
            macro_terms=("trendcapex",),
            tailwinds=("투자 확대",),
            risks=("과열",),
        )
        source_stock = StockProfile(
            ticker="TRD",
            name="Trend Co",
            industry=source.name,
            role="core",
            thesis="원인 산업 대표 기업입니다.",
            risks=("변동성",),
            fundamentals=Fundamentals(20.0, 20.0, 18.0, 30.0, 25.0, 20.0, 10_000_000_000),
        )
        beneficiary = BeneficiaryIndustryProfile(
            name="Cooling Trend",
            description="뉴스가 늘어난 수혜 산업",
            source_industry=source.name,
            mechanism="수요 병목이 후행 투자로 이어집니다.",
            time_horizon="3~12개월",
            keywords=("cooling", "thermal management"),
            risks=("발주 지연",),
            connection_strength=80,
        )
        created_at = datetime(2026, 5, 21, tzinfo=timezone.utc)
        news_items = tuple(
            NewsItem(
                "data center cooling thermal management orders rise",
                "Reuters",
                published=(created_at - timedelta(days=day)).isoformat(),
            )
            for day in (1, 2, 3)
        )

        report = build_report(
            macro_context="trendcapex",
            industries=(source,),
            stocks=(source_stock,),
            news_items=news_items,
            beneficiary_industries=(beneficiary,),
            created_at=created_at,
        )
        score = report.beneficiary_industry_scores[0]

        self.assertGreater(score.news_acceleration_score, 50)
        self.assertIn("증가", score.news_coverage_label)

    def test_beneficiary_news_source_weights_raise_high_trust_sources(self):
        source = IndustryProfile(
            name="Source Trust",
            description="원인 산업",
            news_terms=("trustsource",),
            macro_terms=("trustcapex",),
            tailwinds=("투자 확대",),
            risks=("과열",),
        )
        source_stock = StockProfile(
            ticker="TRU",
            name="Trust Co",
            industry=source.name,
            role="core",
            thesis="원인 산업 대표 기업입니다.",
            risks=("변동성",),
            fundamentals=Fundamentals(20.0, 20.0, 18.0, 30.0, 25.0, 20.0, 10_000_000_000),
        )
        beneficiary = BeneficiaryIndustryProfile(
            name="Security Trust",
            description="출처 가중치를 테스트하는 수혜 산업",
            source_industry=source.name,
            mechanism="후행 투자가 늘어납니다.",
            time_horizon="3~12개월",
            keywords=("cybersecurity", "cloud security"),
            risks=("발주 지연",),
            connection_strength=80,
        )
        created_at = datetime(2026, 5, 21, tzinfo=timezone.utc)

        def score_for_source(source_name: str):
            report = build_report(
                macro_context="trustcapex",
                industries=(source,),
                stocks=(source_stock,),
                news_items=(
                    NewsItem(
                        "cybersecurity cloud security demand expands",
                        source_name,
                        published=(created_at - timedelta(days=1)).isoformat(),
                    ),
                    NewsItem(
                        "cloud security cybersecurity budgets rise",
                        source_name,
                        published=(created_at - timedelta(days=2)).isoformat(),
                    ),
                ),
                beneficiary_industries=(beneficiary,),
                created_at=created_at,
            )
            return report.beneficiary_industry_scores[0]

        self.assertGreater(score_for_source("Reuters").news_score, score_for_source("PR Newswire").news_score)

    def test_report_renders_beneficiary_industries(self):
        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=INDUSTRIES[:1],
            stocks=STOCKS[:2],
            news_items=(),
            beneficiary_industries=BENEFICIARY_INDUSTRIES[:1],
        )

        markdown = render_markdown(report, top_industries=1, top_stocks=1)

        self.assertIn("## 현재 활발한 산업", markdown)
        self.assertIn("## 미래 수혜 산업", markdown)
        self.assertIn("원인 산업: AI 반도체 및 데이터센터", markdown)
        self.assertIn("예상 시차", markdown)
        self.assertIn("대표 ETF/종목", markdown)
        self.assertIn("뉴스 흐름", markdown)

    def test_early_growth_ranking_prefers_small_growth_pullback(self):
        industry = INDUSTRIES[0]
        small_growth = StockProfile(
            ticker="SMALL",
            name="Small Growth",
            industry=industry.name,
            role="adjacent",
            thesis="작은 시가총액과 높은 매출 성장을 가진 후보입니다.",
            risks=("소형주 변동성",),
            fundamentals=Fundamentals(42.0, 18.0, 16.0, 20.0, 28.0, 22.0, 1_200_000_000),
        )
        mega_growth = StockProfile(
            ticker="MEGA",
            name="Mega Growth",
            industry=industry.name,
            role="core",
            thesis="이미 대형주가 된 성장 기업입니다.",
            risks=("고평가",),
            fundamentals=Fundamentals(42.0, 30.0, 30.0, 10.0, 45.0, 35.0, 1_000_000_000_000),
        )

        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=(industry,),
            stocks=(small_growth, mega_growth),
            news_items=(),
            momentums={
                "SMALL": Momentum(4.0, -10.0, -12.0, -20.0, 35.0),
                "MEGA": Momentum(28.0, 45.0, 82.0, 0.0, 100.0),
            },
        )

        self.assertEqual(report.early_growth_scores[0].stock_score.stock.ticker, "SMALL")
        self.assertGreater(report.early_growth_scores[0].pullback_score, 70)
        self.assertLess(report.early_growth_scores[1].size_score, 20)

    def test_short_term_ranking_uses_news_market_chart_and_company_data(self):
        industry = INDUSTRIES[0]
        strong = StockProfile(
            ticker="FAST",
            name="Fast AI",
            industry=industry.name,
            role="adjacent",
            thesis="단기 AI 인프라 수주 모멘텀을 가진 후보입니다.",
            risks=("단기 급등 변동성",),
            fundamentals=Fundamentals(36.0, 18.0, 16.0, 30.0, 28.0, 22.0, 1_500_000_000),
            recent_issues=("AI chip 공급 계약과 데이터센터 고객 확대",),
        )
        weak = StockProfile(
            ticker="SLOW",
            name="Slow AI",
            industry=industry.name,
            role="adjacent",
            thesis="실적과 가격 흐름이 약한 비교 후보입니다.",
            risks=("실적 둔화",),
            fundamentals=Fundamentals(-5.0, -8.0, -4.0, 260.0, 20.0, 18.0, 1_500_000_000),
        )

        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=(industry,),
            stocks=(strong, weak),
            news_items=(
                NewsItem(
                    title="Fast AI wins AI chip data center contract",
                    source="Test News",
                    summary="GPU accelerator order expands near-term demand.",
                ),
            ),
            momentums={
                "FAST": Momentum(12.0, 24.0, 42.0, -8.0, 72.0),
                "SLOW": Momentum(-18.0, -28.0, -35.0, -45.0, 8.0),
            },
        )

        top = report.short_term_scores[0]

        self.assertEqual(top.stock_score.stock.ticker, "FAST")
        self.assertGreater(top.news_score, report.short_term_scores[1].news_score)
        self.assertGreater(top.market_score, 70)
        self.assertGreater(top.chart_score, 70)
        self.assertIn(top.signal_label, {"단기 강세 후보", "단기 관심"})

    def test_short_term_prefers_chart_volume_setup_over_news_only(self):
        industry = INDUSTRIES[0]
        chart_leader = StockProfile(
            ticker="CHART",
            name="Chart Leader",
            industry=industry.name,
            role="adjacent",
            thesis="스윙 차트와 거래량이 개선되는 후보입니다.",
            risks=("단기 변동성",),
            fundamentals=Fundamentals(28.0, 18.0, 16.0, 40.0, 26.0, 20.0, 2_000_000_000),
            recent_issues=("AI data center order momentum",),
        )
        news_only = StockProfile(
            ticker="NEWSY",
            name="News Only",
            industry=industry.name,
            role="adjacent",
            thesis="뉴스는 많지만 가격 데이터가 부족한 후보입니다.",
            risks=("차트 확인 필요",),
            fundamentals=Fundamentals(28.0, 18.0, 16.0, 40.0, 26.0, 20.0, 2_000_000_000),
        )

        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=(industry,),
            stocks=(chart_leader, news_only),
            news_items=(
                NewsItem(
                    title="NEWSY wins AI infrastructure contract",
                    source="Test News",
                    summary="News Only gets a near-term catalyst.",
                ),
            ),
            momentums={
                "CHART": _strong_swing_momentum(),
                "NEWSY": Momentum(),
            },
        )

        top = report.short_term_scores[0]
        news_candidate = next(item for item in report.short_term_scores if item.stock_score.stock.ticker == "NEWSY")

        self.assertEqual(top.stock_score.stock.ticker, "CHART")
        self.assertGreater(top.chart_score, 80)
        self.assertGreater(top.volume_score, 75)
        self.assertGreater(news_candidate.news_score, top.news_score)
        self.assertLessEqual(news_candidate.score, 55)

    def test_short_term_caps_missing_momentum_data(self):
        industry = INDUSTRIES[0]
        candidate = StockProfile(
            ticker="NODATA",
            name="No Data",
            industry=industry.name,
            role="adjacent",
            thesis="뉴스와 기업 지표는 좋지만 가격 데이터가 비어 있습니다.",
            risks=("가격 데이터 부족",),
            fundamentals=Fundamentals(35.0, 22.0, 18.0, 30.0, 28.0, 21.0, 2_000_000_000),
            recent_issues=("AI accelerator order",),
        )

        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=(industry,),
            stocks=(candidate,),
            news_items=(
                NewsItem(
                    title="NODATA AI accelerator order expands",
                    source="Test News",
                    summary="Positive near-term catalyst.",
                ),
            ),
            momentums={},
        )

        short = report.short_term_scores[0]

        self.assertLessEqual(short.score, 55)
        self.assertEqual(short.setup_label, "차트 데이터 부족")
        self.assertIn(short.confidence_label, {"낮음", "확인 필요"})

    def test_short_term_caps_missing_volume_data(self):
        industry = INDUSTRIES[0]
        candidate = StockProfile(
            ticker="NOVOL",
            name="No Volume",
            industry=industry.name,
            role="adjacent",
            thesis="가격 돌파는 있으나 거래량 데이터가 없는 후보입니다.",
            risks=("거래량 확인 필요",),
            fundamentals=Fundamentals(35.0, 22.0, 18.0, 30.0, 28.0, 21.0, 2_000_000_000),
            recent_issues=("AI accelerator order",),
        )

        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=(industry,),
            stocks=(candidate,),
            news_items=(
                NewsItem(
                    title="NOVOL AI accelerator order expands",
                    source="Test News",
                    summary="Positive near-term catalyst.",
                ),
            ),
            momentums={"NOVOL": _strong_swing_momentum(with_volume=False)},
        )

        short = report.short_term_scores[0]

        self.assertLessEqual(short.score, 72)
        self.assertEqual(short.volume_score, 50)
        self.assertIn("거래량", " ".join(short.cautions))

    def test_medium_term_ranking_uses_company_market_chart_and_news(self):
        industry = INDUSTRIES[0]
        steady = StockProfile(
            ticker="STEADY",
            name="Steady AI",
            industry=industry.name,
            role="adjacent",
            thesis="분기 실적 성장과 3개월 추세가 함께 개선되는 후보입니다.",
            risks=("고객 투자 지연",),
            fundamentals=Fundamentals(32.0, 21.0, 18.0, 35.0, 30.0, 22.0, 2_000_000_000),
            recent_issues=("AI data center 수주와 분기 실적 성장 지속",),
        )
        fading = StockProfile(
            ticker="FADING",
            name="Fading AI",
            industry=industry.name,
            role="adjacent",
            thesis="중기 추세와 실적 지표가 약한 비교 후보입니다.",
            risks=("수익성 악화",),
            fundamentals=Fundamentals(-8.0, -6.0, -5.0, 240.0, 16.0, 15.0, 2_000_000_000),
        )

        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=(industry,),
            stocks=(steady, fading),
            news_items=(
                NewsItem(
                    title="AI data center spending supports Steady AI quarterly growth",
                    source="Test News",
                    summary="Semiconductor infrastructure demand remains firm.",
                ),
            ),
            momentums={
                "STEADY": Momentum(8.0, 20.0, 35.0, -10.0, 65.0),
                "FADING": Momentum(-18.0, -25.0, -30.0, -42.0, 12.0),
            },
        )

        top = report.medium_term_scores[0]

        self.assertEqual(top.stock_score.stock.ticker, "STEADY")
        self.assertGreater(top.company_score, report.medium_term_scores[1].company_score)
        self.assertGreater(top.market_score, 70)
        self.assertGreater(top.chart_score, 70)
        self.assertIn(top.signal_label, {"중기 강세 후보", "중기 관심"})

    def test_medium_and_long_confidence_labels_limit_top_signals(self):
        industry = INDUSTRIES[0]
        thin_data = StockProfile(
            ticker="THIN",
            name="Thin Data",
            industry=industry.name,
            role="core",
            thesis="성장성은 좋아 보이나 가격·재무 데이터 보강이 필요한 후보입니다.",
            risks=("데이터 확인 필요",),
            fundamentals=Fundamentals(38.0, 24.0, 20.0, 30.0, 24.0, 18.0, 2_000_000_000),
            recent_issues=("AI infrastructure demand supports growth",),
        )

        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=(industry,),
            stocks=(thin_data,),
            news_items=(
                NewsItem(
                    title="THIN AI infrastructure demand expands",
                    source="Test News",
                    summary="Durable growth narrative but data coverage is thin.",
                ),
            ),
            momentums={},
        )

        medium = report.medium_term_scores[0]
        long = report.long_term_scores[0]

        self.assertLess(medium.confidence_score, 52)
        self.assertLess(long.confidence_score, 55)
        self.assertEqual(medium.confidence_label, "확인 필요")
        self.assertEqual(long.confidence_label, "확인 필요")
        self.assertNotEqual(medium.signal_label, "중기 강세 후보")
        self.assertNotEqual(long.signal_label, "장기 핵심 후보")

    def test_long_term_ranking_uses_company_value_industry_chart_and_structural_news(self):
        industry = INDUSTRIES[0]
        compounder = StockProfile(
            ticker="COMPOUND",
            name="Compound AI",
            industry=industry.name,
            role="core",
            thesis="높은 수익성, 현금흐름, 구조적 산업 성장성을 가진 장기 후보입니다.",
            risks=("산업 경쟁 심화",),
            fundamentals=Fundamentals(
                revenue_growth_pct=28.0,
                operating_margin_pct=32.0,
                roe_pct=26.0,
                debt_to_equity_pct=25.0,
                pe=28.0,
                forward_pe=22.0,
                market_cap_usd=30_000_000_000,
                revenue=10_000_000_000,
                free_cash_flow=2_000_000_000,
                current_ratio_pct=180.0,
                interest_coverage=12.0,
            ),
            recent_issues=("AI infrastructure demand and market share expansion",),
        )
        fragile = StockProfile(
            ticker="FRAGILE",
            name="Fragile AI",
            industry=industry.name,
            role="adjacent",
            thesis="성장 둔화와 재무 부담이 큰 비교 후보입니다.",
            risks=("현금흐름 악화",),
            fundamentals=Fundamentals(
                revenue_growth_pct=-4.0,
                operating_margin_pct=-8.0,
                roe_pct=-6.0,
                debt_to_equity_pct=260.0,
                pe=90.0,
                forward_pe=70.0,
                market_cap_usd=30_000_000_000,
                revenue=10_000_000_000,
                free_cash_flow=-800_000_000,
                current_ratio_pct=80.0,
                interest_coverage=1.5,
            ),
        )

        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=(industry,),
            stocks=(compounder, fragile),
            news_items=(
                NewsItem(
                    title="AI infrastructure demand expands long-term semiconductor opportunity",
                    source="Test News",
                    summary="Data center capex supports durable market growth.",
                ),
            ),
            momentums={
                "COMPOUND": Momentum(5.0, 16.0, 34.0, -12.0, 62.0),
                "FRAGILE": Momentum(-12.0, -25.0, -38.0, -50.0, 8.0),
            },
        )

        top = report.long_term_scores[0]

        self.assertEqual(top.stock_score.stock.ticker, "COMPOUND")
        self.assertGreater(top.company_score, report.long_term_scores[1].company_score)
        self.assertGreater(top.market_score, 45)
        self.assertGreater(top.chart_score, 70)
        self.assertIn(top.signal_label, {"장기 핵심 후보", "장기 관심"})

    def test_legend_lynch_prefers_low_peg_growth_candidate(self):
        industry = INDUSTRIES[0]
        low_peg = StockProfile(
            ticker="PEGGOOD",
            name="PEG Good",
            industry=industry.name,
            role="adjacent",
            thesis="성장 대비 가격이 낮은 후보입니다.",
            risks=(),
            fundamentals=Fundamentals(30.0, 18.0, 20.0, 30.0, 24.0, 18.0, 2_000_000_000),
        )
        high_peg = StockProfile(
            ticker="PEGBAD",
            name="PEG Bad",
            industry=industry.name,
            role="adjacent",
            thesis="성장 대비 가격 부담이 큰 후보입니다.",
            risks=(),
            fundamentals=Fundamentals(8.0, 12.0, 10.0, 80.0, 80.0, 64.0, 2_000_000_000),
        )

        report = build_report(DEFAULT_MACRO_CONTEXT, (industry,), (low_peg, high_peg), ())
        by_ticker = {item.stock_score.stock.ticker: item for item in report.legend_strategy_scores}

        self.assertGreater(by_ticker["PEGGOOD"].lynch_score, by_ticker["PEGBAD"].lynch_score)

    def test_legend_oneil_prefers_growth_momentum_and_catalyst(self):
        industry = INDUSTRIES[0]
        leader = StockProfile(
            ticker="ONLEAD",
            name="O Neil Leader",
            industry=industry.name,
            role="core",
            thesis="성장과 모멘텀이 함께 강한 후보입니다.",
            risks=(),
            fundamentals=Fundamentals(32.0, 18.0, 20.0, 25.0, 35.0, 28.0, 4_000_000_000),
            recent_issues=("신제품 수요와 대형 고객 확대",),
        )
        laggard = StockProfile(
            ticker="ONLAG",
            name="O Neil Laggard",
            industry=industry.name,
            role="adjacent",
            thesis="성장과 가격 흐름이 약한 후보입니다.",
            risks=(),
            fundamentals=Fundamentals(2.0, 8.0, 6.0, 120.0, 24.0, 22.0, 4_000_000_000),
        )

        report = build_report(
            DEFAULT_MACRO_CONTEXT,
            (industry,),
            (leader, laggard),
            (),
            momentums={
                "ONLEAD": Momentum(14.0, 32.0, 52.0, -6.0, 78.0),
                "ONLAG": Momentum(-14.0, -22.0, -30.0, -42.0, 12.0),
            },
        )
        by_ticker = {item.stock_score.stock.ticker: item for item in report.legend_strategy_scores}

        self.assertGreater(by_ticker["ONLEAD"].oneil_score, by_ticker["ONLAG"].oneil_score)

    def test_legend_greenblatt_prefers_profitability_and_earnings_yield(self):
        industry = INDUSTRIES[0]
        quality_value = StockProfile(
            ticker="MAGIC",
            name="Magic Formula",
            industry=industry.name,
            role="adjacent",
            thesis="수익성과 이익수익률이 모두 좋은 후보입니다.",
            risks=(),
            fundamentals=Fundamentals(
                10.0, 28.0, 32.0, 20.0, 16.0, 14.0, 10_000_000_000,
                operating_income=1_600_000_000,
                net_income=1_200_000_000,
            ),
        )
        expensive_low_return = StockProfile(
            ticker="NOMAGIC",
            name="No Magic",
            industry=industry.name,
            role="adjacent",
            thesis="낮은 자본효율과 고멀티플 후보입니다.",
            risks=(),
            fundamentals=Fundamentals(
                10.0, 5.0, 4.0, 20.0, 90.0, 75.0, 10_000_000_000,
                operating_income=250_000_000,
                net_income=150_000_000,
            ),
        )

        report = build_report(DEFAULT_MACRO_CONTEXT, (industry,), (quality_value, expensive_low_return), ())
        by_ticker = {item.stock_score.stock.ticker: item for item in report.legend_strategy_scores}

        self.assertGreater(by_ticker["MAGIC"].greenblatt_score, by_ticker["NOMAGIC"].greenblatt_score)

    def test_legend_greenblatt_uses_actual_roic_and_ebit_ev_first(self):
        industry = INDUSTRIES[0]
        actual_magic = StockProfile(
            ticker="REALMAGIC",
            name="Real Magic Formula",
            industry=industry.name,
            role="adjacent",
            thesis="실제 ROIC와 EBIT/EV가 좋은 후보입니다.",
            risks=(),
            fundamentals=Fundamentals(
                8.0, 4.0, 3.0, 80.0, 90.0, 80.0, 10_000_000_000,
                operating_income=900_000_000,
                net_income=500_000_000,
                roic_pct=28.0,
                ev_to_ebit=9.0,
                earnings_yield_pct=11.1,
            ),
        )
        proxy_only = StockProfile(
            ticker="PROXYMAGIC",
            name="Proxy Magic Formula",
            industry=industry.name,
            role="adjacent",
            thesis="프록시는 좋아 보이지만 실제 자본수익률이 낮은 후보입니다.",
            risks=(),
            fundamentals=Fundamentals(
                8.0, 34.0, 35.0, 30.0, 9.0, 8.0, 10_000_000_000,
                operating_income=900_000_000,
                net_income=700_000_000,
                roic_pct=2.0,
                ev_to_ebit=60.0,
                earnings_yield_pct=1.7,
            ),
        )

        report = build_report(DEFAULT_MACRO_CONTEXT, (industry,), (actual_magic, proxy_only), ())
        by_ticker = {item.stock_score.stock.ticker: item for item in report.legend_strategy_scores}

        self.assertGreater(by_ticker["REALMAGIC"].greenblatt_score, by_ticker["PROXYMAGIC"].greenblatt_score)
        self.assertTrue(any("실제 ROIC" in reason for reason in by_ticker["REALMAGIC"].reasons))

    def test_legend_fisher_prefers_durable_growth_margin_and_roe(self):
        industry = INDUSTRIES[0]
        compounder = StockProfile(
            ticker="FISHER",
            name="Fisher Compounder",
            industry=industry.name,
            role="core",
            thesis="장기 성장성과 수익성이 좋은 후보입니다.",
            risks=(),
            fundamentals=Fundamentals(26.0, 30.0, 28.0, 20.0, 35.0, 28.0, 30_000_000_000),
        )
        weak = StockProfile(
            ticker="NOFISH",
            name="No Fisher",
            industry=industry.name,
            role="adjacent",
            thesis="성장과 자본효율이 약한 후보입니다.",
            risks=(),
            fundamentals=Fundamentals(1.0, 4.0, 3.0, 160.0, 18.0, 16.0, 30_000_000_000),
        )

        report = build_report(DEFAULT_MACRO_CONTEXT, (industry,), (compounder, weak), ())
        by_ticker = {item.stock_score.stock.ticker: item for item in report.legend_strategy_scores}

        self.assertGreater(by_ticker["FISHER"].fisher_score, by_ticker["NOFISH"].fisher_score)

    def test_legend_fisher_rewards_actual_rd_to_revenue(self):
        industry = INDUSTRIES[0]
        rd_leader = StockProfile(
            ticker="RDLEAD",
            name="R&D Leader",
            industry=industry.name,
            role="core",
            thesis="연구개발 투자 비중이 높은 성장 후보입니다.",
            risks=(),
            fundamentals=Fundamentals(
                18.0, 20.0, 18.0, 30.0, 30.0, 24.0, 20_000_000_000,
                rd_to_revenue_pct=16.0,
            ),
        )
        rd_laggard = StockProfile(
            ticker="RDLAG",
            name="R&D Laggard",
            industry=industry.name,
            role="core",
            thesis="기본 성장성은 비슷하지만 연구개발 투자 비중이 낮습니다.",
            risks=(),
            fundamentals=Fundamentals(
                18.0, 20.0, 18.0, 30.0, 30.0, 24.0, 20_000_000_000,
                rd_to_revenue_pct=0.5,
            ),
        )

        report = build_report(DEFAULT_MACRO_CONTEXT, (industry,), (rd_leader, rd_laggard), ())
        by_ticker = {item.stock_score.stock.ticker: item for item in report.legend_strategy_scores}

        self.assertGreater(by_ticker["RDLEAD"].fisher_score, by_ticker["RDLAG"].fisher_score)
        self.assertTrue(any("R&D/매출" in reason for reason in by_ticker["RDLEAD"].reasons))

    def test_report_api_serializes_legend_strategy_fields(self):
        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=INDUSTRIES[:1],
            stocks=STOCKS[:2],
            news_items=(),
            momentums={STOCKS[0].ticker.upper(): _strong_swing_momentum()},
            beneficiary_industries=BENEFICIARY_INDUSTRIES[:1],
        )

        with patch("stock_recommender.web._technical_by_ticker", return_value={}):
            payload = report_to_dict(report)

        self.assertIn("legendCandidates", payload)
        self.assertIn("beneficiaryIndustries", payload)
        self.assertEqual(payload["beneficiaryIndustries"][0]["sourceIndustry"], "AI 반도체 및 데이터센터")
        self.assertIn("timeHorizon", payload["beneficiaryIndustries"][0])
        self.assertIn("evidence", payload["beneficiaryIndustries"][0])
        self.assertIn("marketProxies", payload["beneficiaryIndustries"][0])
        self.assertIn("proxyMomentumScore", payload["beneficiaryIndustries"][0])
        self.assertIn("proxyCoveragePct", payload["beneficiaryIndustries"][0])
        self.assertIn("newsRecentScore", payload["beneficiaryIndustries"][0])
        self.assertIn("newsCoverageLabel", payload["beneficiaryIndustries"][0])
        self.assertIn("legendScores", payload["stocks"][0])
        self.assertIn("legendCompositeScore", payload["stocks"][0])
        self.assertIn("legendReasons", payload["stocks"][0])
        self.assertIn("legendWarnings", payload["stocks"][0])
        self.assertIn("roicCoveragePct", payload["dataQuality"])
        self.assertIn("evEbitCoveragePct", payload["dataQuality"])
        self.assertIn("rdCoveragePct", payload["dataQuality"])
        self.assertIn("roicPct", payload["stocks"][0]["fundamentals"])
        self.assertIn("evToEbit", payload["stocks"][0]["fundamentals"])
        self.assertIn("earningsYieldPct", payload["stocks"][0]["fundamentals"])
        self.assertIn("rdToRevenuePct", payload["stocks"][0]["fundamentals"])
        self.assertIn("shortTermCandidates", payload)
        self.assertIn("volumeScore", payload["shortTermCandidates"][0])
        self.assertIn("confidenceScore", payload["shortTermCandidates"][0])
        self.assertIn("setupLabel", payload["shortTermCandidates"][0])
        self.assertIn("confidenceScore", payload["mediumTermCandidates"][0])
        self.assertIn("confidenceLabel", payload["longTermCandidates"][0])


class ConfigTests(unittest.TestCase):
    def test_load_config_reads_dotenv(self):
        with TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        'SEC_USER_AGENT="stock-recommender test@example.com"',
                        "FRED_API_KEY=fred-key",
                        "STOCK_RECOMMENDER_UNIVERSE_MODE=curated",
                        "STOCK_RECOMMENDER_UNIVERSE_LIMIT=42",
                        "STOCK_RECOMMENDER_US_UNIVERSE_LIMIT=30",
                        "STOCK_RECOMMENDER_KR_UNIVERSE_LIMIT=12",
                        "STOCK_RECOMMENDER_US_FUNDAMENTAL_LIMIT=9",
                        "STOCK_RECOMMENDER_KR_FUNDAMENTAL_LIMIT=3",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(env_path)

        self.assertEqual(config.sec_user_agent, "stock-recommender test@example.com")
        self.assertEqual(config.fred_api_key, "fred-key")
        self.assertIn("SEC EDGAR", configured_source_names(config))
        self.assertIn("FRED", configured_source_names(config))
        self.assertIn("OpenDART", missing_optional_source_names(config))
        self.assertEqual(config.universe_mode, "curated")
        self.assertEqual(config.universe_limit, 42)
        self.assertEqual(config.us_universe_limit, 30)
        self.assertEqual(config.kr_universe_limit, 12)
        self.assertEqual(config.us_fundamental_limit, 9)
        self.assertEqual(config.kr_fundamental_limit, 3)

    def test_load_config_defaults_to_korea_timezone(self):
        with TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text("", encoding="utf-8")

            with patch.dict("os.environ", {"STOCK_RECOMMENDER_TIMEZONE": ""}):
                config = load_config(env_path)

        self.assertEqual(config.timezone_name, "Asia/Seoul")
        self.assertFalse(config.persist_repo_ledger)
        self.assertIsNone(config.full_snapshot_dir)


class UniverseLoaderTests(unittest.TestCase):
    def test_sec_ticker_map_creates_us_candidates_from_real_source(self):
        with TemporaryDirectory() as tmpdir:
            config = _test_app_config(tmpdir, universe_limit=2, us_universe_limit=2)
            cache = CacheStore(config.cache_db_path)
            quotes = {
                "AAA": _quote("AAA", "AAA Semiconductor", 30_000_000_000),
                "BBB": _quote("BBB", "BBB Health", 20_000_000_000),
                "MISS": {},
            }

            with (
                patch.object(universe_loader.SecEdgarClient, "fetch_ticker_map", return_value={"AAA": "1", "BBB": "2", "MISS": "3"}),
                patch.object(universe_loader.OpenDartClient, "fetch_corp_code_map") as dart_map,
                patch.object(universe_loader, "fetch_yahoo_quotes", side_effect=lambda tickers, **kwargs: {
                    ticker: quotes[ticker]
                    for ticker in tickers
                    if ticker in quotes and quotes[ticker]
                }),
            ):
                result = universe_loader.load_stock_universe(config, cache)

        dart_map.assert_not_called()
        self.assertEqual(result.candidate_count, 3)
        self.assertEqual(result.quote_ready_count, 2)
        self.assertEqual([stock.ticker for stock in result.stocks], ["AAA", "BBB"])
        self.assertEqual(result.stocks[0].industry, "AI 반도체 및 데이터센터")
        self.assertEqual(result.stocks[0].fundamentals.sources["marketCap"]["source"], "Yahoo Finance")
        self.assertTrue(any("OpenDART API 키" in warning for warning in result.warnings))

    def test_opendart_candidates_confirm_ks_or_kq_quote(self):
        with TemporaryDirectory() as tmpdir:
            config = _test_app_config(
                tmpdir,
                opendart_api_key="dart-key",
                universe_limit=3,
                us_universe_limit=0,
                kr_universe_limit=3,
            )
            cache = CacheStore(config.cache_db_path)

            def fake_quotes(tickers, **kwargs):
                symbols = set(tickers)
                payload = {}
                if "005930.KS" in symbols:
                    payload["005930.KS"] = _quote("005930.KS", "Samsung Electronics", 400_000_000_000_000, "KRW")
                if "123456.KQ" in symbols:
                    payload["123456.KQ"] = _quote("123456.KQ", "KQ Bio", 700_000_000_000, "KRW")
                return payload

            with (
                patch.object(universe_loader.SecEdgarClient, "fetch_ticker_map", return_value={}),
                patch.object(
                    universe_loader.OpenDartClient,
                    "fetch_corp_code_map",
                    return_value=type("Response", (), {
                        "ok": True,
                        "source": "OpenDART",
                        "payload": {
                            "005930": {"corp_code": "00126380", "corp_name": "삼성전자"},
                            "123456": {"corp_code": "00999999", "corp_name": "코스닥바이오"},
                            "654321": {"corp_code": "00888888", "corp_name": "가격없음"},
                        },
                        "warning": None,
                    })(),
                ),
                patch.object(universe_loader, "fetch_yahoo_quotes", side_effect=fake_quotes),
            ):
                result = universe_loader.load_stock_universe(config, cache)

        self.assertEqual(result.candidate_count, 3)
        self.assertEqual(result.quote_ready_count, 2)
        self.assertEqual({stock.ticker for stock in result.stocks}, {"005930.KS", "123456.KQ"})
        self.assertTrue(all(stock.country == "KR" for stock in result.stocks))
        self.assertEqual({stock.dart_stock_code for stock in result.stocks}, {"005930", "123456"})

    def test_data_coverage_gate_caps_source_less_financials(self):
        cap, cautions = data_coverage_gate_for_stock(Fundamentals(market_cap=100, pe=20))

        self.assertEqual(cap, 58.0)
        self.assertIn("공식 재무 데이터가 없어", cautions[0])

    def test_screened_pipeline_uses_mocked_universe_and_reports_stats(self):
        industry = INDUSTRIES[0]
        stock = StockProfile(
            ticker="AAA",
            name="AAA Semiconductor",
            industry=industry.name,
            role="core",
            thesis="실제 소스 기반 후보",
            risks=(),
            fundamentals=Fundamentals(
                market_cap=30_000_000_000,
                sources={"marketCap": {"source": "Yahoo Finance"}},
            ),
        )
        universe_result = universe_loader.UniverseLoadResult(
            stocks=(stock,),
            candidate_count=3,
            quote_ready_count=1,
            financial_target_count=1,
        )

        with TemporaryDirectory() as tmpdir:
            with (
                patch("stock_recommender.pipeline.load_stock_universe", return_value=universe_result),
                patch("stock_recommender.pipeline.SCREENED_INDUSTRIES", (industry,)),
                patch("stock_recommender.pipeline.fetch_macro_snapshot", return_value=MacroSnapshot()),
                patch("stock_recommender.pipeline.fetch_news", return_value=()),
                patch("stock_recommender.pipeline.enrich_with_live_market_data", side_effect=lambda stocks, **kwargs: tuple(stocks)),
                patch("stock_recommender.pipeline.fetch_many_momentums", return_value={}),
                patch("stock_recommender.pipeline.SecEdgarClient") as sec_client,
                patch("stock_recommender.pipeline.OpenDartFinancialClient") as dart_client,
                patch("stock_recommender.pipeline.load_config", return_value=_test_app_config(tmpdir, universe_limit=1, us_fundamental_limit=1)),
            ):
                sec_client.return_value.enrich_stocks.return_value = type("Result", (), {
                    "stocks": (stock,),
                    "updated_count": 0,
                    "warnings": (),
                })()
                dart_client.return_value.enrich_stocks.return_value = type("Result", (), {
                    "stocks": (),
                    "updated_count": 0,
                    "warnings": (),
                })()
                report = create_recommendation_report()

        self.assertEqual(report.data_quality.universe_mode, "screened")
        self.assertEqual(report.data_quality.universe_candidate_count, 3)
        self.assertEqual(report.data_quality.universe_quote_ready_count, 1)
        self.assertEqual(report.data_quality.universe_final_count, 1)
        self.assertLessEqual(report.stock_scores[0].score, 58.0)

    def test_curated_pipeline_mode_keeps_existing_universe(self):
        with TemporaryDirectory() as tmpdir:
            config = _test_app_config(tmpdir, universe_mode="curated", universe_limit=2)
            cache = CacheStore(config.cache_db_path)
            result = universe_loader.load_stock_universe(config, cache)

        self.assertEqual(result.candidate_count, len(STOCKS))
        self.assertEqual(len(result.stocks), 2)
        self.assertEqual(result.quote_ready_count, 2)


class SecEdgarTests(unittest.TestCase):
    def test_extract_fundamentals_from_companyfacts(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {
                            "USD": [
                                _annual_fact("2023-01-01", "2023-12-31", "2024-02-01", 100),
                                _annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 125),
                            ]
                        }
                    },
                    "OperatingIncomeLoss": {
                        "units": {"USD": [_annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 25)]}
                    },
                    "NetIncomeLoss": {
                        "units": {"USD": [_annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 18)]}
                    },
                    "DepreciationDepletionAndAmortization": {
                        "units": {"USD": [_annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 7)]}
                    },
                    "NetCashProvidedByUsedInOperatingActivities": {
                        "units": {"USD": [_annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 30)]}
                    },
                    "PaymentsToAcquirePropertyPlantAndEquipment": {
                        "units": {"USD": [_annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 8)]}
                    },
                    "Liabilities": {
                        "units": {"USD": [_annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 60)]}
                    },
                    "AssetsCurrent": {
                        "units": {"USD": [_annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 50)]}
                    },
                    "LiabilitiesCurrent": {
                        "units": {"USD": [_annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 25)]}
                    },
                    "InterestExpenseNonOperating": {
                        "units": {"USD": [_annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 5)]}
                    },
                    "CashAndCashEquivalentsAtCarryingValue": {
                        "units": {"USD": [_annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 20)]}
                    },
                    "LongTermDebtCurrent": {
                        "units": {"USD": [_annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 10)]}
                    },
                    "LongTermDebtNoncurrent": {
                        "units": {"USD": [_annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 30)]}
                    },
                    "IncomeLossFromContinuingOperationsBeforeIncomeTaxes": {
                        "units": {"USD": [_annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 23)]}
                    },
                    "IncomeTaxExpenseBenefit": {
                        "units": {"USD": [_annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 5)]}
                    },
                    "ResearchAndDevelopmentExpense": {
                        "units": {"USD": [_annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 9)]}
                    },
                    "StockholdersEquity": {
                        "units": {
                            "USD": [
                                _annual_fact("2023-12-31", "2023-12-31", "2024-02-01", 80),
                                _annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 90),
                            ]
                        }
                    },
                }
            }
        }

        fundamentals = extract_fundamentals(facts, fallback=Fundamentals(market_cap=200))

        self.assertAlmostEqual(fundamentals.revenue_growth_pct, 25.0)
        self.assertAlmostEqual(fundamentals.operating_margin_pct, 20.0)
        self.assertAlmostEqual(fundamentals.roe_pct, 18 / 85 * 100)
        self.assertAlmostEqual(fundamentals.debt_to_equity_pct, 40 / 90 * 100)
        self.assertEqual(fundamentals.revenue, 125)
        self.assertEqual(fundamentals.operating_income, 25)
        self.assertEqual(fundamentals.ebitda, 32)
        self.assertEqual(fundamentals.net_income, 18)
        self.assertEqual(fundamentals.operating_cash_flow, 30)
        self.assertEqual(fundamentals.capital_expenditure, 8)
        self.assertEqual(fundamentals.free_cash_flow, 22)
        self.assertAlmostEqual(fundamentals.current_ratio_pct, 200.0)
        self.assertAlmostEqual(fundamentals.interest_coverage, 5.0)
        self.assertEqual(fundamentals.cash_and_equivalents, 20)
        self.assertEqual(fundamentals.total_debt, 40)
        self.assertEqual(fundamentals.pretax_income, 23)
        self.assertEqual(fundamentals.income_tax_expense, 5)
        self.assertEqual(fundamentals.research_and_development, 9)
        self.assertEqual(fundamentals.enterprise_value, 220)
        self.assertAlmostEqual(fundamentals.roic_pct, 25 * (1 - 5 / 23) / (40 + 90 - 20) * 100)
        self.assertAlmostEqual(fundamentals.ev_to_ebit, 220 / 25)
        self.assertAlmostEqual(fundamentals.earnings_yield_pct, 25 / 220 * 100)
        self.assertAlmostEqual(fundamentals.rd_to_revenue_pct, 9 / 125 * 100)
        self.assertEqual(fundamentals.sources["revenue"]["source"], "SEC EDGAR")
        self.assertEqual(fundamentals.sources["revenue"]["periodEnd"], "2024-12-31")
        self.assertEqual(fundamentals.sources["revenue"]["filed"], "2025-02-01")
        self.assertEqual(fundamentals.sources["freeCashFlow"]["derivedFrom"], ["operatingCashFlow", "capitalExpenditure"])
        self.assertEqual(fundamentals.sources["roic"]["derivedFrom"][0], "operatingIncome")
        self.assertFalse(fundamentals.sources["roic"]["taxRateDefault"])

    def test_extract_fundamentals_uses_period_not_filed_date(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {
                            "USD": [
                                _annual_fact("2023-01-01", "2023-12-31", "2025-02-01", 100),
                                _annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 130),
                                _annual_fact("2024-01-01", "2024-12-31", "2026-02-01", 150),
                            ]
                        }
                    }
                }
            }
        }

        fundamentals = extract_fundamentals(facts)

        self.assertEqual(fundamentals.revenue, 150)
        self.assertAlmostEqual(fundamentals.revenue_growth_pct, 50.0)

    def test_extract_fundamentals_calculates_growth_quality_series(self):
        annual_revenue = [
            _annual_fact("2019-01-01", "2019-12-31", "2020-02-01", 100),
            _annual_fact("2020-01-01", "2020-12-31", "2021-02-01", 120),
            _annual_fact("2021-01-01", "2021-12-31", "2022-02-01", 140),
            _annual_fact("2022-01-01", "2022-12-31", "2023-02-01", 165),
            _annual_fact("2023-01-01", "2023-12-31", "2024-02-01", 180),
            _annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 200),
            _annual_fact("2024-01-01", "2024-12-31", "2026-02-01", 220),
        ]
        annual_operating_income = [
            _annual_fact("2019-01-01", "2019-12-31", "2020-02-01", 10),
            _annual_fact("2020-01-01", "2020-12-31", "2021-02-01", 14),
            _annual_fact("2021-01-01", "2021-12-31", "2022-02-01", 18),
            _annual_fact("2022-01-01", "2022-12-31", "2023-02-01", 25),
            _annual_fact("2023-01-01", "2023-12-31", "2024-02-01", 30),
            _annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 44),
        ]
        quarterly_revenue = [
            _quarter_fact("2024-01-01", "2024-03-31", "2024-05-01", 100, "Q1", 2024),
            _quarter_fact("2024-04-01", "2024-06-30", "2024-08-01", 110, "Q2", 2024),
            _quarter_fact("2025-01-01", "2025-03-31", "2025-05-01", 130, "Q1", 2025),
            _quarter_fact("2025-04-01", "2025-06-30", "2025-08-01", 160, "Q2", 2025),
            _quarter_fact("2025-01-01", "2025-06-30", "2025-08-15", 999, "Q2", 2025),
        ]
        quarterly_operating_income = [
            _quarter_fact("2024-01-01", "2024-03-31", "2024-05-01", 20, "Q1", 2024),
            _quarter_fact("2024-04-01", "2024-06-30", "2024-08-01", 22, "Q2", 2024),
            _quarter_fact("2025-01-01", "2025-03-31", "2025-05-01", 35, "Q1", 2025),
            _quarter_fact("2025-04-01", "2025-06-30", "2025-08-01", 50, "Q2", 2025),
        ]
        facts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": [*annual_revenue, *quarterly_revenue]}
                    },
                    "OperatingIncomeLoss": {
                        "units": {"USD": [*annual_operating_income, *quarterly_operating_income]}
                    },
                }
            }
        }

        fundamentals = extract_fundamentals(facts)

        self.assertEqual(fundamentals.revenue, 220)
        self.assertAlmostEqual(fundamentals.revenue_cagr_3y_pct, ((220 / 140) ** (1 / 3) - 1) * 100)
        self.assertAlmostEqual(fundamentals.revenue_cagr_5y_pct, ((220 / 100) ** (1 / 5) - 1) * 100)
        self.assertAlmostEqual(fundamentals.operating_income_growth_pct, ((44 / 30) - 1) * 100)
        self.assertAlmostEqual(fundamentals.operating_leverage_spread_pct, fundamentals.operating_income_growth_pct - fundamentals.revenue_growth_pct)
        self.assertAlmostEqual(fundamentals.latest_quarter_revenue_yoy_pct, ((160 / 110) - 1) * 100)
        self.assertAlmostEqual(fundamentals.latest_quarter_operating_income_yoy_pct, ((50 / 22) - 1) * 100)
        self.assertEqual(fundamentals.quarterly_revenue_yoy_streak, 2)
        self.assertEqual(len(fundamentals.annual_financials), 5)
        self.assertLess(fundamentals.quarterly_financials[0]["revenue"], 999)

    def test_extract_fundamentals_uses_default_tax_rate_when_tax_data_missing(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": [_annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 500)]}
                    },
                    "OperatingIncomeLoss": {
                        "units": {"USD": [_annual_fact("2024-01-01", "2024-12-31", "2025-02-01", 100)]}
                    },
                    "CashAndCashEquivalentsAtCarryingValue": {
                        "units": {"USD": [_annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 20)]}
                    },
                    "LongTermDebtCurrent": {
                        "units": {"USD": [_annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 10)]}
                    },
                    "LongTermDebtNoncurrent": {
                        "units": {"USD": [_annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 20)]}
                    },
                    "StockholdersEquity": {
                        "units": {"USD": [_annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 120)]}
                    },
                }
            }
        }

        fundamentals = extract_fundamentals(facts, fallback=Fundamentals(market_cap=600))

        self.assertAlmostEqual(fundamentals.roic_pct, 100 * (1 - 0.21) / (30 + 120 - 20) * 100)
        self.assertTrue(fundamentals.sources["roic"]["taxRateDefault"])
        self.assertEqual(fundamentals.sources["roic"]["defaultTaxRate"], 0.21)


class OpenDartTests(unittest.TestCase):
    def test_extract_opendart_fundamentals(self):
        payload = {
            "status": "000",
            "list": [
                _dart_row("CFS", "매출액", "125,000", "100,000"),
                _dart_row("CFS", "영업이익", "25,000", "18,000"),
                _dart_row("CFS", "당기순이익", "17,000", "14,000"),
                _dart_row("CFS", "감가상각비", "7,000", "6,000"),
                _dart_row("CFS", "영업활동현금흐름", "30,000", "22,000"),
                _dart_row("CFS", "유형자산의 취득", "-8,000", "-7,000"),
                _dart_row("CFS", "부채총계", "60,000", "55,000"),
                _dart_row("CFS", "자본총계", "90,000", "80,000"),
                _dart_row("CFS", "유동자산", "50,000", "45,000"),
                _dart_row("CFS", "유동부채", "25,000", "24,000"),
                _dart_row("CFS", "이자비용", "5,000", "4,500"),
                _dart_row("CFS", "현금및현금성자산", "20,000", "18,000"),
                _dart_row("CFS", "단기차입금", "10,000", "8,000"),
                _dart_row("CFS", "장기차입금", "30,000", "28,000"),
                _dart_row("CFS", "법인세비용차감전순이익", "22,000", "19,000"),
                _dart_row("CFS", "법인세비용", "5,000", "4,000"),
                _dart_row("CFS", "연구개발비", "9,000", "7,000"),
            ],
        }

        fundamentals = extract_opendart_fundamentals(
            payload,
            fallback=Fundamentals(market_cap=200_000, market_cap_currency="KRW"),
        )

        self.assertAlmostEqual(fundamentals.revenue_growth_pct, 25.0)
        self.assertAlmostEqual(fundamentals.operating_margin_pct, 20.0)
        self.assertAlmostEqual(fundamentals.roe_pct, 17_000 / 85_000 * 100)
        self.assertAlmostEqual(fundamentals.debt_to_equity_pct, 40_000 / 90_000 * 100)
        self.assertEqual(fundamentals.market_cap_currency, "KRW")
        self.assertEqual(fundamentals.revenue, 125_000)
        self.assertEqual(fundamentals.operating_income, 25_000)
        self.assertEqual(fundamentals.ebitda, 32_000)
        self.assertEqual(fundamentals.net_income, 17_000)
        self.assertEqual(fundamentals.operating_cash_flow, 30_000)
        self.assertEqual(fundamentals.capital_expenditure, 8_000)
        self.assertEqual(fundamentals.free_cash_flow, 22_000)
        self.assertAlmostEqual(fundamentals.current_ratio_pct, 200.0)
        self.assertAlmostEqual(fundamentals.interest_coverage, 5.0)
        self.assertEqual(fundamentals.cash_and_equivalents, 20_000)
        self.assertEqual(fundamentals.total_debt, 40_000)
        self.assertEqual(fundamentals.pretax_income, 22_000)
        self.assertEqual(fundamentals.income_tax_expense, 5_000)
        self.assertEqual(fundamentals.research_and_development, 9_000)
        self.assertEqual(fundamentals.enterprise_value, 220_000)
        self.assertAlmostEqual(
            fundamentals.roic_pct,
            25_000 * (1 - 5_000 / 22_000) / (40_000 + 90_000 - 20_000) * 100,
        )
        self.assertAlmostEqual(fundamentals.ev_to_ebit, 220_000 / 25_000)
        self.assertAlmostEqual(fundamentals.earnings_yield_pct, 25_000 / 220_000 * 100)
        self.assertAlmostEqual(fundamentals.rd_to_revenue_pct, 9_000 / 125_000 * 100)
        self.assertEqual(fundamentals.sources["revenue"]["source"], "OpenDART")
        self.assertEqual(fundamentals.sources["revenue"]["reportCode"], "11011")
        self.assertFalse(fundamentals.sources["roic"]["taxRateDefault"])

    def test_extract_opendart_fundamentals_calculates_growth_quality_series(self):
        annual_payloads = (
            _dart_payload(2024, "11011", 220_000, 44_000),
            _dart_payload(2023, "11011", 180_000, 30_000),
            _dart_payload(2022, "11011", 165_000, 25_000),
            _dart_payload(2021, "11011", 140_000, 18_000),
            _dart_payload(2020, "11011", 120_000, 14_000),
            _dart_payload(2019, "11011", 100_000, 10_000),
        )
        quarterly_payloads = (
            _dart_payload(2025, "11013", 130_000, 35_000),
            _dart_payload(2025, "11012", 290_000, 85_000),
            _dart_payload(2024, "11013", 100_000, 20_000),
            _dart_payload(2024, "11012", 210_000, 42_000),
        )

        fundamentals = extract_opendart_fundamentals(
            annual_payloads[0],
            fallback=Fundamentals(market_cap_currency="KRW"),
            annual_payloads=annual_payloads,
            quarterly_payloads=quarterly_payloads,
        )

        self.assertAlmostEqual(fundamentals.revenue_cagr_3y_pct, ((220_000 / 140_000) ** (1 / 3) - 1) * 100)
        self.assertAlmostEqual(fundamentals.revenue_cagr_5y_pct, ((220_000 / 100_000) ** (1 / 5) - 1) * 100)
        self.assertAlmostEqual(fundamentals.operating_income_growth_pct, ((44_000 / 30_000) - 1) * 100)
        self.assertAlmostEqual(fundamentals.latest_quarter_revenue_yoy_pct, ((160_000 / 110_000) - 1) * 100)
        self.assertAlmostEqual(fundamentals.latest_quarter_operating_income_yoy_pct, ((50_000 / 22_000) - 1) * 100)
        self.assertEqual(fundamentals.quarterly_revenue_yoy_streak, 2)
        self.assertEqual(fundamentals.quarterly_financials[0]["fiscalPeriod"], "Q2")


class MarketDataSourceTests(unittest.TestCase):
    def test_yahoo_quote_updates_market_cap_and_source(self):
        stock = StockProfile(
            ticker="TEST",
            name="Test Co",
            industry=INDUSTRIES[0].name,
            role="adjacent",
            thesis="test",
            risks=(),
            fundamentals=Fundamentals(pe=30, forward_pe=25, market_cap=1_000_000),
        )
        original_fetch = data_sources.fetch_yahoo_quotes

        try:
            data_sources.fetch_yahoo_quotes = lambda tickers, timeout=8.0, cache=None: {
                "TEST": {"trailingPE": 20, "forwardPE": 18, "marketCap": 2_000_000, "currency": "USD"}
            }
            enriched = data_sources.enrich_with_live_market_data((stock,))
        finally:
            data_sources.fetch_yahoo_quotes = original_fetch

        fundamentals = enriched[0].fundamentals
        self.assertEqual(fundamentals.market_cap, 2_000_000)
        self.assertEqual(fundamentals.sources["marketCap"]["source"], "Yahoo Finance")
        self.assertEqual(fundamentals.sources["pe"]["source"], "Yahoo Finance")


class DecisionGradeTests(unittest.TestCase):
    def test_decision_grade_penalizes_high_risk(self):
        self.assertEqual(decision_grade_for_stock(78, 70, 70, 70, "낮음"), "매수 후보")
        self.assertEqual(decision_grade_for_stock(78, 70, 70, 70, "높음"), "관심")


class BacktestTests(unittest.TestCase):
    def test_run_backtest_computes_monthly_results(self):
        stocks = tuple(stock for stock in STOCKS if stock.ticker in {"NVDA", "NVO", "LMT"})
        histories = {
            "NVDA": _history(100, 0.006),
            "NVO": _history(100, 0.002),
            "LMT": _history(100, 0.001),
            "SPY": _history(100, 0.001),
            "QQQ": _history(100, 0.0015),
            "^KS11": _history(100, 0.0005),
        }

        result = run_backtest(
            stocks=stocks,
            industries=INDUSTRIES,
            histories=histories,
            months=6,
            top_n=3,
            benchmark_ticker="SPY",
        )

        self.assertGreater(result.period_count, 0)
        self.assertIsNotNone(result.strategy_return_pct)
        self.assertIsNotNone(result.benchmark_return_pct)
        self.assertGreater(result.data_coverage_pct, 90)
        self.assertIn("NVDA", result.periods[-1].tickers)

    def test_snapshot_backtest_uses_saved_snapshot_rankings(self):
        histories = {
            "AAA": _monthly_history(
                {
                    date(2025, 1, 31): 100,
                    date(2025, 2, 28): 110,
                    date(2025, 3, 31): 90,
                    date(2025, 4, 30): 120,
                }
            ),
            "BBB": _monthly_history(
                {
                    date(2025, 1, 31): 100,
                    date(2025, 2, 28): 90,
                    date(2025, 3, 31): 120,
                    date(2025, 4, 30): 100,
                }
            ),
            "SPY": _monthly_history(
                {
                    date(2025, 1, 31): 100,
                    date(2025, 2, 28): 101,
                    date(2025, 3, 31): 102,
                    date(2025, 4, 30): 103,
                }
            ),
            "QQQ": _monthly_history(
                {
                    date(2025, 1, 31): 100,
                    date(2025, 2, 28): 101,
                    date(2025, 3, 31): 102,
                    date(2025, 4, 30): 103,
                }
            ),
            "^KS11": _monthly_history(
                {
                    date(2025, 1, 31): 100,
                    date(2025, 2, 28): 101,
                    date(2025, 3, 31): 102,
                    date(2025, 4, 30): 103,
                }
            ),
        }
        snapshots = (
            _snapshot(date(2025, 1, 31), "AAA", "Alpha"),
            _snapshot(date(2025, 2, 28), "BBB", "Beta"),
            _snapshot(date(2025, 3, 31), "AAA", "Alpha"),
        )

        result = run_snapshot_backtest(
            snapshots=snapshots,
            histories=histories,
            months=3,
            top_n=1,
            benchmark_ticker="SPY",
            created_at=datetime(2025, 5, 1, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            timezone_name="Asia/Seoul",
        )
        payload = backtest_to_dict(result)

        self.assertTrue(result.point_in_time)
        self.assertEqual(result.method, "snapshot")
        self.assertEqual(result.periods[0].tickers, ("AAA",))
        self.assertEqual(result.periods[1].tickers, ("BBB",))
        self.assertEqual(payload["periods"][0]["snapshotDate"], "2025-01-31")

    def test_rules_backtest_uses_target_weights_and_risk_gate(self):
        snapshots = (
            _rules_snapshot_with_anchors(
                date(2025, 1, 31),
                stocks=[
                    ("AAA", "Alpha", 100, "매수 후보", "Pass", 8.0),
                    ("BBB", "Beta", 100, "관심", "Pass", 4.0),
                    ("CCC", "Crash", 100, "매수 후보", "Hard Fail", 8.0),
                ],
                spy_close=100,
            ),
            _rules_snapshot_with_anchors(
                date(2025, 2, 28),
                stocks=[
                    ("AAA", "Alpha", 120, "매수 후보", "Pass", 8.0),
                    ("BBB", "Beta", 100, "관심", "Pass", 4.0),
                    ("CCC", "Crash", 200, "매수 후보", "Hard Fail", 8.0),
                ],
                spy_close=100,
            ),
        )

        result = run_snapshot_backtest(
            snapshots=snapshots,
            histories={},
            months=1,
            top_n=2,
            benchmark_ticker="SPY",
            created_at=datetime(2025, 3, 1, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            timezone_name="Asia/Seoul",
            method="rules",
        )
        payload = backtest_to_dict(result)

        self.assertEqual(result.method, "rules")
        self.assertEqual(result.periods[0].tickers, ("AAA", "BBB"))
        self.assertEqual(result.periods[0].weights_pct, (8.0, 4.0))
        self.assertAlmostEqual(result.periods[0].return_pct, 13.33, places=2)
        self.assertEqual(payload["periods"][0]["weightsPct"], [8.0, 4.0])

    def test_snapshot_backtest_uses_horizon_candidate_collections(self):
        snapshots = (
            _term_snapshot_with_anchors(
                date(2025, 1, 31),
                stocks=[("AAA", "Alpha", 100), ("BBB", "Beta", 100)],
                short_candidates=[("BBB", "Beta")],
                spy_close=100,
            ),
            _term_snapshot_with_anchors(
                date(2025, 2, 28),
                stocks=[("AAA", "Alpha", 110), ("BBB", "Beta", 130)],
                short_candidates=[("BBB", "Beta")],
                spy_close=102,
            ),
        )

        short_result = run_snapshot_backtest(
            snapshots=snapshots,
            histories={},
            months=1,
            top_n=1,
            benchmark_ticker="SPY",
            created_at=datetime(2025, 3, 1, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            timezone_name="Asia/Seoul",
            horizon="short",
        )
        overall_result = run_snapshot_backtest(
            snapshots=snapshots,
            histories={},
            months=1,
            top_n=1,
            benchmark_ticker="SPY",
            created_at=datetime(2025, 3, 1, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            timezone_name="Asia/Seoul",
            horizon="unknown",
        )

        self.assertEqual(short_result.periods[0].tickers, ("BBB",))
        self.assertEqual(short_result.horizon, "short")
        self.assertEqual(backtest_to_dict(short_result)["horizon"], "short")
        self.assertEqual(overall_result.periods[0].tickers, ("AAA",))
        self.assertEqual(overall_result.horizon, "overall")

    def test_snapshot_backtest_prefers_snapshot_price_anchors(self):
        snapshots = (
            _snapshot_with_anchors(date(2025, 1, 31), [("AAA", "Alpha", 100)], spy_close=100),
            _snapshot_with_anchors(date(2025, 2, 28), [("BBB", "Beta", 90), ("AAA", "Alpha", 120)], spy_close=110),
            _snapshot_with_anchors(date(2025, 3, 31), [("AAA", "Alpha", 130), ("BBB", "Beta", 99)], spy_close=121),
        )

        result = run_snapshot_backtest(
            snapshots=snapshots,
            histories={},
            months=2,
            top_n=1,
            benchmark_ticker="SPY",
            created_at=datetime(2025, 4, 1, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            timezone_name="Asia/Seoul",
        )
        payload = backtest_to_dict(result)

        self.assertEqual(result.period_count, 2)
        self.assertEqual(result.price_source, "snapshotAnchors")
        self.assertEqual(result.periods[0].tickers, ("AAA",))
        self.assertEqual(result.periods[0].return_pct, 20.0)
        self.assertEqual(payload["priceSource"], "snapshotAnchors")
        self.assertEqual(payload["periods"][0]["priceSource"], "snapshotAnchors")
        self.assertEqual(payload["periods"][0]["anchorCoveragePct"], 100.0)
        self.assertEqual(payload["periods"][0]["periodStatus"], "included")

    def test_snapshot_backtest_excludes_low_quality_v10_snapshots(self):
        snapshots = (
            _snapshot_v10_with_anchors(date(2025, 1, 31), [("AAA", "Alpha", 100)], benchmarks={"SPY": 100}),
            _snapshot_v10_with_anchors(date(2025, 2, 28), [("AAA", "Alpha", 110)], benchmarks={"SPY": 101}),
        )

        result = run_snapshot_backtest(
            snapshots=snapshots,
            histories={},
            months=1,
            top_n=1,
            benchmark_ticker="SPY",
            created_at=datetime(2025, 3, 1, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            timezone_name="Asia/Seoul",
        )

        self.assertEqual(result.periods, ())
        self.assertTrue(any("품질 기준 미달" in warning for warning in result.warnings))
        self.assertTrue(any("benchmarkAnchorCoverageBelow100" in warning for warning in result.warnings))

    def test_snapshot_backtest_anchor_only_uses_month_end_snapshots(self):
        snapshots = (
            _snapshot_v10_with_anchors(date(2025, 1, 15), [("AAA", "Alpha", 90)], benchmarks={"SPY": 90, "QQQ": 90, "^KS11": 90}),
            _snapshot_v10_with_anchors(date(2025, 1, 31), [("AAA", "Alpha", 100)], benchmarks={"SPY": 100, "QQQ": 100, "^KS11": 100}),
            _snapshot_v10_with_anchors(date(2025, 2, 15), [("AAA", "Alpha", 105)], benchmarks={"SPY": 105, "QQQ": 105, "^KS11": 105}),
            _snapshot_v10_with_anchors(date(2025, 2, 28), [("AAA", "Alpha", 120)], benchmarks={"SPY": 110, "QQQ": 110, "^KS11": 110}),
            _snapshot_v10_with_anchors(date(2025, 3, 15), [("AAA", "Alpha", 125)], benchmarks={"SPY": 115, "QQQ": 115, "^KS11": 115}),
            _snapshot_v10_with_anchors(date(2025, 3, 31), [("AAA", "Alpha", 132)], benchmarks={"SPY": 121, "QQQ": 121, "^KS11": 121}),
        )

        result = run_snapshot_backtest(
            snapshots=snapshots,
            histories={},
            months=2,
            top_n=1,
            benchmark_ticker="SPY",
            created_at=datetime(2025, 4, 1, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            timezone_name="Asia/Seoul",
        )

        self.assertEqual(result.period_count, 2)
        self.assertEqual(result.periods[0].start_date, date(2025, 1, 31))
        self.assertEqual(result.periods[0].end_date, date(2025, 2, 28))
        self.assertEqual(result.periods[1].start_date, date(2025, 2, 28))
        self.assertEqual(result.periods[1].end_date, date(2025, 3, 31))

    def test_snapshot_backtest_returns_clear_warning_when_snapshots_are_missing(self):
        result = run_snapshot_backtest(
            snapshots=(),
            histories={},
            months=3,
            top_n=1,
            benchmark_ticker="SPY",
            created_at=datetime(2025, 5, 1, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            timezone_name="Asia/Seoul",
        )

        self.assertTrue(result.point_in_time)
        self.assertEqual(result.method, "snapshot")
        self.assertEqual(result.periods, ())
        self.assertIn("스냅샷이 부족", result.warnings[0])


class SnapshotTests(unittest.TestCase):
    def test_snapshot_payload_and_daily_upsert(self):
        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=INDUSTRIES,
            stocks=STOCKS,
            news_items=(),
        )
        payload = report_to_snapshot_payload(report, mode="live")

        with TemporaryDirectory() as tmpdir:
            cache = CacheStore(Path(tmpdir) / "cache.sqlite")
            first_id = cache.save_recommendation_snapshot(
                snapshot_date=payload["snapshotDate"],
                mode="live",
                top_ticker=payload["stocks"][0]["ticker"],
                top_name=payload["stocks"][0]["name"],
                top_score=payload["stocks"][0]["score"],
                payload=payload,
            )
            second_id = cache.save_recommendation_snapshot(
                snapshot_date=payload["snapshotDate"],
                mode="live",
                top_ticker=payload["stocks"][0]["ticker"],
                top_name=payload["stocks"][0]["name"],
                top_score=payload["stocks"][0]["score"],
                payload=payload,
            )
            rows = cache.list_recommendation_snapshots()

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["stocks"][0]["ticker"], payload["stocks"][0]["ticker"])
        self.assertIn("decisionGrade", rows[0]["payload"]["stocks"][0])
        self.assertIn("earlyGrowthCandidates", rows[0]["payload"])
        self.assertIn("shortTermCandidates", rows[0]["payload"])
        self.assertIn("mediumTermCandidates", rows[0]["payload"])
        self.assertIn("longTermCandidates", rows[0]["payload"])

    def test_snapshot_payload_uses_current_timezone_and_audit_fields(self):
        ticker = STOCKS[0].ticker.upper()
        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=INDUSTRIES,
            stocks=STOCKS[:1],
            news_items=(),
            momentums={
                ticker: Momentum(
                    1.0,
                    2.0,
                    3.0,
                    -4.0,
                    70.0,
                    latest_close=123.4,
                    latest_close_date="2026-05-17",
                    six_month_high=130,
                    six_month_low=90,
                    source="Yahoo Finance",
                ),
                "SPY": Momentum(latest_close=500, latest_close_date="2026-05-17", source="Yahoo Finance"),
            },
            source_events=(
                {
                    "source": "Yahoo Finance",
                    "eventType": "error",
                    "message": "failed https://example.com/path?api_key=SECRET12345678901234567890",
                    "createdAt": "2026-05-17T21:30:00+00:00",
                    "metadata": {"url": "https://example.com/SECRET12345678901234567890?token=SECRET12345678901234567890"},
                },
            ),
            data_quality=DataQuality(
                warnings=("failed token=SECRET12345678901234567890",),
            ),
            created_at=datetime(2026, 5, 18, 6, 30, tzinfo=ZoneInfo("Asia/Seoul")),
            beneficiary_industries=BENEFICIARY_INDUSTRIES[:1],
        )

        payload = report_to_snapshot_payload(report, mode="live")

        self.assertEqual(payload["version"], 16)
        self.assertEqual(payload["snapshotDate"], "2026-05-18")
        self.assertEqual(payload["createdAtTimezone"], "Asia/Seoul")
        self.assertIn("gitCommit", payload["audit"])
        self.assertEqual(payload["sourceEventSummary"]["errorCount"], 1)
        self.assertNotIn("SECRET12345678901234567890", payload["sourceEvents"][0]["message"])
        self.assertNotIn("SECRET12345678901234567890", payload["sourceEvents"][0]["metadata"]["url"])
        self.assertNotIn("SECRET12345678901234567890", payload["dataQuality"]["warnings"][0])
        self.assertIn("marketCap", payload["stocks"][0]["fundamentals"])
        self.assertIn("marketCapUsd", payload["stocks"][0]["fundamentals"])
        self.assertIn("roicCoveragePct", payload["dataQuality"])
        self.assertIn("evEbitCoveragePct", payload["dataQuality"])
        self.assertIn("rdCoveragePct", payload["dataQuality"])
        self.assertIn("roicPct", payload["stocks"][0]["fundamentals"])
        self.assertIn("evToEbit", payload["stocks"][0]["fundamentals"])
        self.assertIn("earningsYieldPct", payload["stocks"][0]["fundamentals"])
        self.assertIn("rdToRevenuePct", payload["stocks"][0]["fundamentals"])
        self.assertIn("fundamentalSources", payload["stocks"][0])
        self.assertIn("legendCandidates", payload)
        self.assertIn("beneficiaryIndustries", payload)
        self.assertEqual(payload["beneficiaryIndustries"][0]["sourceIndustry"], "AI 반도체 및 데이터센터")
        self.assertIn("displaySummary", payload["beneficiaryIndustries"][0])
        self.assertIn("marketProxies", payload["beneficiaryIndustries"][0])
        self.assertIn("proxyMomentumScore", payload["beneficiaryIndustries"][0])
        self.assertIn("proxyCoveragePct", payload["beneficiaryIndustries"][0])
        self.assertIn("newsAccelerationScore", payload["beneficiaryIndustries"][0])
        self.assertIn("newsTopSources", payload["beneficiaryIndustries"][0])
        self.assertIn("legendScores", payload["stocks"][0])
        self.assertIn("legendCompositeScore", payload["stocks"][0])
        self.assertEqual(payload["stocks"][0]["momentumRaw"]["latestClose"], 123.4)
        self.assertEqual(payload["stocks"][0]["priceAnchor"]["latestClose"], 123.4)
        self.assertEqual(payload["benchmarks"][0]["priceAnchor"]["latestClose"], 500)
        self.assertEqual(payload["priceAnchors"][0]["priceAnchor"]["latestClose"], 123.4)
        self.assertIn("snapshotQuality", payload)
        self.assertIn("volumeScore", payload["shortTermCandidates"][0])
        self.assertIn("confidenceScore", payload["shortTermCandidates"][0])
        self.assertIn("setupLabel", payload["shortTermCandidates"][0])
        self.assertIn("confidenceScore", payload["mediumTermCandidates"][0])
        self.assertIn("confidenceLabel", payload["longTermCandidates"][0])

    def test_snapshot_payload_uses_korea_date_after_utc_market_close(self):
        created_at = datetime(2026, 5, 17, 21, 30, tzinfo=timezone.utc).astimezone(
            ZoneInfo("Asia/Seoul")
        )
        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=INDUSTRIES,
            stocks=STOCKS[:1],
            news_items=(),
            created_at=created_at,
        )

        payload = report_to_snapshot_payload(report, mode="live")

        self.assertEqual(payload["snapshotDate"], "2026-05-18")
        self.assertEqual(payload["createdAtTimezone"], "Asia/Seoul")

    def test_cache_store_records_source_events(self):
        with TemporaryDirectory() as tmpdir:
            cache = CacheStore(Path(tmpdir) / "cache.sqlite")
            cache.record_source_event("Yahoo Finance", "error", "테스트 실패")
            rows = cache.list_source_events(limit=5)

        self.assertEqual(rows[0]["source"], "Yahoo Finance")
        self.assertEqual(rows[0]["eventType"], "error")

    def test_cache_store_records_cache_hit_metadata(self):
        with TemporaryDirectory() as tmpdir:
            cache = CacheStore(Path(tmpdir) / "cache.sqlite")
            cache.set_json("test:key", "Yahoo Finance", "https://example.com", {"ok": True}, ttl_seconds=60)
            payload = cache.get_json("test:key")
            rows = cache.list_source_events(limit=5)
            metadata = cache.get_cache_metadata("test:key")

        self.assertEqual(payload, {"ok": True})
        self.assertTrue(rows[0]["metadata"]["cacheHit"])
        self.assertEqual(rows[0]["metadata"]["cacheKey"], "test:key")
        self.assertEqual(metadata["source"], "Yahoo Finance")
        self.assertFalse(metadata["expired"])

    def test_cache_store_filters_source_events_since_report_start(self):
        with TemporaryDirectory() as tmpdir:
            cache = CacheStore(Path(tmpdir) / "cache.sqlite")
            with cache._connect() as connection:
                connection.execute(
                    """
                    insert into source_events(source, event_type, message, created_at)
                    values(?, ?, ?, ?)
                    """,
                    ("Yahoo Finance", "error", "old", "2026-05-17T00:00:00+00:00"),
                )
            since = datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc)
            with cache._connect() as connection:
                connection.execute(
                    """
                    insert into source_events(source, event_type, message, created_at)
                    values(?, ?, ?, ?)
                    """,
                    ("Yahoo Finance", "stale", "new", "2026-05-18T00:00:01+00:00"),
                )
            rows = cache.list_source_events_since(since)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "new")

    def test_snapshot_history_reads_persistent_file_store(self):
        with TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "snapshot_store" / "recommendation_snapshots.json"
            report = build_report(
                macro_context=DEFAULT_MACRO_CONTEXT,
                industries=INDUSTRIES,
                stocks=STOCKS[:1],
                news_items=(),
                created_at=datetime(2026, 5, 19, 6, 30, tzinfo=ZoneInfo("Asia/Seoul")),
            )
            payload = report_to_snapshot_payload(report, mode="live")
            top_stock = report.stock_scores[0]
            SnapshotFileStore(ledger_path).save_snapshot(
                snapshot_date=payload["snapshotDate"],
                mode="live",
                top_ticker=top_stock.stock.ticker,
                top_name=top_stock.stock.name,
                top_score=top_stock.score,
                payload=payload,
            )

            with patch.dict(
                "os.environ",
                {
                    "STOCK_RECOMMENDER_DATA_DIR": str(Path(tmpdir) / "data"),
                    "STOCK_RECOMMENDER_SNAPSHOT_STORE_PATH": str(ledger_path),
                    "STOCK_RECOMMENDER_TIMEZONE": "Asia/Seoul",
                },
            ):
                history = snapshot_history(limit=10)

        self.assertEqual(history["snapshotCount"], 1)
        self.assertEqual(history["uniqueDays"], 1)
        self.assertEqual(history["latest"]["snapshotDate"], "2026-05-19")
        self.assertIn("sourceEventSummary", history["latest"])
        self.assertIn("priceAnchorCoveragePct", history["latest"])
        self.assertIn("fundamentalSourceCoveragePct", history["latest"])
        self.assertEqual(history["latest"]["payloadKind"], "compact")
        self.assertIn("payloadDigest", history["latest"])

    def test_persistent_snapshot_store_writes_compact_v2_payload(self):
        with TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "recommendation_snapshots.json"
            report = build_report(
                macro_context=DEFAULT_MACRO_CONTEXT,
                industries=INDUSTRIES,
                stocks=STOCKS,
                news_items=(),
                created_at=datetime(2026, 5, 19, 6, 30, tzinfo=ZoneInfo("Asia/Seoul")),
            )
            payload = report_to_snapshot_payload(report, mode="live")
            top_stock = report.stock_scores[0]

            SnapshotFileStore(ledger_path).save_snapshot(
                snapshot_date=payload["snapshotDate"],
                mode="live",
                top_ticker=top_stock.stock.ticker,
                top_name=top_stock.stock.name,
                top_score=top_stock.score,
                payload=payload,
            )
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))

        row = ledger["snapshots"][0]
        compact_payload = row["payload"]
        self.assertEqual(ledger["version"], 2)
        self.assertEqual(row["payloadKind"], "compact")
        self.assertEqual(compact_payload["payloadKind"], "compact")
        self.assertLessEqual(len(compact_payload["stocks"]), 10)
        self.assertIn("payloadDigest", row)
        self.assertNotIn("earlyGrowthCandidates", compact_payload)
        self.assertNotIn("fundamentals", compact_payload["stocks"][0])

    def test_snapshot_history_reads_legacy_v1_full_ledger(self):
        with TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "recommendation_snapshots.json"
            payload = {
                "version": 9,
                "createdAt": "2026-05-18T06:30:00+09:00",
                "createdAtDisplay": "2026-05-18 06:30:00",
                "stocks": [{"ticker": "AAA", "name": "Alpha", "score": 90}],
                "dataQuality": {"configuredSources": [], "liveNews": False, "liveMarketData": False},
            }
            ledger_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "snapshots": [
                            {
                                "id": 1,
                                "snapshotDate": "2026-05-18",
                                "mode": "live",
                                "topTicker": "AAA",
                                "topName": "Alpha",
                                "topScore": 90,
                                "createdAt": "2026-05-18T06:30:00+09:00",
                                "payload": payload,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "STOCK_RECOMMENDER_DATA_DIR": str(Path(tmpdir) / "data"),
                    "STOCK_RECOMMENDER_SNAPSHOT_STORE_PATH": str(ledger_path),
                },
            ):
                history = snapshot_history(limit=10)

        self.assertEqual(history["latest"]["payloadKind"], "full")
        self.assertEqual(history["latest"]["topTicker"], "AAA")

    def test_save_recommendation_snapshot_keeps_repo_ledger_local_by_default(self):
        with TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "snapshot_store" / "recommendation_snapshots.json"
            report = build_report(
                macro_context=DEFAULT_MACRO_CONTEXT,
                industries=INDUSTRIES,
                stocks=STOCKS[:1],
                news_items=(),
                created_at=datetime(2026, 5, 19, 6, 30, tzinfo=ZoneInfo("Asia/Seoul")),
            )
            with patch.dict(
                "os.environ",
                {
                    "STOCK_RECOMMENDER_DATA_DIR": str(Path(tmpdir) / "data"),
                    "STOCK_RECOMMENDER_SNAPSHOT_STORE_PATH": str(ledger_path),
                    "STOCK_RECOMMENDER_TIMEZONE": "Asia/Seoul",
                    "STOCK_RECOMMENDER_PERSIST_REPO_LEDGER": "",
                },
            ):
                saved = save_recommendation_snapshot(report)
                history = snapshot_history(limit=10)

        self.assertEqual(saved.snapshot_date, "2026-05-19")
        self.assertFalse(ledger_path.exists())
        self.assertEqual(history["snapshotCount"], 1)

    def test_full_snapshot_artifact_is_optional_local_output(self):
        with TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifacts" / "snapshots"
            ledger_path = Path(tmpdir) / "snapshot_store" / "recommendation_snapshots.json"
            report = build_report(
                macro_context=DEFAULT_MACRO_CONTEXT,
                industries=INDUSTRIES,
                stocks=STOCKS[:1],
                news_items=(),
                created_at=datetime(2026, 5, 19, 6, 30, tzinfo=ZoneInfo("Asia/Seoul")),
            )
            with patch.dict(
                "os.environ",
                {
                    "STOCK_RECOMMENDER_DATA_DIR": str(Path(tmpdir) / "data"),
                    "STOCK_RECOMMENDER_SNAPSHOT_STORE_PATH": str(ledger_path),
                    "STOCK_RECOMMENDER_FULL_SNAPSHOT_DIR": str(artifact_dir),
                    "STOCK_RECOMMENDER_TIMEZONE": "Asia/Seoul",
                },
            ):
                save_recommendation_snapshot(report)

            files = list(artifact_dir.glob("*.json"))
            self.assertEqual(len(files), 1)
            artifact_payload = json.loads(files[0].read_text(encoding="utf-8"))

        self.assertIn("earlyGrowthCandidates", artifact_payload)
        self.assertFalse(ledger_path.exists())

    def test_malformed_persistent_snapshot_store_fails_closed(self):
        with TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "recommendation_snapshots.json"
            ledger_path.write_text("{not json", encoding="utf-8")
            store = SnapshotFileStore(ledger_path)

            with self.assertRaises(SnapshotStoreError):
                store.save_snapshot(
                    snapshot_date="2026-05-19",
                    mode="live",
                    top_ticker="AAA",
                    top_name="Alpha",
                    top_score=90,
                    payload={"createdAt": "2026-05-19T00:00:00+09:00", "stocks": []},
                )

            self.assertEqual(ledger_path.read_text(encoding="utf-8"), "{not json")


class ApiBoundaryTests(unittest.TestCase):
    def test_backtest_api_hides_exception_detail_and_records_event(self):
        import api.backtest as backtest_api

        original_create = backtest_api.create_backtest
        original_record = backtest_api._record_api_error
        captured: dict[str, object] = {}
        events: list[tuple[str, str]] = []

        def explode(**kwargs):
            raise RuntimeError("secret stack detail")

        def send_json(payload, status=HTTPStatus.OK):
            captured["payload"] = payload
            captured["status"] = status

        try:
            backtest_api.create_backtest = explode
            backtest_api._record_api_error = lambda source, exc: events.append((source, str(exc)))
            fake_handler = object.__new__(backtest_api.handler)
            fake_handler.path = "/api/backtest?method=snapshot"
            fake_handler._send_json = send_json

            backtest_api.handler.do_GET(fake_handler)
        finally:
            backtest_api.create_backtest = original_create
            backtest_api._record_api_error = original_record

        self.assertEqual(captured["status"], HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(captured["payload"], {"error": "백테스트를 생성하지 못했습니다."})
        self.assertNotIn("secret stack detail", json.dumps(captured["payload"], ensure_ascii=False))
        self.assertEqual(events, [("api/backtest", "secret stack detail")])

    def test_backtest_api_accepts_horizon_and_falls_back_to_overall(self):
        import api.backtest as backtest_api

        original_create = backtest_api.create_backtest
        original_to_dict = backtest_api.backtest_to_dict
        captured_horizons: list[str] = []
        responses: list[dict] = []

        def fake_create(**kwargs):
            captured_horizons.append(kwargs["horizon"])
            return object()

        def fake_to_dict(result):
            return {"horizon": captured_horizons[-1]}

        def send_json(payload, status=HTTPStatus.OK):
            responses.append({"payload": payload, "status": status})

        try:
            backtest_api.create_backtest = fake_create
            backtest_api.backtest_to_dict = fake_to_dict
            fake_handler = object.__new__(backtest_api.handler)
            fake_handler._send_json = send_json

            fake_handler.path = "/api/backtest?method=snapshot&horizon=short"
            backtest_api.handler.do_GET(fake_handler)
            fake_handler.path = "/api/backtest?method=snapshot&horizon=invalid"
            backtest_api.handler.do_GET(fake_handler)
        finally:
            backtest_api.create_backtest = original_create
            backtest_api.backtest_to_dict = original_to_dict

        self.assertEqual(captured_horizons, ["short", "overall"])
        self.assertEqual(responses[0]["payload"]["horizon"], "short")
        self.assertEqual(responses[1]["payload"]["horizon"], "overall")


class StaticExportTests(unittest.TestCase):
    def test_report_payload_raises_on_report_failure(self):
        import scripts.export_cloudflare_static as export_static

        original_create = export_static.create_recommendation_report

        def explode(**kwargs):
            raise RuntimeError("report failed")

        try:
            export_static.create_recommendation_report = explode
            with self.assertRaisesRegex(RuntimeError, "report failed"):
                export_static.report_payload()
        finally:
            export_static.create_recommendation_report = original_create

    def test_empty_backtest_payload_keeps_public_fields_and_warning(self):
        import scripts.export_cloudflare_static as export_static

        payload = export_static.empty_backtest_payload(12, 5, "SPY", "partial failure", "short")

        self.assertEqual(payload["method"], "snapshot")
        self.assertEqual(payload["horizon"], "short")
        self.assertTrue(payload["pointInTime"])
        self.assertEqual(payload["priceSource"], "unknown")
        self.assertEqual(payload["requiredSnapshotDays"], 13)
        self.assertEqual(payload["warnings"], ["partial failure"])
        self.assertIn("createdAtTimezone", payload)

    def test_static_shell_copies_react_assets_and_injects_static_mode(self):
        import scripts.export_cloudflare_static as export_static

        original_web_dir = export_static.WEB_DIR
        original_dist_dir = export_static.DIST_DIR

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            web_dir = tmp_path / "web"
            dist_dir = tmp_path / "dist"
            (web_dir / "assets").mkdir(parents=True)
            (web_dir / "assets" / "index-test.js").write_text("console.log('ok')", encoding="utf-8")
            (web_dir / "assets" / "index-test.css").write_text("body{}", encoding="utf-8")
            (web_dir / "favicon.svg").write_text("<svg />", encoding="utf-8")
            (web_dir / "index.html").write_text(
                "<!doctype html><html><head></head><body><div id='root'></div></body></html>",
                encoding="utf-8",
            )

            try:
                export_static.WEB_DIR = web_dir
                export_static.DIST_DIR = dist_dir
                export_static.build_shell()
            finally:
                export_static.WEB_DIR = original_web_dir
                export_static.DIST_DIR = original_dist_dir

            index_html = (dist_dir / "index.html").read_text(encoding="utf-8")
            headers = (dist_dir / "_headers").read_text(encoding="utf-8")

            self.assertIn("window.STATIC_DATA_ONLY = true", index_html)
            self.assertTrue((dist_dir / "assets" / "index-test.js").exists())
            self.assertTrue((dist_dir / "assets" / "index-test.css").exists())
            self.assertTrue((dist_dir / "favicon.svg").exists())
            self.assertIn("/favicon.svg", headers)

    def test_static_export_does_not_hide_malformed_snapshot_ledger(self):
        import scripts.export_cloudflare_static as export_static

        with TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "recommendation_snapshots.json"
            ledger_path.write_text("{not json", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {
                    "STOCK_RECOMMENDER_DATA_DIR": str(Path(tmpdir) / "data"),
                    "STOCK_RECOMMENDER_SNAPSHOT_STORE_PATH": str(ledger_path),
                },
            ):
                with self.assertRaises(SnapshotStoreError):
                    export_static.snapshots_payload()

    def test_github_actions_snapshot_commit_before_cloudflare_deploy(self):
        workflow = Path(".github/workflows/daily-cloudflare-redeploy.yml").read_text(encoding="utf-8")
        save_index = workflow.index("Save daily recommendation snapshot")
        commit_index = workflow.index("Commit snapshot ledger")
        deploy_index = workflow.index("Trigger Cloudflare Pages build")

        self.assertLess(save_index, commit_index)
        self.assertLess(commit_index, deploy_index)
        self.assertIn("python3 -m stock_recommender.snapshot_cli", workflow)
        self.assertIn("STOCK_RECOMMENDER_PERSIST_REPO_LEDGER: \"1\"", workflow)
        self.assertIn("git add snapshot_store/recommendation_snapshots.json", workflow)


def _annual_fact(start: str, end: str, filed: str, value: float) -> dict:
    return {
        "start": start,
        "end": end,
        "filed": filed,
        "form": "10-K",
        "fp": "FY",
        "val": value,
    }


def _quarter_fact(start: str, end: str, filed: str, value: float, period: str, year: int) -> dict:
    return {
        "start": start,
        "end": end,
        "filed": filed,
        "form": "10-Q",
        "fp": period,
        "fy": year,
        "val": value,
    }


def _dart_row(fs_div: str, account_name: str, current: str, previous: str) -> dict:
    return {
        "fs_div": fs_div,
        "account_nm": account_name,
        "thstrm_amount": current,
        "frmtrm_amount": previous,
    }


def _dart_payload(year: int, report_code: str, revenue: int, operating_income: int) -> dict:
    rows = [
        _dart_row("CFS", "매출액", f"{revenue:,}", "0"),
        _dart_row("CFS", "영업이익", f"{operating_income:,}", "0"),
    ]
    for row in rows:
        row["bsns_year"] = str(year)
        row["reprt_code"] = report_code
        row["thstrm_dt"] = f"{year}-12-31"
    return {
        "status": "000",
        "bsns_year": str(year),
        "reprt_code": report_code,
        "list": rows,
    }


def _history(start_price: float, daily_return: float) -> tuple[PricePoint, ...]:
    current = date(2025, 1, 1)
    price = start_price
    points: list[PricePoint] = []
    while len(points) < 420:
        if current.weekday() < 5:
            price *= 1 + daily_return
            points.append(PricePoint(current, price))
        current += timedelta(days=1)
    return tuple(points)


def _strong_swing_momentum(with_volume: bool = True) -> Momentum:
    volume_fields = {
        "latest_volume": 2_000_000,
        "avg_volume_20": 1_200_000,
        "volume_ratio": 1.67,
    }
    if not with_volume:
        volume_fields = {
            "latest_volume": None,
            "avg_volume_20": None,
            "volume_ratio": None,
        }
    return Momentum(
        one_month_pct=8.0,
        three_month_pct=18.0,
        six_month_pct=34.0,
        drawdown_from_high_pct=-6.0,
        range_position_pct=72.0,
        latest_close=105.0,
        latest_close_date="2025-12-31",
        six_month_high=112.0,
        six_month_low=78.0,
        ma20=100.0,
        ma60=92.0,
        ma120=84.0,
        rsi14=58.0,
        ma20_distance_pct=5.0,
        ma60_distance_pct=14.1,
        ma120_distance_pct=25.0,
        ma20_slope_pct=2.2,
        ma60_slope_pct=1.1,
        twenty_day_breakout_pct=4.0,
        sixty_day_breakout_pct=9.0,
        source="Yahoo Finance",
        **volume_fields,
    )


def _monthly_history(values: dict[date, float]) -> tuple[PricePoint, ...]:
    return tuple(PricePoint(day, close) for day, close in sorted(values.items()))


def _snapshot(snapshot_date: date, ticker: str, name: str) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_date=snapshot_date,
        payload={
            "stocks": [
                {
                    "ticker": ticker,
                    "name": name,
                    "score": 90,
                }
            ]
        },
    )


def _snapshot_with_anchors(snapshot_date: date, stocks: list[tuple[str, str, float]], spy_close: float) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_date=snapshot_date,
        payload={
            "stocks": [
                {
                    "ticker": ticker,
                    "name": name,
                    "score": 90,
                    "priceAnchor": {
                        "latestClose": close,
                        "latestCloseDate": snapshot_date.isoformat(),
                        "currency": "USD",
                        "source": "Yahoo Finance",
                        "stale": False,
                    },
                }
                for ticker, name, close in stocks
            ],
            "benchmarks": [
                {
                    "ticker": "SPY",
                    "priceAnchor": {
                        "latestClose": spy_close,
                        "latestCloseDate": snapshot_date.isoformat(),
                        "currency": "USD",
                        "source": "Yahoo Finance",
                        "stale": False,
                    },
                }
            ],
        },
    )


def _rules_snapshot_with_anchors(
    snapshot_date: date,
    stocks: list[tuple[str, str, float, str, str, float]],
    spy_close: float,
) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_date=snapshot_date,
        payload={
            "version": 16,
            "stocks": [
                {
                    "ticker": ticker,
                    "name": name,
                    "score": 90,
                    "decisionGrade": decision_grade,
                    "riskGate": risk_gate,
                    "targetWeightPct": target_weight,
                    "priceAnchor": _anchor(snapshot_date, close, "USD"),
                }
                for ticker, name, close, decision_grade, risk_gate, target_weight in stocks
            ],
            "benchmarks": [
                {"ticker": "SPY", "priceAnchor": _anchor(snapshot_date, spy_close, "USD")},
                {"ticker": "QQQ", "priceAnchor": _anchor(snapshot_date, spy_close, "USD")},
                {"ticker": "^KS11", "priceAnchor": _anchor(snapshot_date, spy_close, "KRW")},
            ],
            "snapshotQuality": {
                "priceAnchorCoveragePct": 100,
                "benchmarkAnchorCoveragePct": 100,
                "fundamentalSourceCoveragePct": 0,
                "sourceErrorCount": 0,
                "sourceStaleCount": 0,
                "backtestEligible": True,
                "exclusionReasons": [],
            },
        },
    )


def _term_snapshot_with_anchors(
    snapshot_date: date,
    stocks: list[tuple[str, str, float]],
    short_candidates: list[tuple[str, str]],
    spy_close: float,
) -> SnapshotRecord:
    stock_rows = [
        {
            "ticker": ticker,
            "name": name,
            "score": 90,
            "priceAnchor": _anchor(snapshot_date, close, "USD"),
        }
        for ticker, name, close in stocks
    ]
    return SnapshotRecord(
        snapshot_date=snapshot_date,
        payload={
            "version": 11,
            "stocks": stock_rows,
            "shortTermCandidates": [
                {"ticker": ticker, "name": name, "score": 90}
                for ticker, name in short_candidates
            ],
            "benchmarks": [
                {"ticker": "SPY", "priceAnchor": _anchor(snapshot_date, spy_close, "USD")},
                {"ticker": "QQQ", "priceAnchor": _anchor(snapshot_date, spy_close, "USD")},
                {"ticker": "^KS11", "priceAnchor": _anchor(snapshot_date, spy_close, "KRW")},
            ],
            "snapshotQuality": {
                "priceAnchorCoveragePct": 100,
                "benchmarkAnchorCoveragePct": 100,
                "fundamentalSourceCoveragePct": 0,
                "sourceErrorCount": 0,
                "sourceStaleCount": 0,
                "backtestEligible": True,
                "exclusionReasons": [],
            },
        },
    )


def _snapshot_v10_with_anchors(
    snapshot_date: date,
    stocks: list[tuple[str, str, float]],
    benchmarks: dict[str, float],
) -> SnapshotRecord:
    stock_rows = [
        {
            "ticker": ticker,
            "name": name,
            "score": 90,
            "priceAnchor": _anchor(snapshot_date, close, "USD"),
        }
        for ticker, name, close in stocks
    ]
    benchmark_rows = [
        {
            "ticker": ticker,
            "priceAnchor": _anchor(snapshot_date, close, "KRW" if ticker == "^KS11" else "USD"),
        }
        for ticker, close in benchmarks.items()
    ]
    price_coverage = round(
        sum(1 for item in stock_rows if item["priceAnchor"]["latestClose"] is not None) / len(stock_rows) * 100,
        1,
    )
    benchmark_coverage = round(len(benchmark_rows) / 3 * 100, 1)
    reasons = []
    if price_coverage < 80:
        reasons.append("priceAnchorCoverageBelow80")
    if benchmark_coverage < 100:
        reasons.append("benchmarkAnchorCoverageBelow100")
    return SnapshotRecord(
        snapshot_date=snapshot_date,
        payload={
            "version": 10,
            "stocks": stock_rows,
            "benchmarks": benchmark_rows,
            "snapshotQuality": {
                "priceAnchorCoveragePct": price_coverage,
                "benchmarkAnchorCoveragePct": benchmark_coverage,
                "fundamentalSourceCoveragePct": 0,
                "sourceErrorCount": 0,
                "sourceStaleCount": 0,
                "backtestEligible": not reasons,
                "exclusionReasons": reasons,
            },
        },
    )


def _anchor(snapshot_date: date, close: float | None, currency: str) -> dict:
    return {
        "latestClose": close,
        "latestCloseDate": snapshot_date.isoformat() if close is not None else None,
        "currency": currency,
        "source": "Yahoo Finance" if close is not None else None,
        "stale": False,
    }


def _test_app_config(
    root: str,
    *,
    opendart_api_key: str | None = None,
    universe_mode: str = "screened",
    universe_limit: int = 500,
    us_universe_limit: int = 350,
    kr_universe_limit: int = 150,
    us_fundamental_limit: int = 200,
    kr_fundamental_limit: int = 30,
) -> AppConfig:
    project_root = Path(root)
    data_dir = project_root / "data"
    return AppConfig(
        project_root=project_root,
        data_dir=data_dir,
        cache_db_path=data_dir / "cache.sqlite",
        snapshot_store_path=project_root / "snapshot_store" / "recommendation_snapshots.json",
        full_snapshot_dir=None,
        persist_repo_ledger=False,
        sec_user_agent="stock-recommender-test test@example.com",
        opendart_api_key=opendart_api_key,
        timezone_name="Asia/Seoul",
        universe_mode=universe_mode,
        universe_limit=universe_limit,
        us_universe_limit=us_universe_limit,
        kr_universe_limit=kr_universe_limit,
        us_fundamental_limit=us_fundamental_limit,
        kr_fundamental_limit=kr_fundamental_limit,
    )


def _quote(symbol: str, name: str, market_cap: float, currency: str = "USD") -> dict:
    return {
        "symbol": symbol,
        "shortName": name,
        "longName": name,
        "regularMarketPrice": 100.0,
        "marketCap": market_cap,
        "currency": currency,
        "financialCurrency": currency,
        "trailingPE": 20.0,
        "forwardPE": 18.0,
    }


if __name__ == "__main__":
    unittest.main()
