from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import AppConfig, DEFAULT_TIMEZONE


def app_timezone(config: AppConfig | None = None) -> ZoneInfo:
    return ZoneInfo(config.timezone_name if config is not None else DEFAULT_TIMEZONE)


def now_in_app_timezone(config: AppConfig | None = None) -> datetime:
    return datetime.now(app_timezone(config))
