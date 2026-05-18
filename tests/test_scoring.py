import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from stock_recommender.backtest import PricePoint, run_backtest
from stock_recommender.config import configured_source_names, load_config, missing_optional_source_names
from stock_recommender.macro_data import industry_macro_data_score
from stock_recommender.models import DataQuality, Fundamentals, MacroIndicator, MacroSnapshot, Momentum, NewsItem, StockProfile
from stock_recommender.opendart_financials import extract_opendart_fundamentals
from stock_recommender.report import render_markdown
from stock_recommender.scoring import build_report, decision_grade_for_stock, quality_score, valuation_score
from stock_recommender.sec_edgar import extract_fundamentals
from stock_recommender.snapshots import report_to_snapshot_payload
from stock_recommender.storage import CacheStore
from stock_recommender.universe import DEFAULT_MACRO_CONTEXT, INDUSTRIES, STOCKS


class ScoringTests(unittest.TestCase):
    def test_quality_score_rewards_profitable_growth(self):
        nvidia = next(stock for stock in STOCKS if stock.ticker == "NVDA")
        cloudflare = next(stock for stock in STOCKS if stock.ticker == "NET")

        self.assertGreater(quality_score(nvidia.fundamentals), quality_score(cloudflare.fundamentals))

    def test_valuation_score_penalizes_high_multiple(self):
        amd = next(stock for stock in STOCKS if stock.ticker == "AMD")
        lockheed = next(stock for stock in STOCKS if stock.ticker == "LMT")

        self.assertGreater(valuation_score(lockheed.fundamentals), valuation_score(amd.fundamentals))

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

    def test_stock_scores_include_analysis_checks(self):
        report = build_report(
            macro_context=DEFAULT_MACRO_CONTEXT,
            industries=INDUSTRIES,
            stocks=STOCKS,
            news_items=(),
        )
        nvidia = next(item for item in report.stock_scores if item.stock.ticker == "NVDA")

        self.assertEqual(nvidia.analysis_style, "성장주")
        self.assertTrue(any("멀티플" in check for check in nvidia.analysis_checks))
        self.assertGreaterEqual(len(nvidia.second_order_checks), 4)
        self.assertEqual(nvidia.valuation_range.profit_metric, "PER 역산 이익")
        self.assertIsNotNone(nvidia.valuation_range.market_cap_low)
        self.assertTrue(any("밸류에이션 범위" in check for check in nvidia.analysis_checks))

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
        self.assertIn("미국 기준금리", markdown)

    def test_industry_macro_data_score_uses_industry_sensitivity(self):
        snapshot = MacroSnapshot(growth_score=30, defensive_score=80, infrastructure_score=70, korea_fx_score=45)

        power_score = industry_macro_data_score("전력 인프라 및 에너지 장비", snapshot)
        ai_score = industry_macro_data_score("AI 반도체 및 데이터센터", snapshot)

        self.assertGreater(power_score, ai_score)

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


class ConfigTests(unittest.TestCase):
    def test_load_config_reads_dotenv(self):
        with TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        'SEC_USER_AGENT="stock-recommender test@example.com"',
                        "FRED_API_KEY=fred-key",
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

        fundamentals = extract_fundamentals(facts)

        self.assertAlmostEqual(fundamentals.revenue_growth_pct, 25.0)
        self.assertAlmostEqual(fundamentals.operating_margin_pct, 20.0)
        self.assertAlmostEqual(fundamentals.roe_pct, 18 / 85 * 100)
        self.assertAlmostEqual(fundamentals.debt_to_equity_pct, 60 / 90 * 100)
        self.assertEqual(fundamentals.revenue, 125)
        self.assertEqual(fundamentals.operating_income, 25)
        self.assertEqual(fundamentals.ebitda, 32)
        self.assertEqual(fundamentals.net_income, 18)
        self.assertEqual(fundamentals.operating_cash_flow, 30)
        self.assertEqual(fundamentals.capital_expenditure, 8)
        self.assertEqual(fundamentals.free_cash_flow, 22)
        self.assertAlmostEqual(fundamentals.current_ratio_pct, 200.0)
        self.assertAlmostEqual(fundamentals.interest_coverage, 5.0)


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
            ],
        }

        fundamentals = extract_opendart_fundamentals(payload)

        self.assertAlmostEqual(fundamentals.revenue_growth_pct, 25.0)
        self.assertAlmostEqual(fundamentals.operating_margin_pct, 20.0)
        self.assertAlmostEqual(fundamentals.roe_pct, 17_000 / 85_000 * 100)
        self.assertAlmostEqual(fundamentals.debt_to_equity_pct, 60_000 / 90_000 * 100)
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


def _annual_fact(start: str, end: str, filed: str, value: float) -> dict:
    return {
        "start": start,
        "end": end,
        "filed": filed,
        "form": "10-K",
        "fp": "FY",
        "val": value,
    }


def _dart_row(fs_div: str, account_name: str, current: str, previous: str) -> dict:
    return {
        "fs_div": fs_div,
        "account_nm": account_name,
        "thstrm_amount": current,
        "frmtrm_amount": previous,
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


if __name__ == "__main__":
    unittest.main()
