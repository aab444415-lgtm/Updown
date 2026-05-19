from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig
from .storage import CacheStore


STORE_VERSION = 1


class SnapshotStoreError(RuntimeError):
    pass


class SnapshotFileStore:
    def __init__(self, path: Path):
        self.path = path

    def save_snapshot(
        self,
        snapshot_date: str,
        mode: str,
        top_ticker: str | None,
        top_name: str | None,
        top_score: float | None,
        payload: dict,
    ) -> int:
        document = self._read()
        snapshots = _valid_rows(document.get("snapshots"))
        existing_index = _find_row_index(snapshots, snapshot_date, mode)
        snapshot_id = (
            int(snapshots[existing_index].get("id") or 0)
            if existing_index is not None
            else _next_id(snapshots)
        )
        row = {
            "id": snapshot_id,
            "snapshotDate": snapshot_date,
            "mode": mode,
            "topTicker": top_ticker,
            "topName": top_name,
            "topScore": top_score,
            "createdAt": str(payload.get("createdAt") or ""),
            "payload": payload,
        }
        if existing_index is None:
            snapshots.append(row)
        else:
            snapshots[existing_index] = row
        document = {
            "version": STORE_VERSION,
            "snapshots": _sort_rows(snapshots),
            "lastUpdatedAt": datetime.now(timezone.utc).isoformat(),
        }
        self._write(document)
        return snapshot_id

    def list_snapshots(self, limit: int = 30, mode: str | None = None) -> list[dict]:
        rows = _valid_rows(self._read().get("snapshots"))
        if mode:
            rows = [row for row in rows if row.get("mode") == mode]
        return _sort_rows(rows)[:limit]

    def _read(self) -> dict:
        if not self.path.exists():
            return _empty_document()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SnapshotStoreError(f"스냅샷 ledger를 읽지 못했습니다: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise SnapshotStoreError(f"스냅샷 ledger JSON 파싱 실패: {self.path}") from exc
        _validate_document(payload, self.path)
        return payload

    def _write(self, document: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self.path)


def save_persistent_snapshot(
    config: AppConfig,
    snapshot_date: str,
    mode: str,
    top_ticker: str | None,
    top_name: str | None,
    top_score: float | None,
    payload: dict,
) -> int:
    return SnapshotFileStore(config.snapshot_store_path).save_snapshot(
        snapshot_date=snapshot_date,
        mode=mode,
        top_ticker=top_ticker,
        top_name=top_name,
        top_score=top_score,
        payload=payload,
    )


def list_snapshot_rows(
    config: AppConfig,
    cache: CacheStore,
    limit: int = 30,
    mode: str | None = None,
) -> list[dict]:
    file_rows = SnapshotFileStore(config.snapshot_store_path).list_snapshots(limit=max(limit, 365), mode=mode)
    db_rows = cache.list_recommendation_snapshots(limit=max(limit, 365), mode=mode)
    merged: dict[tuple[str, str], dict] = {}
    for row in (*file_rows, *db_rows):
        key = (str(row.get("snapshotDate") or ""), str(row.get("mode") or ""))
        if not key[0] or not key[1]:
            continue
        existing = merged.get(key)
        if existing is None or _sort_key(row) > _sort_key(existing):
            merged[key] = row
    return _sort_rows(list(merged.values()))[:limit]


def _valid_rows(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict) and isinstance(row.get("payload"), dict)]


def _empty_document() -> dict:
    return {
        "version": STORE_VERSION,
        "snapshots": [],
        "lastUpdatedAt": None,
    }


def _validate_document(payload: object, path: Path) -> None:
    if not isinstance(payload, dict):
        raise SnapshotStoreError(f"스냅샷 ledger 최상위 구조가 올바르지 않습니다: {path}")
    if payload.get("version") != STORE_VERSION:
        raise SnapshotStoreError(f"스냅샷 ledger version이 올바르지 않습니다: {path}")
    if not isinstance(payload.get("snapshots"), list):
        raise SnapshotStoreError(f"스냅샷 ledger snapshots 배열이 올바르지 않습니다: {path}")
    for index, row in enumerate(payload["snapshots"]):
        if not isinstance(row, dict):
            raise SnapshotStoreError(f"스냅샷 ledger row 구조가 올바르지 않습니다: {path}#{index}")
        if not isinstance(row.get("payload"), dict):
            raise SnapshotStoreError(f"스냅샷 ledger payload가 올바르지 않습니다: {path}#{index}")


def _find_row_index(rows: list[dict], snapshot_date: str, mode: str) -> int | None:
    for index, row in enumerate(rows):
        if row.get("snapshotDate") == snapshot_date and row.get("mode") == mode:
            return index
    return None


def _next_id(rows: list[dict]) -> int:
    ids = [int(row.get("id") or 0) for row in rows if isinstance(row.get("id"), int)]
    return max(ids, default=0) + 1


def _sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=_sort_key, reverse=True)


def _sort_key(row: dict) -> tuple[str, str]:
    created_at = str(row.get("createdAt") or row.get("payload", {}).get("createdAt") or "")
    try:
        parsed = datetime.fromisoformat(created_at)
        normalized_created_at = parsed.isoformat()
    except ValueError:
        normalized_created_at = created_at
    return (str(row.get("snapshotDate") or ""), normalized_created_at)
