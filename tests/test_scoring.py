import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from stock_recommender.backtest import PricePoint, run_backtest
from stock_recommender.config import configured_source_names, load_config, missing_optional_source_names
from stock_recommender.macro_data import industry_macro_data_score
from stock_recommender.models import DataQuality, MacroIndicator, MacroSnapshot
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
                    "Liabilities": {
                        "units": {"USD": [_annual_fact("2024-12-31", "2024-12-31", "2025-02-01", 60)]}
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


class OpenDartTests(unittest.TestCase):
    def test_extract_opendart_fundamentals(self):
        payload = {
            "status": "000",
            "list": [
                _dart_row("CFS", "매출액", "125,000", "100,000"),
                _dart_row("CFS", "영업이익", "25,000", "18,000"),
                _dart_row("CFS", "당기순이익", "17,000", "14,000"),
                _dart_row("CFS", "부채총계", "60,000", "55,000"),
                _dart_row("CFS", "자본총계", "90,000", "80,000"),
            ],
        }

        fundamentals = extract_opendart_fundamentals(payload)

        self.assertAlmostEqual(fundamentals.revenue_growth_pct, 25.0)
        self.assertAlmostEqual(fundamentals.operating_margin_pct, 20.0)
        self.assertAlmostEqual(fundamentals.roe_pct, 17_000 / 85_000 * 100)
        self.assertAlmostEqual(fundamentals.debt_to_equity_pct, 60_000 / 90_000 * 100)
        self.assertEqual(fundamentals.market_cap_currency, "KRW")


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
        payload = report_to_snapshot_payload(report, mode="sample")

        with TemporaryDirectory() as tmpdir:
            cache = CacheStore(Path(tmpdir) / "cache.sqlite")
            first_id = cache.save_recommendation_snapshot(
                snapshot_date=payload["snapshotDate"],
                mode="sample",
                top_ticker=payload["stocks"][0]["ticker"],
                top_name=payload["stocks"][0]["name"],
                top_score=payload["stocks"][0]["score"],
                payload=payload,
            )
            second_id = cache.save_recommendation_snapshot(
                snapshot_date=payload["snapshotDate"],
                mode="sample",
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
