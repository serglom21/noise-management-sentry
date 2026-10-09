from datetime import datetime, timezone
from pathlib import Path

import pytest

from noisebot.config import Settings


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        org="sandbox",
        project="noise-repro",
        token="test-token",
        dsn="https://example.invalid/1",
        client_secret="test-webhook-key",
        host="https://sentry.io",
        dry_run=True,
        min_age_days=7,
        daily_cap=20,
        threshold=50,
        window_hours=3,
        allowed_projects=frozenset({"noise-repro"}),
        require_owner=False,
        ledger_path=tmp_path / "ledger.sqlite",
        save_webhook_sample=False,
        team=None,
    )
    values.update(overrides)
    return Settings(**values)


def make_issue(**overrides) -> dict:
    issue = {
        "id": "10",
        "title": "LegacyParseException: Value was missing",
        "count": 100,
        "level": "error",
        "first_seen": "2020-01-01T00:00:00Z",
        "project": "noise-repro",
        "assigned_to": None,
        "release": "sample-app@6.1.0",
        "latest_release": "sample-app@7.0.0",
        "error_type": "LegacyParseException",
        "function": "parse_payload",
    }
    issue.update(overrides)
    return issue


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def now():
    return datetime(2026, 10, 8, tzinfo=timezone.utc)
