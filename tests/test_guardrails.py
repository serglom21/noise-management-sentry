from datetime import datetime, timezone

import pytest

from noisebot.config import load_settings
from noisebot.guardrails import guardrail_failures
from tests.conftest import make_issue, make_settings


def test_missing_env_names_the_variable(monkeypatch):
    monkeypatch.delenv("SENTRY_ORG", raising=False)
    with pytest.raises(SystemExit, match="SENTRY_ORG"):
        load_settings(dotenv=False, require=("SENTRY_ORG",))


def test_too_young(tmp_path):
    settings = make_settings(tmp_path, min_age_days=7)
    issue = make_issue(first_seen="2026-10-08T12:00:00Z")
    reasons = guardrail_failures(issue, settings, now=datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc))
    assert "too_young" in reasons


def test_min_age_zero_allows_a_new_issue(tmp_path):
    settings = make_settings(tmp_path, min_age_days=0)
    issue = make_issue(first_seen="2026-10-08T12:00:00Z")
    reasons = guardrail_failures(issue, settings, now=datetime(2026, 10, 8, 12, 1, tzinfo=timezone.utc))
    assert "too_young" not in reasons


def test_latest_release(settings):
    issue = make_issue(release="sample-app@7.0.0", latest_release="sample-app@7.0.0")
    assert "latest_release" in guardrail_failures(issue, settings)


def test_fatal(settings):
    assert "fatal" in guardrail_failures(make_issue(level="fatal"), settings)
    assert "fatal" in guardrail_failures(make_issue(level="FATAL"), settings)


def test_project_not_in_allowlist(settings):
    assert "project_not_allowed" in guardrail_failures(make_issue(project="other-project"), settings)


def test_missing_owner_only_when_required(tmp_path):
    optional = make_settings(tmp_path, require_owner=False)
    required = make_settings(tmp_path, require_owner=True)
    issue = make_issue(assigned_to=None)
    assert "missing_owner" not in guardrail_failures(issue, optional)
    assert "missing_owner" in guardrail_failures(issue, required)
    owned = make_issue(assigned_to={"type": "user", "id": "1"})
    assert "missing_owner" not in guardrail_failures(owned, required)


def test_old_legacy_release_passes(settings):
    assert guardrail_failures(make_issue(), settings) == []


def test_unknown_release_fails_closed(settings):
    assert "release_unknown" in guardrail_failures(make_issue(release=None), settings)
    assert "latest_release_unknown" in guardrail_failures(make_issue(latest_release=None), settings)
