from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


class CacheStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def get_json(self, key: str, allow_expired: bool = False) -> dict | list | None:
        with self._connect() as connection:
            row = connection.execute(
                "select payload, expires_at from api_cache where cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None

        expires_at = _parse_utc_datetime(row["expires_at"])
        if expires_at < _utc_now() and not allow_expired:
            return None
        return json.loads(row["payload"])

    def set_json(self, key: str, source: str, url: str, payload: dict | list, ttl_seconds: int) -> None:
        now = _utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._connect() as connection:
            connection.execute(
                """
                insert into api_cache(cache_key, source, url, payload, fetched_at, expires_at)
                values(?, ?, ?, ?, ?, ?)
                on conflict(cache_key) do update set
                    source = excluded.source,
                    url = excluded.url,
                    payload = excluded.payload,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (
                    key,
                    source,
                    url,
                    json.dumps(payload, ensure_ascii=False),
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )

    def record_source_event(self, source: str, event_type: str, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                insert into source_events(source, event_type, message, created_at)
                values(?, ?, ?, ?)
                """,
                (source, event_type, message, _utc_now().isoformat()),
            )

    def list_source_events(self, limit: int = 50, source: str | None = None) -> list[dict]:
        where_clause = "where source = ?" if source else ""
        params: tuple[object, ...] = (source, limit) if source else (limit,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select id, source, event_type, message, created_at
                from source_events
                {where_clause}
                order by id desc
                limit ?
                """,
                params,
            ).fetchall()
        return [
            {
                "id": row["id"],
                "source": row["source"],
                "eventType": row["event_type"],
                "message": row["message"],
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def save_recommendation_snapshot(
        self,
        snapshot_date: str,
        mode: str,
        top_ticker: str | None,
        top_name: str | None,
        top_score: float | None,
        payload: dict,
    ) -> int:
        created_at = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                insert into recommendation_snapshots(
                    snapshot_date,
                    mode,
                    top_ticker,
                    top_name,
                    top_score,
                    payload,
                    created_at
                )
                values(?, ?, ?, ?, ?, ?, ?)
                on conflict(snapshot_date, mode) do update set
                    top_ticker = excluded.top_ticker,
                    top_name = excluded.top_name,
                    top_score = excluded.top_score,
                    payload = excluded.payload,
                    created_at = excluded.created_at
                """,
                (
                    snapshot_date,
                    mode,
                    top_ticker,
                    top_name,
                    top_score,
                    json.dumps(payload, ensure_ascii=False),
                    created_at,
                ),
            )
            row = connection.execute(
                """
                select id from recommendation_snapshots
                where snapshot_date = ? and mode = ?
                """,
                (snapshot_date, mode),
            ).fetchone()
        return int(row["id"]) if row is not None else 0

    def list_recommendation_snapshots(self, limit: int = 30, mode: str | None = None) -> list[dict]:
        where_clause = "where mode = ?" if mode else ""
        params: tuple[object, ...] = (mode, limit) if mode else (limit,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select
                    id,
                    snapshot_date,
                    mode,
                    top_ticker,
                    top_name,
                    top_score,
                    payload,
                    created_at
                from recommendation_snapshots
                {where_clause}
                order by snapshot_date desc, created_at desc
                limit ?
                """,
                params,
            ).fetchall()
        snapshots: list[dict] = []
        for row in rows:
            snapshots.append(
                {
                    "id": row["id"],
                    "snapshotDate": row["snapshot_date"],
                    "mode": row["mode"],
                    "topTicker": row["top_ticker"],
                    "topName": row["top_name"],
                    "topScore": row["top_score"],
                    "createdAt": row["created_at"],
                    "payload": json.loads(row["payload"]),
                }
            )
        return snapshots

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                create table if not exists api_cache (
                    cache_key text primary key,
                    source text not null,
                    url text not null,
                    payload text not null,
                    fetched_at text not null,
                    expires_at text not null
                )
                """
            )
            connection.execute(
                """
                create table if not exists source_events (
                    id integer primary key autoincrement,
                    source text not null,
                    event_type text not null,
                    message text not null,
                    created_at text not null
                )
                """
            )
            connection.execute(
                """
                create table if not exists recommendation_snapshots (
                    id integer primary key autoincrement,
                    snapshot_date text not null,
                    mode text not null,
                    top_ticker text,
                    top_name text,
                    top_score real,
                    payload text not null,
                    created_at text not null
                )
                """
            )
            connection.execute(
                """
                create unique index if not exists idx_recommendation_snapshots_date_mode
                on recommendation_snapshots(snapshot_date, mode)
                """
            )
            connection.execute(
                """
                create index if not exists idx_recommendation_snapshots_created_at
                on recommendation_snapshots(created_at)
                """
            )
            connection.execute(
                """
                create index if not exists idx_source_events_source_created_at
                on source_events(source, created_at)
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
