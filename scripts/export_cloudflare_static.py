from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from stock_recommender.backtest import BENCHMARKS, backtest_to_dict, create_backtest
from stock_recommender.pipeline import create_recommendation_report
from stock_recommender.snapshots import snapshot_history
from stock_recommender.universe import DEFAULT_MACRO_CONTEXT
from stock_recommender.web import report_to_dict


WEB_DIR = ROOT_DIR / "web"
DIST_DIR = ROOT_DIR / "dist"


def main() -> int:
    build_shell()
    sample_payload = report_to_dict(
        create_recommendation_report(live=False, macro_context=DEFAULT_MACRO_CONTEXT)
    )
    write_json(DIST_DIR / "data" / "report-sample.json", sample_payload)
    write_json(DIST_DIR / "data" / "report-live.json", live_report_payload(sample_payload))
    export_backtests()
    write_json(DIST_DIR / "data" / "snapshots.json", snapshots_payload())
    return 0


def build_shell() -> None:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    (DIST_DIR / "assets").mkdir(parents=True)
    (DIST_DIR / "data").mkdir()
    index_html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    index_html = index_html.replace(
        '<script src="/assets/app.js"></script>',
        '<script>window.STATIC_DATA_ONLY = true;</script>\n'
        '    <script src="/assets/app.js?v=top3-pages-v2"></script>',
    )
    (DIST_DIR / "index.html").write_text(index_html, encoding="utf-8")
    shutil.copy2(WEB_DIR / "styles.css", DIST_DIR / "assets" / "styles.css")
    shutil.copy2(WEB_DIR / "app.js", DIST_DIR / "assets" / "app.js")
    (DIST_DIR / "_headers").write_text(
        "/assets/*\n"
        "  Cache-Control: public, max-age=3600\n"
        "/data/*\n"
        "  Cache-Control: no-store\n",
        encoding="utf-8",
    )


def live_report_payload(sample_payload: dict) -> dict:
    try:
        return report_to_dict(
            create_recommendation_report(live=True, macro_context=DEFAULT_MACRO_CONTEXT)
        )
    except Exception as exc:
        payload = copy.deepcopy(sample_payload)
        payload["dataQuality"]["warnings"].append(
            f"Cloudflare 배포용 정적 생성 중 라이브 리포트 생성 실패: {exc}"
        )
        return payload


def export_backtests() -> None:
    for months in (6, 12, 24):
        for top_n in (3, 5, 10):
            for benchmark in BENCHMARKS:
                path = DIST_DIR / "data" / f"backtest-{months}-{top_n}-{benchmark}.json"
                try:
                    payload = backtest_to_dict(
                        create_backtest(months=months, top_n=top_n, benchmark_ticker=benchmark)
                    )
                except Exception as exc:
                    payload = empty_backtest_payload(months, top_n, benchmark, str(exc))
                write_json(path, payload)


def snapshots_payload() -> dict:
    try:
        return snapshot_history(limit=30)
    except Exception as exc:
        return {
            "snapshotCount": 0,
            "uniqueDays": 0,
            "latest": None,
            "coverageLabel": f"스냅샷 로드 실패: {exc}",
            "readinessScore": 0,
            "minimumDaysForPointInTimeBacktest": 30,
            "rows": [],
        }


def empty_backtest_payload(months: int, top_n: int, benchmark: str, warning: str) -> dict:
    return {
        "createdAt": "",
        "months": months,
        "topN": top_n,
        "benchmarkTicker": benchmark,
        "periodCount": 0,
        "strategyReturnPct": None,
        "benchmarkReturnPct": None,
        "alphaPct": None,
        "averageMonthlyReturnPct": None,
        "winRatePct": None,
        "hitRatePct": None,
        "maxDrawdownPct": None,
        "volatilityPct": None,
        "dataCoveragePct": 0,
        "benchmarks": [],
        "warnings": [warning],
        "periods": [],
        "assumptions": [
            "Cloudflare 정적 배포에서는 배포 시점에 생성된 백테스트 JSON을 표시합니다."
        ],
    }


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
