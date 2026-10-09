"""Environment configuration. Secrets stay in the environment, never in code."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "state" / "ledger.sqlite"
DEFAULT_STATE = ROOT / "state" / "issues.json"


class ConfigError(SystemExit):
    """Missing or unusable configuration. The message names the variable, not the value."""


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required environment variable {name}. "
            "Set it in .env (see .env.example)."
        )
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc


def normalize_host(host: str) -> str:
    host = host.strip().rstrip("/")
    if not host:
        host = "https://sentry.io"
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    if host.endswith("/api/0"):
        host = host[: -len("/api/0")]
    return host


@dataclass(frozen=True)
class Settings:
    org: str
    project: str
    token: str
    dsn: str | None
    client_secret: str | None
    host: str
    dry_run: bool
    min_age_days: int
    daily_cap: int
    threshold: int
    window_hours: int
    allowed_projects: frozenset[str]
    require_owner: bool
    ledger_path: Path
    save_webhook_sample: bool
    team: str | None

    @property
    def api_base(self) -> str:
        return self.host + "/api/0"


def load_settings(*, dotenv: bool = False, require: tuple[str, ...] = ()) -> Settings:
    """Load settings.

    ``dotenv=True`` reads ``.env`` from the repo root without overriding variables
    that are already set. Tests leave this off so a developer's ``.env`` cannot
    leak into unit tests.
    """

    if sys.version_info < (3, 11):
        raise ConfigError("Python 3.11 or newer is required.")
    if dotenv:
        _load_dotenv(ROOT / ".env")

    required = set(require)
    org = _require("SENTRY_ORG") if "SENTRY_ORG" in required else os.environ.get("SENTRY_ORG", "").strip()
    project = (
        _require("SENTRY_PROJECT")
        if "SENTRY_PROJECT" in required
        else os.environ.get("SENTRY_PROJECT", "").strip()
    )
    token = (
        _require("SENTRY_TOKEN")
        if "SENTRY_TOKEN" in required
        else os.environ.get("SENTRY_TOKEN", "").strip()
    )
    dsn = _require("SENTRY_DSN") if "SENTRY_DSN" in required else os.environ.get("SENTRY_DSN", "").strip() or None
    client_secret = (
        _require("SENTRY_CLIENT_SECRET")
        if "SENTRY_CLIENT_SECRET" in required
        else os.environ.get("SENTRY_CLIENT_SECRET", "").strip() or None
    )
    if "SENTRY_ORG" in required and not org:
        raise ConfigError("Missing required environment variable SENTRY_ORG.")
    if "SENTRY_PROJECT" in required and not project:
        raise ConfigError("Missing required environment variable SENTRY_PROJECT.")
    if "SENTRY_TOKEN" in required and not token:
        raise ConfigError("Missing required environment variable SENTRY_TOKEN.")

    allow_raw = os.environ.get("ALLOWED_PROJECTS", "").strip()
    if allow_raw:
        allowed = frozenset(part.strip() for part in allow_raw.split(",") if part.strip())
    elif project:
        allowed = frozenset({project})
    else:
        allowed = frozenset()
    if project and project not in allowed:
        raise ConfigError(
            "SENTRY_PROJECT is not listed in ALLOWED_PROJECTS. "
            "Refusing to start, because actions are limited to the allowlist."
        )

    ledger_raw = os.environ.get("LEDGER_PATH", "").strip()
    ledger_path = Path(ledger_raw) if ledger_raw else DEFAULT_LEDGER

    return Settings(
        org=org,
        project=project,
        token=token,
        dsn=dsn,
        client_secret=client_secret,
        host=normalize_host(os.environ.get("SENTRY_HOST", "https://sentry.io")),
        dry_run=_bool("DRY_RUN", True),
        min_age_days=_int("MIN_AGE_DAYS", 7),
        daily_cap=_int("DAILY_CAP", 20),
        threshold=_int("THRESHOLD", 50),
        window_hours=_int("WINDOW_HOURS", 24),
        allowed_projects=allowed,
        require_owner=_bool("REQUIRE_OWNER", False),
        ledger_path=ledger_path,
        save_webhook_sample=_bool("SAVE_WEBHOOK_SAMPLE", False),
        team=os.environ.get("SENTRY_TEAM", "").strip() or None,
    )


def describe(settings: Settings) -> str:
    """Non-secret startup line."""

    allow = ",".join(sorted(settings.allowed_projects))
    return (
        f"org={settings.org} project={settings.project} host={settings.host} "
        f"dry_run={settings.dry_run} min_age_days={settings.min_age_days} "
        f"threshold={settings.threshold} window_hours={settings.window_hours} "
        f"daily_cap={settings.daily_cap} require_owner={settings.require_owner} "
        f"allowed_projects={allow}"
    )
