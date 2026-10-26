"""Configuration from environment variables, with defaults from `.env` when present."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader. Never overrides variables already set in the environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ROOT / ".env")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def data_dir() -> Path:
    d = Path(env("EWS_DATA_DIR", "data"))
    return d if d.is_absolute() else ROOT / d


def out_dir() -> Path:
    return ROOT / "out"


def db_url() -> str:
    from urllib.parse import quote

    user = quote(env("PGUSER", "ews"))
    password = quote(env("PGPASSWORD", "ews"))
    host = env("PGHOST", "localhost")
    port = env("PGPORT", "5433")
    db = env("PGDATABASE", "ews")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


def event_window() -> tuple[date, date]:
    return (date.fromisoformat(env("EWS_EVENT_START", "2022-07-20")),
            date.fromisoformat(env("EWS_EVENT_END", "2022-08-05")))


def clim_years() -> tuple[int, int]:
    y0, y1 = env("EWS_CLIM_YEARS", "1991-2020").split("-")
    return int(y0), int(y1)


def clim_half_window() -> int:
    return int(env("EWS_CLIM_HALF_WINDOW", "15"))


CAP = {
    "sender": env("EWS_CAP_SENDER", "ews-demo@example.org"),
    "sender_name": env("EWS_CAP_SENDER_NAME", "Uganda national EWS demonstration (unofficial)"),
    "identifier_prefix": env("EWS_CAP_IDENTIFIER_PREFIX", "UG-EWS-DEMO"),
    "web": env("EWS_CAP_WEB", "https://example.org/ews-demo"),
    "contact": env("EWS_CAP_CONTACT", "Duty officer, national situation room (demo)"),
}
