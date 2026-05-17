from __future__ import annotations

import argparse
import sys

from .pipeline import create_recommendation_report
from .snapshots import save_recommendation_snapshot, snapshot_history
from .universe import DEFAULT_MACRO_CONTEXT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="추천 결과를 일별 스냅샷으로 저장합니다.")
    parser.add_argument("--live", action="store_true", help="라이브 데이터로 스냅샷을 만듭니다.")
    parser.add_argument("--macro", default=DEFAULT_MACRO_CONTEXT, help="거시경제/시장 상황 설명 문장")
    parser.add_argument(
        "--skip-sec",
        action="store_true",
        help="라이브 모드에서 SEC EDGAR 재무제표 갱신을 건너뜁니다.",
    )
    args = parser.parse_args(argv)

    report = create_recommendation_report(
        live=args.live,
        macro_context=args.macro,
        use_sec_fundamentals=not args.skip_sec,
    )
    mode = "live" if args.live else "sample"
    saved = save_recommendation_snapshot(report, mode=mode)
    history = snapshot_history(limit=365)
    top = f"{saved.top_name} ({saved.top_ticker}) {saved.top_score:.1f}점" if saved.top_ticker else "-"
    print(f"스냅샷 저장 완료: {saved.snapshot_date} / {mode} / #{saved.id}")
    print(f"상위 종목: {top}")
    print(f"누적 기록일: {history['uniqueDays']}일 / 상태: {history['coverageLabel']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
