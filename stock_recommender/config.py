from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import gettempdir
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEZONE = "Asia/Seoul"
DEFAULT_UNIVERSE_MODE = "screened"
UNIVERSE_MODES = {"screened", "curated"}


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    data_dir: Path
    cache_db_path: Path
    snapshot_store_path: Path
    full_snapshot_dir: Path | None
    persist_repo_ledger: bool
    sec_user_agent: str
    opendart_api_key: str | None = None
    fred_api_key: str | None = None
    ecos_api_key: str | None = None
    polygon_api_key: str | None = None
    news_api_key: str | None = None
    timezone_name: str = DEFAULT_TIMEZONE
    universe_mode: str = DEFAULT_UNIVERSE_MODE
    universe_limit: int = 800
    us_universe_limit: int = 600
    kr_universe_limit: int = 200
    us_fundamental_limit: int = 250
    kr_fundamental_limit: int = 40
    polygon_fresh_limit: int = 4


def load_config(env_path: Path | None = None) -> AppConfig:
    values = _read_dotenv(env_path or PROJECT_ROOT / ".env")
    merged = {**values, **os.environ}
    data_dir = _data_dir(merged)
    return AppConfig(
        project_root=PROJECT_ROOT,
        data_dir=data_dir,
        cache_db_path=data_dir / "cache.sqlite",
        snapshot_store_path=_snapshot_store_path(merged),
        full_snapshot_dir=_optional_project_path(_clean(merged.get("STOCK_RECOMMENDER_FULL_SNAPSHOT_DIR"))),
        persist_repo_ledger=_truthy(_clean(merged.get("STOCK_RECOMMENDER_PERSIST_REPO_LEDGER"))),
        sec_user_agent=_clean(merged.get("SEC_USER_AGENT"))
        or "stock-recommender/0.1 your-email@example.com",
        opendart_api_key=_clean(merged.get("OPENDART_API_KEY")),
        fred_api_key=_clean(merged.get("FRED_API_KEY")),
        ecos_api_key=_clean(merged.get("ECOS_API_KEY")),
        polygon_api_key=_clean(merged.get("POLYGON_API_KEY")),
        news_api_key=_clean(merged.get("NEWS_API_KEY")),
        timezone_name=_timezone_name(_clean(merged.get("STOCK_RECOMMENDER_TIMEZONE"))),
        universe_mode=_universe_mode(_clean(merged.get("STOCK_RECOMMENDER_UNIVERSE_MODE"))),
        universe_limit=_positive_int(_clean(merged.get("STOCK_RECOMMENDER_UNIVERSE_LIMIT")), 800),
        us_universe_limit=_positive_int(_clean(merged.get("STOCK_RECOMMENDER_US_UNIVERSE_LIMIT")), 600),
        kr_universe_limit=_positive_int(_clean(merged.get("STOCK_RECOMMENDER_KR_UNIVERSE_LIMIT")), 200),
        us_fundamental_limit=_positive_int(
            _clean(merged.get("STOCK_RECOMMENDER_US_FUNDAMENTAL_LIMIT")),
            250,
        ),
        kr_fundamental_limit=_positive_int(
            _clean(merged.get("STOCK_RECOMMENDER_KR_FUNDAMENTAL_LIMIT")),
            40,
        ),
        polygon_fresh_limit=_positive_int(
            _clean(merged.get("STOCK_RECOMMENDER_POLYGON_FRESH_LIMIT")),
            4,
        ),
    )


def _data_dir(values: dict[str, str]) -> Path:
    configured = _clean(values.get("STOCK_RECOMMENDER_DATA_DIR"))
    if configured:
        return Path(configured).expanduser()
    if _clean(values.get("VERCEL")):
        return Path(gettempdir()) / "stock_recommender"
    return PROJECT_ROOT / "data"


def _snapshot_store_path(values: dict[str, str]) -> Path:
    configured = _clean(values.get("STOCK_RECOMMENDER_SNAPSHOT_STORE_PATH"))
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path
    return PROJECT_ROOT / "snapshot_store" / "recommendation_snapshots.json"


def _optional_project_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def configured_source_names(config: AppConfig) -> tuple[str, ...]:
    names = ["SEC EDGAR"]
    if config.opendart_api_key:
        names.append("OpenDART")
    if config.fred_api_key:
        names.append("FRED")
    if config.ecos_api_key:
        names.append("ECOS")
    if config.polygon_api_key:
        names.append("Polygon")
    if config.news_api_key:
        names.append("NewsAPI")
    return tuple(names)


def missing_optional_source_names(config: AppConfig) -> tuple[str, ...]:
    missing: list[str] = []
    if not config.opendart_api_key:
        missing.append("OpenDART")
    if not config.fred_api_key:
        missing.append("FRED")
    if not config.ecos_api_key:
        missing.append("ECOS")
    if not config.polygon_api_key:
        missing.append("Polygon")
    if not config.news_api_key:
        missing.append("NewsAPI")
    return tuple(missing)


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _strip_quotes(value.strip())
    return values


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _timezone_name(value: str | None) -> str:
    name = value or DEFAULT_TIMEZONE
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return DEFAULT_TIMEZONE
    return name


def _universe_mode(value: str | None) -> str:
    mode = (value or DEFAULT_UNIVERSE_MODE).strip().lower()
    return mode if mode in UNIVERSE_MODES else DEFAULT_UNIVERSE_MODE


def _positive_int(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
