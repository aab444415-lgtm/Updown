from __future__ import annotations

import argparse
import sys

from .config import load_config
from .snapshots import snapshot_history
from .time_utils import now_in_app_timezone


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="추천 스냅샷 저장 상태를 점검합니다.")
    parser.add_argument("--min-days", type=int, default=30, help="의미 있는 포인트인타임 검증에 필요한 최소 기록일")
    parser.add_argument("--require-today", action="store_true", help="앱 타임존 기준 오늘 스냅샷이 없으면 실패합니다.")
    parser.add_argument(
        "--fail-under-min-days",
        action="store_true",
        help="누적 기록일이 --min-days보다 적으면 실패합니다.",
    )
    args = parser.parse_args(argv)

    config = load_config()
    today = now_in_app_timezone(config).date().isoformat()
    history = snapshot_history(limit=365)
    latest = history.get("latest") if isinstance(history.get("latest"), dict) else None
    latest_date = str(latest.get("snapshotDate") or "") if latest else ""
    unique_days = int(history.get("uniqueDays") or 0)
    min_days = max(1, args.min_days)

    print(f"스냅샷 상태: {unique_days}/{min_days}일, 최신 {latest_date or '-'}, 오늘 {today}")
    if unique_days < min_days:
        print(f"주의: 포인트인타임 검증까지 {min_days - unique_days}일 이상 더 필요합니다.")

    failed = False
    if args.require_today and latest_date != today:
        print(f"오류: 오늘({today}) live 스냅샷이 없습니다.")
        failed = True
    if args.fail_under_min_days and unique_days < min_days:
        print(f"오류: 누적 스냅샷이 최소 기준({min_days}일)보다 부족합니다.")
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
