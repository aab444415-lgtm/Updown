from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import create_recommendation_report
from .report import render_markdown
from .snapshots import save_recommendation_snapshot
from .universe import DEFAULT_MACRO_CONTEXT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="뉴스, 거시경제, 산업, 기업 지표를 합쳐 주식 리서치 후보를 추천합니다."
    )
    parser.add_argument("--macro", default=DEFAULT_MACRO_CONTEXT, help="거시경제/시장 상황 설명 문장")
    parser.add_argument("--top-industries", type=int, default=3, help="표시할 산업 수")
    parser.add_argument("--top-stocks", type=int, default=6, help="표시할 종목 수")
    parser.add_argument("--output", type=Path, help="Markdown 리포트 저장 경로")
    parser.add_argument("--save-snapshot", action="store_true", help="이번 추천 결과를 일별 스냅샷으로 저장합니다.")
    parser.add_argument(
        "--skip-sec",
        action="store_true",
        help="SEC EDGAR 재무제표 갱신을 건너뜁니다.",
    )
    args = parser.parse_args(argv)

    report = create_recommendation_report(
        macro_context=args.macro,
        use_sec_fundamentals=not args.skip_sec,
    )
    output = render_markdown(report, top_industries=args.top_industries, top_stocks=args.top_stocks)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        print(f"리포트를 저장했습니다: {args.output}")
    else:
        print(output)

    if args.save_snapshot:
        mode = "live"
        saved = save_recommendation_snapshot(report, mode=mode)
        top = f"{saved.top_name} ({saved.top_ticker}) {saved.top_score:.1f}점" if saved.top_ticker else "-"
        print(f"스냅샷 저장 완료: {saved.snapshot_date} / {mode} / 상위 종목 {top}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
