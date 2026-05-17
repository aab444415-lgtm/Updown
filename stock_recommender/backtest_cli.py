from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .backtest import BENCHMARKS, create_backtest, render_backtest_markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="추천 모델의 월별 리밸런싱 백테스트를 실행합니다.")
    parser.add_argument("--months", type=int, default=12, help="검증할 최근 개월 수")
    parser.add_argument("--top", type=int, default=5, help="월별로 보유할 상위 종목 수")
    parser.add_argument("--benchmark", choices=BENCHMARKS, default="SPY", help="비교 벤치마크")
    parser.add_argument("--output", type=Path, help="Markdown 백테스트 리포트 저장 경로")
    args = parser.parse_args(argv)

    result = create_backtest(months=args.months, top_n=args.top, benchmark_ticker=args.benchmark)
    output = render_backtest_markdown(result)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        print(f"백테스트 리포트를 저장했습니다: {args.output}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
