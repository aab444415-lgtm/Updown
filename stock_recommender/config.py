from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import gettempdir


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    data_dir: Path
    cache_db_path: Path
    sec_user_agent: str
    opendart_api_key: str | None = None
    fred_api_key: str | None = None
    ecos_api_key: str | None = None
    polygon_api_key: str | None = None
    news_api_key: str | None = None


def load_config(env_path: Path | None = None) -> AppConfig:
    values = _read_dotenv(env_path or PROJECT_ROOT / ".env")
    merged = {**values, **os.environ}
    data_dir = _data_dir(merged)
    return AppConfig(
        project_root=PROJECT_ROOT,
        data_dir=data_dir,
        cache_db_path=data_dir / "cache.sqlite",
        sec_user_agent=_clean(merged.get("SEC_USER_AGENT"))
        or "stock-recommender/0.1 your-email@example.com",
        opendart_api_key=_clean(merged.get("OPENDART_API_KEY")),
        fred_api_key=_clean(merged.get("FRED_API_KEY")),
        ecos_api_key=_clean(merged.get("ECOS_API_KEY")),
        polygon_api_key=_clean(merged.get("POLYGON_API_KEY")),
        news_api_key=_clean(merged.get("NEWS_API_KEY")),
    )


def _data_dir(values: dict[str, str]) -> Path:
    configured = _clean(values.get("STOCK_RECOMMENDER_DATA_DIR"))
    if configured:
        return Path(configured).expanduser()
    if _clean(values.get("VERCEL")):
        return Path(gettempdir()) / "stock_recommender"
    return PROJECT_ROOT / "data"


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
